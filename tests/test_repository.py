"""Repository selection never guesses credentials for an unknown host."""

import unittest

from bronson.models import Repository
from bronson.providers.repository import parse_remote, resolve_repository


class RepositoryTests(unittest.TestCase):
    def test_supported_origin_transports(self):
        for remote in (
            "git@gitlab.example.test:team/sub/repo.git",
            "ssh://git@gitlab.example.test:2222/team/sub/repo.git",
            "https://gitlab.example.test/team/sub/repo.git",
        ):
            with self.subTest(remote=remote):
                self.assertEqual(
                    resolve_repository(None, "gitlab", None, remote),
                    Repository("gitlab", "gitlab.example.test", "team/sub/repo"),
                )
        self.assertEqual(
            parse_remote("https://gitlab.example.test:8443/team/repo.git"),
            ("gitlab.example.test:8443", "team/repo"),
        )

    def test_defaults_and_explicit_selection(self):
        self.assertEqual(
            resolve_repository("owner/repo", None, None, None),
            Repository("github", "github.com", "owner/repo"),
        )
        self.assertEqual(
            resolve_repository(None, None, None, "git@gitlab.com:team/repo.git"),
            Repository("gitlab", "gitlab.com", "team/repo"),
        )
        self.assertEqual(
            resolve_repository("team/sub/repo", "gitlab", "GL.EXAMPLE.TEST:8443", None),
            Repository("gitlab", "gl.example.test:8443", "team/sub/repo"),
        )

    def test_ambiguous_or_invalid_identity_is_rejected(self):
        cases = [
            (None, None, None, None),
            (None, None, None, "git@unknown.test:team/repo.git"),
            (None, "gitlab", "other.test", "git@gl.test:team/repo.git"),
            ("team/repo", "github", "gl.test", None),
            ("team/sub/repo", "github", None, None),
            ("team/repo", "gitlab", "https://gl.test", None),
            ("team/repo", "gitlab", "-evil.test", None),
            ("team/repo", "gitlab", "gl.test:70000", None),
            ("team/repo", "gitlab", "user@gl.test", None),
            ("team/repo", "gitlab", "gl..test", None),
            ("team/../repo", "gitlab", "gl.test", None),
            ("team/repo?x=y", "gitlab", "gl.test", None),
            ("team/repo#frag", "gitlab", "gl.test", None),
            (None, "gitlab", None, "https://secret@gl.test/team/repo.git"),
            (None, "gitlab", None, "https://user:secret@gl.test/team/repo.git"),
            (None, "gitlab", None, "file:///tmp/team/repo"),
            (None, "gitlab", None, "https://gl.test/team/repo?token=secret"),
        ]
        for case in cases:
            with self.subTest(case=case), self.assertRaises(ValueError):
                resolve_repository(*case)

    def test_invalid_origin_diagnostics_do_not_echo_credentials(self):
        remote = "https://user:fake-secret@gl.test：443/team/repo"
        with self.assertRaisesRegex(ValueError, "Invalid origin URL") as result:
            resolve_repository(None, "gitlab", None, remote)
        self.assertNotIn("fake-secret", str(result.exception))
        self.assertIsInstance(result.exception.__cause__, ValueError)

    def test_cache_identity_separates_provider_host_and_project(self):
        github = Repository("github", "github.com", "team/repo")
        gitlab = Repository("gitlab", "github.com", "team/repo")
        other_host = Repository("gitlab", "other.test", "team/repo")
        other_project = Repository("gitlab", "other.test", "team/other")
        keys = {repository.cache_key for repository in (github, gitlab, other_host, other_project)}
        self.assertEqual(len(keys), 4)
        self.assertEqual(github.cache_key, "team/repo")
