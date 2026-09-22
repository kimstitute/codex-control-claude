"""Portable file-lock behavior."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/claude-control/scripts"))

from claude_control.platform import locks  # noqa: E402


class FakeWindowsApi:
    def __init__(self, error=None):
        self.error = error
        self.calls = []

    def get_osfhandle(self, fd):
        return fd + 1000

    def lock(self, handle, *, exclusive, blocking):
        self.calls.append(("lock", handle, exclusive, blocking))
        if self.error is not None:
            raise locks._windows_error(self.error, "LockFileEx")

    def unlock(self, handle):
        self.calls.append(("unlock", handle))


class WindowsLockTests(unittest.TestCase):
    def test_injected_api_receives_mode_and_unlocks(self):
        api = FakeWindowsApi()
        with tempfile.TemporaryDirectory() as root:
            with locks.file_lock(
                Path(root) / "lock",
                exclusive=True,
                blocking=False,
                platform="win32",
                windows_api=api,
            ):
                pass

        self.assertEqual(api.calls[0][0], "lock")
        self.assertEqual(api.calls[0][2:], (True, False))
        self.assertEqual(api.calls[1][0], "unlock")

    def test_contention_is_a_blocking_io_error(self):
        api = FakeWindowsApi(locks.ERROR_LOCK_VIOLATION)
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(BlockingIOError):
                with locks.file_lock(
                    Path(root) / "lock",
                    exclusive=True,
                    blocking=False,
                    platform="win32",
                    windows_api=api,
                ):
                    pass

    def test_win32_flags_preserve_shared_exclusive_and_blocking_modes(self):
        self.assertEqual(locks._lock_flags(exclusive=False, blocking=True), 0)
        self.assertEqual(
            locks._lock_flags(exclusive=True, blocking=False),
            locks.LOCKFILE_EXCLUSIVE_LOCK | locks.LOCKFILE_FAIL_IMMEDIATELY,
        )


@unittest.skipUnless(os.name == "posix", "flock behavior requires POSIX")
class PosixLockTests(unittest.TestCase):
    def test_shared_locks_coexist_and_exclude_an_exclusive_lock(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "lock"
            with locks.file_lock(path, exclusive=False):
                with locks.file_lock(path, exclusive=False, blocking=False):
                    pass
                with self.assertRaises(BlockingIOError):
                    with locks.file_lock(path, exclusive=True, blocking=False):
                        pass

    def test_exclusive_lock_is_released_on_exit(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "lock"
            with locks.file_lock(path, exclusive=True):
                with self.assertRaises(BlockingIOError):
                    with locks.file_lock(path, exclusive=True, blocking=False):
                        pass
            with locks.file_lock(path, exclusive=True, blocking=False):
                pass


if __name__ == "__main__":
    unittest.main()
