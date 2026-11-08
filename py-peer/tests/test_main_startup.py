"""Regression test for API-mode startup failure handling."""

import threading
import time
import unittest

from main import run_headless_in_thread


class _FailingService:
    ready = False
    failed = False

    async def start(self):
        self.failed = True


class StartupTests(unittest.TestCase):
    def test_failed_service_fails_fast_instead_of_waiting_30s(self):
        service = _FailingService()
        started = time.monotonic()
        with self.assertRaisesRegex(RuntimeError, "failed to start"):
            run_headless_in_thread(service, threading.Event())
        self.assertLess(time.monotonic() - started, 2.0)


if __name__ == "__main__":
    unittest.main()
