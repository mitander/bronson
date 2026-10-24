"""Filesystem and dashboard-v1 codec tests, without importing widgets."""

import copy
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bronson.cache import Cache, DashboardSnapshot


def revision(run):
    return (run.get("attempt", 1), run.get("status"), run.get("updatedAt"))


class CacheTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "logs"
        self.cache = Cache(self.root)
        self.run_record = {
            "databaseId": 1,
            "status": "completed",
            "updatedAt": "2026-01-01T00:00:00Z",
        }
        self.jobs = [
            {"databaseId": 10, "name": "build", "status": "completed", "conclusion": "success"}
        ]
        self.snapshot = DashboardSnapshot([self.run_record], [(self.run_record, self.jobs)])
        self.payload = {
            "version": 1,
            "repo": "owner/repo",
            "runs": [self.run_record],
            "details": {"1": {"revision": list(revision(self.run_record)), "jobs": self.jobs}},
        }

    def legacy_path(self, repo, key, suffix):
        digest = hashlib.sha1(f"{repo}:{key}".encode()).hexdigest()
        return self.root / f"{digest}{suffix}"

    def write_payload(self, payload):
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.legacy_path("owner/repo", "dashboard-v1", ".json")
        path.write_text(json.dumps(payload))
        return path

    def test_logs_keep_legacy_keys_and_are_repo_scoped(self):
        self.assertIsNone(self.cache.get_log("owner/repo", "job-raw-v1:10"))
        self.cache.put_log("owner/repo", "job-raw-v1:10", "first\nsecond")
        path = self.legacy_path("owner/repo", "job-raw-v1:10", ".log")
        self.assertEqual(path.read_text(), "first\nsecond")
        self.assertEqual(self.cache.get_log("owner/repo", "job-raw-v1:10"), "first\nsecond")
        self.assertIsNone(self.cache.get_log("different/repo", "job-raw-v1:10"))
        path.write_text("legacy log")
        self.assertEqual(self.cache.get_log("owner/repo", "job-raw-v1:10"), "legacy log")
        self.cache.put_log("owner/repo", "empty", "")
        self.assertEqual(self.cache.get_log("owner/repo", "empty"), "")

    def test_log_pruning_is_oldest_first_and_leaves_snapshots_alone(self):
        snapshot_path = self.write_payload(self.payload)
        self.cache.put_log("owner/repo", "old", "1234")
        old = self.legacy_path("owner/repo", "old", ".log")
        os.utime(old, (1, 1))
        with patch("bronson.cache.CACHE_LIMIT", 8):
            self.cache.put_log("owner/repo", "new", "56789")
        self.assertFalse(old.exists())
        self.assertEqual(self.cache.get_log("owner/repo", "new"), "56789")
        self.assertTrue(snapshot_path.exists())

    def test_unavailable_storage_is_a_miss_and_writes_are_nonfatal(self):
        self.root.write_text("not a directory")
        self.assertIsNone(self.cache.get_log("owner/repo", "job"))
        self.assertIsNone(self.cache.load_snapshot("owner/repo", revision))
        self.cache.put_log("owner/repo", "job", "output")
        self.cache.save_snapshot("owner/repo", self.snapshot, revision)
        self.assertEqual(self.root.read_text(), "not a directory")

    def test_snapshot_reads_and_writes_the_existing_format(self):
        path = self.write_payload(self.payload)
        self.assertEqual(self.cache.load_snapshot("owner/repo", revision), self.snapshot)
        self.cache.save_snapshot("owner/repo", self.snapshot, revision)
        self.assertEqual(json.loads(path.read_text()), self.payload)
        self.assertIsNone(self.cache.load_snapshot("different/repo", revision))
        self.assertEqual(list(self.root.iterdir()), [path])

    def test_missing_and_malformed_snapshots_are_misses(self):
        self.assertIsNone(self.cache.load_snapshot("owner/repo", revision))
        path = self.write_payload(self.payload)
        for contents in ("{broken", "null", "[]", '"text"', "1", "false", "{}"):
            with self.subTest(contents=contents):
                path.write_text(contents)
                self.assertIsNone(self.cache.load_snapshot("owner/repo", revision))
        path.write_bytes(b"\xff")
        self.assertIsNone(self.cache.load_snapshot("owner/repo", revision))

    def test_invalid_snapshot_fields_are_rejected(self):
        replacements = [
            ("version", 2),
            ("repo", "different/repo"),
            ("runs", {}),
            ("runs", [None]),
            ("runs", [{}]),
            ("runs", [{"databaseId": True}]),
            ("runs", [{"databaseId": 1, "attempt": "1"}]),
            ("runs", [{"databaseId": 1, "attempt": True}]),
            ("runs", [{"databaseId": 1, "url": 2}]),
            ("runs", [self.run_record] * 101),
            ("details", []),
            ("details", {"1": True}),
            ("details", {"1": {"jobs": []}}),
            ("details", {"1": {"revision": list(revision(self.run_record)), "jobs": {}}}),
            ("details", {"1": {"revision": list(revision(self.run_record)), "jobs": [None]}}),
            (
                "details",
                {
                    "1": {
                        "revision": list(revision(self.run_record)),
                        "jobs": [{"databaseId": False}],
                    }
                },
            ),
            (
                "details",
                {
                    "1": {
                        "revision": list(revision(self.run_record)),
                        "jobs": [{"databaseId": 10, "name": 3}],
                    }
                },
            ),
        ]
        for field, value in replacements:
            with self.subTest(field=field, value=value):
                payload = {**self.payload, field: value}
                self.write_payload(payload)
                self.assertIsNone(self.cache.load_snapshot("owner/repo", revision))

    def test_cached_timestamps_must_be_parseable_and_timezone_aware(self):
        for value in ("invalid", "2026-01-01T00:00:00", "2026-01-01"):
            for field in ("startedAt", "completedAt", "updatedAt", "createdAt"):
                with self.subTest(value=value, field=field):
                    payload = copy.deepcopy(self.payload)
                    payload["runs"][0][field] = value
                    self.write_payload(payload)
                    self.assertIsNone(self.cache.load_snapshot("owner/repo", revision))
            for field in ("startedAt", "completedAt"):
                with self.subTest(value=value, job_field=field):
                    payload = copy.deepcopy(self.payload)
                    payload["details"]["1"]["jobs"][0][field] = value
                    self.write_payload(payload)
                    self.assertIsNone(self.cache.load_snapshot("owner/repo", revision))

    def test_valid_cached_completion_time_round_trips(self):
        payload = copy.deepcopy(self.payload)
        payload["runs"][0]["startedAt"] = "2026-01-01T00:00:00Z"
        payload["runs"][0]["completedAt"] = "2026-01-01T00:01:00+00:00"
        self.write_payload(payload)
        snapshot = self.cache.load_snapshot("owner/repo", revision)
        assert snapshot is not None
        self.assertEqual(snapshot.runs[0]["completedAt"], "2026-01-01T00:01:00+00:00")

    def test_size_limit_is_checked_before_reading(self):
        path = self.write_payload(self.payload)
        with path.open("ab") as file:
            file.truncate(5 * 1024 * 1024 + 1)
        with patch.object(Path, "read_text", side_effect=AssertionError("oversized read")):
            self.assertIsNone(self.cache.load_snapshot("owner/repo", revision))

    def test_stale_and_unreferenced_details_are_ignored_before_job_validation(self):
        payload = copy.deepcopy(self.payload)
        payload["details"]["1"] = {"revision": [99, "completed", None], "jobs": "invalid"}
        payload["details"]["2"] = True
        self.write_payload(payload)
        self.assertEqual(
            self.cache.load_snapshot("owner/repo", revision),
            DashboardSnapshot([self.run_record], []),
        )

    def test_revision_identity_is_supplied_by_the_caller(self):
        def revision_for(run):
            return ("opaque", run["databaseId"])

        self.cache.save_snapshot("owner/repo", self.snapshot, revision_for)
        self.assertEqual(self.cache.load_snapshot("owner/repo", revision_for), self.snapshot)
        self.assertEqual(
            self.cache.load_snapshot("owner/repo", revision),
            DashboardSnapshot([self.run_record], []),
        )

    def test_duplicate_run_ids_do_not_attach_details_to_a_stale_revision(self):
        payload = copy.deepcopy(self.payload)
        stale = {**self.run_record, "attempt": 2}
        payload["runs"] = [stale, self.run_record]
        self.write_payload(payload)
        self.assertEqual(
            self.cache.load_snapshot("owner/repo", revision),
            DashboardSnapshot([stale, self.run_record], [(self.run_record, self.jobs)]),
        )

    def test_atomic_replace_keeps_old_snapshot_until_new_file_is_complete(self):
        path = self.write_payload(self.payload)
        before = path.read_bytes()
        replace = os.replace

        def inspect_replace(source, destination):
            self.assertEqual(Path(destination), path)
            self.assertEqual(Path(source).parent, path.parent)
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(json.loads(Path(source).read_text())["runs"], [])
            replace(source, destination)

        with patch("bronson.cache.os.replace", side_effect=inspect_replace) as replace_mock:
            self.cache.save_snapshot("owner/repo", DashboardSnapshot([], []), revision)
        replace_mock.assert_called_once()
        self.assertEqual(json.loads(path.read_text())["runs"], [])
        self.assertEqual(list(self.root.iterdir()), [path])

    def test_failed_write_or_replace_preserves_old_snapshot_and_cleans_temporary(self):
        path = self.write_payload(self.payload)
        before = path.read_bytes()
        for target in ("json.dump", "os.replace"):
            with (
                self.subTest(target=target),
                patch(f"bronson.cache.{target}", side_effect=OSError("disk failure")),
            ):
                self.cache.save_snapshot("owner/repo", DashboardSnapshot([], []), revision)
                self.assertEqual(path.read_bytes(), before)
                self.assertEqual(list(self.root.iterdir()), [path])
