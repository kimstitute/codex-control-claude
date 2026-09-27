"""Cross-platform default state paths."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def default_state_dir(environ=None, home=None, os_name=None, sys_platform=None):
    """Return the controller state directory for injected or current host facts."""
    environ = os.environ if environ is None else environ
    os_name = os.name if os_name is None else os_name
    sys_platform = sys.platform if sys_platform is None else sys_platform
    if os_name == "nt":
        base = environ.get("LOCALAPPDATA")
        if not base:
            profile = environ.get("USERPROFILE")
            if not profile:
                raise ValueError(
                    "LOCALAPPDATA or USERPROFILE is required for the Windows state directory."
                )
            base = str(Path(profile) / "AppData" / "Local")
        return Path(base) / "codex-control-claude" / "state"
    base = environ.get("XDG_STATE_HOME")
    if base:
        return Path(base) / "claude-control"
    home = Path.home() if home is None else Path(home)
    if sys_platform == "darwin":
        return home / "Library" / "Application Support" / "codex-control-claude" / "state"
    return home / ".local" / "state" / "claude-control"
