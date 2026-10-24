"""GitLab contract tests use API fixtures, never a live host or credentials."""

import json
import subprocess
import unittest
from unittest.mock import patch

from bronson.models import Repository
from bronson.providers.gitlab import GitLabProvider


def pipeline(identifier=42, status="running", **extra):
    return {
        "id": identifier,
        "iid": 7,
        "status": status,
        "ref": "main",
        "sha": "abc1234",
        "source": "push",
        "web_url": f"https://gl.example.test/team/sub/repo/-/pipelines/{identifier}",
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:05:00Z",
        **extra,
    }


def job(identifier=10, status="success", **extra):
    return {
        "id": identifier,
        "name": "unit tests",
        "status": status,
        "allow_failure": False,
        "started_at": "2026-01-01T00:01:00Z",
        "finished_at": "2026-01-01T00:04:00Z",
        "web_url": f"https://gl.example.test/team/sub/repo/-/jobs/{identifier}",
        **extra,
    }


class GitLabTests(unittest.TestCase):
    def setUp(self):
        self.provider = GitLabProvider(Repository("gitlab", "gl.example.test", "team/sub/repo"))

    def test_pipeline_list_is_bounded_get_with_explicit_host_and_encoded_project(self):
        process = subprocess.CompletedProcess([], 0, json.dumps([pipeline()]), "")
        with patch("bronson.providers.gitlab.run_process", return_value=process) as execute:
            runs = self.provider.list_runs(100)
        self.assertEqual(
            execute.call_args.args[0],
            [
                "glab",
                "api",
                "--hostname",
                "gl.example.test",
                "--method",
                "GET",
                "projects/team%2Fsub%2Frepo/pipelines?per_page=100&order_by=id&sort=desc",
            ],
        )
        self.assertEqual(runs[0]["databaseId"], 42)
        self.assertEqual(runs[0]["headBranch"], "main")
        self.assertEqual(runs[0]["status"], "in_progress")
        self.assertIsNone(runs[0]["conclusion"])
        self.assertEqual(runs[0]["displayTitle"], "Pipeline #42")

    def test_status_mapping_preserves_manual_scheduled_unknown_and_failures(self):
        cases = {
            "success": ("completed", "success"),
            "failed": ("completed", "failure"),
            "canceled": ("completed", "cancelled"),
            "skipped": ("completed", "skipped"),
            "pending": ("queued", None),
            "running": ("in_progress", None),
            "waiting_for_resource": ("waiting", None),
            "manual": ("manual", None),
            "scheduled": ("scheduled", None),
            "new_status": ("new_status", None),
        }
        for native, expected in cases.items():
            with (
                self.subTest(native=native),
                patch.object(self.provider, "_json", return_value=[pipeline(status=native)]),
            ):
                run = self.provider.list_runs(1)[0]
                self.assertEqual((run["status"], run["conclusion"]), expected)

    def test_jobs_paginate_without_retried_attempts(self):
        pages = [
            [job(identifier) for identifier in range(1, 101)],
            [job(101, "failed", allow_failure=True)],
        ]
        with patch.object(self.provider, "_json", side_effect=pages) as fetch:
            jobs = self.provider.jobs(42)
        self.assertEqual(len(jobs), 101)
        self.assertEqual(jobs[-1]["name"], "unit tests (allowed failure)")
        self.assertEqual(jobs[-1]["conclusion"], "failure")
        self.assertEqual(
            fetch.call_args_list[0].args[0],
            "projects/team%2Fsub%2Frepo/pipelines/42/jobs?include_retried=false&per_page=100&page=1",
        )
        self.assertTrue(fetch.call_args_list[1].args[0].endswith("&page=2"))

    def test_exact_full_page_fetches_empty_final_page_and_page_errors_fail(self):
        full = [job(identifier) for identifier in range(1, 101)]
        with patch.object(self.provider, "_json", side_effect=[full, []]) as fetch:
            self.assertEqual(len(self.provider.jobs(42)), 100)
        self.assertEqual(fetch.call_count, 2)
        with patch.object(self.provider, "_json", side_effect=[full, ValueError("offline")]):
            with self.assertRaisesRegex(ValueError, "offline"):
                self.provider.jobs(42)
        with (
            patch("bronson.providers.gitlab.MAX_JOB_PAGES", 1),
            patch.object(self.provider, "_json", return_value=full),
        ):
            with self.assertRaisesRegex(ValueError, "safety limit"):
                self.provider.jobs(42)

    def test_detail_supplies_commit_title_and_actual_times(self):
        detail = pipeline(started_at="2026-01-01T00:01:00Z", finished_at="2026-01-01T00:04:00Z")
        with patch.object(
            self.provider, "_json", side_effect=[detail, [job(commit={"title": "Fix pipeline"})]]
        ) as fetch:
            result = self.provider.details(42)
        self.assertEqual(fetch.call_args_list[0].args[0], "projects/team%2Fsub%2Frepo/pipelines/42")
        self.assertEqual(result.title, "Fix pipeline")
        self.assertEqual(result.started_at, "2026-01-01T00:01:00Z")
        self.assertEqual(result.completed_at, "2026-01-01T00:04:00Z")
        self.assertEqual(result.jobs[0]["databaseId"], 10)

    def test_manual_and_scheduled_jobs_remain_unfinished(self):
        with patch.object(
            self.provider,
            "_json",
            return_value=[job(status="manual"), job(11, "scheduled"), job(12, "unknown")],
        ):
            jobs = self.provider.jobs(42)
        self.assertEqual([item["status"] for item in jobs], ["manual", "scheduled", "unknown"])
        self.assertEqual([item["conclusion"] for item in jobs], [None, None, None])

    def test_trace_is_raw_get_and_all_mutations_are_rejected_at_boundary(self):
        process = subprocess.CompletedProcess([], 0, "\x1b[31merror: bad\x1b[0m\n", "")
        with patch("bronson.providers.gitlab.run_process", return_value=process) as execute:
            self.assertEqual(self.provider.job_log(10), process.stdout)
            self.assertEqual(
                execute.call_args.args[0],
                [
                    "glab",
                    "api",
                    "--hostname",
                    "gl.example.test",
                    "--method",
                    "GET",
                    "projects/team%2Fsub%2Frepo/jobs/10/trace",
                ],
            )
            self.assertEqual(execute.call_args.kwargs["timeout"], 60)
            execute.reset_mock()
            for action in ("rerun_failed", "rerun_all", "cancel"):
                with self.assertRaisesRegex(ValueError, "read-only"):
                    self.provider.mutate(42, action)
            execute.assert_not_called()

    def test_host_auth_ignores_ambient_tokens_and_passes_cancellation(self):
        def cancelled():
            return True

        provider = GitLabProvider(self.provider.repository, cancelled)
        environment: dict[str, str] = dict.fromkeys(
            (
                "GITLAB_TOKEN",
                "GITLAB_ACCESS_TOKEN",
                "OAUTH_TOKEN",
                "CI_JOB_TOKEN",
                "JOB_TOKEN",
                "GLAB_ENABLE_CI_AUTOLOGIN",
                "GITLAB_API_HOST",
                "GLAB_API_HOST",
                "API_HOST",
                "GITLAB_API_PROTOCOL",
                "GLAB_API_PROTOCOL",
                "API_PROTOCOL",
                "GITLAB_HOST",
                "GITLAB_URI",
                "GL_HOST",
                "GITLAB_SUBFOLDER",
                "GLAB_CLIENT_CERT",
                "CLIENT_CERT",
                "GLAB_CLIENT_KEY",
                "CLIENT_KEY",
                "GITLAB_CI",
                "CI",
                "GLAB_DEBUG_HTTP",
            ),
            "do-not-forward",
        )
        environment.update({"PATH": "/bin", "GLAB_CONFIG_DIR": "/tmp/fake-glab-config"})
        with (
            patch.dict("os.environ", environment, clear=True),
            patch(
                "bronson.providers.gitlab.run_process",
                return_value=subprocess.CompletedProcess([], 0, "trace", ""),
            ) as execute,
        ):
            provider.job_log(10)
        forwarded = execute.call_args.kwargs["env"]
        self.assertEqual(
            forwarded,
            {
                "PATH": "/bin",
                "GLAB_CONFIG_DIR": "/tmp/fake-glab-config",
                "GIT_TERMINAL_PROMPT": "0",
            },
        )
        self.assertIs(execute.call_args.kwargs["cancelled"], cancelled)

    def test_errors_do_not_echo_cli_diagnostics_or_invalid_json(self):
        secret = "private-token-do-not-print"
        for error in (
            subprocess.CalledProcessError(1, ["glab"], stderr=secret),
            FileNotFoundError(secret),
        ):
            with (
                self.subTest(error=type(error).__name__),
                patch("bronson.providers.gitlab.run_process", side_effect=error),
            ):
                with self.assertRaises(ValueError) as result:
                    self.provider.job_log(10)
                self.assertNotIn(secret, str(result.exception))
                self.assertIs(result.exception.__cause__, error)
        with patch.object(self.provider, "_get", return_value=secret):
            with self.assertRaisesRegex(ValueError, "Invalid GitLab JSON"):
                self.provider.list_runs(1)

    def test_malformed_provider_data_and_ids_are_rejected(self):
        malformed = [
            None,
            {},
            [None],
            [{"id": True}],
            [pipeline(id=-1)],
            [pipeline(ref=1)],
            [pipeline(created_at="not-a-time")],
            [pipeline(updated_at="2026-01-01")],
        ]
        for response in malformed:
            with (
                self.subTest(response=response),
                patch.object(self.provider, "_json", return_value=response),
            ):
                with self.assertRaises(ValueError):
                    self.provider.list_runs(1)
        with patch.object(self.provider, "_json", return_value=[job(allow_failure="false")]):
            with self.assertRaises(ValueError):
                self.provider.jobs(42)
        for identifier in (True, -1, "10/erase"):
            with (
                self.subTest(identifier=identifier),
                patch("bronson.providers.gitlab.run_process") as execute,
            ):
                with self.assertRaises(ValueError):
                    # Deliberately violate the interface to exercise runtime validation.
                    self.provider.job_log(identifier)  # pyright: ignore[reportArgumentType]
                execute.assert_not_called()
