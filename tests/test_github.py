"""GitHub mapping and exact CLI operations remain owned by its adapter."""

import subprocess
import unittest
from unittest.mock import patch

from bronson.models import Repository, run_revision
from bronson.providers import Mutation
from bronson.providers.github import GitHubProvider


class GitHubTests(unittest.TestCase):
    def setUp(self) -> None:
        self.provider = GitHubProvider(Repository("github", "github.com", "owner/repo"))

    def test_jobs_paginate_latest_attempts_and_normalize(self) -> None:
        pages = [
            {
                "jobs": [
                    {
                        "id": 10,
                        "name": "build",
                        "status": "completed",
                        "conclusion": "success",
                        "started_at": "2026-01-01T00:00:00Z",
                        "completed_at": "2026-01-01T00:01:00Z",
                        "html_url": "https://github.com/owner/repo/actions/runs/1/job/10",
                    }
                ]
            },
            {"jobs": [{"id": 11, "status": "new_state"}]},
        ]
        with patch.object(self.provider, "_json", return_value=pages) as fetch:
            jobs = self.provider.jobs(1)
        self.assertEqual([job["databaseId"] for job in jobs], [10, 11])
        self.assertEqual(jobs[0]["startedAt"], "2026-01-01T00:00:00Z")
        self.assertEqual(jobs[0]["completedAt"], "2026-01-01T00:01:00Z")
        self.assertEqual(jobs[0]["url"], "https://github.com/owner/repo/actions/runs/1/job/10")
        self.assertEqual(jobs[1]["status"], "new_state")
        self.assertIsNone(jobs[1]["conclusion"])
        self.assertNotIn("html_url", jobs[0])
        self.assertEqual(
            fetch.call_args.args[0],
            [
                "api",
                "repos/owner/repo/actions/runs/1/jobs?filter=latest&per_page=100",
                "--paginate",
                "--slurp",
            ],
        )

    def test_list_uses_existing_fields_and_bound(self) -> None:
        with patch.object(self.provider, "_json", return_value=[]) as fetch:
            self.assertEqual(self.provider.list_runs(100), [])
        self.assertEqual(
            fetch.call_args.args[0],
            [
                "run",
                "list",
                "--repo",
                "owner/repo",
                "--json",
                "databaseId,workflowName,headBranch,headSha,status,conclusion,startedAt,updatedAt,createdAt,displayTitle,url,attempt",
                "--limit",
                "100",
            ],
        )

    def test_runs_validate_display_fields_and_preserve_unknown_states(self) -> None:
        record = {
            "databaseId": 42,
            "attempt": 2,
            "status": "future_state",
            "conclusion": "future_result",
            "updatedAt": "2026-01-01T00:00:00Z",
            "displayTitle": "Fix pipeline",
            "unused": {"native": "metadata"},
        }
        with patch.object(self.provider, "_json", return_value=[record]):
            runs = self.provider.list_runs(100)

        self.assertEqual(runs[0]["status"], "future_state")
        self.assertEqual(runs[0]["conclusion"], "future_result")
        self.assertEqual(runs[0]["displayTitle"], "Fix pipeline")
        self.assertIsNone(runs[0]["headSha"])
        self.assertNotIn("unused", runs[0])
        self.assertEqual(run_revision(runs[0]), (2, "future_state", "2026-01-01T00:00:00Z"))

    def test_revision_preserves_partial_legacy_values(self) -> None:
        self.assertEqual(run_revision({}), (1, None, None))
        self.assertEqual(run_revision({"attempt": None, "status": "new"}), (None, "new", None))

    def test_malformed_runs_are_not_display_records(self) -> None:
        malformed: list[object] = [
            {},
            [None],
            [{"databaseId": True, "attempt": 1}],
            [{"databaseId": 0, "attempt": 1}],
            [{"databaseId": 42, "attempt": False}],
            [{"databaseId": 42, "attempt": 0}],
            [{"databaseId": 42, "attempt": 1, "status": []}],
            [{"databaseId": 42, "attempt": 1, "url": {"secret": "do-not-print"}}],
        ]
        for response in malformed:
            with (
                self.subTest(response=response),
                patch.object(self.provider, "_json", return_value=response),
                self.assertRaises(ValueError) as raised,
            ):
                self.provider.list_runs(100)
            self.assertNotIn("do-not-print", str(raised.exception))

    def test_malformed_pages_and_jobs_fail_instead_of_returning_partial_results(self) -> None:
        malformed: list[object] = [
            {},
            [None],
            [{}],
            [{"jobs": {}}],
            [{"jobs": [{"id": 10}]}, {"jobs": [None]}],
            [{"jobs": [{"id": True}]}],
            [{"jobs": [{"id": -1}]}],
            [{"jobs": [{"id": "10"}]}],
            [{"jobs": [{"id": 10, "name": []}]}],
            [{"jobs": [{"id": 10, "conclusion": False}]}],
        ]
        for response in malformed:
            with (
                self.subTest(response=response),
                patch.object(self.provider, "_json", return_value=response),
                self.assertRaises(ValueError),
            ):
                self.provider.jobs(1)

    def test_invalid_json_has_safe_context_and_preserves_cause(self) -> None:
        response = subprocess.CompletedProcess([], 0, "do-not-print", "")
        with patch("bronson.providers.github.run_process", return_value=response):
            with self.assertRaisesRegex(ValueError, "Invalid GitHub JSON response") as raised:
                self.provider.list_runs(100)
        self.assertIsNotNone(raised.exception.__cause__)
        self.assertNotIn("do-not-print", str(raised.exception))

    def test_exact_mutation_commands_receive_cancellation(self) -> None:
        def cancelled() -> bool:
            return False

        provider = GitHubProvider(self.provider.repository, cancelled)
        commands: dict[Mutation, list[str]] = {
            "rerun_failed": ["gh", "run", "rerun", "42", "--failed", "--repo", "owner/repo"],
            "rerun_all": ["gh", "run", "rerun", "42", "--repo", "owner/repo"],
            "cancel": ["gh", "run", "cancel", "42", "--repo", "owner/repo"],
        }
        for action, command in commands.items():
            with (
                self.subTest(action=action),
                patch("bronson.providers.github.run_process") as execute,
            ):
                provider.mutate(42, action)
                execute.assert_called_once_with(command, check=True, cancelled=cancelled)

    def test_uncertain_mutations_are_not_replayed(self) -> None:
        for error in (
            subprocess.TimeoutExpired(["gh"], 30),
            subprocess.SubprocessError("Request cancelled"),
            subprocess.CalledProcessError(1, ["gh"]),
        ):
            with (
                self.subTest(error=type(error).__name__),
                patch("bronson.providers.github.run_process", side_effect=error) as execute,
            ):
                with self.assertRaises(type(error)):
                    self.provider.mutate(42, "rerun_failed")
                execute.assert_called_once()

    def test_log_download_and_errors(self) -> None:
        success = subprocess.CompletedProcess([], 0, "raw trace\n", "")
        with patch("bronson.providers.github.run_process", return_value=success) as execute:
            self.assertEqual(self.provider.job_log(10), "raw trace\n")
        self.assertEqual(
            execute.call_args.args[0],
            ["gh", "api", "--allow-escape-sequences", "repos/owner/repo/actions/jobs/10/logs"],
        )
        self.assertEqual(execute.call_args.kwargs["timeout"], 60)
        failure = subprocess.CompletedProcess([], 1, "", "untrusted diagnostics")
        with patch("bronson.providers.github.run_process", return_value=failure):
            with self.assertRaisesRegex(ValueError, "GitHub log download failed"):
                self.provider.job_log(10)
