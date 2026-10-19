"""Resolve explicit repository identity without guessing self-hosted providers."""

import re
import subprocess
from urllib.parse import urlsplit

from bronson.models import ProviderName, Repository

_HOST = re.compile(r"[a-zA-Z0-9](?:[a-zA-Z0-9.-]*[a-zA-Z0-9])?(?::[0-9]{1,5})?\Z")
_SEGMENT = re.compile(r"[a-zA-Z0-9_.-]+\Z")


def origin_remote(cwd: str) -> str | None:
    try:
        return subprocess.run(
            ["git", "-C", cwd, "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None


def validate_repository(provider: ProviderName, host: str, path: str) -> Repository:
    if not _HOST.fullmatch(host) or any(
        part in ("", ".", "..") for part in host.split(":")[0].split(".")
    ):
        raise ValueError("Invalid hostname. Use a hostname with an optional HTTPS port, not a URL.")
    if ":" in host and not 0 < int(host.rsplit(":", 1)[1]) < 65536:
        raise ValueError("Invalid HTTPS port")
    parts = path.split("/")
    if len(parts) < 2 or any(part in (".", "..") or not _SEGMENT.fullmatch(part) for part in parts):
        raise ValueError("Repository must be a full group/project path")
    if provider == "github" and (host.lower() != "github.com" or len(parts) != 2):
        raise ValueError("GitHub support requires github.com and owner/repository")
    return Repository(provider, host.lower(), path)


def parse_remote(remote: str) -> tuple[str, str]:
    if "://" not in remote:
        match = re.fullmatch(r"(?:[^/@\s]+@)?([^/:\s]+):(.+)", remote)
        if not match:
            raise ValueError("Unsupported origin URL. Use --provider, --host and --repo.")
        host, path = match.groups()
    else:
        try:
            parsed = urlsplit(remote)
        except ValueError as exc:
            # urllib's diagnostics may echo a credential-bearing netloc.
            raise ValueError("Invalid origin URL") from exc
        if parsed.scheme not in ("https", "ssh") or not parsed.hostname:
            raise ValueError("Origin must use HTTPS or SSH")
        if parsed.password or (parsed.scheme == "https" and parsed.username):
            raise ValueError(
                "Credential-bearing origin URLs are not supported. Use --repo and --host."
            )
        if parsed.query or parsed.fragment:
            raise ValueError("Origin URLs cannot contain a query or fragment")
        # SSH transport ports do not select the HTTPS API port.
        host = parsed.netloc if parsed.scheme == "https" else parsed.hostname
        path = parsed.path.removeprefix("/")
    return host, path.removesuffix(".git")


def resolve_repository(
    repo: str | None,
    provider: ProviderName | None,
    host: str | None,
    remote: str | None,
) -> Repository:
    if repo is None:
        if remote is None:
            raise ValueError("No --repo and no git origin remote")
        origin_host, repo = parse_remote(remote)
        if host and host.lower() != origin_host.lower():
            raise ValueError(
                "--host differs from origin. Supply --repo to select a different host."
            )
        host = origin_host
    if provider is None:
        if host is None or host.lower() == "github.com":
            provider = "github"
        elif host.lower() == "gitlab.com":
            provider = "gitlab"
        else:
            raise ValueError("Self-hosted origins require --provider gitlab")
    host = host or ("github.com" if provider == "github" else "gitlab.com")
    return validate_repository(provider, host, repo)
