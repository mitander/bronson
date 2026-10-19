"""CLI startup resolves explicit provider and host identity before opening UI."""

import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from bronson.cli import main
from bronson.models import Repository


class CliTests(unittest.TestCase):
    def test_help_exits_before_repository_detection(self):
        with (
            patch("sys.argv", ["bronson", "--help"]),
            patch("bronson.cli.origin_remote") as detect,
            redirect_stdout(io.StringIO()) as output,
            self.assertRaises(SystemExit) as result,
        ):
            main()
        self.assertEqual(result.exception.code, 0)
        for option in ("--repo", "--theme", "--provider", "--host"):
            self.assertIn(option, output.getvalue())
        detect.assert_not_called()

    def test_origin_detection_uses_callers_directory(self):
        with (
            patch("sys.argv", ["bronson"]),
            patch(
                "bronson.cli.origin_remote", return_value="git@github.com:owner/repo.git"
            ) as detect,
            patch("bronson.cli.ActionsDashboard") as dashboard,
        ):
            main()
        detect.assert_called_once_with(str(Path.cwd()))
        self.assertEqual(dashboard.call_args.args[0], "owner/repo")
        self.assertEqual(
            dashboard.call_args.kwargs["provider"].repository,
            Repository("github", "github.com", "owner/repo"),
        )
        dashboard.return_value.run.assert_called_once_with()

    def test_explicit_repository_and_theme_are_passed_literally(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict("os.environ", {"XDG_CACHE_HOME": directory}),
            patch("sys.argv", ["bronson", "--repo", "owner/repo", "--theme", "textual-dark"]),
            patch("bronson.cli.origin_remote") as detect,
            patch("bronson.cli.ActionsDashboard") as dashboard,
        ):
            main()
        detect.assert_not_called()
        self.assertEqual(dashboard.call_args.args[0], "owner/repo")
        self.assertEqual(dashboard.call_args.kwargs["theme_name"], "textual-dark")
        self.assertEqual(dashboard.call_args.args[1].root, Path(directory) / "bronson" / "logs")
        dashboard.return_value.run.assert_called_once_with()

    def test_self_hosted_origin_selects_glab_only_when_explicit(self):
        with (
            patch("sys.argv", ["bronson", "--provider", "gitlab"]),
            patch(
                "bronson.cli.origin_remote",
                return_value="git@gitlab.example.test:team/sub/repo.git",
            ),
            patch("bronson.cli.ActionsDashboard") as dashboard,
        ):
            main()
        provider = dashboard.call_args.kwargs["provider"]
        self.assertEqual(
            provider.repository, Repository("gitlab", "gitlab.example.test", "team/sub/repo")
        )
        self.assertFalse(provider.can_mutate)

    def test_unrecognized_host_fails_before_opening_dashboard(self):
        with (
            patch("sys.argv", ["bronson"]),
            patch("bronson.cli.origin_remote", return_value="git@unknown.test:team/repo.git"),
            patch("bronson.cli.ActionsDashboard") as dashboard,
            redirect_stderr(io.StringIO()) as error,
            self.assertRaises(SystemExit) as result,
        ):
            main()
        self.assertEqual(result.exception.code, 1)
        self.assertIn("--provider gitlab", error.getvalue())
        dashboard.assert_not_called()
