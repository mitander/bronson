"""Tests for pure log presentation, without importing the application."""

import unittest

from rich.text import Text

from bronson import logs


class LogsTest(unittest.TestCase):
    def test_prefixed_sections_and_unknown_labels(self):
        raw = "build\tsetup\tfirst\nbuild\tUNKNOWN STEP\tsecond\nbuild\ttest\tthird\nfourth"
        lines = logs.render_log(raw, "fallback", 2, 1)
        self.assertEqual(
            [line.plain for line in lines],
            ["── setup ", "first", "── fallback ", "second", "── test ", "third", "fourth"],
        )

    def test_rendering_truncates_at_the_existing_limit(self):
        raw = "line\n" * logs.LOG_MAX_LINES
        lines = logs.render_log(raw, "build", 0, 0)
        self.assertEqual(len(lines), logs.LOG_MAX_LINES + 1)
        lines = logs.render_log(raw + "overflow", "build", 0, 0)
        self.assertEqual(len(lines), logs.LOG_MAX_LINES + 2)
        self.assertEqual(lines[-1].plain, f"… truncated after {logs.LOG_MAX_LINES} lines")

    def test_theme_painting_leaves_source_unchanged(self):
        roles = {"error": "magenta", "warning": "cyan", "text_muted": "blue"}
        source = Text("section", style="bold dim")
        source.append("error", style="red")
        source.append("warning", style="yellow")
        source.append("plain", style="italic")
        painted = logs.paint_log(source, lambda role, fallback: roles.get(role, fallback))
        self.assertEqual(painted.plain, source.plain)
        self.assertEqual(painted.style, "bold blue")
        self.assertEqual([str(span.style) for span in painted.spans], ["magenta", "cyan", "italic"])
        self.assertEqual(source.style, "bold dim")
        self.assertEqual([str(span.style) for span in source.spans], ["red", "yellow", "italic"])

    def test_timestamp_warning_and_group_styles(self):
        line = logs.style_log_line(Text(), "2026-01-01T00:00:00Z warning: unused")
        self.assertEqual([str(span.style) for span in line.spans], ["dim", "yellow"])
        group = logs.style_log_line(Text(), "##[group]build")
        self.assertEqual(str(group.spans[0].style), "dim")

    def test_raw_log_lines_stay_in_one_section_and_empty_is_a_list(self):
        lines = logs.render_log("first\n##[error]second", "build", 0, 0)
        self.assertEqual([line.plain for line in lines], ["── build ", "first", "##[error]second"])
        self.assertTrue(any("red" in str(span.style) for span in lines[-1].spans))
        self.assertIsInstance(logs.render_log("", "build", 0, 0), list)

    def test_failure_patterns_and_ansi_normalization(self):
        failures = [
            "2026-01-01T00:00:00Z \x1b[31merror[E0308]: mismatched types\x1b[0m",
            "\ufeff##[error]build failed",
            "::error file=src/main.rs,line=4::bad type",
            "src/main.c:12:4: error: unknown type",
            "main.ts(4,2): error TS2322: wrong type",
            "[ERROR] Failed to execute goal",
            "npm ERR! code ELIFECYCLE",
            "npm error code 1",
            "FAILED tests/test_app.py::test_save",
            " FAIL  src/app.test.ts",
            "--- FAIL: TestSave (0.00s)",
            "FAIL\texample.org/app\t0.2s",
            "not ok 3 - saves correctly",
            "  × should save",
            "test tests::save ... FAILED",
            "test result: FAILED. 4 passed; 1 failed",
            "=== 2 failed, 5 passed in 0.2s ===",
            "thread 'main' panicked at src/main.rs:10:2:",
            "Traceback (most recent call last):",
            'Exception in thread "main" java.lang.IllegalStateException',
            "Process completed with exit code 1.",
            "Process completed with exit code -1.",
        ]
        for raw in failures:
            with self.subTest(raw=raw):
                self.assertEqual(logs.log_severity(raw), "error")
                rendered = logs.style_log_line(Text(), raw)
                self.assertNotIn("\x1b", rendered.plain)
                self.assertTrue(any(str(span.style) == "red" for span in rendered.spans))
        for raw in [
            "Process completed with exit code 0.",
            "0 failed, 12 passed",
            "test handles errors ... ok",
            "error handling tests passed",
            "Run echo 'FAILED'",
            "\x1b[31mordinary red output\x1b[0m",
            "Downloading error-utils",
            "warning: unused variable",
        ]:
            with self.subTest(raw=raw):
                self.assertNotEqual(logs.log_severity(raw), "error")
