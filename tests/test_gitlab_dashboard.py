"""Read-only GitLab behavior through the existing dashboard and log screens."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from textual.widgets import Static

from bronson.cache import Cache
from bronson.cli import ActionsDashboard, HelpScreen, JobDetailScreen, LogScreen
from bronson.models import Repository
from bronson.providers.gitlab import GitLabProvider


class GitLabDashboardTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.cache = Cache(Path(directory.name))
        self.provider = GitLabProvider(Repository("gitlab", "gl.example.test", "team/repo"))
        self.pipeline = {
            "id": 42,
            "status": "success",
            "name": "CI",
            "ref": "main",
            "sha": "abc1234",
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:04:00Z",
            "web_url": "https://gl.example.test/team/repo/-/pipelines/42",
        }
        self.jobs = [
            {
                "id": 10,
                "status": "failed",
                "name": "test",
                "allow_failure": True,
                "started_at": "2026-01-01T00:01:00Z",
                "finished_at": "2026-01-01T00:03:00Z",
                "web_url": "https://gl.example.test/team/repo/-/jobs/10",
                "commit": {"title": "Fix pipeline title"},
            }
        ]
        for target in (
            "load_flume_themes",
            "active_flume_theme",
            "watch_flume_theme_changes",
            "theme_roles",
        ):
            context = patch(f"bronson.cli.{target}", None)
            context.start()
            self.addCleanup(context.stop)
        context = patch.object(self.provider, "_json", side_effect=self.fetch)
        context.start()
        self.addCleanup(context.stop)

    def fetch(self, endpoint):
        if "/jobs?" in endpoint:
            return list(self.jobs)
        if endpoint.endswith("/pipelines/42"):
            return {
                **self.pipeline,
                "started_at": "2026-01-01T00:01:00Z",
                "finished_at": "2026-01-01T00:03:00Z",
            }
        return [dict(self.pipeline)]

    async def settle(self, app, pilot):
        await app.workers.wait_for_complete()
        await pilot.pause()

    async def test_wide_jobs_preview_failed_logs_filter_and_read_only_keys(self):
        app = ActionsDashboard("team/repo", self.cache, provider=self.provider)
        with (
            patch.object(self.provider, "job_log", return_value="error: allowed failure\n") as log,
            patch.object(self.provider, "mutate") as mutate,
        ):
            async with app.run_test(size=(150, 40)) as pilot:
                await self.settle(app, pilot)
                self.assertEqual(app.runs[0]["displayTitle"], "Fix pipeline title")
                self.assertEqual(app.runs[0]["startedAt"], "2026-01-01T00:01:00Z")
                self.assertEqual(app.gates[42][2], ["test (allowed failure)"])
                self.assertIn(
                    "gl.example.test/team/repo", str(app.query_one("#repo-title", Static).render())
                )
                await pilot.press("r", "R", "c")
                await self.settle(app, pilot)
                mutate.assert_not_called()
                await pilot.press("?")
                self.assertIsInstance(app.screen, HelpScreen)
                help_text = str(app.screen.query_one(Static).render())
                self.assertIn("read-only", help_text)
                self.assertNotIn("Rerun failed", help_text)
                await pilot.press("escape", "enter")
                await pilot.pause(0.2)
                await self.settle(app, pilot)
                self.assertEqual(app.selected_job()["databaseId"], 10)
                await pilot.press("enter")
                await self.settle(app, pilot)
                self.assertIsInstance(app.screen, LogScreen)
                self.assertIn("error: allowed failure", [line.plain for line in app.screen.lines])
                log.assert_called_once_with(10)
                await pilot.press("escape", "escape", "G")
                await self.settle(app, pilot)
                self.assertTrue(app.screen.failed_only)
                log.assert_called_once()
                await pilot.press("escape", "/", "F", "i", "x", "enter")
                self.assertEqual(len(app.ordered_runs()), 1)
                app.apply_runs([dict(self.pipeline_record(app))], None)
                self.assertEqual(app.runs[0]["displayTitle"], "Fix pipeline title")

    @staticmethod
    def pipeline_record(app):
        return {**app.runs[0], "displayTitle": "CI", "startedAt": None}

    async def test_narrow_jobs_manual_scheduled_and_unknown_do_not_fetch_logs(self):
        self.pipeline["status"] = "manual"
        self.jobs = [
            {"id": 10, "status": "manual", "name": "deploy"},
            {"id": 11, "status": "scheduled", "name": "delayed"},
            {"id": 12, "status": "new_state", "name": "new job"},
        ]
        app = ActionsDashboard("team/repo", self.cache, provider=self.provider)
        with patch.object(self.provider, "job_log") as log:
            async with app.run_test(size=(80, 24)) as pilot:
                await self.settle(app, pilot)
                self.assertEqual(app.runs[0]["status"], "manual")
                self.assertNotIn(42, app._detail_revisions)
                await pilot.press("enter")
                await self.settle(app, pilot)
                self.assertIsInstance(app.screen, JobDetailScreen)
                await pilot.press("enter")
                await self.settle(app, pilot)
                self.assertIsInstance(app.screen, LogScreen)
                text = "\n".join(line.plain for line in app.screen.lines)
                self.assertIn("manual", text)
                self.assertIn("GitLab", text)
                self.assertNotIn("GitHub", text)
                log.assert_not_called()

    async def test_cached_startup_failed_refresh_and_host_isolation(self):
        app = ActionsDashboard("team/repo", self.cache, provider=self.provider)
        async with app.run_test() as pilot:
            await self.settle(app, pilot)
        reopened = ActionsDashboard("team/repo", self.cache, provider=self.provider)
        with patch.object(reopened, "fetch_runs"):
            async with reopened.run_test() as pilot:
                await self.settle(reopened, pilot)
                self.assertEqual(reopened.runs[0]["displayTitle"], "Fix pipeline title")
                self.assertIn(
                    "cached", str(reopened.query_one("#dashboard-status", Static).render())
                )
                reopened.apply_runs([], "offline")
                self.assertEqual(len(reopened.runs), 1)
        other = GitLabProvider(Repository("gitlab", "other.example.test", "team/repo"))
        isolated = ActionsDashboard("team/repo", self.cache, provider=other)
        with patch.object(isolated, "fetch_runs"):
            async with isolated.run_test() as pilot:
                await self.settle(isolated, pilot)
                self.assertEqual(isolated.runs, [])
        with patch.object(self.provider, "job_log", return_value="first host") as first:
            self.assertEqual(app.load_job_log(10), "first host")
            self.assertEqual(app.load_job_log(10), "first host")
            first.assert_called_once()
        with patch.object(other, "job_log", return_value="second host") as second:
            self.assertEqual(isolated.load_job_log(10), "second host")
            second.assert_called_once()
        github = ActionsDashboard("team/repo", self.cache)
        with patch.object(github.provider, "job_log", return_value="GitHub"):
            self.assertEqual(github.load_job_log(10), "GitHub")
