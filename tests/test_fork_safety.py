"""A gunicorn worker is forked while the master's threads hold locks -- no app,
no network.

Under ``--preload`` the master runs the background threads and forks the
workers, at boot and every time one is recycled. A fork copies a held lock
held, and the thread that would release it does not exist in the child. On
2026-10-08 the workers forked during a new box's first 13F fetch, when
``sec13f._throttle`` held ``_rate_lock`` through its sleep; a ``/13f/refresh``
in one of them hung all six fetch threads on it for good. Each module whose
lock a master thread holds through a sleep, and whose callers also run in
workers, replaces it in the child with ``os.register_at_fork``. These tests
fork for real, with a thread holding the lock, and ask whether the child can
take it.
"""

from __future__ import annotations

import os
import threading
import unittest
import warnings

from ystocker import insiders, sec13f


def _child_can_take(module, attr: str) -> bool:
    """Fork while a thread holds ``module.attr``; whether the child gets it."""
    held, release = threading.Event(), threading.Event()

    def hold():
        with getattr(module, attr):
            held.set()
            release.wait(10)

    thread = threading.Thread(target=hold, daemon=True)
    thread.start()
    if not held.wait(5):
        raise AssertionError(f"could not take {attr} in the parent")
    try:
        with warnings.catch_warnings():
            # Python 3.12 warns about forking a threaded process: the very case.
            warnings.simplefilter("ignore", DeprecationWarning)
            pid = os.fork()
        if pid == 0:
            code = 1
            try:
                code = 0 if getattr(module, attr).acquire(timeout=2) else 1
            finally:
                os._exit(code)  # never back into unittest in the child
        _, status = os.waitpid(pid, 0)
    finally:
        release.set()
        thread.join(5)
    return os.waitstatus_to_exitcode(status) == 0


@unittest.skipUnless(hasattr(os, "fork"), "needs os.fork")
class ForkedWorkerTests(unittest.TestCase):
    def test_the_edgar_throttle(self):
        self.assertTrue(_child_can_take(sec13f, "_rate_lock"))

    def test_the_13f_cache_lock(self):
        self.assertTrue(_child_can_take(sec13f, "_sec13f_lock"))

    def test_the_insider_sweep_pace(self):
        self.assertTrue(_child_can_take(insiders, "_pace_lock"))

    def test_the_insider_file_write(self):
        self.assertTrue(_child_can_take(insiders, "_write_lock"))


class AfterForkTests(unittest.TestCase):
    def setUp(self):
        saved = {name: getattr(sec13f, name)
                 for name in ("_rate_lock", "_sec13f_lock", "_cusip_cache_lock", "_SESSION")}

        def restore():
            for name, value in saved.items():
                setattr(sec13f, name, value)

        self.addCleanup(restore)

    def test_the_child_opens_its_own_connections_as_the_same_client(self):
        parent = sec13f._SESSION
        sec13f._after_fork_in_child()
        self.assertIsNot(sec13f._SESSION, parent)
        self.assertEqual(sec13f._SESSION.headers["User-Agent"], parent.headers["User-Agent"])


if __name__ == "__main__":
    unittest.main()
