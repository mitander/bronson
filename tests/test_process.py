"""Subprocess guarantees at the process owner, without a Textual worker."""

import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import patch

from bronson.providers.process import run_process


class ProcessTests(unittest.TestCase):
    def test_cancelled_request_kills_and_reaps_process_promptly(self):
        cancelled = threading.Event()
        launched = []
        popen = subprocess.Popen

        def launch(*args, **kwargs):
            process = popen(*args, **kwargs)
            launched.append(process)
            return process

        timer = threading.Timer(0.1, cancelled.set)
        with patch("bronson.providers.process.subprocess.Popen", side_effect=launch):
            timer.start()
            started = time.monotonic()
            try:
                with self.assertRaisesRegex(subprocess.SubprocessError, "cancelled"):
                    run_process(
                        [sys.executable, "-c", "import time; time.sleep(10)"],
                        cancelled=cancelled.is_set,
                    )
            finally:
                timer.cancel()
                timer.join()
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertIsNotNone(launched[0].poll())

    def test_output_errors_and_timeout(self):
        process = run_process([sys.executable, "-c", "print('ready')"], check=True)
        self.assertEqual(process.stdout, "ready\n")
        with self.assertRaises(subprocess.CalledProcessError):
            run_process([sys.executable, "-c", "raise SystemExit(2)"], check=True)
        with self.assertRaises(subprocess.TimeoutExpired):
            run_process([sys.executable, "-c", "import time; time.sleep(10)"], timeout=0.1)
