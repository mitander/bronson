"""GitHub CLI mapping and actions for github.com repositories."""

import json
from collections.abc import Callable
from typing import cast

from bronson.models import Job, Repository, Run, RunDetails
from bronson.providers import Mutation
from bronson.providers.process import run_process

RUN_FIELDS = (
    "databaseId,workflowName,headBranch,headSha,status,conclusion,"
    "startedAt,updatedAt,createdAt,displayTitle,url,attempt"
)


def _record(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("Invalid GitHub response: expected an object")
    record = cast(dict[object, object], value)
    if any(not isinstance(key, str) for key in record):
        raise ValueError("Invalid GitHub response: expected text keys")
    return cast(dict[str, object], record)


def _records(value: object) -> list[object]:
    if not isinstance(value, list):
        raise ValueError("Invalid GitHub response: expected a list")
    return cast(list[object], value)


def _integer(record: dict[str, object], key: str) -> int:
    value = record.get(key)
    if type(value) is not int or value <= 0:
        raise ValueError(f"Invalid GitHub response field: {key}")
    return value


def _text(record: dict[str, object], key: str) -> str | None:
    value = record.get(key)
    if value is not None and not isinstance(value, str):
        raise ValueError(f"Invalid GitHub response field: {key}")
    return value


def _run(value: object) -> Run:
    record = _record(value)
    return {
        "databaseId": _integer(record, "databaseId"),
        "workflowName": _text(record, "workflowName"),
        "headBranch": _text(record, "headBranch"),
        "headSha": _text(record, "headSha"),
        "status": _text(record, "status") or "unknown",
        "conclusion": _text(record, "conclusion"),
        "startedAt": _text(record, "startedAt"),
        "updatedAt": _text(record, "updatedAt"),
        "createdAt": _text(record, "createdAt"),
        "displayTitle": _text(record, "displayTitle"),
        "url": _text(record, "url"),
        "attempt": _integer(record, "attempt"),
    }


class GitHubProvider:
    name = "GitHub"
    can_mutate = True

    def __init__(
        self, repository: Repository, cancelled: Callable[[], bool] = lambda: False
    ) -> None:
        if repository.provider != "github" or repository.host != "github.com":
            raise ValueError("GitHub support requires github.com")
        self.repository = repository
        self.cancelled = cancelled

    def _json(self, args: list[str]) -> object:
        output = run_process(["gh", *args], check=True, cancelled=self.cancelled).stdout
        try:
            return json.loads(output)
        except json.JSONDecodeError as exc:
            raise ValueError("Invalid GitHub JSON response") from exc

    def list_runs(self, limit: int) -> list[Run]:
        records = self._json(
            [
                "run",
                "list",
                "--repo",
                self.repository.path,
                "--json",
                RUN_FIELDS,
                "--limit",
                str(limit),
            ]
        )

        return [_run(value) for value in _records(records)]

    def jobs(self, run_id: int) -> list[Job]:
        pages = self._json(
            [
                "api",
                f"repos/{self.repository.path}/actions/runs/{run_id}/jobs?filter=latest&per_page=100",
                "--paginate",
                "--slurp",
            ]
        )
        jobs: list[Job] = []
        for value in _records(pages):
            page = _record(value)
            for value in _records(page.get("jobs")):
                job = _record(value)
                jobs.append(
                    {
                        "databaseId": _integer(job, "id"),
                        "name": _text(job, "name"),
                        "status": _text(job, "status") or "unknown",
                        "conclusion": _text(job, "conclusion"),
                        "startedAt": _text(job, "started_at"),
                        "completedAt": _text(job, "completed_at"),
                        "url": _text(job, "html_url"),
                    }
                )
        return jobs

    def details(self, run_id: int) -> RunDetails:
        return RunDetails(self.jobs(run_id))

    def job_log(self, job_id: int) -> str:
        result = run_process(
            [
                "gh",
                "api",
                "--allow-escape-sequences",
                f"repos/{self.repository.path}/actions/jobs/{job_id}/logs",
            ],
            timeout=60,
            cancelled=self.cancelled,
        )
        if result.returncode:
            raise ValueError("GitHub log download failed")
        return result.stdout

    def mutate(self, run_id: int, action: Mutation) -> None:
        actions = {
            "rerun_failed": ["run", "rerun", str(run_id), "--failed"],
            "rerun_all": ["run", "rerun", str(run_id)],
            "cancel": ["run", "cancel", str(run_id)],
        }
        run_process(
            ["gh", *actions[action], "--repo", self.repository.path],
            check=True,
            cancelled=self.cancelled,
        )
