"""The dashboard depends on operations, not CLI arguments or provider responses."""

from typing import Literal, Protocol

from bronson.models import Job, Repository, Run, RunDetails

Mutation = Literal["rerun_failed", "rerun_all", "cancel"]


class Provider(Protocol):
    repository: Repository
    name: str
    can_mutate: bool

    def list_runs(self, limit: int) -> list[Run]: ...

    def jobs(self, run_id: int) -> list[Job]: ...

    def details(self, run_id: int) -> RunDetails: ...

    def job_log(self, job_id: int) -> str: ...

    def mutate(self, run_id: int, action: Mutation) -> None: ...
