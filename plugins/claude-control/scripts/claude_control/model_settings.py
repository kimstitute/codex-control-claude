"""Validated Claude model selectors and host-local per-role defaults."""

from __future__ import annotations

import re

from .execution_settings import validate_effort

CONTRACT = "claude-control.role-models.v1"
ROLES = ("executor", "researcher", "planner", "architect", "critic", "verifier")
FAMILY_ALIASES = {
    "sonnet": "claude-sonnet-",
    "opus": "claude-opus-",
    "haiku": "claude-haiku-",
    "fable": "claude-fable-",
}
LEGACY_DEFAULTS = {
    "executor": {"model": "sonnet"},
    "researcher": {"model": "sonnet"},
    "planner": {"model": "fable"},
    "architect": {"model": "fable"},
    "critic": {"model": "fable"},
    "verifier": {"model": "fable"},
}
_EXACT_MODEL_RE = re.compile(r"^claude-[a-z0-9]+(?:-[a-z0-9]+)*$")
_CONTEXT_SUFFIX = "[1m]"


def _base_selector(value):
    return value[: -len(_CONTEXT_SUFFIX)] if value.endswith(_CONTEXT_SUFFIX) else value


def validate_model(value):
    """Accept audited family aliases or a bounded exact Claude model identifier."""
    if not isinstance(value, str):
        raise ValueError("model must be a string")
    base = _base_selector(value)
    if len(value) <= 128 and (base in FAMILY_ALIASES or _EXACT_MODEL_RE.fullmatch(base)):
        return value
    raise ValueError(
        "model must be sonnet, opus, haiku, fable, an optional [1m] variant, "
        "or an exact identifier beginning claude-"
    )


def model_matches(selector, actual):
    """Return whether provider-reported model identity satisfies the frozen selector."""
    selector = validate_model(selector)
    if not isinstance(actual, str):
        return False
    selector = _base_selector(selector)
    actual = _base_selector(actual)
    prefix = FAMILY_ALIASES.get(selector)
    return actual.startswith(prefix) if prefix is not None else actual == selector


def normalize_defaults(value):
    """Normalize the complete, replace-only role defaults document."""
    if not isinstance(value, dict) or set(value) != {"contract", "roles"}:
        raise ValueError("role model settings must contain exactly contract and roles")
    if value["contract"] != CONTRACT:
        raise ValueError(f"role model settings contract must be {CONTRACT}")
    roles = value["roles"]
    if not isinstance(roles, dict) or set(roles) != set(ROLES):
        raise ValueError("role model settings must define every supported role exactly once")
    normalized = {}
    for role in ROLES:
        setting = roles[role]
        if not isinstance(setting, dict) or not set(setting).issubset({"model", "effort"}):
            raise ValueError(f"{role} setting may contain only model and effort")
        if "model" not in setting:
            raise ValueError(f"{role} setting requires model")
        result = {"model": validate_model(setting["model"])}
        if "effort" in setting:
            if setting["effort"] is None:
                raise ValueError(f"{role} effort cannot be null; omit it to use provider default")
            result["effort"] = validate_effort(setting["effort"])
        normalized[role] = result
    return {"contract": CONTRACT, "roles": normalized}


def built_in_defaults():
    return {role: dict(LEGACY_DEFAULTS[role]) for role in ROLES}
