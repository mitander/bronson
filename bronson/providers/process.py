"""Timed CLI execution with explicit local cancellation and child cleanup."""

import subprocess
import time
from collections.abc import Callable, Mapping


def run_process(
    args: list[str],
    *,
    timeout: float = 30,
    check: bool = False,
    cancelled: Callable[[], bool] = lambda: False,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    deadline = time.monotonic() + timeout
    with subprocess.Popen(
        args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env
    ) as process:
        try:
            while True:
                if cancelled():
                    raise subprocess.SubprocessError("Request cancelled")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(args, timeout)
                try:
                    stdout, stderr = process.communicate(timeout=min(0.05, remaining))
                    break
                except subprocess.TimeoutExpired:
                    continue
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()

        result = subprocess.CompletedProcess(args, process.returncode, stdout, stderr)
        if check:
            result.check_returncode()
        return result
