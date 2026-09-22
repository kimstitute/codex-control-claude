"""Pure, injectable platform capability reporting."""

from __future__ import annotations

REPORT_VERSION = 1
CAPABILITY_NAMES = (
    "provider_usage",
    "tui",
    "session_control",
    "workspace",
    "sandbox",
)


def _capability(supported, *, backend=None, reason=None):
    return {
        "supported": bool(supported),
        "backend": backend,
        "reason": reason,
    }


def _linux_report(has_module, which, environ):
    has_curses = bool(has_module("curses"))
    bwrap = which("bwrap")
    capabilities = {
        "provider_usage": _capability(True, backend="linux-local"),
        "tui": _capability(
            has_curses,
            backend="curses" if has_curses else None,
            reason=None if has_curses else "curses module is unavailable",
        ),
        "session_control": _capability(True, backend="posix-process-group"),
        "workspace": _capability(
            bool(bwrap),
            backend="bubblewrap" if bwrap else None,
            reason=None if bwrap else "Bubblewrap is unavailable",
        ),
        "sandbox": _capability(
            bool(bwrap),
            backend="bubblewrap" if bwrap else None,
            reason=None if bwrap else "Bubblewrap is unavailable",
        ),
    }
    return {
        "report_version": REPORT_VERSION,
        "platform": "linux",
        "wsl": bool(environ.get("WSL_DISTRO_NAME") or environ.get("WSL_INTEROP")),
        "capabilities": capabilities,
    }


def _windows_report(environ, helper_probe):
    helper_supported, helper_backend, helper_reason, _ = helper_probe(environ)
    reasons = {
        "workspace": "The WSL2 Bubblewrap workspace bridge is not implemented yet",
        "sandbox": "The WSL2 Bubblewrap sandbox bridge is not implemented yet",
    }
    capabilities = {
        "provider_usage": _capability(True, backend="windows-local"),
        "tui": _capability(True, backend="windows-ansi-vt"),
        "session_control": _capability(
            helper_supported,
            backend=helper_backend,
            reason=helper_reason,
        ),
        **{
            name: _capability(False, reason=reason)
            for name, reason in reasons.items()
        },
    }
    return {
        "report_version": REPORT_VERSION,
        "platform": "windows",
        "wsl": False,
        "capabilities": {name: capabilities[name] for name in CAPABILITY_NAMES},
    }


def _unsupported_report(sys_platform):
    reason = f"Unsupported platform: {sys_platform}"
    return {
        "report_version": REPORT_VERSION,
        "platform": "unsupported",
        "wsl": False,
        "capabilities": {
            name: _capability(False, reason=reason) for name in CAPABILITY_NAMES
        },
    }


def capability_report(
    os_name,
    sys_platform,
    has_module,
    which,
    environ=None,
    helper_probe=None,
):
    """Return current feature support from caller-supplied platform facts."""
    environ = {} if environ is None else environ
    if sys_platform.startswith("linux"):
        return _linux_report(has_module, which, environ)
    if os_name == "nt":
        if helper_probe is None:
            from ..windows_process import helper_capability

            helper_probe = helper_capability
        return _windows_report(environ, helper_probe)
    return _unsupported_report(sys_platform)
