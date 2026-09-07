"""Log files and the legacy dashboard-v1 startup snapshot codec."""

import hashlib
import json
import os
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

CACHE_LIMIT = 100 * 1024 * 1024
# These fields and limits belong to the persisted dashboard-v1 format.
SNAPSHOT_RUN_LIMIT = 100
SNAPSHOT_SIZE_LIMIT = 5 * 1024 * 1024
RUN_TEXT_FIELDS = (
    "workflowName",
    "headBranch",
    "headSha",
    "status",
    "conclusion",
    "startedAt",
    "updatedAt",
    "createdAt",
    "displayTitle",
    "url",
)
JOB_TEXT_FIELDS = ("name", "status", "conclusion", "startedAt", "completedAt", "url")


@dataclass
class DashboardSnapshot:
    """Startup hints with only revision-matched job details."""

    runs: list[dict]
    details: list[tuple[dict, list[dict]]]


class Cache:
    def __init__(self, root: Path) -> None:
        self.root = root

    def _path(self, repo: str, key: str) -> Path:
        digest = hashlib.sha1(f"{repo}:{key}".encode()).hexdigest()
        return self.root / f"{digest}.log"

    def get_log(self, repo: str, key: str) -> str | None:
        try:
            return self._path(repo, key).read_text()
        except OSError:
            return None

    def put_log(self, repo: str, key: str, raw: str) -> None:
        """Store a log and prune oldest-first under the existing cap."""
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            self._path(repo, key).write_text(raw)
            files = sorted(self.root.glob("*.log"), key=lambda p: p.stat().st_mtime)
            total = sum(p.stat().st_size for p in files)
            for path in files:
                if total <= CACHE_LIMIT:
                    break
                total -= path.stat().st_size
                path.unlink()
        except OSError:
            pass

    def load_snapshot(
        self, repo: str, revision_for: Callable[[dict], tuple]
    ) -> DashboardSnapshot | None:
        """Validate v1 data without applying it or treating it as a refresh.

        The caller owns revision identity. Stale details are ignored before
        validating jobs, as in the original dashboard recovery path.
        """
        path = self._path(repo, "dashboard-v1").with_suffix(".json")
        try:
            if path.stat().st_size > SNAPSHOT_SIZE_LIMIT:
                return None
            snapshot = json.loads(path.read_text())
            if snapshot["version"] != 1 or snapshot["repo"] != repo:
                return None
            runs, details = snapshot["runs"], snapshot["details"]
            if (
                not isinstance(runs, list)
                or len(runs) > SNAPSHOT_RUN_LIMIT
                or not isinstance(details, dict)
            ):
                return None
            for run in runs:
                if not isinstance(run, dict) or type(run.get("databaseId")) is not int:
                    return None
                if type(run.get("attempt", 1)) is not int:
                    return None
                if any(
                    run.get(key) is not None and not isinstance(run[key], str)
                    for key in RUN_TEXT_FIELDS
                ):
                    return None
            restored = []
            for run in runs:
                entry = details.get(str(run["databaseId"]))
                if not entry or entry["revision"] != list(revision_for(run)):
                    continue
                jobs = entry["jobs"]
                if not isinstance(jobs, list):
                    return None
                for job in jobs:
                    if not isinstance(job, dict) or type(job.get("databaseId")) is not int:
                        return None
                    if any(
                        job.get(key) is not None and not isinstance(job[key], str)
                        for key in JOB_TEXT_FIELDS
                    ):
                        return None
                restored.append((run, jobs))
        except (OSError, ValueError, KeyError, TypeError):
            return None
        return DashboardSnapshot(runs, restored)

    def save_snapshot(
        self, repo: str, snapshot: DashboardSnapshot, revision_for: Callable[[dict], tuple]
    ) -> None:
        """Atomically replace v1 data. The caller supplies only fresh details."""
        path = self._path(repo, "dashboard-v1").with_suffix(".json")
        payload = {
            "version": 1,
            "repo": repo,
            "runs": snapshot.runs,
            "details": {
                str(run["databaseId"]): {
                    "revision": list(revision_for(run)),
                    "jobs": jobs,
                }
                for run, jobs in snapshot.details
            },
        }
        temporary = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as file:
                temporary = Path(file.name)
                json.dump(payload, file)
            os.replace(temporary, path)
        except OSError:
            pass
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
