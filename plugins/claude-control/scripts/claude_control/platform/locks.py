"""Portable whole-file advisory locking."""

from __future__ import annotations

import os
import sys
from contextlib import contextmanager

LOCKFILE_FAIL_IMMEDIATELY = 0x00000001
LOCKFILE_EXCLUSIVE_LOCK = 0x00000002
ERROR_LOCK_VIOLATION = 33
ERROR_IO_PENDING = 997


def _posix_lock(handle, *, exclusive, blocking):
    import fcntl

    operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
    if not blocking:
        operation |= fcntl.LOCK_NB
    fcntl.flock(handle, operation)


def _posix_unlock(handle):
    import fcntl

    fcntl.flock(handle, fcntl.LOCK_UN)


def _lock_flags(*, exclusive, blocking):
    flags = LOCKFILE_EXCLUSIVE_LOCK if exclusive else 0
    if not blocking:
        flags |= LOCKFILE_FAIL_IMMEDIATELY
    return flags


def _windows_error(code, operation):
    if code in (ERROR_LOCK_VIOLATION, ERROR_IO_PENDING):
        return BlockingIOError(code, f"{operation} could not acquire the file lock")
    return OSError(code, f"{operation} failed with Windows error {code}")


class _WindowsApi:
    """Lazy Win32 adapter; never constructed on POSIX."""

    def __init__(self):
        import ctypes
        from ctypes import wintypes

        class Overlapped(ctypes.Structure):
            _fields_ = [
                ("Internal", ctypes.c_size_t),
                ("InternalHigh", ctypes.c_size_t),
                ("Offset", wintypes.DWORD),
                ("OffsetHigh", wintypes.DWORD),
                ("hEvent", wintypes.HANDLE),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.LockFileEx.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(Overlapped),
        ]
        kernel32.LockFileEx.restype = wintypes.BOOL
        kernel32.UnlockFileEx.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(Overlapped),
        ]
        kernel32.UnlockFileEx.restype = wintypes.BOOL
        self.ctypes = ctypes
        self.kernel32 = kernel32
        self.overlapped_type = Overlapped

    @staticmethod
    def get_osfhandle(fd):
        import msvcrt

        return msvcrt.get_osfhandle(fd)

    def lock(self, handle, *, exclusive, blocking):
        overlapped = self.overlapped_type()
        result = self.kernel32.LockFileEx(
            handle,
            _lock_flags(exclusive=exclusive, blocking=blocking),
            0,
            0xFFFFFFFF,
            0xFFFFFFFF,
            self.ctypes.byref(overlapped),
        )
        if not result:
            raise _windows_error(self.ctypes.get_last_error(), "LockFileEx")

    def unlock(self, handle):
        overlapped = self.overlapped_type()
        result = self.kernel32.UnlockFileEx(
            handle,
            0,
            0xFFFFFFFF,
            0xFFFFFFFF,
            self.ctypes.byref(overlapped),
        )
        if not result:
            raise _windows_error(self.ctypes.get_last_error(), "UnlockFileEx")


_WINDOWS_API = None


def _get_windows_api():
    global _WINDOWS_API
    if _WINDOWS_API is None:
        _WINDOWS_API = _WindowsApi()
    return _WINDOWS_API


def _windows_lock(handle, api, *, exclusive, blocking):
    os_handle = api.get_osfhandle(handle.fileno())
    api.lock(os_handle, exclusive=exclusive, blocking=blocking)
    return os_handle


@contextmanager
def file_lock(path, *, exclusive, blocking=True, platform=None, windows_api=None):
    """Open *path* and hold a shared or exclusive whole-file lock."""
    platform = sys.platform if platform is None else platform
    with open(path, "a+b") as handle:
        if platform == "win32":
            api = _get_windows_api() if windows_api is None else windows_api
            os_handle = _windows_lock(
                handle,
                api,
                exclusive=exclusive,
                blocking=blocking,
            )
            try:
                yield handle
            finally:
                api.unlock(os_handle)
            return
        if os.name == "posix" and platform != "win32":
            _posix_lock(handle, exclusive=exclusive, blocking=blocking)
            try:
                yield handle
            finally:
                _posix_unlock(handle)
            return
        raise RuntimeError(f"No file-lock backend for platform {platform!r}")
