"""Read-only GitLab REST access through host-scoped glab authentication."""

import json
import os
import subprocess
from collections.abc import Callable
from datetime import datetime
from urllib.parse import quote

from bronson.models import Job, Repository, Run, RunDetails
from bronson.providers import Mutation
from bronson.providers.process import run_process
from bronson.providers.repository import validate_repository

PAGE_SIZE = 100
MAX_JOB_PAGES = 100
# glab resolves these before host-specific configuration. Keep account and
# routing authority in the saved host settings, not the ambient environment.
_HOST_OVERRIDES = {
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
}
_TERMINAL = {
    "success": "success",
    "failed": "failure",
    "canceled": "cancelled",
    "skipped": "skipped",
}
_ACTIVE = {
    "running": "in_progress",
    "canceling": "in_progress",
    "created": "queued",
    "pending": "queued",
    "preparing": "queued",
    "waiting_for_resource": "waiting",
    "waiting_for_callback": "waiting",
    "manual": "manual",
    "scheduled": "scheduled",
}


def _record(value: object) -> dict:
    if not isinstance(value, dict):
        raise ValueError("Invalid GitLab response: expected an object")
    identifier = value.get("id")
    if type(identifier) is not int or identifier <= 0:
        raise ValueError("Invalid GitLab response: expected a positive ID")
    return value


def _text(record: dict, key: str) -> str | None:
    value = record.get(key)
    if value is not None and not isinstance(value, str):
        raise ValueError(f"Invalid GitLab response field: {key}")
    return value


def _timestamp(record: dict, key: str) -> str | None:
    value = _text(record, key)
    if value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                raise ValueError("Missing timezone")
        except ValueError as exc:
            raise ValueError(f"Invalid GitLab timestamp: {key}") from exc
    return value


def _state(record: dict) -> tuple[str, str | None]:
    native = _text(record, "status") or "unknown"
    if native in _TERMINAL:
        return "completed", _TERMINAL[native]
    return _ACTIVE.get(native, native), None


def _identifier(identifier: int) -> int:
    if type(identifier) is not int or identifier <= 0:
        raise ValueError("Expected a positive GitLab ID")
    return identifier


class GitLabProvider:
    name = "GitLab"
    can_mutate = False

    def __init__(
        self, repository: Repository, cancelled: Callable[[], bool] = lambda: False
    ) -> None:
        if repository.provider != "gitlab":
            raise ValueError("GitLab provider requires a GitLab repository")
        self.repository = validate_repository("gitlab", repository.host, repository.path)
        self.cancelled = cancelled
        self._project = f"projects/{quote(self.repository.path, safe='')}"

    def _get(self, endpoint: str, *, timeout: float = 30) -> str:
        environment = {
            key: value for key, value in os.environ.items() if key not in _HOST_OVERRIDES
        }
        environment["GIT_TERMINAL_PROMPT"] = "0"
        try:
            result = run_process(
                ["glab", "api", "--hostname", self.repository.host, "--method", "GET", endpoint],
                timeout=timeout,
                check=True,
                cancelled=self.cancelled,
                env=environment,
            )
        except FileNotFoundError as exc:
            raise ValueError(
                "GitLab requires glab on PATH. Install it and run glab auth login --hostname HOST."
            ) from exc
        except subprocess.CalledProcessError as exc:
            # CLI diagnostics can contain server-controlled data or credentials.
            raise ValueError(
                "GitLab request failed. Check access with glab auth status --hostname HOST."
            ) from exc
        return result.stdout

    def _json(self, endpoint: str) -> object:
        try:
            return json.loads(self._get(endpoint))
        except json.JSONDecodeError as exc:
            raise ValueError("Invalid GitLab JSON response") from exc

    def list_runs(self, limit: int) -> list[Run]:
        if not 1 <= limit <= PAGE_SIZE:
            raise ValueError("GitLab run limit must be between 1 and 100")
        records = self._json(f"{self._project}/pipelines?per_page={limit}&order_by=id&sort=desc")
        if not isinstance(records, list) or len(records) > limit:
            raise ValueError("Invalid GitLab pipeline list")
        runs = []
        for value in records:
            pipeline = _record(value)
            phase, result = _state(pipeline)
            name = _text(pipeline, "name") or f"Pipeline #{pipeline['id']}"
            runs.append(
                {
                    "databaseId": pipeline["id"],
                    "workflowName": name,
                    "headBranch": _text(pipeline, "ref"),
                    "headSha": _text(pipeline, "sha"),
                    "status": phase,
                    "conclusion": result,
                    "startedAt": _timestamp(pipeline, "started_at"),
                    "updatedAt": _timestamp(pipeline, "updated_at"),
                    "createdAt": _timestamp(pipeline, "created_at"),
                    "displayTitle": name,
                    "url": _text(pipeline, "web_url"),
                    "attempt": 1,
                }
            )
        return runs

    def _job_records(self, run_id: int) -> list[dict]:
        run_id = _identifier(run_id)
        jobs = []
        # Explicit pages avoid glab-version differences in paginated JSON output.
        # A short page ends the list. Fail, rather than present a truncated success.
        for page in range(1, MAX_JOB_PAGES + 1):
            records = self._json(
                f"{self._project}/pipelines/{run_id}/jobs?include_retried=false&per_page={PAGE_SIZE}&page={page}"
            )
            if not isinstance(records, list) or len(records) > PAGE_SIZE:
                raise ValueError("Invalid GitLab job list")
            jobs.extend(_record(value) for value in records)
            if len(records) < PAGE_SIZE:
                return jobs
        raise ValueError("GitLab job pagination exceeded the safety limit")

    @staticmethod
    def _job(job: dict) -> Job:
        phase, result = _state(job)
        allowed = job.get("allow_failure", False)
        if type(allowed) is not bool:
            raise ValueError("Invalid GitLab allow_failure value")
        name = _text(job, "name") or "job"
        if allowed and result == "failure":
            name += " (allowed failure)"
        return {
            "databaseId": job["id"],
            "name": name,
            "status": phase,
            "conclusion": result,
            "startedAt": _timestamp(job, "started_at"),
            "completedAt": _timestamp(job, "finished_at"),
            "url": _text(job, "web_url"),
        }

    def jobs(self, run_id: int) -> list[Job]:
        return [self._job(job) for job in self._job_records(run_id)]

    def details(self, run_id: int) -> RunDetails:
        pipeline = _record(self._json(f"{self._project}/pipelines/{_identifier(run_id)}"))
        if pipeline["id"] != run_id:
            raise ValueError("GitLab returned a different pipeline")
        records = self._job_records(run_id)
        title = None
        if records:
            commit = records[0].get("commit")
            if isinstance(commit, dict):
                title = _text(commit, "title")
        return RunDetails(
            [self._job(job) for job in records],
            title,
            _timestamp(pipeline, "started_at"),
            _timestamp(pipeline, "finished_at"),
        )

    def job_log(self, job_id: int) -> str:
        return self._get(f"{self._project}/jobs/{_identifier(job_id)}/trace", timeout=60)

    def mutate(self, run_id: int, action: Mutation) -> None:
        raise ValueError("GitLab is read-only. Pipeline actions are disabled.")
