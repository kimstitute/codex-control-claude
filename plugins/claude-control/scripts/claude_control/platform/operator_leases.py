"""Host-wide leases for direct operator access to terminals and source trees."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from .host import private_dir


def root(environ=None, os_name=None) -> Path:
    """Return a store-independent, same-user lease directory on this host."""
    environ = os.environ if environ is None else environ
    os_name = os.name if os_name is None else os_name
    if os_name == "nt":
        base = environ.get("LOCALAPPDATA")
        if not base:
            profile = environ.get("USERPROFILE")
            if not profile:
                raise ValueError("LOCALAPPDATA or USERPROFILE is required for operator leases.")
            base = str(Path(profile) / "AppData" / "Local")
        return private_dir(Path(base) / "codex-control-claude" / "operator-leases")
    return private_dir(Path("/tmp") / f"codex-control-claude-operator-leases-{os.getuid()}")


def terminal(session_id: str) -> Path:
    digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
    return root() / f"terminal-{digest}.lock"


def project(project_path: str) -> Path:
    canonical = str(Path(project_path).resolve())
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return root() / f"project-{digest}.lock"
