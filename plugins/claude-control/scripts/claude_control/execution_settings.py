"""Strict controller settings and bounded local CLI capability probes."""

import functools
import subprocess
from pathlib import Path

EFFORTS = ("low", "medium", "high", "xhigh", "max")


def validate_effort(value):
    """None is internal absence; assignment JSON must reject explicit null separately."""
    if value is not None and (not isinstance(value, str) or value not in EFFORTS):
        raise ValueError(f"effort must be one of {', '.join(EFFORTS)}")
    return value


def binary_identity(binary):
    info = Path(binary).stat()
    return (str(binary), info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


@functools.lru_cache(maxsize=16)
def _help_support(identity, version, cwd):
    from .runner import child_environment

    result = subprocess.run(
        [identity[0], "--help"],
        capture_output=True,
        text=True,
        timeout=15,
        cwd=cwd,
        env=child_environment(),
    )
    return result.returncode == 0 and "--effort" in result.stdout.split()


def probe_effort(binary, cwd, *, fresh=False):
    """Cache by binary identity + reported version; never invoke inside a DB transaction."""
    from .runner import child_environment

    identity = binary_identity(binary)
    version = subprocess.run(
        [binary, "--version"],
        capture_output=True,
        text=True,
        timeout=15,
        cwd=cwd,
        env=child_environment(),
    )
    if version.returncode != 0:
        return identity, False
    probe = _help_support.__wrapped__ if fresh else _help_support
    supported = probe(identity, version.stdout.strip(), str(cwd))
    return identity, supported and identity == binary_identity(binary)
