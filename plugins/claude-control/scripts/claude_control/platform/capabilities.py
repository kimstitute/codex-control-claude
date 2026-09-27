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


def _windows_report(environ, helper_probe, wsl_probe):
    helper_supported, helper_backend, helper_reason, _ = helper_probe(environ)
    wsl_result = (
        wsl_probe()
        if helper_supported
        else {"ready": False, "backend": None, "reason": helper_reason}
    )
    workspace_supported = helper_supported and bool(wsl_result.get("ready"))
    workspace_reason = None if workspace_supported else (
        wsl_result.get("reason") or helper_reason or "WSL2 Bubblewrap is unavailable"
    )
    workspace_backend = wsl_result.get("backend") if workspace_supported else None
    capabilities = {
        "provider_usage": _capability(True, backend="windows-local"),
        "tui": _capability(True, backend="windows-ansi-vt"),
        "session_control": _capability(
            helper_supported,
            backend=helper_backend,
            reason=helper_reason,
        ),
        "workspace": _capability(
            workspace_supported, backend=workspace_backend, reason=workspace_reason
        ),
        "sandbox": _capability(
            workspace_supported, backend=workspace_backend, reason=workspace_reason
        ),
    }
    return {
        "report_version": REPORT_VERSION,
        "platform": "windows",
        "wsl": False,
        "capabilities": {name: capabilities[name] for name in CAPABILITY_NAMES},
    }


def _macos_report(has_module, which):
    has_curses = bool(has_module("curses"))
    base_tools = {
        "ps": "/bin/ps",
        "sysctl": "/usr/sbin/sysctl",
        "ioreg": "/usr/sbin/ioreg",
    }
    required = tuple(
        name for name, path in base_tools.items() if not (which(name) or which(path))
    )
    session_supported = not required
    session_reason = (
        None if session_supported else "Missing macOS base-system tools: " + ", ".join(required)
    )
    workspace_reason = "No supported fail-closed macOS workspace sandbox backend is available"
    capabilities = {
        "provider_usage": _capability(True, backend="macos-local"),
        "tui": _capability(
            has_curses,
            backend="curses" if has_curses else None,
            reason=None if has_curses else "curses module is unavailable",
        ),
        "session_control": _capability(
            session_supported,
            backend="macos-posix-process-group" if session_supported else None,
            reason=session_reason,
        ),
        "workspace": _capability(False, reason=workspace_reason),
        "sandbox": _capability(False, reason=workspace_reason),
    }
    return {
        "report_version": REPORT_VERSION,
        "platform": "macos",
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
    wsl_probe=None,
):
    """Return current feature support from caller-supplied platform facts."""
    environ = {} if environ is None else environ
    if sys_platform.startswith("linux"):
        return _linux_report(has_module, which, environ)
    if os_name == "nt":
        if helper_probe is None:
            from ..windows_process import helper_capability

            helper_probe = helper_capability
        if wsl_probe is None:
            from ..windows_wsl_sandbox import probe as wsl_probe
        return _windows_report(environ, helper_probe, wsl_probe)
    if sys_platform == "darwin":
        return _macos_report(has_module, which)
    return _unsupported_report(sys_platform)
