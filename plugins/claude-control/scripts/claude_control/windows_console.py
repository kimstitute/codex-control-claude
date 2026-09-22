"""Dependency-free Windows ANSI/VT console helpers."""

from __future__ import annotations

import os
import shutil
import time

ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
STD_OUTPUT_HANDLE = -11

ARROW_UP = "arrow_up"
ARROW_DOWN = "arrow_down"
PAGE_UP = "page_up"
PAGE_DOWN = "page_down"
KEY_QUIT = "quit"
KEY_TAB = "tab"
KEY_REFRESH = "refresh"
KEY_ACTIVE = "active"

_EXTENDED_KEYS = {"H": ARROW_UP, "P": ARROW_DOWN, "I": PAGE_UP, "Q": PAGE_DOWN}


def enable_vt_processing():
    if os.name != "nt":
        return False
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetStdHandle.argtypes = [wintypes.DWORD]
        kernel32.GetStdHandle.restype = wintypes.HANDLE
        kernel32.GetConsoleMode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel32.GetConsoleMode.restype = wintypes.BOOL
        kernel32.SetConsoleMode.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.SetConsoleMode.restype = wintypes.BOOL
        handle = kernel32.GetStdHandle(STD_OUTPUT_HANDLE)
        invalid = ctypes.c_void_p(-1).value
        if not handle or handle == invalid:
            return False
        mode = wintypes.DWORD()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        return bool(
            kernel32.SetConsoleMode(handle, mode.value | ENABLE_VIRTUAL_TERMINAL_PROCESSING)
        )
    except (AttributeError, OSError):
        return False


def terminal_size(default=(80, 24)):
    size = shutil.get_terminal_size(default)
    return size.columns, size.lines


def decode_key(sequence):
    if not sequence:
        return None
    first = sequence[0]
    if first in ("\x00", "\xe0"):
        return _EXTENDED_KEYS.get(sequence[1:2])
    if first == "\t":
        return KEY_TAB
    if first in "1234":
        return first
    if first in ("q", "Q"):
        return KEY_QUIT
    if first in ("r", "R"):
        return KEY_REFRESH
    if first in ("a", "A"):
        return KEY_ACTIVE
    if first in ("j", "J"):
        return ARROW_DOWN
    if first in ("k", "K"):
        return ARROW_UP
    return None


def read_key(timeout):
    import msvcrt

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if msvcrt.kbhit():
            first = msvcrt.getwch()
            if first in ("\x00", "\xe0"):
                return decode_key(first + msvcrt.getwch())
            return decode_key(first)
        time.sleep(min(0.03, max(0, deadline - time.monotonic())))
    return None


def render_frame(header, lines, *, columns, rows, clear):
    usable = max(1, rows)
    rendered = [str(line)[:columns] for line in [*header, *lines]][:usable]
    prefix = "\x1b[2J\x1b[H" if clear else ""
    suffix = "" if clear else "\r\n" + "-" * min(columns, 40)
    return prefix + "\r\n".join(rendered) + suffix
