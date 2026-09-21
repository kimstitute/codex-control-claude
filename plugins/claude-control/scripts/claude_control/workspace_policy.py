"""Explicit, immutable authority for controller-mediated workspace operations."""

import re

from .store import ControlError

PROTECTED = {".git", ".claude", ".codex", ".omx", ".agents", ".env"}


def _invalid(message):
    raise ControlError("invalid_workspace_policy", message)


def _require_utf8(value, message):
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        _invalid(message)


def path(value, allow_directory=False):
    if not isinstance(value, str) or not value or len(value) > 240:
        _invalid("Expected a relative path of 1–240 characters.")
    _require_utf8(value, "Path must be valid UTF-8 text.")
    if any(ord(c) < 32 for c in value) or any(c in value for c in "\\*?[]"):
        _invalid("Path contains forbidden characters.")
    name = value[:-1] if allow_directory and value.endswith("/") else value
    parts = name.split("/")
    if any(p in ("", ".", "..") or p in PROTECTED or p.startswith(".env.") for p in parts):
        _invalid("Unsafe or protected path.")
    if parts[-1] in ("credentials.json", "auth.json") or parts[-1].endswith((".pem", ".key")):
        _invalid("Credential files cannot enter a workspace.")
    return value


def permits(paths, relative):
    try:
        path(relative)
    except ControlError:
        return False
    return any(
        p == "." or p == relative or (p.endswith("/") and relative.startswith(p)) for p in paths
    )


def check_access(policy, relative, write=False):
    if not permits(policy["write_paths" if write else "read_paths"], relative):
        raise ControlError(
            "workspace_path_denied", "Path is outside the explicit workspace policy."
        )
    return relative


def normalize_policy(value):
    required = {"version", "role", "read_paths", "write_paths", "checks"}
    optional = {"max_actions", "max_calls"}
    if (
        not isinstance(value, dict)
        or not required <= value.keys()
        or value.keys() - required - optional
    ):
        _invalid("Policy fields must match the workspace policy schema.")
    if (
        type(value["version"]) is not int
        or value["version"] != 1
        or value["role"] not in ("executor", "verifier")
    ):
        _invalid("Use policy version 1 and role executor or verifier.")
    result = {"version": 1, "role": value["role"]}
    for name in ("read_paths", "write_paths"):
        values = value[name]
        if (
            not isinstance(values, list)
            or not (1 if name == "read_paths" else 0) <= len(values) <= 64
        ):
            _invalid("Use 1–64 read paths and 0–64 write paths.")
        checked = [
            v if v == "." and name == "read_paths" else path(v, name == "read_paths")
            for v in values
        ]
        if len(set(checked)) != len(checked):
            _invalid("Duplicate paths are not allowed.")
        result[name] = sorted(checked)
    if any(not permits(result["read_paths"], p) for p in result["write_paths"]):
        _invalid("Every writable file must also be readable.")
    checks = value["checks"]
    if not isinstance(checks, dict) or len(checks) > 8:
        _invalid("Use at most eight named checks.")
    result["checks"] = {}
    for name, check in checks.items():
        if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", name):
            _invalid("Invalid check name.")
        if not isinstance(check, dict) or set(check) != {"argv", "timeout"}:
            _invalid("A check requires exact argv and timeout fields.")
        argv = check["argv"]
        if (
            not isinstance(argv, list)
            or not 1 <= len(argv) <= 32
            or any(not isinstance(a, str) or not a or len(a) > 1024 or "\0" in a for a in argv)
        ):
            _invalid("A check needs 1–32 bounded argv strings.")
        for argument in argv:
            _require_utf8(argument, "Check arguments must be valid UTF-8 text.")
        executable = argv[0].removeprefix("/usr/bin/")
        if (
            not argv[0].startswith("/usr/bin/")
            or not executable
            or "/" in executable
            or executable in (".", "..")
        ):
            _invalid("Check executable must be an absolute /usr/bin basename.")
        if type(check["timeout"]) is not int or not 1 <= check["timeout"] <= 60:
            _invalid("Check timeout must be an integer from 1 to 60 seconds.")
        result["checks"][name] = {"argv": list(argv), "timeout": check["timeout"]}
    result["checks"] = dict(sorted(result["checks"].items()))
    if result["role"] == "verifier" and (result["write_paths"] or result["checks"]):
        _invalid("Verifier policies are read-only and cannot execute commands.")
    for name, default, maximum in (("max_actions", 32, 100), ("max_calls", 8, 32)):
        limit = value.get(name, default)
        if type(limit) is not int or not 1 <= limit <= maximum:
            _invalid(f"{name} must be an integer from 1 to {maximum}.")
        result[name] = limit
    return result
