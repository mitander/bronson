"""Tests for the dashboard application."""

import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from rich.text import Text
from textual.theme import Theme
from textual.widgets import DataTable, Input, Static

import bronson.cli as tui


def run(run_id=1, status="in_progress", attempt=1):
    return dict(
        databaseId=run_id,
        status=status,
        attempt=attempt,
        updatedAt="2026-01-01T00:00:00Z",
        startedAt="2026-01-01T00:00:00Z",
        workflowName="CI",
        url=f"https://github.com/owner/repo/actions/runs/{run_id}",
    )


def job(job_id=10, status="in_progress", conclusion=None):
    return dict(
        id=job_id,
        databaseId=job_id,
        name=f"job {job_id}",
        status=status,
        conclusion=conclusion,
        started_at="2026-01-01T00:00:00Z",
        startedAt="2026-01-01T00:00:00Z",
        html_url=f"https://github.com/owner/repo/actions/runs/1/job/{job_id}",
        url=f"https://github.com/owner/repo/actions/runs/1/job/{job_id}",
    )


class HelpersTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.cache = tui.Cache(Path(temp.name))

    def test_module_help(self):
        result = subprocess.run(
            [sys.executable, "-m", "bronson", "--help"],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        self.assertIn("--repo", result.stdout)
        self.assertIn("--theme", result.stdout)

    def test_cancelled_request_kills_and_reaps_process_promptly(self):
        worker = SimpleNamespace(is_cancelled=False)
        launched = []
        popen = subprocess.Popen

        def launch(*args, **kwargs):
            proc = popen(*args, **kwargs)
            launched.append(proc)
            return proc

        timer = threading.Timer(0.1, lambda: setattr(worker, "is_cancelled", True))
        with (
            patch.object(tui, "get_current_worker", return_value=worker),
            patch.object(tui.subprocess, "Popen", side_effect=launch),
        ):
            timer.start()
            started = time.monotonic()
            try:
                with self.assertRaisesRegex(subprocess.SubprocessError, "cancelled"):
                    tui.run_process([sys.executable, "-c", "import time; time.sleep(10)"])
            finally:
                timer.cancel()
                timer.join()
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertIsNotNone(launched[0].poll())

    def test_process_output_errors_and_timeout(self):
        proc = tui.run_process([sys.executable, "-c", "print('ready')"], check=True)
        self.assertEqual(proc.stdout, "ready\n")
        with self.assertRaises(subprocess.CalledProcessError):
            tui.run_process([sys.executable, "-c", "raise SystemExit(2)"], check=True)
        with self.assertRaises(subprocess.TimeoutExpired):
            tui.run_process([sys.executable, "-c", "import time; time.sleep(10)"], timeout=0.1)

    def test_rail_projects_every_error_even_between_sampled_lines(self):
        self.assertEqual(tui.rail_markers({1, 123, 124, 998, 999}, 1000, 10), {0, 1, 9})
        self.assertEqual(tui.rail_markers({0, 2}, 3, 20), {0, 13})
        self.assertEqual(tui.rail_markers({0}, 0, 0), set())

    def test_stopped_app_ignores_late_refresh_callbacks(self):
        app = tui.ActionsDashboard("owner/repo", self.cache)
        self.assertFalse(app.is_running)
        # During shutdown, the header may already be unmounted when a timer fires.
        app.refresh_rows()
        app.update_chrome()
        app.update_footer()
        app.resize_layout()
        app.apply_gates(1, tui.run_revision(run()), [], None)

    def test_run_search_matches_metadata_case_insensitively(self):
        item = {
            **run(),
            "displayTitle": "Fix installer startup",
            "headBranch": "fix/linux",
            "workflowName": "Release",
            "headSha": "abcdef123",
            "conclusion": "failure",
        }
        for query in ("", "INSTALLER", "linux release", "abcde", "failure", "startup fix"):
            with self.subTest(query=query):
                self.assertTrue(tui.run_matches(item, query))
        self.assertFalse(tui.run_matches(item, "installer windows"))

    def test_jobs_paginate_and_normalize(self):
        with patch.object(
            tui, "gh_json", return_value=[{"jobs": [job(10)]}, {"jobs": [job(11)]}]
        ) as fetch:
            jobs = tui.fetch_jobs("owner/repo", 1)
        self.assertEqual([j["databaseId"] for j in jobs], [10, 11])
        self.assertEqual(jobs[0]["startedAt"], jobs[0]["started_at"])
        self.assertEqual(
            fetch.call_args.args[0],
            [
                "api",
                "repos/owner/repo/actions/runs/1/jobs?filter=latest&per_page=100",
                "--paginate",
                "--slurp",
            ],
        )


class DashboardTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.runs = [run()]
        self.jobs = [job()]
        self.calls = []
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cache = tui.Cache(Path(self.temp.name))
        self.patches = [
            patch.object(tui, "load_flume_themes", None),
            patch.object(tui, "active_flume_theme", None),
            patch.object(tui, "watch_flume_theme_changes", None),
            patch.object(tui, "theme_roles", None),
            patch.object(tui, "gh_json", side_effect=self.fetch),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def fetch(self, args, **kwargs):
        self.calls.append(args)
        if args[:2] == ["run", "list"]:
            return list(self.runs)
        return [{"jobs": list(self.jobs)}]

    async def settle(self, app, pilot):
        await app.workers.wait_for_complete()
        await pilot.pause()

    async def test_cached_dashboard_opens_before_network_and_reuses_completed_jobs(self):
        self.runs = [run(status="completed")]
        self.jobs = [job(10, "completed", "success")]
        app = tui.ActionsDashboard("owner/repo", self.cache)
        async with app.run_test() as pilot:
            await self.settle(app, pilot)
        self.calls.clear()
        reopened = tui.ActionsDashboard("owner/repo", self.cache)
        with patch.object(reopened, "fetch_runs"):
            async with reopened.run_test() as pilot:
                await self.settle(reopened, pilot)
                self.assertEqual(reopened.runs, self.runs)
                self.assertEqual(reopened.gates[1][:2], (1, 1))
                self.assertTrue(reopened._table().display)
                self.assertIn("cached", str(reopened.query_one("#dashboard-status").render()))
                self.assertEqual(self.calls, [])
                reopened.apply_runs(self.runs, None)
                await self.settle(reopened, pilot)
                self.assertEqual(self.calls, [])
                changed = {**self.runs[0], "updatedAt": "2026-01-02T00:00:00Z"}
                reopened.apply_runs([changed], None)
                await self.settle(reopened, pilot)
                self.assertEqual(len(self.calls), 1)

    async def test_cached_active_runs_refresh_and_offline_preserves_snapshot(self):
        app = tui.ActionsDashboard("owner/repo", self.cache)
        async with app.run_test() as pilot:
            await self.settle(app, pilot)
        self.calls.clear()
        reopened = tui.ActionsDashboard("owner/repo", self.cache)
        async with reopened.run_test() as pilot:
            await self.settle(reopened, pilot)
            self.assertTrue(any(args[0] == "api" for args in self.calls))
            path = next(self.cache.root.glob("*.json"))
            before = path.read_bytes()
            reopened.apply_runs([], "offline")
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(reopened.runs, self.runs)

    async def test_ticks_do_not_rebuild_and_selection_follows_run(self):
        app = tui.ActionsDashboard("owner/repo", self.cache)
        async with app.run_test() as pilot:
            await self.settle(app, pilot)
            table = app.query_one("#runs", DataTable)
            with patch.object(table, "clear", wraps=table.clear) as clear:
                app.refresh_rows()
                app.refresh_rows()
                clear.assert_not_called()
            self.assertEqual(app.selected_run()["databaseId"], 1)
            app.apply_runs([run(2), run(1, "completed")], None)
            self.assertEqual(app.selected_run()["databaseId"], 1)
            app.apply_runs([], "offline")
            self.assertEqual(len(app.runs), 2)
            await self.settle(app, pilot)

    async def test_detail_deduplicates_requests_and_updates_open_dialog(self):
        app = tui.ActionsDashboard("owner/repo", self.cache)
        async with app.run_test() as pilot:
            await self.settle(app, pilot)
            screen = tui.JobDetailScreen(app.repo, self.runs[0])
            await app.push_screen(screen)
            await pilot.pause()
            self.assertEqual(screen.jobs[0]["status"], "in_progress")
            self.jobs = [job(10, "completed", "success"), job(11)]
            with patch.object(app, "fetch_gates") as fetch:
                app.request_detail(self.runs[0], force=True)
                app.request_detail(self.runs[0], force=True)
                fetch.assert_called_once()
            app.apply_gates(1, tui.run_revision(self.runs[0]), self.jobs, None)
            self.assertEqual(screen.jobs[0]["status"], "completed")
            self.assertEqual(screen.query_one("#jobs", DataTable).row_count, 2)
            screen.query_one("#jobs", DataTable).move_cursor(row=1)
            app.apply_gates(1, tui.run_revision(self.runs[0]), list(reversed(self.jobs)), None)
            self.assertEqual(screen.selected_job()["databaseId"], 11)

    async def test_completion_and_rerun_invalidate_cached_details(self):
        app = tui.ActionsDashboard("owner/repo", self.cache)
        async with app.run_test() as pilot:
            await self.settle(app, pilot)
            old_revision = tui.run_revision(self.runs[0])
            completed = run(1, "completed")
            self.runs = [completed]
            self.jobs = [job(10, "completed", "failure")]
            app.apply_runs(self.runs, None)
            await self.settle(app, pilot)
            self.assertEqual(app.gates[1][2], ["job 10"])
            with patch.object(app, "fetch_gates") as fetch:
                app.request_detail(completed)
                fetch.assert_not_called()
                app.apply_gates(1, old_revision, [job()], None)
                fetch.assert_called_once()  # Late active response cannot overwrite final data.
            self.assertEqual(app.gates[1][2], ["job 10"])
            app._detail_inflight.clear()
            self.runs = [run(1, attempt=2)]
            self.jobs = [job(20)]
            app.apply_runs(self.runs, None)
            await self.settle(app, pilot)
            self.assertEqual(app.details[1]["jobs"][0]["databaseId"], 20)
            self.assertNotIn(1, app._detail_revisions)

    async def test_active_poll_interval_and_overlap_guard(self):
        app = tui.ActionsDashboard("owner/repo", self.cache)
        async with app.run_test() as pilot:
            await self.settle(app, pilot)
            with (
                patch.object(app, "fetch_runs") as fetch,
                patch.object(tui.time, "monotonic", return_value=100),
            ):
                app._last_poll = 94
                app.poll_if_due()
                app.poll_if_due()
                fetch.assert_called_once()
                app._polling = False
                app.runs = [run(status="completed")]
                app._last_poll = 94
                app.poll_if_due()
                fetch.assert_called_once()

    async def test_completed_job_logs_work_during_run_and_use_cache(self):
        self.jobs = [job(10, "completed", "success"), job(11)]
        app = tui.ActionsDashboard("owner/repo", self.cache)
        async with app.run_test() as pilot:
            await self.settle(app, pilot)
            proc = subprocess.CompletedProcess([], 0, stdout="hello\nworld\n", stderr="")
            with patch.object(tui, "run_process", return_value=proc) as fetch:
                screen = tui.LogScreen("CI", self.runs[0])
                await app.push_screen(screen)
                await self.settle(app, pilot)
                fetch.assert_called_once()
                self.assertIn("repos/owner/repo/actions/jobs/10/logs", fetch.call_args.args[0])
                self.assertIn("hello", "\n".join(line.plain for line in screen.lines))
                self.assertIn("still running", "\n".join(line.plain for line in screen.lines))
                screen.run_search("hello")
                self.jobs = [job(10, "completed", "success"), job(11, "completed", "success")]
                app.apply_gates(1, tui.run_revision(self.runs[0]), self.jobs, None)
                await self.settle(app, pilot)
                self.assertEqual(fetch.call_count, 2)  # Only newly completed job downloaded.
                self.assertEqual(len(screen.matches), 2)
                screen.dismiss()
                await pilot.pause()
                app.log_cache.clear()  # Reopen from disk cache.
                await app.push_screen(tui.LogScreen("job", self.runs[0], job=self.jobs[0]))
                await self.settle(app, pilot)
                self.assertEqual(fetch.call_count, 2)

    async def test_pending_final_jobs_are_not_frozen(self):
        self.runs = [run(status="completed")]
        app = tui.ActionsDashboard("owner/repo", self.cache)
        async with app.run_test() as pilot:
            await self.settle(app, pilot)
            self.assertNotIn(1, app._detail_revisions)
            app._detail_fetched.clear()
            self.jobs = [job(10, "completed", "success")]
            app.request_detail(self.runs[0])
            await self.settle(app, pilot)
            self.assertEqual(app.gates[1][:2], (1, 1))
            self.assertEqual(app._detail_revisions[1], tui.run_revision(self.runs[0]))

    async def test_running_and_skipped_jobs_do_not_download_logs(self):
        self.jobs = [job(10), job(11, "completed", "skipped")]
        app = tui.ActionsDashboard("owner/repo", self.cache)
        async with app.run_test() as pilot:
            await self.settle(app, pilot)
            with patch.object(tui, "run_process") as fetch:
                screen = tui.LogScreen("CI", self.runs[0])
                await app.push_screen(screen)
                await self.settle(app, pilot)
                fetch.assert_not_called()
                self.assertIn("Job skipped. No log output.", [line.plain for line in screen.lines])
                with patch.object(tui, "open_in_browser") as browser:
                    screen.action_open_url()
                    browser.assert_called_once_with(self.runs[0]["url"])

    async def test_failed_logs_only_download_failed_jobs(self):
        self.jobs = [job(10, "completed", "success"), job(11, "completed", "failure")]
        app = tui.ActionsDashboard("owner/repo", self.cache)
        async with app.run_test() as pilot:
            await self.settle(app, pilot)
            proc = subprocess.CompletedProcess([], 0, stdout="##[error]failed", stderr="")
            with patch.object(tui, "run_process", return_value=proc) as fetch:
                await app.push_screen(tui.LogScreen("CI", self.runs[0], failed_only=True))
                await self.settle(app, pilot)
                fetch.assert_called_once()
                self.assertIn("repos/owner/repo/actions/jobs/11/logs", fetch.call_args.args[0])

    async def test_failure_rail_and_chords(self):
        app = tui.ActionsDashboard("owner/repo", self.cache)
        async with app.run_test(size=(80, 24)) as pilot:
            await self.settle(app, pilot)
            screen = tui.LogScreen("CI", self.runs[0])
            await app.push_screen(screen)
            await self.settle(app, pilot)
            lines = [Text(f"output {i}") for i in range(1000)]
            for i in (123, 124, 991, 998):
                lines[i] = tui.style_log_line(Text(), "error: broken")
            lines[10] = Text("red but not an error", style="red")
            screen.apply_log(lines)
            await pilot.pause()
            self.assertEqual(screen.failure_rows, [123, 991, 998])
            self.assertNotIn(10, screen.error_rows)
            screen.update_rail()
            await pilot.pause()
            rail = screen.query_one("#log-rail", Static)
            self.assertEqual(rail.size.width, 1)
            self.assertEqual(rail.size.height, screen._scroll().size.height)
            self.assertEqual(rail.region.right, screen.size.width)
            self.assertEqual(rail.region.y, screen._scroll().region.y)
            expected = tui.rail_markers(screen.error_rows, len(lines), rail.size.height)
            for row in expected:
                strip = rail.render_line(row)
                self.assertEqual(strip.text, "█")
                self.assertTrue(any(segment.style.color.number == 1 for segment in strip))
            await pilot.press("]", "d")
            self.assertEqual(screen._failure_row, 123)
            await pilot.press("]", "d")
            self.assertEqual(screen._failure_row, 991)
            await pilot.press("]", "d")
            self.assertEqual(screen._failure_row, 998)
            await pilot.press("]", "d")
            self.assertEqual(screen._failure_row, 123)
            await pilot.press("[", "d")
            self.assertEqual(screen._failure_row, 998)
            # Search must accept the chords literally, not navigate.
            await pilot.press("/", "[", "d", "]", "d")
            self.assertEqual(screen.query_one("#log-search", Input).value, "[d]d")
            self.assertEqual(screen._failure_row, 998)
            await pilot.press("escape")
            self.assertFalse(screen.query_one("#log-search", Input).display)
            screen.action_scroll_top()
            await pilot.pause()
            await pilot.press("d")
            self.assertGreater(screen._scroll().scroll_offset.y, 0)
            await pilot.resize_terminal(60, 12)
            await pilot.pause()
            screen.update_rail()
            await pilot.pause()
            self.assertEqual(rail.size.height, screen._scroll().size.height)
            for row in tui.rail_markers(screen.error_rows, len(lines), rail.size.height):
                self.assertEqual(rail.render_line(row).text, "█")
            screen.apply_log([Text("everything passed")])
            await pilot.press("]", "d", "[", "d")
            self.assertIsNone(screen._failure_row)

    async def test_startup_empty_and_error_states_are_distinct(self):
        app = tui.ActionsDashboard("owner/repo", self.cache)
        with patch.object(app, "fetch_runs"):
            async with app.run_test(size=(140, 35)) as pilot:
                await pilot.pause()
                self.assertFalse(app._loaded)
                self.assertTrue(app.query_one("#startup-spinner").display)
                self.assertFalse(app.query_one(tui.JobPane).display)
                self.assertFalse(app.query_one("#runs").display)
                brand = app.query_one("#startup-brand", Static)
                spinner = app.query_one("#startup-spinner")
                label = brand.render_line(0).text
                dots = spinner.render_line(0).text
                text_center = brand.region.x + label.index("bronson") + (len("bronson") - 1) / 2
                dots_center = spinner.region.x + (dots.index("●") + dots.rindex("●")) / 2
                self.assertLessEqual(abs(text_center - dots_center), 1)
                self.assertGreater(spinner.region.y, app.query_one("#run-state-message").region.y)
                group, area = app.query_one("#startup-content"), app.query_one("#run-state")
                self.assertLessEqual(
                    abs(
                        group.region.y
                        + group.size.height / 2
                        - (area.region.y + area.size.height / 2)
                    ),
                    1,
                )
                app.apply_runs([], "offline")
                self.assertTrue(app._loaded)
                self.assertFalse(app.query_one("#startup-spinner").display)
                self.assertTrue(app.query_one("#run-state").display)
                self.assertEqual(app.error, "offline")
                app.apply_runs([], None)
                self.assertIsNone(app.error)
                self.assertTrue(app.query_one("#run-state").display)
                self.assertFalse(app.query_one("#startup-spinner").display)

    async def test_wide_panes_focus_expansion_and_responsive_fallback(self):
        self.jobs = [job(10, "completed", "failure"), job(11, "completed", "success")]
        app = tui.ActionsDashboard("owner/repo", self.cache)
        with patch.object(app, "load_job_log", return_value="intro\nerror: bad input\nmore output"):
            async with app.run_test(size=(150, 40)) as pilot:
                await self.settle(app, pilot)
                await pilot.pause(0.2)
                await self.settle(app, pilot)
                pane = app.query_one(tui.JobPane)
                self.assertTrue(app.wide)
                self.assertTrue(pane.display)
                self.assertEqual(pane.jobs[0]["databaseId"], 10)
                self.assertIsNone(pane._preview_key)
                self.assertLessEqual(app._table().virtual_size.width, app._table().size.width)
                await pilot.press("enter")
                await pilot.pause(0.2)
                await self.settle(app, pilot)
                self.assertEqual(pane._preview_key[:2], (1, 10))
                self.assertEqual(app.focused.id, "runs")
                self.assertEqual(len(app.job_rows), 2)
                await pilot.press("j")
                await pilot.pause(0.2)
                await self.settle(app, pilot)
                self.assertEqual(pane.selected_job()["databaseId"], 11)
                await pilot.press("enter")
                await self.settle(app, pilot)
                self.assertIsInstance(app.screen, tui.LogScreen)
                self.assertEqual(app.screen.job["databaseId"], 11)
                await pilot.press("escape")
                self.assertEqual(app.focused.id, "runs")
                self.assertEqual(pane.selected_job()["databaseId"], 11)
                app.refresh_rows()
                await pilot.pause()
                self.assertEqual(pane.selected_job()["databaseId"], 11)
                await pilot.press("tab")
                self.assertEqual(app.focused.id, "preview-scroll")
                await pilot.press("?")
                self.assertIsInstance(app.screen, tui.HelpScreen)
                await pilot.press("escape")
                self.assertEqual(app.focused.id, "preview-scroll")
                await pilot.resize_terminal(80, 24)
                await pilot.pause()
                self.assertFalse(app.wide)
                self.assertFalse(pane.display)
                self.assertEqual(app.focused.id, "runs")
                await pilot.press("enter")
                await self.settle(app, pilot)
                self.assertIsInstance(app.screen, tui.JobDetailScreen)
                await pilot.press("escape")
                await pilot.resize_terminal(150, 40)
                await pilot.pause()
                self.assertTrue(pane.display)
                await pilot.press("escape")
                self.assertIsNone(app.expanded_run)
                self.assertFalse(app.job_rows)

    async def test_pane_insets_compact_search_and_scrollbar_widths(self):
        self.runs = [{**run(i), "displayTitle": "A long commit title " * 8} for i in range(1, 41)]
        self.jobs = [{**job(i), "name": "A long job name " * 12} for i in range(10, 30)]
        app = tui.ActionsDashboard("owner/repo", self.cache)
        async with app.run_test(size=(150, 35)) as pilot:
            await self.settle(app, pilot)
            table = app._table()
            pane = app.query_one(tui.JobPane)
            await pilot.press("enter")
            await pilot.pause()
            self.assertTrue(table.show_vertical_scrollbar)
            self.assertFalse(table.show_horizontal_scrollbar)
            self.assertLessEqual(
                table.virtual_size.width, table.scrollable_content_region.width - 1
            )
            self.assertEqual(table.styles.scrollbar_size_vertical, 1)
            self.assertFalse(table.show_header)
            self.assertEqual(len(table.columns), 2)
            self.assertEqual(app.query_one("#runs-pane").styles.padding, pane.styles.padding)
            preview_x = pane.query_one("#preview-scroll").region.x
            await pilot.press("tab")
            self.assertEqual(pane.query_one("#preview-scroll").region.x, preview_x)
            await pilot.press("]")
            await pilot.pause()
            self.assertGreater(pane.query_one("#preview-scroll").region.x, preview_x)
            await pilot.press("[")
            await pilot.pause()
            self.assertEqual(pane.query_one("#preview-scroll").region.x, preview_x)
            await pilot.press("escape", "/")
            search = app.query_one("#run-search", Input)
            self.assertEqual(search.size.height, 1)
            self.assertEqual(search.region.x, table.region.x)
            self.assertFalse(table.show_horizontal_scrollbar)

    async def test_inline_selection_survives_reordering_and_returns_to_parent_on_resize(self):
        self.runs = [run(1), run(2)]
        self.jobs = [job(10, "completed", "success"), job(11, "completed", "success")]
        app = tui.ActionsDashboard("owner/repo", self.cache)
        with patch.object(app, "load_job_log", return_value="ok"):
            async with app.run_test(size=(150, 40)) as pilot:
                await self.settle(app, pilot)
                await pilot.press("enter", "j")
                self.assertEqual(app.selected_job()["databaseId"], 11)
                app.apply_gates(1, tui.run_revision(self.runs[0]), list(reversed(self.jobs)), None)
                await pilot.pause()
                self.assertEqual(app.selected_job()["databaseId"], 11)
                self.assertEqual(app.selected_run()["databaseId"], 1)
                await pilot.resize_terminal(80, 24)
                await pilot.pause()
                self.assertIsNone(app.selected_job())
                self.assertEqual(app.selected_run()["databaseId"], 1)
                self.assertEqual(app.query_one("#runs-pane").region.width, 80)
                await pilot.resize_terminal(150, 40)
                await pilot.pause()
                await pilot.press("escape")
                self.assertFalse(app.job_rows)
                await pilot.press("enter", "j")
                selected = app.selected_job()["databaseId"]
                app.apply_gates(
                    1,
                    tui.run_revision(self.runs[0]),
                    [j for j in self.jobs if j["id"] != selected],
                    None,
                )
                await pilot.pause()
                self.assertIsNone(app.selected_job())
                self.assertEqual(app.selected_run()["databaseId"], 1)

    async def test_inline_jobs_loading_empty_error_and_filter_cleanup(self):
        self.runs = [run(1), run(2)]
        app = tui.ActionsDashboard("owner/repo", self.cache)
        async with app.run_test(size=(150, 40)) as pilot:
            await self.settle(app, pilot)
            with patch.object(app, "request_detail"):
                app.details.clear()
                await pilot.press("enter")
                self.assertEqual(app.expanded_run, 1)
                self.assertIn("Fetching jobs", str(app._table().get_row("jobs:1")))
                app.detail_errors[1] = "offline"
                app.refresh_rows()
                self.assertIn("Couldn't fetch jobs", str(app._table().get_row("jobs:1")))
                app.detail_errors.clear()
                app.details[1] = {"jobs": []}
                app.refresh_rows()
                self.assertIn("No jobs yet", str(app._table().get_row("jobs:1")))
                app.apply_gates(1, tui.run_revision(self.runs[0]), [job(10)], None)
                await pilot.press("j")
                self.assertEqual(app.selected_job()["databaseId"], 10)
                app.run_filter = "does-not-match"
                app.refresh_rows()
                self.assertIsNone(app.expanded_run)
                self.assertFalse(app.job_rows)
                self.assertIsNone(app.query_one(tui.JobPane)._preview_key)

    async def test_successful_job_diagnostics_are_not_labeled_job_failure(self):
        self.jobs = [job(10, "completed", "success")]
        app = tui.ActionsDashboard("owner/repo", self.cache)
        with patch.object(app, "load_job_log", return_value="error: optional coverage failed"):
            async with app.run_test(size=(150, 40)) as pilot:
                await self.settle(app, pilot)
                await pilot.press("enter")
                await pilot.pause(0.2)
                await self.settle(app, pilot)
                title = str(app.query_one("#preview-title").render())
                self.assertIn("1 diagnostic block · job passed", title)

    async def test_preview_rejects_stale_job_response_and_resets_on_run_change(self):
        app = tui.ActionsDashboard("owner/repo", self.cache)
        async with app.run_test(size=(150, 40)) as pilot:
            await self.settle(app, pilot)
            pane = app.query_one(tui.JobPane)
            await pilot.press("enter")
            await pilot.pause(0.2)
            await self.settle(app, pilot)
            old_key = pane._preview_key
            self.assertIsNotNone(old_key)
            self.runs = [run(2)]
            app.apply_runs(self.runs, None)
            await self.settle(app, pilot)
            key = pane._preview_key
            with patch.object(pane.query_one("#preview-body"), "update") as update:
                pane.apply_preview(old_key, Text("wrong run"), "wrong title", None)
                update.assert_not_called()
            self.assertNotEqual(key, old_key)

    async def test_preview_and_expanded_view_share_one_log_download(self):
        self.jobs = [job(10, "completed", "success")]
        app = tui.ActionsDashboard("owner/repo", self.cache)
        proc = subprocess.CompletedProcess([], 0, stdout="hello", stderr="")
        with patch.object(tui, "run_process", return_value=proc) as download:
            async with app.run_test(size=(150, 40)) as pilot:
                await self.settle(app, pilot)
                self.assertEqual(download.call_count, 0)
                await pilot.press("enter")
                await pilot.pause(0.2)
                await self.settle(app, pilot)
                self.assertEqual(download.call_count, 1)
                await pilot.press("enter")
                await self.settle(app, pilot)
                self.assertIsInstance(app.screen, tui.LogScreen)
                self.assertEqual(download.call_count, 1)

    async def test_dashboard_search_filters_and_clears_without_losing_input_focus(self):
        self.runs = [
            {**run(1), "displayTitle": "Fix installer", "headBranch": "linux"},
            {**run(2), "displayTitle": "Update docs", "headBranch": "main"},
        ]
        app = tui.ActionsDashboard("owner/repo", self.cache)
        async with app.run_test(size=(150, 40)) as pilot:
            await self.settle(app, pilot)
            self.assertFalse(app.query_one("#run-search", Input).display)
            await pilot.press("/", "i", "n", "s", "t")
            self.assertEqual(app.focused.id, "run-search")
            self.assertEqual([r["databaseId"] for r in app.ordered_runs()], [1])
            await pilot.press("enter")
            self.assertEqual(app.focused.id, "runs")
            self.assertEqual(app.run_filter, "inst")
            await pilot.press("escape")
            self.assertEqual(len(app.ordered_runs()), 2)
            await pilot.press("/", "z", "z", "z")
            self.assertEqual(app.focused.id, "run-search")
            self.assertTrue(app.query_one("#run-state").display)
            self.assertFalse(app.query_one(tui.JobPane).display)
            self.assertFalse(app.query_one("#startup-spinner").display)
            await pilot.press("escape")
            self.assertFalse(app.query_one("#run-search").display)
            self.assertEqual(app.run_filter, "")
            self.assertTrue(app.query_one(tui.JobPane).display)

    async def test_log_search_is_one_visible_text_row(self):
        app = tui.ActionsDashboard("owner/repo", self.cache)
        async with app.run_test(size=(80, 24)) as pilot:
            await self.settle(app, pilot)
            screen = tui.LogScreen("CI", self.runs[0])
            await app.push_screen(screen)
            await self.settle(app, pilot)
            await pilot.press("slash", "e", "r", "r", "o", "r")
            search = screen.query_one("#log-search", Input)
            self.assertIs(app.focused, search)
            self.assertEqual(search.value, "error")
            self.assertEqual(search.region.height, 1)
            self.assertEqual(search.content_region.height, 1)
            self.assertIn("error", search.render_line(0).text)
            await pilot.press("enter")
            self.assertEqual(screen.query_text, "error")
            self.assertFalse(search.display)
            await pilot.press("slash", "escape")
            self.assertFalse(search.display)
            self.assertIs(app.screen, screen)

    async def test_chrome_uses_base_background_across_theme_changes(self):
        app = tui.ActionsDashboard("owner/repo", self.cache, theme_name="flume-first")
        surfaces = {"flume-first": "#353252", "flume-second": "#ded4d1"}
        for name, surface in surfaces.items():
            app.register_theme(
                Theme(name=name, primary="#73a6b6", surface=surface, panel="#262938")
            )
        async with app.run_test() as pilot:
            await self.settle(app, pilot)
            screen = tui.LogScreen("CI", self.runs[0])
            await app.push_screen(screen)
            await self.settle(app, pilot)
            for name in surfaces:
                app.select_theme(name)
                await pilot.pause()
                for widget in (
                    app.screen_stack[0].query_one("#dashboard-header"),
                    app.screen_stack[0].query_one("#dashboard-footer"),
                    screen.query_one("#log-header"),
                    screen.query_one("#log-help"),
                ):
                    self.assertEqual(
                        widget.styles.background, app.screen_stack[0].styles.background
                    )

    async def test_theme_changes_repaint_cached_log_errors(self):
        self.jobs = [job(10, "completed", "failure")]
        palettes = {
            "flume-first": {"error": "#ee7583", "text_muted": "#8d8a9e"},
            "flume-second": {"error": "#bc3945", "text_muted": "#666666"},
        }
        app = tui.ActionsDashboard("owner/repo", self.cache, theme_name="flume-first")
        for name in palettes:
            app.register_theme(Theme(name=name, primary="#73a6b6"))
        with (
            patch.object(tui, "theme_roles", side_effect=lambda name: palettes.get(name)),
            patch.object(app, "load_job_log", return_value="2026-01-01T00:00:00Z error: bad input"),
        ):
            async with app.run_test() as pilot:
                await self.settle(app, pilot)
                screen = tui.LogScreen("CI", self.runs[0])
                await app.push_screen(screen)
                await self.settle(app, pilot)
                self.assertTrue(
                    any(
                        str(span.style) == "#ee7583" for line in screen.lines for span in line.spans
                    )
                )
                app.select_theme("flume-second")
                await pilot.pause()
                self.assertEqual(app.theme_override, "flume-second")
                self.assertTrue(
                    any(
                        str(span.style) == "#bc3945" for line in screen.lines for span in line.spans
                    )
                )
                self.assertEqual(screen.failure_rows, [1])
                app.select_theme("auto")
                self.assertIsNone(app.theme_override)

    async def test_log_errors_retry_without_poisoning_cache(self):
        self.jobs = [job(10, "completed", "success")]
        app = tui.ActionsDashboard("owner/repo", self.cache)
        async with app.run_test() as pilot:
            await self.settle(app, pilot)
            error = subprocess.CompletedProcess([], 1, stdout="", stderr="not published")
            success = subprocess.CompletedProcess([], 0, stdout="ready", stderr="")
            with patch.object(tui, "run_process", side_effect=[error, success]) as fetch:
                screen = tui.LogScreen("CI", self.runs[0])
                await app.push_screen(screen)
                await self.settle(app, pilot)
                self.assertFalse(app.log_cache)
                self.assertEqual(list(Path(self.temp.name).glob("*.log")), [])
                screen.refresh_log()
                await self.settle(app, pilot)
                self.assertEqual(fetch.call_count, 2)
                self.assertIn("ready", [line.plain for line in screen.lines])


if __name__ == "__main__":
    unittest.main()
