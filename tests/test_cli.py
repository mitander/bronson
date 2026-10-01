"""CLI startup stays identical for console and module entry points."""

import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from bronson.cli import main


class CliTests(unittest.TestCase):
    def test_help_exits_before_repository_detection(self):
        with (
            patch("sys.argv", ["bronson", "--help"]),
            patch("bronson.cli.repo_from_git") as detect,
            redirect_stdout(io.StringIO()) as output,
            self.assertRaises(SystemExit) as result,
        ):
            main()
        self.assertEqual(result.exception.code, 0)
        self.assertIn("--repo", output.getvalue())
        self.assertIn("--theme", output.getvalue())
        detect.assert_not_called()

    def test_origin_detection_uses_callers_directory(self):
        with (
            patch("sys.argv", ["bronson"]),
            patch("bronson.cli.repo_from_git", return_value="owner/repo") as detect,
            patch("bronson.cli.ActionsDashboard") as dashboard,
        ):
            main()
        detect.assert_called_once_with(str(Path.cwd()))
        self.assertEqual(dashboard.call_args.args[0], "owner/repo")
        dashboard.return_value.run.assert_called_once_with()

    def test_explicit_repository_and_theme_are_passed_literally(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict("os.environ", {"XDG_CACHE_HOME": directory}),
            patch("sys.argv", ["bronson", "--repo", "owner/repo", "--theme", "textual-dark"]),
            patch("bronson.cli.repo_from_git") as detect,
            patch("bronson.cli.ActionsDashboard") as dashboard,
        ):
            main()
        detect.assert_not_called()
        self.assertEqual(dashboard.call_args.args[0], "owner/repo")
        self.assertEqual(dashboard.call_args.kwargs["theme_name"], "textual-dark")
        dashboard.return_value.run.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
