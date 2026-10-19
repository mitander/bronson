"""Repository identity and the dashboard's shared display records.

The display field names retain the dashboard-v1 vocabulary so the GitHub
extraction does not change persisted snapshots. Adapters populate these records,
never expose arbitrary provider responses. Unknown native states stay visible.
"""

from dataclasses import dataclass
from typing import Literal, TypedDict

ProviderName = Literal["github", "gitlab"]


@dataclass(frozen=True)
class Repository:
    provider: ProviderName
    host: str
    path: str

    @property
    def label(self) -> str:
        return self.path if self.provider == "github" else f"{self.host}/{self.path}"

    @property
    def cache_key(self) -> str:
        # Keep legacy GitHub keys. Other contexts cannot share their namespace.
        if self.provider == "github" and self.host == "github.com":
            return self.path
        return f"{self.provider}://{self.host}/{self.path}"


class _OptionalRunFields(TypedDict, total=False):
    completedAt: str | None


class Run(_OptionalRunFields):
    databaseId: int
    workflowName: str | None
    headBranch: str | None
    headSha: str | None
    status: str
    conclusion: str | None
    startedAt: str | None
    updatedAt: str | None
    createdAt: str | None
    displayTitle: str | None
    url: str | None
    attempt: int


class Job(TypedDict):
    databaseId: int
    name: str | None
    status: str
    conclusion: str | None
    startedAt: str | None
    completedAt: str | None
    url: str | None


@dataclass
class RunDetails:
    jobs: list[Job]
    title: str | None = None
    started_at: str | None = None
    completed_at: str | None = None


def run_revision(run: Run) -> tuple:
    return (run.get("attempt", 1), run.get("status"), run.get("updatedAt"))
