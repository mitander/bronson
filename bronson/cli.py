#!/usr/bin/env python3
"""GitHub Actions and read-only GitLab CI dashboard.

A responsive dashboard: runs and inline jobs on the left, job logs on the right.
Small terminals use full-width drill-down views. Press ? for the full key list.

Keys
    j/k/h/l   move
    enter     expand/collapse a run; enter on a job opens its log
    tab       switch between the run/job list and preview
    [ / ]     narrow / widen the run pane
    ?         help
    g / G     run logs / failed jobs' logs (in-app viewer, esc to close)
    r / R     rerun failed jobs / rerun all jobs
    c         cancel a queued or running run
    o         open the run in a browser
    q         quit (esc closes a dialog)

Log viewer keys: j/k line, d/u or ctrl-d/ctrl-u page, h/l pan, / search with
n/N for next/previous match, [d/]d previous/next failure block, g/G top/bottom,
esc/q close. The header keeps the
gate name and status visible while you scroll; job sections are called out in
the text and error lines are red, warnings yellow.

Providers own CLI access through `gh` or `glab`. The dashboard owns refresh,
selection, and shared log caching. Colors follow Flume when available.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, ScrollableContainer, Vertical
from textual.screen import ModalScreen
from textual.widgets import DataTable, Input, LoadingIndicator, OptionList, Static
from textual.widgets.option_list import Option
from textual.worker import NoActiveWorker, get_current_worker

from bronson.cache import Cache, DashboardSnapshot
from bronson.logs import (
    LOG_MAX_LINES,
    TIMESTAMP_RE,
    log_severity,
    paint_log,
    render_log,
    style_log_line,
)
from bronson.models import Repository, RunDetails, run_revision
from bronson.providers import Mutation, Provider
from bronson.providers.github import GitHubProvider
from bronson.providers.gitlab import GitLabProvider
from bronson.providers.repository import origin_remote, resolve_repository
from bronson.themes import (
    active_flume_theme,
    load_flume_themes,
    theme_roles,
    watch_flume_theme_changes,
)

# --------------------------------------------------------------------------
# startup and shared presentation
# --------------------------------------------------------------------------

VERSION = "0.5.0"
PANE_MIN_WIDTH = 120
PANE_MIN_HEIGHT = 22
POLL_SECONDS = 20
ACTIVE_POLL_SECONDS = 5
TICK_SECONDS = 1
ROW_LIMIT = 100
MAX_COMPLETED = 30
ACTIVE_PREFETCH = 5
COMPLETED_PREFETCH = 8

# conclusion/status -> (theme role, ANSI fallback, glyph)
STATUS_STYLE = {
    "success": ("success", "green", "✓"),
    "failure": ("error", "red", "✗"),
    "startup_failure": ("error", "red", "✗"),
    "cancelled": ("text_muted", "dim", "⊘"),
    "skipped": ("text_muted", "dim", "◦"),
    "in_progress": ("warning", "yellow", "●"),
    "queued": ("warning", "yellow", "○"),
    "waiting": ("warning", "yellow", "○"),
    "requested": ("warning", "yellow", "○"),
    "pending": ("warning", "yellow", "○"),
    "manual": ("warning", "yellow", "◇"),
    "scheduled": ("warning", "yellow", "◷"),
}


def request_cancelled() -> bool:
    """Supply worker cancellation without coupling providers to Textual."""
    try:
        return get_current_worker().is_cancelled
    except NoActiveWorker:
        return False


def open_in_browser(url: str) -> None:
    """Open links in Zen on macOS, regardless of Python's browser registry."""
    if sys.platform == "darwin":
        try:
            subprocess.run(
                ["open", "-a", "Zen", url],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
            )
            return
        except (OSError, subprocess.SubprocessError):
            pass
    webbrowser.open(url)


def update_table(table: DataTable, rows: list[tuple[str, list[Text]]]) -> None:
    """Keep rows stable on ticks and preserve selected identity on reordering."""
    keys = [row.key.value for row in table.ordered_rows]
    selected = keys[table.cursor_row] if keys else None
    row, column = table.cursor_row, table.cursor_column
    if keys != [key for key, _ in rows]:
        table.clear()
        for key, cells in rows:
            table.add_row(*cells, key=key, height=None)
        new_keys = [key for key, _ in rows]
        row = new_keys.index(selected) if selected in new_keys else min(row, len(rows) - 1)
        table.move_cursor(row=max(0, row), column=column, animate=False, scroll=False)
    else:
        columns = list(table.columns)
        for key, cells in rows:
            for column_key, old, new in zip(columns, table.get_row(key), cells, strict=False):
                if old != new:
                    table.update_cell(key, column_key, new, update_width=True)


def set_column_width(table: DataTable, key: str, width: int) -> None:
    column_key = next(k for k in table.columns if k.value == key)
    column = table.columns[column_key]
    if column.width == width:
        return
    column.width = width
    if table.ordered_rows:
        row_key = table.ordered_rows[0].key
        table.update_cell(
            row_key, column_key, table.get_cell(row_key, column_key), update_width=True
        )
    table.refresh(layout=True)


class FittedTable(DataTable):
    """Fit the name column to the real viewport, including scrollbar changes."""

    def on_resize(self) -> None:
        if self.is_mounted:
            self.call_after_refresh(self.fit_name)

    def watch_show_vertical_scrollbar(self, show: bool) -> None:
        if self.is_mounted:
            self.call_after_refresh(self.fit_name)

    def fit_name(self) -> None:
        if not self.is_mounted or not self.app.is_running:
            return
        width = self.scrollable_content_region.width
        if width <= 0:
            return
        other = sum(c.get_render_width(self) for k, c in self.columns.items() if k.value != "name")
        # Keep text off the scrollbar, independently of the pane's outer padding.
        set_column_width(self, "name", max(1, width - other - self.cell_padding * 2 - 1))


def run_matches(run: dict, query: str) -> bool:
    text = " ".join(
        str(run.get(field) or "")
        for field in (
            "displayTitle",
            "workflowName",
            "headBranch",
            "headSha",
            "status",
            "conclusion",
            "databaseId",
        )
    ).casefold()
    return all(term in text for term in query.casefold().split())


def rail_markers(rows: set[int], total: int, height: int) -> set[int]:
    """Project every marked line, rather than sampling one line per rail cell."""
    if total <= 0 or height <= 0:
        return set()
    return {min(height - 1, row * height // total) for row in rows if 0 <= row < total}


class LogScreen(ModalScreen):
    """Full-screen log viewer with a sticky context header and / search."""

    # These widgets only mount in ActionsDashboard. Keep Textual's runtime app property.
    if TYPE_CHECKING:
        app: ActionsDashboard

    BINDINGS = [
        ("escape", "close", "Close"),
        ("q", "close", "Close"),
        ("j", "scroll_down", "Down"),
        ("k", "scroll_up", "Up"),
        ("h", "scroll_left", "Left"),
        ("l", "scroll_right", "Right"),
        ("d", "page_down", "Page down"),
        ("u", "page_up", "Page up"),
        ("ctrl+d", "page_down", "Page down"),
        ("ctrl+u", "page_up", "Page up"),
        ("g", "scroll_top", "Top"),
        ("G", "scroll_bottom", "Bottom"),
        ("/", "search", "Search"),
        ("n", "next_match", "Next match"),
        ("N", "prev_match", "Previous match"),
        ("o", "open_url", "Live in browser"),
        ("question_mark", "app.help", "Help"),
    ]

    CSS = """
    #log-help { dock: bottom; height: 1; padding: 0 1; color: $text-muted; background: $background; }
    #log-header {
        dock: top;
        height: 1;
        padding: 0 1;
        background: $background;
    }
    #log-content { height: 1fr; }
    #log-scroll {
        width: 1fr;
        height: 1fr;
        overflow-x: auto;
        overflow-y: auto;
        scrollbar-size-vertical: 0;
        scrollbar-size-horizontal: 0;
    }
    #log-body { width: auto; height: auto; padding: 0 1; background: $background; }
    #log-search {
        display: none; dock: bottom; height: 1;
        border: none; padding: 0; margin: 0;
        background: $background; color: $text;
    }
    #log-search:focus { border: none; background-tint: transparent; }
    #log-search > .input--placeholder { color: $text-muted; }
    #log-rail { width: 1; height: 1fr; overflow: hidden; background: $background; }
    """

    def __init__(
        self,
        header: str,
        run: dict,
        job: dict | None = None,
        failed_only: bool = False,
    ) -> None:
        super().__init__()
        self.header_markup = header
        self.run = run
        self.job = job
        self.failed_only = failed_only
        self._fetching = False
        self._snapshot = None
        self.lines: list[Text] = []
        self._raw_lines: list[Text] = []
        self.query_text = ""
        self.matches: list[int] = []
        self.match_i = 0
        self.error_rows: set[int] = set()
        self.section_rows: set[int] = set()
        self.failure_rows: list[int] = []
        self._failure_row: int | None = None
        self._failure_scroll_y: float | None = None
        self._bracket: str | None = None

    def compose(self) -> ComposeResult:
        yield Static(self.header_markup, id="log-header")
        yield Static("[d / ]d failures   / search   esc back   ? help", id="log-help", markup=False)
        with Horizontal(id="log-content"):
            with ScrollableContainer(id="log-scroll"):
                yield Static("loading log…", id="log-body")
            yield Static(id="log-rail")
        yield Input(placeholder="search", id="log-search")

    def on_mount(self) -> None:
        self.query_one("#log-scroll", ScrollableContainer).focus()
        self.set_interval(0.15, self.update_rail)
        self.set_interval(ACTIVE_POLL_SECONDS, self.refresh_log)
        self.refresh_log()

    def refresh_log(self) -> None:
        self.app.request_detail(self.run)
        data = self.app.details.get(self.run["databaseId"])
        jobs = data.get("jobs", []) if data else ([self.job] if self.job else [])
        if self.job:
            jobs = [j for j in jobs if j["databaseId"] == self.job["databaseId"]]
            if jobs:
                self.job = jobs[0]
                status = self.job.get("conclusion") or self.job.get("status") or "?"
                self.header_markup = (
                    f"[b]{escape_markup(self.job.get('name') or 'job')}[/b]"
                    f"  ·  {escape_markup(status)}  ·  {job_duration(self.job, datetime.now(timezone.utc))}"
                )
        else:
            self.header_markup = self.app.run_header(self.run)
            if self.failed_only:
                self.header_markup += "  ·  failed only"
        self.update_log_header()
        if not self.job and self.failed_only:
            jobs = [j for j in jobs if j.get("conclusion") == "failure"]
        snapshot = [(j["databaseId"], j.get("status"), j.get("conclusion")) for j in jobs]
        if self._fetching or snapshot == self._snapshot:
            return
        self._fetching = True
        self.fetch_log(jobs, snapshot)

    @work(thread=True, exclusive=True, group="log")
    def fetch_log(self, jobs: list[dict], snapshot: list) -> None:
        worker = get_current_worker()
        text: list[Text] = []
        retry = False
        for job in jobs:
            if worker.is_cancelled:
                return
            label = job.get("name") or "job"
            if job.get("status") != "completed":
                text.extend(
                    [
                        Text(f"── {label} ", style="bold dim"),
                        Text(
                            f"Job is {job.get('status') or 'unknown'}. "
                            "Logs appear when it finishes. "
                            f"Press o to open {self.app.provider.name}.",
                            style="dim",
                        ),
                    ]
                )
                continue
            if job.get("conclusion") == "skipped":
                text.extend(
                    [
                        Text(f"── {label} ", style="bold dim"),
                        Text("Job skipped. No log output.", style="dim"),
                    ]
                )
                continue
            try:
                raw = self.app.load_job_log(job["databaseId"])
            except (OSError, subprocess.SubprocessError, ValueError) as exc:
                retry = True
                text.extend(
                    [
                        Text(f"── {label} ", style="bold dim"),
                        Text(f"Log unavailable, retrying: {exc}", style="dim"),
                    ]
                )
                continue
            if not raw:
                retry = True
            text.extend(render_log(raw, label, 0, 0))
            if not worker.is_cancelled:
                # Show each job as it arrives instead of waiting for every download.
                self.app.call_from_thread(self.show_log, list(text))
            if len(text) >= LOG_MAX_LINES:
                text = text[:LOG_MAX_LINES]
                text.append(
                    Text(
                        f"… truncated after {LOG_MAX_LINES} lines; open individual jobs for more",
                        style="dim italic",
                    )
                )
                break
        if not text:
            text = [Text("No job logs available yet.", style="dim")]
        if not worker.is_cancelled:
            self.app.call_from_thread(self.finish_log, text, None if retry else snapshot)

    def show_log(self, text: list[Text]) -> None:
        if self.is_mounted:
            self.apply_log(text)

    def finish_log(self, text: list[Text], snapshot: list | None) -> None:
        self._fetching = False
        self._snapshot = snapshot
        self.show_log(text)

    def update_log_header(self) -> None:
        count = len(self.failure_rows)
        suffix = f"  ·  {count} error block{'s' if count != 1 else ''}" if self.lines else ""
        self.query_one("#log-header", Static).update(self.header_markup + suffix)

    def action_open_url(self) -> None:
        url = (self.job or {}).get("url") or self.run.get("url")
        if url:
            open_in_browser(url)

    def apply_log(self, text: list[Text]) -> None:
        self._raw_lines = text
        text = [paint_log(line, self.app.color) for line in text]
        self.lines = text
        self.error_rows = {
            i
            for i, line in enumerate(text)
            if not line.plain.startswith("──") and log_severity(line.plain) == "error"
        }
        failures = sorted(i for i in self.error_rows if i - 1 not in self.error_rows)
        if failures != self.failure_rows:
            self._failure_row = self._failure_scroll_y = None
        self.failure_rows = failures
        self.section_rows = {i for i, line in enumerate(text) if line.plain.startswith("──")}
        self.query_one("#log-body", Static).update(Text("\n").join(text))
        if self.query_text:
            needle = self.query_text.lower()
            self.matches = [i for i, line in enumerate(text) if needle in line.plain.lower()]
            self.match_i = min(self.match_i, max(0, len(self.matches) - 1))

        self.highlight_matches()
        self.update_log_header()

    def action_close(self) -> None:
        self.dismiss()

    def on_key(self, event) -> None:
        """Vim-style diagnostic chords, without stealing characters from search."""
        search = self.query_one("#log-search", Input)
        if search.has_focus:
            self._bracket = None
            if event.key == "escape":
                self._close_search()
                event.stop()
                event.prevent_default()
            return
        bracket, self._bracket = self._bracket, None
        if event.character in ("[", "]"):
            self._bracket = event.character
        elif bracket and event.character == "d":
            self.jump_failure(1 if bracket == "]" else -1)
        elif not (bracket and event.key == "escape"):
            return
        event.stop()
        event.prevent_default()

    def jump_failure(self, direction: int) -> None:
        """Jump to consecutive-error block starts, wrapping at either end."""
        if not self.failure_rows:
            self.notify("no failures found")
            return
        scroll = self._scroll()
        y = scroll.scroll_offset.y
        # Several targets near EOF can share the same clamped scroll position.
        anchor = self._failure_row if y == self._failure_scroll_y else y
        if anchor is None:
            anchor = y
        if direction > 0:
            candidates = [row for row in self.failure_rows if row > anchor]
            target = candidates[0] if candidates else self.failure_rows[0]
        else:
            candidates = [row for row in self.failure_rows if row < anchor]
            target = candidates[-1] if candidates else self.failure_rows[-1]

        scroll.scroll_to(y=target, animate=False)
        self._failure_row = target
        self._failure_scroll_y = min(target, scroll.max_scroll_y)
        self.highlight_matches()
        self.notify(
            f"error block {self.failure_rows.index(target) + 1}/{len(self.failure_rows)}", timeout=1
        )

    # -- search --------------------------------------------------------------

    def action_search(self) -> None:
        if not self.lines:
            self.app.notify("log is still loading")
            return
        self._bracket = None
        search = self.query_one("#log-search", Input)
        search.display = True
        search.focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self.run_search(event.value)
        self._close_search()

    def _close_search(self) -> None:
        search = self.query_one("#log-search", Input)
        search.display = False
        self._scroll().focus()

    def run_search(self, query: str) -> None:
        self.query_text = query.strip()
        if not self.query_text:
            self.matches = []
            return
        needle = self.query_text.lower()
        self.matches = [i for i, line in enumerate(self.lines) if needle in line.plain.lower()]
        if not self.matches:
            self.notify("no matches")
            return
        y = self._scroll().scroll_offset.y
        after = [m for m in self.matches if m >= y]
        self.match_i = self.matches.index(after[0]) if after else 0
        self.highlight_matches()
        self.jump_to_match()

    def highlight_matches(self) -> None:
        needle = self.query_text.lower()
        shown = list(self.lines)
        for i in self.matches:
            line = self.lines[i]
            plain = line.plain
            marked = Text(plain)
            marked.spans = list(line.spans)
            at = plain.lower().find(needle)
            while at != -1:
                marked.stylize("black on yellow", at, at + len(needle))
                at = plain.lower().find(needle, at + len(needle))
            shown[i] = marked
        if self._failure_row is not None and self._failure_row < len(shown):
            marked = shown[self._failure_row].copy()
            marked.stylize("bold reverse")
            shown[self._failure_row] = marked
        self.query_one("#log-body", Static).update(Text("\n").join(shown))

    def jump_to_match(self) -> None:
        if not self.matches:
            return
        self._scroll().scroll_to(y=self.matches[self.match_i], animate=False)
        self.notify(f"match {self.match_i + 1}/{len(self.matches)}", timeout=1)

    def action_next_match(self) -> None:
        if not self.matches:
            self.app.notify("no search — press / first")
            return
        self.match_i = (self.match_i + 1) % len(self.matches)
        self.jump_to_match()

    def action_prev_match(self) -> None:
        if not self.matches:
            self.app.notify("no search — press / first")
            return
        self.match_i = (self.match_i - 1) % len(self.matches)
        self.jump_to_match()

    # -- position rail -------------------------------------------------------

    def update_rail(self) -> None:
        """One-column overview: red ticks at failures, thumb for the viewport."""
        rail = self.query_one("#log-rail", Static)
        total = len(self.lines)
        if not total:
            rail.update(Text(" "))
            return
        scroll = self._scroll()
        height = max(1, rail.size.height)
        thumb_start = min(height - 1, int(scroll.scroll_offset.y * height / total))
        thumb_rows = max(1, round(height * min(1.0, scroll.size.height / total)))
        thumb_end = min(height, thumb_start + thumb_rows)
        errors = rail_markers(self.error_rows, total, height)
        sections = rail_markers(self.section_rows, total, height)
        error, section, thumb = (
            self.app.color("error", "red"),
            self.app.color("text_muted", "dim"),
            self.app.color("border_subtle", "#444444"),
        )
        strip = Text()
        for row in range(height):
            if row in errors:
                char, fg = "█", error
            elif row in sections:
                char, fg = "·", section
            else:
                char, fg = " ", section
            style = f"{fg} on {thumb}" if thumb_start <= row < thumb_end else fg
            strip.append(char, style=style)
            if row < height - 1:
                strip.append("\n")
        rail.update(strip)

    def _scroll(self) -> ScrollableContainer:
        return self.query_one("#log-scroll", ScrollableContainer)

    def action_scroll_left(self) -> None:
        self._scroll().scroll_relative(x=-8, animate=False)

    def action_scroll_right(self) -> None:
        self._scroll().scroll_relative(x=8, animate=False)

    def action_scroll_down(self) -> None:
        self._scroll().scroll_relative(y=1, animate=False)

    def action_scroll_up(self) -> None:
        self._scroll().scroll_relative(y=-1, animate=False)

    def action_page_down(self) -> None:
        self._scroll().scroll_page_down(animate=False)

    def action_page_up(self) -> None:
        self._scroll().scroll_page_up(animate=False)

    def action_scroll_top(self) -> None:
        self._scroll().scroll_home(animate=False)

    def action_scroll_bottom(self) -> None:
        self._scroll().scroll_end(animate=False)


# --------------------------------------------------------------------------
# time formatting
# --------------------------------------------------------------------------


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def length_label(start: datetime, end: datetime) -> str:
    seconds = max(0, int((end - start).total_seconds()))
    minutes, sec = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{sec:02d}"
    return f"{minutes:02d}:{sec:02d}"


def run_length(run: dict, now: datetime) -> str | None:
    start = parse_time(run.get("startedAt"))
    if start is None:
        return None
    if run.get("status") == "completed":
        end = parse_time(run.get("completedAt") or run.get("updatedAt")) or now
    else:
        end = now
    return length_label(start, end)


def start_label(run: dict, now: datetime) -> str:
    start = parse_time(run.get("startedAt") or run.get("createdAt"))
    if start is None:
        return "—"
    local = start.astimezone()
    if local.date() == now.astimezone().date():
        return local.strftime("%H:%M")
    return local.strftime("%m-%d %H:%M")


def dash() -> Text:
    return Text("—", style="dim")


def escape_markup(text: str) -> str:
    return text.replace("[", "\\[")


def status_glyph(status: str) -> tuple[str, str, str]:
    """(theme role, ANSI fallback, glyph) for a run or job conclusion/status."""
    return STATUS_STYLE.get(status, ("accent", "magenta", status[:3]))


def job_duration(job: dict, now: datetime) -> str:
    start = parse_time(job.get("startedAt"))
    if not start:
        return "—"
    end = (parse_time(job.get("completedAt")) or now) if job.get("status") == "completed" else now
    return length_label(start, end)


def job_cells(job: dict, now: datetime, color) -> list[Text]:
    role, fallback, glyph = status_glyph(job.get("conclusion") or job.get("status") or "?")
    return [
        Text(glyph, style=color(role, fallback)),
        Text(job.get("name") or "?", no_wrap=True, overflow="ellipsis"),
        Text(job_duration(job, now), style="dim"),
    ]


class HelpScreen(ModalScreen):
    if TYPE_CHECKING:
        app: ActionsDashboard

    BINDINGS = [
        ("escape", "dismiss", "Close"),
        ("q", "dismiss", "Close"),
        ("question_mark", "dismiss", "Close"),
    ]
    CSS = """
    HelpScreen { align: center middle; }
    #help-body { width: 64; max-width: 100%; height: auto; max-height: 95%;
                 padding: 1 2; border: round $accent; background: $panel; }
    """

    def compose(self) -> ComposeResult:
        mutation_help = (
            "  r / R           Rerun failed / all jobs in selected run\n"
            "  c               Cancel selected run\n"
            if self.app.provider.can_mutate
            else "  GitLab is read-only. Pipeline actions are disabled.\n"
        )
        with ScrollableContainer(id="help-body"):
            yield Static(
                Text(
                    "KEYBOARD\n\n"
                    "Dashboard\n"
                    "  j / k, arrows   Move through runs or jobs\n"
                    "  tab / shift-tab Switch panes\n"
                    "  /               Filter fetched runs as you type\n"
                    "  enter / escape  Keep filter / clear it\n"
                    "  t               Choose theme or follow Flume sync\n"
                    "  enter           Jobs, or expand selected job log\n"
                    "  escape          Return to runs / close expanded view\n"
                    "  g / G           Logs / failed job logs\n"
                    "  o               Open selected run or job in browser\n"
                    "  ctrl+r          Refresh now\n" + mutation_help + "  q               Quit\n\n"
                    "Expanded logs\n"
                    "  j / k           Scroll lines\n"
                    "  d / u, ctrl-d/u Page down / up\n"
                    "  h / l           Pan horizontally\n"
                    "  g / G           Top / bottom\n"
                    "  [d / ]d         Previous / next failure block\n"
                    "  /, n / N        Search, next / previous match\n"
                    "  o               Live output in browser\n\n"
                    "Search covers message, branch, workflow, SHA and status.\n"
                    "Search only filters the latest fetched runs.\n"
                    "Counts cover the runs shown, not historical pass rates.\n"
                    "Wide terminals show inline jobs beside a log preview.\n"
                    "Enter expands a run. Esc collapses it. [ / ] resize the run pane.\n"
                    "Narrow terminals use full-width drill-down views.\n\n"
                    "esc close",
                    overflow="fold",
                )
            )


class ThemeScreen(ModalScreen):
    if TYPE_CHECKING:
        app: ActionsDashboard

    BINDINGS = [("escape", "dismiss", "Close"), ("q", "dismiss", "Close")]
    CSS = """
    ThemeScreen { align: center middle; }
    #theme-options { width: 46; height: auto; max-height: 80%; padding: 1;
                     border: round $accent; background: $panel; }
    """

    def compose(self) -> ComposeResult:
        names = sorted(self.app.available_themes)
        flume = [name for name in names if name.startswith("flume-")]
        options = [Option("Follow Flume sync", id="auto")] if flume else []
        options.extend(
            Option(("● " if name == self.app.theme else "  ") + name, id=name)
            for name in flume + [n for n in names if n not in flume]
        )
        yield OptionList(*options, id="theme-options")

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        if event.option.id is not None:
            self.app.select_theme(event.option.id)
        self.dismiss()


class JobPane(Vertical):
    """A bounded log preview. Navigation belongs to the inline run/job list."""

    if TYPE_CHECKING:
        app: ActionsDashboard

    def __init__(self) -> None:
        super().__init__(id="detail-pane")
        self.run: dict | None = None
        self.jobs: list[dict] = []
        self._preview_key: tuple | None = None
        self._preview_timer = None
        self._retry_at = 0.0
        self._preview_text = Text()

    def compose(self) -> ComposeResult:
        yield Static("Select a run", id="detail-title", markup=False)
        yield Static("", id="detail-meta", markup=False)
        yield Static("", id="detail-message", markup=False)
        yield Static("Log preview", id="preview-title", markup=False)
        with ScrollableContainer(id="preview-scroll"):
            yield Static("Select a job to inspect its output.", id="preview-body", markup=False)

    def show_run(self, run: dict | None) -> None:
        previous_identity = (self.run or {}).get("databaseId"), (self.run or {}).get("attempt")
        selected_identity = (run or {}).get("databaseId"), (run or {}).get("attempt")
        self.run = run
        if previous_identity != selected_identity:
            self.jobs = []
            self._preview_key = None
            self.set_preview("Select a job to inspect its output.")
        if not run:
            self.query_one("#detail-title", Static).update("Select a run")
            self.query_one("#detail-meta", Static).update("")
            self.query_one("#detail-message", Static).update("")
            return
        status = run.get("conclusion") or run.get("status") or "?"
        self.query_one("#detail-title", Static).update(
            run.get("displayTitle") or run.get("workflowName") or "Workflow"
        )
        meta = Text(
            run.get("workflowName") or "Workflow", style=self.app.color("text_subtle", "dim")
        )
        meta.append(" · ")
        meta.append(
            run.get("headBranch") or "?", style=self.app.color("accent_secondary", "magenta")
        )
        meta.append(" · ")
        meta.append((run.get("headSha") or "")[:7], style=self.app.color("accent_tertiary", "cyan"))
        self.query_one("#detail-meta", Static).update(meta)
        role, fallback, glyph = status_glyph(status)
        now = datetime.now(timezone.utc)
        summary = Text(f"{glyph} {status.replace('_', ' ')}", style=self.app.color(role, fallback))
        summary.append(
            f" · {run_length(run, now) or '—'} · started {start_label(run, now)}",
            style=self.app.color("text_subtle", "dim"),
        )
        self.query_one("#detail-message", Static).update(summary)
        data = self.app.details.get(run["databaseId"])
        self.jobs = (data or {}).get("jobs") or []
        job = self.selected_job()
        if not job:
            self._preview_key = None
            error = self.app.detail_errors.get(run["databaseId"])
            if error:
                message = "Couldn't fetch jobs. Retrying…"
            elif data is None:
                message = "Fetching jobs…"
            elif not self.jobs:
                message = "No jobs yet."
            else:
                message = "Enter expands this run. Select a job to preview its log."
            self.set_preview(message)
            return
        self.query_one("#detail-title", Static).update(job.get("name") or "Job")
        role, fallback, glyph = status_glyph(job.get("conclusion") or job.get("status") or "?")
        summary = Text(
            f"{glyph} {job.get('conclusion') or job.get('status') or '?'}",
            style=self.app.color(role, fallback),
        )
        summary.append(
            f" · {job_cells(job, now, self.app.color)[2].plain}",
            style=self.app.color("text_subtle", "dim"),
        )
        self.query_one("#detail-message", Static).update(summary)
        self.schedule_preview()

    def selected_job(self) -> dict | None:
        return self.app.selected_job()

    def set_preview(self, message: str) -> None:
        self.query_one("#preview-title", Static).update("Log preview")
        self._preview_text = Text(message, style="dim")
        self.retheme()
        self.query_one("#preview-scroll", ScrollableContainer).scroll_home(animate=False)

    def schedule_preview(self) -> None:
        job = self.selected_job()
        if not self.run or not job:
            return
        key = (self.run["databaseId"], job["databaseId"], job.get("status"), job.get("conclusion"))
        if key == self._preview_key and (not self._retry_at or time.monotonic() < self._retry_at):
            return
        self._preview_key, self._retry_at = key, 0.0
        if self._preview_timer:
            self._preview_timer.stop()
        if job.get("status") != "completed":
            self.set_preview(
                f"Job is {job.get('status') or 'unknown'}. "
                f"Completed job logs appear here. Open in {self.app.provider.name} for live output."
            )
        elif job.get("conclusion") == "skipped":
            self.set_preview("Job skipped. No log output.")
        else:
            self.set_preview("Fetching job log…")
            self._preview_timer = self.set_timer(0.15, lambda: self.fetch_preview(key, dict(job)))

    @work(thread=True, exclusive=True, group="preview")
    def fetch_preview(self, key: tuple, job: dict) -> None:
        try:
            raw = self.app.load_job_log(job["databaseId"])
            lines = render_log(raw, job.get("name") or "job", 0, 0)
            errors = [i for i, line in enumerate(lines) if log_severity(line.plain) == "error"]
            error_set = set(errors)
            blocks = sum(i - 1 not in error_set for i in errors)
            start = max(0, errors[0] - 3) if errors else 0
            # Timestamps dominate narrow previews. Keep them in expanded logs.
            excerpt = Text("\n").join(
                style_log_line(Text(), TIMESTAMP_RE.sub("", line.plain))
                for line in lines[start : start + 200]
            )
            diagnostics = f"{blocks} diagnostic {'block' if blocks == 1 else 'blocks'}"
            if blocks and job.get("conclusion") == "success":
                diagnostics += " · job passed"
            title = (
                f"Log preview · {diagnostics} · lines {start + 1}–{min(len(lines), start + 200)}"
            )
            error = None if raw else "No log output published yet. Retrying…"
        except (OSError, subprocess.SubprocessError, ValueError):
            excerpt, title, error = Text(), "Log preview", "Couldn't fetch log. Retrying…"
        if not get_current_worker().is_cancelled:
            self.app.call_from_thread(self.apply_preview, key, excerpt, title, error)

    def apply_preview(self, key: tuple, excerpt: Text, title: str, error: str | None) -> None:
        if not self.is_mounted or key is None or key != self._preview_key:
            return
        if error:
            self.set_preview(error)
            self._retry_at = time.monotonic() + ACTIVE_POLL_SECONDS
        else:
            self.query_one("#preview-title", Static).update(title)
            self._preview_text = excerpt
            self.retheme()

    def retheme(self) -> None:
        self.query_one("#preview-body", Static).update(
            paint_log(self._preview_text, self.app.color)
        )

    def open_log(self) -> None:
        job = self.selected_job()
        if self.run and job:
            self.app.push_screen(LogScreen(job.get("name") or "job", self.run, job=job))


# --------------------------------------------------------------------------
# per-job dialog
# --------------------------------------------------------------------------


class JobDetailScreen(ModalScreen):
    """One run's jobs. enter/l opens the highlighted job's log in a pager."""

    if TYPE_CHECKING:
        app: ActionsDashboard

    BINDINGS = [
        ("escape", "close", "Close"),
        ("q", "close", "Close"),
        ("enter", "job_logs", "Job log"),
        ("l", "job_logs", "Job log"),
        ("j", "cursor_down", "Down"),
        ("k", "cursor_up", "Up"),
        ("G", "run_failed_logs", "Failed logs"),
        ("o", "open_url", "Open"),
        ("question_mark", "app.help", "Help"),
    ]

    CSS = """
    JobDetailScreen { align: center middle; }
    #jobs {
        width: 100%;
        max-width: 110;
        height: auto;
        max-height: 80%;
        background: $panel;
        border: round $border-blurred;
    }
    """

    def __init__(self, repo: str, run: dict) -> None:
        super().__init__()
        self.repo = repo
        self.run = run
        self.url: str | None = run.get("url")
        self.jobs: list[dict] = []

    def compose(self) -> ComposeResult:
        table = DataTable(id="jobs")
        table.cursor_type = "row"
        table.add_columns("", "Job", "Time")
        yield table
        yield Static("enter logs   esc back   ? help", classes="quiet-footer")

    def on_mount(self) -> None:
        self.render_summary()
        self.set_interval(TICK_SECONDS, self.refresh_detail)
        self.refresh_detail()

    def render_summary(self) -> None:
        """Immediate placeholder from run-list data; jobs land a moment later."""
        table = self.query_one("#jobs", DataTable)
        now = datetime.now(timezone.utc)
        run = self.run
        table.add_row(
            Text("…", style="dim"),
            Text(
                f"{run.get('workflowName', 'run')} · {run.get('headBranch', '?')}",
                style="bold",
            ),
            Text(run_length(run, now) or "—"),
        )

    def refresh_detail(self) -> None:
        self.app.request_detail(self.run)
        data = self.app.details.get(self.run["databaseId"])
        if data is not None:
            self.apply_detail(data, None)
        elif error := self.app.detail_errors.get(self.run["databaseId"]):
            self.apply_detail(None, error)

    def apply_detail(self, data: dict | None, error: str | None) -> None:
        table = self.query_one("#jobs", DataTable)
        now = datetime.now(timezone.utc)
        if error or data is None:
            self.jobs = []
            update_table(
                table,
                [
                    (
                        "error",
                        [Text("✗", style="red"), Text(f"Provider request failed: {error}"), dash()],
                    )
                ],
            )
            return
        self.jobs = data.get("jobs") or []
        rows = []
        for job in self.jobs:
            rows.append((str(job["databaseId"]), job_cells(job, now, self.app.color)))
        update_table(table, rows or [("empty", [dash(), Text("(no job data)"), dash()])])

    def selected_job(self) -> dict | None:
        table = self.query_one("#jobs", DataTable)
        row = table.cursor_row
        return self.jobs[row] if 0 <= row < len(self.jobs) else None

    def action_cursor_down(self) -> None:
        self.query_one("#jobs", DataTable).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one("#jobs", DataTable).action_cursor_up()

    def on_data_table_row_selected(self, event) -> None:
        if event.data_table.id == "jobs":
            self.action_job_logs()

    def action_close(self) -> None:
        self.dismiss()

    def action_open_url(self) -> None:
        if self.url:
            open_in_browser(self.url)

    def action_job_logs(self) -> None:
        job = self.selected_job()
        if job is None or not job.get("databaseId"):
            self.app.notify("no job selected yet")
            return
        self.app.push_screen(
            LogScreen(
                self.job_header(job),
                self.run,
                job=job,
            )
        )

    def job_header(self, job: dict) -> str:
        conclusion = job.get("conclusion") or job.get("status") or "?"
        role, fallback, glyph = status_glyph(conclusion)
        color = self.app.color(role, fallback)
        started = parse_time(job.get("startedAt"))
        span = ""
        if started:
            now = datetime.now(timezone.utc)
            if job.get("status") == "completed":
                span = length_label(started, parse_time(job.get("completedAt")) or now)
            else:
                span = length_label(started, now)
        name = escape_markup(job.get("name") or "?")
        return (
            f"[{color}]{glyph}[/{color}] [b]{name}[/b]"
            f"  ·  {escape_markup(conclusion)}  ·  {span or '—'}"
            "  ·  esc close  j/k scroll"
        )

    def action_run_failed_logs(self) -> None:
        if not self.app.run_has_failures(self.run):
            self.app.notify("no failed jobs in this run")
            return
        self.app.push_screen(
            LogScreen(
                f"[b]{escape_markup(self.run.get('workflowName', 'run'))}[/b]"
                "  ·  failed jobs only  ·  esc close",
                self.run,
                failed_only=True,
            )
        )


# --------------------------------------------------------------------------
# main dashboard
# --------------------------------------------------------------------------


class ActionsDashboard(App):
    TITLE = "bronson"
    CSS = """
    Screen { background: $background; }
    * { scrollbar-size: 1 1; scrollbar-background: $background;
        scrollbar-background-hover: $background; scrollbar-background-active: $background;
        scrollbar-color: $primary 40%; scrollbar-color-hover: $primary 70%;
        scrollbar-color-active: $primary; }
    DataTable { height: 1fr; background: $background; }
    DataTable > .datatable--even-row { background: transparent; }
    DataTable:focus { background-tint: transparent; }
    #dashboard-header { height: 1; padding: 0 1; background: $background; }
    #repo-title { width: 1fr; text-style: bold; }
    #dashboard-status { width: auto; color: $text-muted; }
    #workspace { height: 1fr; }
    #runs-pane { width: 45%; border: round $border-blurred; padding: 0 1;
                 border-title-color: $text-muted; }
    #runs-pane:focus-within { border: round $primary; border-title-color: $primary; }
    #detail-pane { width: 1fr; border: round $border-blurred; padding: 0 1;
                   border-title-color: $text-muted; }
    #detail-pane:focus-within { border: round $primary; border-title-color: $primary; }
    #detail-title { height: auto; max-height: 2; text-style: bold; color: $text; }
    #detail-meta { height: 1; color: $text-muted; }
    #detail-message { height: 1; margin-bottom: 1; color: $text; }
    #preview-title { height: auto; max-height: 2; color: $secondary;
                     border-bottom: solid $border-blurred; }
    #preview-title.-focused { color: $primary; text-style: bold; }
    #preview-scroll { height: 1fr; }
    #preview-body { width: auto; height: auto; }
    #run-state { height: 1fr; align: center middle; padding: 1; }
    #startup-content { height: auto; width: 100%; }
    #startup-brand { height: 1; margin-bottom: 1; text-align: center; color: $secondary; text-style: bold; }
    #startup-spinner { height: 1; width: 100%; margin-top: 1; content-align: center middle; color: $primary; }
    #run-state-message { height: auto; width: 100%; text-align: center; color: $text-muted; }
    #run-caption { height: 1; color: $text-muted; }
    #run-search { height: 1; border: none; padding: 0; margin: 0; background: transparent; }
    #run-search:focus { border: none; background: $panel; background-tint: transparent; }
    #run-search > .input--placeholder { color: $text-muted; }
    DataTable:focus > .datatable--cursor { background: $primary 30%; color: $text; text-style: bold; }
    DataTable > .datatable--cursor { background: $primary 12%; color: $text; }
    DataTable > .datatable--header { background: $background; color: $text-muted; }
    .quiet-footer { dock: bottom; height: 1; padding: 0 1; color: $text-muted; background: $background; }
    """
    BINDINGS = [
        ("q", "quit", "Quit"),
        ("j", "cursor_down", "Down"),
        ("k", "cursor_up", "Up"),
        ("h", "cursor_left", "Left"),
        ("l", "cursor_right", "Right"),
        ("enter", "detail", "Jobs"),
        ("g", "run_logs", "Logs"),
        ("G", "failed_logs", "Failed logs"),
        ("r", "rerun_failed", "Rerun failed"),
        ("R", "rerun_all", "Rerun all"),
        ("c", "cancel_run", "Cancel"),
        ("o", "open_run", "Open"),
        ("escape", "focus_runs", "Runs"),
        ("ctrl+r", "refresh", "Refresh"),
        ("question_mark", "help", "Help"),
        ("slash", "search_runs", "Search"),
        ("t", "themes", "Theme"),
        ("left_square_bracket", "narrow_runs", "Narrow runs"),
        ("right_square_bracket", "widen_runs", "Widen runs"),
    ]

    def __init__(
        self,
        repo: str,
        cache: Cache,
        theme_name: str | None = None,
        *,
        provider: Provider | None = None,
    ) -> None:
        super().__init__()
        self.provider = provider or GitHubProvider(
            Repository("github", "github.com", repo), request_cancelled
        )
        self.repo = self.provider.repository.label
        self.cache_key = self.provider.repository.cache_key
        self.cache = cache
        self.theme_override = theme_name or os.environ.get("BRONSON_THEME")
        if self.theme_override == "auto":
            self.theme_override = None

        self.run_filter = ""
        self.runs: list[dict] = []
        self.row_map: list[dict | None] = []  # job rows also map to their parent run
        self.job_rows: dict[int, dict] = {}
        self.expanded_run: int | None = None
        self.run_width = 45
        self.details: dict[int, dict] = {}  # run id -> jobs payload
        self.detail_errors: dict[int, str] = {}
        self._detail_revisions: dict[int, tuple] = {}
        self._detail_fetched: dict[int, float] = {}
        self._detail_inflight: set[int] = set()

        self._polling = False
        self._last_poll = 0.0
        self.gates: dict[int, tuple] = {}  # run id -> (passed, total, failed, pending)
        self.log_cache: dict[tuple, str] = {}  # (repo, key) -> raw log text
        self._log_locks: dict[tuple, threading.Lock] = {}
        self._loaded = False
        self._last_success: datetime | None = None
        self.wide = False
        self.error: str | None = None
        self._roles: dict[str, str] = {}
        self._prefetch_timer = None

    def compose(self) -> ComposeResult:
        with Horizontal(id="dashboard-header"):
            yield Static(self.repo, id="repo-title", markup=False)
            yield Static("Connecting…", id="dashboard-status", markup=False)
        with Horizontal(id="workspace"):
            with Vertical(id="runs-pane"):
                yield Input(placeholder="/ Filter runs…", id="run-search")
                with Vertical(id="run-state"):
                    with Vertical(id="startup-content"):
                        yield Static("bronson", id="startup-brand", markup=False)
                        yield Static(
                            "Preparing your allegedly working pipelines…",
                            id="run-state-message",
                            markup=False,
                        )
                        yield LoadingIndicator(id="startup-spinner")
                table = FittedTable(
                    id="runs",
                    cursor_type="row",
                    show_header=False,
                    cell_padding=0,
                    cursor_foreground_priority="renderable",
                )
                table.add_column("", width=2)
                table.add_column("Commit / workflow", key="name", width=20)
                yield table
                yield Static("", id="run-caption", markup=False)
            yield JobPane()
        yield Static("? help", id="dashboard-footer", classes="quiet-footer", markup=False)

    def restore_snapshot(self) -> None:
        """Cached data is a startup hint, never a replacement for polling."""
        snapshot = self.cache.load_snapshot(self.cache_key, run_revision)
        if snapshot is None:
            return
        self.runs = snapshot.runs
        self._loaded = bool(self.runs)
        for run, jobs in snapshot.details:
            self.apply_gates(run["databaseId"], run_revision(run), jobs, None, persist=False)
        self.refresh_rows()

    def save_snapshot(self) -> None:
        details = [
            (run, self.details[run["databaseId"]]["jobs"])
            for run in self.runs
            if self._detail_revisions.get(run["databaseId"]) == run_revision(run)
            and run["databaseId"] in self.details
        ]
        self.cache.save_snapshot(
            self.cache_key, DashboardSnapshot(self.runs, details), run_revision
        )

    def on_resize(self) -> None:
        if self.is_mounted:
            self.call_after_refresh(self.resize_layout)

    def resize_layout(self) -> None:
        if not self.is_running:
            return
        self.wide = self.size.width >= PANE_MIN_WIDTH and self.size.height >= PANE_MIN_HEIGHT
        pane = self.query_one(JobPane)
        if not self.wide and self.focused is pane.query_one("#preview-scroll"):
            self.action_focus_runs()
        self.refresh_rows()

    def on_descendant_focus(self) -> None:
        if self.is_mounted:
            self.update_footer()

    def detail_focused(self) -> bool:
        return self.wide and self.focused is self.query_one("#preview-scroll")

    def update_footer(self) -> None:
        if not self.is_running:
            return
        if self.detail_focused():
            text = "enter expand log   tab switch pane   esc runs   ? help"
        elif self.wide:
            text = "/ search   enter jobs/log   tab preview   [ ] resize   ? help"
        else:
            text = "/ search   enter jobs   ? help"
        self.query_one("#dashboard-footer", Static).update(text)
        self.query_one("#preview-title").set_class(
            self.focused is self.query_one("#preview-scroll"), "-focused"
        )

    def update_chrome(self) -> None:
        if not self.is_running:
            return
        shown = self.ordered_runs()
        self.query_one(JobPane).display = self.wide and bool(shown)
        self.query_one("#runs-pane").styles.width = (
            f"{self.run_width}%" if self.wide and shown else "100%"
        )
        self.query_one("#runs", FittedTable).fit_name()
        running = sum(r.get("status") == "in_progress" for r in shown)
        queued = sum(r.get("status") not in ("completed", "in_progress") for r in shown)
        failed = sum(r.get("conclusion") in ("failure", "startup_failure") for r in shown)
        if not self._loaded:
            status = "Fetching runs…"
        elif self.error:
            status = "Refresh failed · ctrl+r retry"
        else:
            status = f"{running} running · {queued} queued · {failed} failed / {len(shown)} shown"
            if self.size.width < PANE_MIN_WIDTH:
                status = f"{running + queued} active · {failed} failed (shown)"
            if self.run_filter:
                status += " · filtered"
            if self._last_success is None:
                status += " · cached"
            if self._polling:
                status += " · refreshing"
        summary = Text(status)
        if self.error:
            summary.stylize(self.color("error", "red"))
        elif self._loaded:
            if failed:
                summary.highlight_words([f"{failed} failed"], style=self.color("error", "red"))
            for count, label in (
                (running, "running"),
                (queued, "queued"),
                (running + queued, "active"),
            ):
                if count:
                    summary.highlight_words(
                        [f"{count} {label}"], style=self.color("warning", "yellow")
                    )
        self.query_one("#dashboard-status", Static).update(summary)
        refreshed = (
            self._last_success.astimezone().strftime("%H:%M:%S") if self._last_success else "never"
        )
        self.query_one(
            "#dashboard-status", Static
        ).tooltip = f"Last successful refresh: {refreshed}"
        empty = not shown
        self.query_one("#run-state").display = empty
        self.query_one("#startup-spinner").display = not self.runs and self._polling
        self.query_one("#startup-brand").display = not self.runs
        self._table().display = not empty
        self.query_one("#run-caption").display = not empty
        if empty:
            if self.run_filter and self.runs:
                message = f"No matching runs in the latest {len(self.runs)} fetched.\nEsc clears the filter."
            elif self._polling:
                message = "Preparing your allegedly working pipelines…"
            elif self.error:
                message = f"Couldn't fetch runs.\n{self.error}\n\nctrl+r to retry"
            else:
                message = "No workflow runs yet."
            self.query_one("#run-state-message", Static).update(message)
        self.query_one("#runs-pane").border_title = "Runs"
        self.query_one(JobPane).border_title = "Job log"
        selected = self.selected_run()
        caption = ""
        if selected:
            caption = (
                f"{selected.get('headBranch') or '?'} · {(selected.get('headSha') or '')[:7]}"
                f" · started {start_label(selected, datetime.now(timezone.utc))}"
            )
        if self.error and self.runs:
            caption = "Showing last fetched data. Refresh failed. ctrl+r to retry."
        self.query_one("#run-caption", Static).update(caption)
        if self.wide and self.screen is self.screen_stack[0]:
            if selected:
                self.request_detail(selected)
            self.query_one(JobPane).show_run(selected)
        self.update_footer()

    def action_help(self) -> None:
        self.push_screen(HelpScreen())

    def action_focus_runs(self) -> None:
        if self.focused is self._table():
            if self.expanded_run is not None:
                parent = self.expanded_run
                self.expanded_run = None
                self.refresh_rows()
                self.select_row_key(str(parent))
            elif self.run_filter:
                self.clear_run_search()
        self._table().focus()

    def select_row_key(self, key: str) -> None:
        for index, row in enumerate(self._table().ordered_rows):
            if row.key.value == key:
                self._table().move_cursor(row=index, animate=False)
                break

    def action_narrow_runs(self) -> None:
        self.resize_runs(-5)

    def action_widen_runs(self) -> None:
        self.resize_runs(5)

    def resize_runs(self, change: int) -> None:
        if self.wide:
            self.run_width = max(30, min(65, self.run_width + change))
            self.query_one("#runs-pane").styles.width = f"{self.run_width}%"

    def action_search_runs(self) -> None:
        if self.screen is not self.screen_stack[0]:
            return
        search = self.query_one("#run-search", Input)
        search.display = True
        search.focus()

    def clear_run_search(self) -> None:
        self.run_filter = ""
        search = self.query_one("#run-search", Input)
        search.value = ""
        search.display = False
        self.refresh_rows()
        self._table().focus()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "run-search":
            event.stop()
            self.run_filter = event.value.strip()
            self.refresh_rows()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "run-search":
            event.stop()
            event.input.display = bool(self.run_filter)
            self._table().focus()

    def on_key(self, event) -> None:
        if event.key == "escape" and self.query_one("#run-search", Input).has_focus:
            self.clear_run_search()
            event.stop()
            event.prevent_default()

    def action_themes(self) -> None:
        self.push_screen(ThemeScreen())

    def select_theme(self, name: str) -> None:
        self.theme_override = None if name == "auto" else name
        selected = active_flume_theme() if name == "auto" and active_flume_theme else name
        if selected in self.available_themes:
            self.theme = selected

    def action_refresh(self) -> None:
        self.poll()
        selected = self.selected_run()
        if selected:
            self.request_detail(selected, force=True)

    # -- theming -------------------------------------------------------------

    def color(self, role: str, fallback: str) -> str:
        return self._roles.get(role, fallback)

    def apply_theme_roles(self, theme) -> None:
        self._roles = (theme_roles(theme.name) or {}) if theme_roles else {}
        if not self.is_mounted or not self.is_running:
            return
        self.refresh_rows()
        self.query_one(JobPane).retheme()
        for screen in self.screen_stack:
            if isinstance(screen, LogScreen):
                screen.apply_log(screen._raw_lines)
                screen.refresh_log()
            elif isinstance(screen, JobDetailScreen):
                screen.refresh_detail()

    @work(exclusive=True, group="flume-theme")
    async def watch_flume_theme(self) -> None:
        if not watch_flume_theme_changes:
            return
        async for name in watch_flume_theme_changes():
            if not self.theme_override and name in self.available_themes and name != self.theme:
                self.theme = name

    def on_mount(self) -> None:
        if load_flume_themes:
            for theme in load_flume_themes("actions"):
                self.register_theme(theme)
        self.theme_changed_signal.subscribe(self, self.apply_theme_roles)
        chosen = self.theme_override or (active_flume_theme() if active_flume_theme else None)
        if self.theme_override and chosen not in self.available_themes:
            self.notify(
                f"Unknown theme: {chosen}. Press t to choose an available theme.",
                severity="warning",
            )
            self.theme_override = None
        if chosen not in self.available_themes:
            flume = [name for name in self.available_themes if name.startswith("flume-")]
            chosen = flume[0] if flume else self.theme

        self.theme = chosen
        self._roles = theme_roles(self.theme) or {} if theme_roles else {}
        self.restore_snapshot()
        self.watch_flume_theme()
        self.query_one("#run-search", Input).display = False

        self.set_interval(TICK_SECONDS, self.refresh_rows)
        self.set_interval(ACTIVE_POLL_SECONDS, self.poll_if_due)
        self.resize_layout()
        self.poll()
        self._table().focus()

    # -- data ----------------------------------------------------------------

    def poll_if_due(self) -> None:
        interval = ACTIVE_POLL_SECONDS if self.active_runs() else POLL_SECONDS
        if time.monotonic() - self._last_poll >= interval:
            self.poll()

    def poll(self) -> None:
        if not self._polling:
            self._polling = True
            self._last_poll = time.monotonic()
            self.update_chrome()
            self.fetch_runs()

    @work(thread=True, exclusive=True, group="poll")
    def fetch_runs(self) -> None:
        try:
            runs = self.provider.list_runs(ROW_LIMIT)
            error = None
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            runs = []
            error = str(exc).strip().splitlines()[-1] if str(exc) else "Provider request failed"
        if not get_current_worker().is_cancelled:
            self.app.call_from_thread(self.apply_runs, runs, error)

    def apply_runs(self, runs: list[dict], error: str | None) -> None:
        self._polling = False
        self._loaded = True
        self.error = error
        if error:
            self.refresh_rows()  # Keep usable data during transient failures.
            return
        self._last_success = datetime.now(timezone.utc)
        old = {r["databaseId"]: r for r in self.runs}
        self.runs = runs
        for run in runs:
            previous = old.get(run["databaseId"])
            if previous and run_revision(previous) == run_revision(run):
                # Details may supply metadata absent from a provider's list API.
                for key in ("displayTitle", "startedAt", "completedAt"):
                    if previous.get(key) is not None:
                        run[key] = previous[key]
            if previous and run_revision(previous) != run_revision(run):
                self.invalidate_detail(run["databaseId"])
                self.request_detail(run, force=True)
        self.save_snapshot()
        self.refresh_rows()
        selected = self.selected_run()
        if selected:
            self.request_detail(selected)
        for run in self.active_runs()[:ACTIVE_PREFETCH]:
            self.request_detail(run, interval=POLL_SECONDS)
        unseen = [
            r
            for r in self.runs
            if r.get("status") == "completed"
            and self._detail_revisions.get(r["databaseId"]) != run_revision(r)
        ]
        for run in unseen[:COMPLETED_PREFETCH]:
            self.request_detail(run, interval=POLL_SECONDS)

    def active_runs(self) -> list[dict]:
        return [r for r in self.runs if r.get("status") != "completed"]

    def ordered_runs(self) -> list[dict]:
        """Active runs first (running before queued), then newest completed."""

        def stamp(run: dict) -> float:
            started_at = parse_time(run.get("startedAt") or run.get("createdAt"))
            return started_at.timestamp() if started_at else 0

        matches = [r for r in self.runs if run_matches(r, self.run_filter)]
        active = sorted(
            (r for r in matches if r.get("status") != "completed"), key=stamp, reverse=True
        )
        completed = sorted(
            (r for r in matches if r.get("status") == "completed"),
            key=stamp,
            reverse=True,
        )
        return active + completed[:MAX_COMPLETED]

    def refresh_rows(self) -> None:
        if not self.is_running:
            return
        table = self.query_one("#runs", DataTable)
        previous_run, previous_job = self.selected_run(), self.selected_job()
        now = datetime.now(timezone.utc)
        self.row_map = []
        self.job_rows = {}
        rows = []
        runs = self.ordered_runs()
        if self.expanded_run not in {r["databaseId"] for r in runs}:
            self.expanded_run = None
        sections = [
            ("in progress", [r for r in runs if r.get("status") != "completed"]),
            ("completed", [r for r in runs if r.get("status") == "completed"]),
        ]
        for label, section in sections:
            if not section:
                continue
            self.row_map.append(None)
            heading = Text(
                f"{label.title()} · {len(section)}", style=self.color("text_muted", "dim")
            )
            rows.append((label, [Text(""), heading]))
            for run in section:
                self.row_map.append(run)
                rows.append((str(run["databaseId"]), self.run_cells(run, now)))
                if self.wide and run["databaseId"] == self.expanded_run:
                    data = self.details.get(run["databaseId"])
                    jobs = (data or {}).get("jobs") or []
                    for job in jobs:
                        self.job_rows[len(self.row_map)] = job
                        self.row_map.append(run)
                        width = next(c.width for k, c in table.columns.items() if k.value == "name")
                        text = self.inline_job_text(job, now, width)
                        rows.append(
                            (f"job:{run['databaseId']}:{job['databaseId']}", [Text(""), text])
                        )
                    if not jobs:
                        self.row_map.append(None)
                        if self.expanded_run in self.detail_errors:
                            message = "Couldn't fetch jobs. Retrying…"
                        elif data is None:
                            message = "Fetching jobs…"
                        else:
                            message = "No jobs yet"
                        rows.append(
                            (
                                f"jobs:{self.expanded_run}",
                                [Text(""), Text("  " + message, style="dim")],
                            )
                        )

        update_table(table, rows)
        if previous_run and previous_job:
            key = f"job:{previous_run['databaseId']}:{previous_job['databaseId']}"
            if key not in {key for key, _ in rows}:
                self.select_row_key(str(previous_run["databaseId"]))
        self._restore_cursor(table, table.cursor_row, table.cursor_column)
        if self.error:
            self.sub_title = f"{self.repo} — {self.error}"
        elif self.runs:
            self.sub_title = f"{self.repo} — {len(self.active_runs())} active · v{VERSION}"
        else:
            self.sub_title = f"{self.repo} — no workflow runs · v{VERSION}"
        self.update_chrome()

    def _restore_cursor(self, table: DataTable, row: int, column: int) -> None:
        """Keep the selection on a data row across rebuilds."""
        row = min(row, len(self.row_map) - 1)
        for _ in range(len(self.row_map)):
            if self.row_map[row] is not None or row >= len(self.row_map) - 1:
                break
            row += 1
        table.move_cursor(row=max(row, 0), column=column, animate=False, scroll=False)

    # -- cells ---------------------------------------------------------------

    def gates_cell(self, run: dict) -> Text:
        if run.get("status") == "queued":
            return Text("queued", style=self.color("text_muted", "dim"))
        gates = self.gates.get(run["databaseId"])
        if gates is None:
            return Text("…", style=self.color("text_muted", "dim"))
        passed, total, failed, pending = gates
        if total == 0:
            return Text("no jobs", style=self.color("error", "red"))
        if failed:
            return Text(f"{passed}/{total} ✗{len(failed)}", style=self.color("error", "red"))
        if pending:
            return Text(f"{passed}/{total} …", style=self.color("warning", "yellow"))
        return Text(f"{passed}/{total}", style=self.color("success", "green"))

    def run_cells(self, run: dict, now: datetime) -> list[Text]:
        status = run.get("conclusion") or run.get("status") or "unknown"
        role, fallback, glyph = status_glyph(status)
        length = run_length(run, now)
        name = Text(overflow="ellipsis", no_wrap=True)
        name.append(run.get("displayTitle") or run.get("workflowName") or "Workflow", style="bold")
        name.append(
            f"\n{run.get('workflowName') or 'Workflow'} · ",
            style="not bold " + self.color("text_subtle", "dim"),
        )
        name.append(
            run.get("headBranch") or "?",
            style="not bold " + self.color("accent_secondary", "magenta"),
        )
        name.append(" · ", style="not bold " + self.color("text_subtle", "dim"))
        name.append_text(self.gates_cell(run))
        name.append(f" · {length or '—'}", style="not bold " + self.color("text_subtle", "dim"))
        return [Text(glyph, style=self.color(role, fallback)), name]

    def inline_job_text(self, job: dict, now: datetime, width: int) -> Text:
        glyph, name, duration = job_cells(job, now, self.color)
        name = name.copy()
        name.truncate(max(1, width - 5 - duration.cell_len), overflow="ellipsis", pad=True)

        text = Text("  ")
        text.append_text(glyph)
        text.append(" ")
        text.append_text(name)
        text.append(" ")
        text.append_text(duration)
        return text

    # -- gates ---------------------------------------------------------------

    def load_job_log(self, job_id: int) -> str:
        """One download per job, shared by previews and expanded viewers."""
        key = (self.cache_key, f"job-raw-v1:{job_id}")
        with self._log_locks.setdefault(key, threading.Lock()):
            raw = self.log_cache.get(key)
            if raw is None:
                raw = self.cache.get_log(*key)
            if raw is None:
                raw = self.provider.job_log(job_id)
                if raw:
                    self.cache.put_log(*key, raw)
            if raw:
                self.log_cache[key] = raw
            return raw

    def invalidate_detail(self, run_id: int) -> None:
        self.details.pop(run_id, None)
        self.gates.pop(run_id, None)
        self._detail_revisions.pop(run_id, None)
        self._detail_fetched.pop(run_id, None)
        self.detail_errors.pop(run_id, None)

    def request_detail(
        self, run: dict, *, interval: int = ACTIVE_POLL_SECONDS, force: bool = False
    ) -> None:
        run_id = run["databaseId"]
        run = next((r for r in self.runs if r["databaseId"] == run_id), run)
        revision = run_revision(run)
        if run_id in self._detail_inflight:
            return
        if not force:
            if run.get("status") == "completed" and self._detail_revisions.get(run_id) == revision:
                return
            if time.monotonic() - self._detail_fetched.get(run_id, 0) < interval:
                return
        self._detail_inflight.add(run_id)
        self._detail_fetched[run_id] = time.monotonic()
        self.fetch_gates(run_id, revision)

    @work(thread=True, exclusive=False, group="gates")
    def fetch_gates(self, run_id: int, revision: tuple) -> None:
        metadata = None
        try:
            metadata = self.provider.details(run_id)
            jobs, error = metadata.jobs, None
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            jobs, error = [], str(exc)
        if not get_current_worker().is_cancelled:
            self.app.call_from_thread(self.apply_gates, run_id, revision, jobs, error, metadata)

    def apply_gates(
        self,
        run_id: int,
        revision: tuple,
        jobs: list[dict],
        error: str | None,
        metadata: RunDetails | None = None,
        *,
        persist: bool = True,
    ) -> None:
        if not self.is_running:
            return
        self._detail_inflight.discard(run_id)
        run = next((r for r in self.runs if r["databaseId"] == run_id), None)
        if run and run_revision(run) != revision:
            self.request_detail(run, force=True)
            return
        if error:
            self.detail_errors[run_id] = error
            return
        self.detail_errors.pop(run_id, None)
        if run and metadata:
            if metadata.title:
                run["displayTitle"] = metadata.title
            if metadata.started_at:
                run["startedAt"] = metadata.started_at
            if metadata.completed_at:
                run["completedAt"] = metadata.completed_at
        self.details[run_id] = {"jobs": jobs}
        # A completed run may briefly precede the final jobs response.
        pending = any(j.get("status") != "completed" for j in jobs)
        if jobs and not pending:
            self._detail_revisions[run_id] = revision
        else:
            self._detail_revisions.pop(run_id, None)
        failed = [j.get("name") or "?" for j in jobs if j.get("conclusion") == "failure"]
        passed = sum(1 for j in jobs if j.get("conclusion") == "success")
        self.gates[run_id] = (passed, len(jobs), failed, pending)
        if persist:
            self.save_snapshot()
        self.refresh_rows()
        for screen in self.screen_stack:
            if (
                isinstance(screen, (JobDetailScreen, LogScreen))
                and screen.run["databaseId"] == run_id
            ):
                if run:
                    screen.run = run
                if isinstance(screen, JobDetailScreen):
                    screen.apply_detail(self.details[run_id], None)
                else:
                    screen.refresh_log()

    def run_has_failures(self, run: dict) -> bool:
        gates = self.gates.get(run["databaseId"])
        return bool(gates and gates[2])

    # -- navigation ----------------------------------------------------------

    def _table(self) -> DataTable:
        return self.query_one("#runs", DataTable)

    def _move(self, step: int) -> None:
        table = self._table()
        row = table.cursor_row + step
        for _ in range(len(self.row_map)):
            if not (0 <= row < len(self.row_map)):
                return
            if self.row_map[row] is not None:
                table.move_cursor(row=row, animate=False)
                return
            row += step

    def action_cursor_down(self) -> None:
        focused = self.focused
        if self.detail_focused() and focused is not None:
            focused.scroll_relative(y=1, animate=False)
        else:
            self._move(1)

    def action_cursor_up(self) -> None:
        focused = self.focused
        if self.detail_focused() and focused is not None:
            focused.scroll_relative(y=-1, animate=False)
        else:
            self._move(-1)

    def action_cursor_left(self) -> None:
        focused = self.focused
        if self.detail_focused() and focused is not None and focused.id == "preview-scroll":
            focused.scroll_relative(x=-8, animate=False)
        else:
            self.action_focus_runs()

    def action_cursor_right(self) -> None:
        focused = self.focused
        if self.detail_focused() and focused is not None and focused.id == "preview-scroll":
            focused.scroll_relative(x=8, animate=False)
        else:
            self.action_detail()

    def selected_run(self) -> dict | None:
        row = self._table().cursor_row
        return self.row_map[row] if 0 <= row < len(self.row_map) else None

    def selected_job(self) -> dict | None:
        return self.job_rows.get(self._table().cursor_row)

    # DataTable consumes enter itself; these messages are how we see it.
    def on_data_table_row_selected(self, event) -> None:
        if event.data_table.id == "runs":
            self.action_detail()

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.data_table.id != "runs":
            return
        run = self.selected_run()
        self.update_chrome()
        if run:
            if self._prefetch_timer is not None:
                self._prefetch_timer.stop()
            self._prefetch_timer = self.set_timer(0.15, lambda: self.request_detail(run))

    # -- actions ---------------------------------------------------------------

    def action_detail(self) -> None:
        run = self.selected_run()
        if not run:
            return
        if self.wide:
            if self.selected_job():
                self.query_one(JobPane).open_log()
            else:
                run_id = run["databaseId"]
                self.expanded_run = None if self.expanded_run == run_id else run_id
                self.request_detail(run)
                self.refresh_rows()
                if self.expanded_run:
                    jobs = self.details.get(run_id, {}).get("jobs") or []
                    if jobs:
                        self.select_row_key(f"job:{run_id}:{jobs[0]['databaseId']}")
        else:
            self.push_screen(JobDetailScreen(self.repo, run))

    def action_run_logs(self) -> None:
        if self.detail_focused() or self.selected_job():
            self.query_one(JobPane).open_log()
            return
        run = self.selected_run()
        if run and run.get("databaseId"):
            self.push_screen(
                LogScreen(
                    self.run_header(run),
                    run,
                )
            )

    def run_header(self, run: dict) -> str:
        status = run.get("conclusion") or run.get("status") or "?"
        role, fallback, glyph = status_glyph(status)
        color = self.color(role, fallback)
        gates = self.gates.get(run["databaseId"])
        gates_text = f"{gates[0]}/{gates[1]} jobs" if gates else "jobs …"
        return (
            f"[{color}]{glyph}[/{color}] [b]{escape_markup(run.get('workflowName', 'run'))}[/b]"
            f"  ·  {escape_markup(run.get('headBranch') or '?')}"
            f"  ·  {(run.get('headSha') or '')[:7]}"
            f"  ·  {escape_markup(status)}  ·  {gates_text}"
        )

    def action_failed_logs(self) -> None:
        run = self.selected_run()
        if not run or not run.get("databaseId"):
            return
        if not self.run_has_failures(run):
            self.notify("no failed jobs in this run")
            return
        self.push_screen(
            LogScreen(
                self.run_header(run) + "  ·  failed only",
                run,
                failed_only=True,
            )
        )

    def action_open_run(self) -> None:
        target = self.selected_job() or self.selected_run()
        if target and target.get("url"):
            open_in_browser(target["url"])

    # -- run actions ---------------------------------------------------------

    @work(thread=True, exclusive=False, group="mutate")
    def mutate_run(self, run_id: int, action: Mutation, label: str) -> None:
        try:
            self.provider.mutate(run_id, action)
            ok, detail = True, label
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            ok = False
            detail = str(exc).strip().splitlines()[-1] if str(exc) else "failed"
        if not get_current_worker().is_cancelled:
            self.app.call_from_thread(self.after_mutation, run_id, ok, detail)

    def after_mutation(self, run_id: int, ok: bool, detail: str) -> None:
        if ok:
            self.invalidate_detail(run_id)
        self.notify(detail)
        self.poll()

    def _selected_completed(self) -> dict | None:
        run = self.selected_run()
        if run and run.get("status") == "completed":
            return run
        if run:
            self.notify("only completed runs can be rerun")
        return None

    def action_rerun_failed(self) -> None:
        if not self.provider.can_mutate:
            self.notify("GitLab is read-only. Pipeline actions are disabled.")
            return
        run = self._selected_completed()
        if not run:
            return
        if run.get("conclusion") not in ("failure", "cancelled", "startup_failure"):
            self.notify("nothing failed in this run")
            return
        self.mutate_run(
            run["databaseId"],
            "rerun_failed",
            "rerunning failed jobs…",
        )

    def action_rerun_all(self) -> None:
        if not self.provider.can_mutate:
            self.notify("GitLab is read-only. Pipeline actions are disabled.")
            return
        run = self._selected_completed()
        if run:
            self.mutate_run(
                run["databaseId"],
                "rerun_all",
                "rerunning all jobs…",
            )

    def action_cancel_run(self) -> None:
        if not self.provider.can_mutate:
            self.notify("GitLab is read-only. Pipeline actions are disabled.")
            return
        run = self.selected_run()
        if not run:
            return
        if run.get("status") not in ("in_progress", "queued", "waiting", "pending"):
            self.notify("only queued or running runs can be cancelled")
            return
        self.mutate_run(
            run["databaseId"],
            "cancel",
            "cancelling…",
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="GitHub Actions and read-only GitLab CI dashboard")
    parser.add_argument(
        "--repo", help="owner/repository or group/subgroup/project (defaults to origin)"
    )
    parser.add_argument(
        "--provider", choices=("github", "gitlab"), help="Required for self-hosted GitLab"
    )
    parser.add_argument("--host", help="API hostname, optionally with an HTTPS port")
    parser.add_argument("--theme", help="Theme name, e.g. flume-dusk, or auto to follow Flume sync")
    args = parser.parse_args()
    try:
        repository = resolve_repository(
            args.repo,
            args.provider,
            args.host,
            origin_remote(str(Path.cwd())) if args.repo is None else None,
        )
    except ValueError as exc:
        print(f"bronson: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    cache_root = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "bronson" / "logs"
    provider_type = GitLabProvider if repository.provider == "gitlab" else GitHubProvider
    provider = provider_type(repository, request_cancelled)
    ActionsDashboard(
        repository.label, Cache(cache_root), theme_name=args.theme, provider=provider
    ).run()


if __name__ == "__main__":
    main()
