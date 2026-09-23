"""Portable allow-list selection for child process environments."""

from __future__ import annotations

from collections.abc import Iterable, Mapping


def select_environment(
    environ: Mapping[str, str],
    names: Iterable[str],
    *,
    case_insensitive: bool = False,
) -> dict[str, str]:
    """Select named variables, retaining the first spelling of duplicate keys."""
    selected: dict[str, str] = {}
    seen: set[str] = set()
    for name in names:
        if name not in environ:
            continue
        identity = name.casefold() if case_insensitive else name
        if identity in seen:
            continue
        seen.add(identity)
        selected[name] = environ[name]
    return selected
