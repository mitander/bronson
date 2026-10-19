"""GitHub CLI mapping and actions for github.com repositories."""

import json
from collections.abc import Callable

from bronson.models import Job, Repository, Run, RunDetails
from bronson.providers import Mutation
from bronson.providers.process import run_process

RUN_FIELDS = (
    "databaseId,workflowName,headBranch,headSha,status,conclusion,"
    "startedAt,updatedAt,createdAt,displayTitle,url,attempt"
)


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

    def _json(self, args: list[str]) -> dict | list:
        output = run_process(["gh", *args], check=True, cancelled=self.cancelled).stdout
        return json.loads(output)

    def list_runs(self, limit: int) -> list[Run]:
        return self._json(
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

    def jobs(self, run_id: int) -> list[Job]:
        pages = self._json(
            [
                "api",
                f"repos/{self.repository.path}/actions/runs/{run_id}/jobs?filter=latest&per_page=100",
                "--paginate",
                "--slurp",
            ]
        )
        return [
            {
                "databaseId": job["id"],
                "name": job.get("name"),
                "status": job.get("status") or "unknown",
                "conclusion": job.get("conclusion"),
                "startedAt": job.get("started_at"),
                "completedAt": job.get("completed_at"),
                "url": job.get("html_url"),
            }
            for page in pages
            for job in page["jobs"]
        ]

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
