"""Read the signed-in account's effective model catalog from Claude Code."""

from __future__ import annotations

import json
import subprocess

from .execution_settings import EFFORTS
from .model_settings import validate_model
from .runner import child_environment
from .store import ControlError

CONTRACT = "claude-control.model-catalog.v1"
SOURCE = "claude-code-agent-sdk-initialize"
REQUEST_ID = "claude-control-model-catalog"
MAX_OUTPUT_BYTES = 1024 * 1024


def _text(value, name, limit, *, empty=False):
    if not isinstance(value, str) or (not empty and not value):
        raise ControlError("invalid_model_catalog", f"Claude Code returned an invalid {name}.")
    if len(value) > limit or any(ord(character) < 32 for character in value):
        raise ControlError("invalid_model_catalog", f"Claude Code returned an invalid {name}.")
    return value


def _setting_supported(selector):
    try:
        validate_model(selector)
    except ValueError:
        return False
    return True


def normalize_catalog(models):
    """Validate and reduce the provider response without retaining account metadata."""
    if not isinstance(models, list) or not 1 <= len(models) <= 128:
        raise ControlError("invalid_model_catalog", "Claude Code returned no valid model catalog.")
    normalized = []
    seen = set()
    for model in models:
        if not isinstance(model, dict):
            raise ControlError("invalid_model_catalog", "Claude Code returned an invalid model.")
        selector = _text(model.get("value"), "model selector", 128)
        if selector in seen:
            raise ControlError("invalid_model_catalog", "Claude Code returned duplicate models.")
        seen.add(selector)
        resolved = _text(model.get("resolvedModel"), "resolved model", 128)
        display_name = _text(model.get("displayName"), "model display name", 120)
        description = _text(model.get("description", ""), "model description", 500, empty=True)
        effort_levels = model.get("supportedEffortLevels", [])
        if not isinstance(effort_levels, list) or len(effort_levels) > 16:
            raise ControlError(
                "invalid_model_catalog", "Claude Code returned invalid effort levels."
            )
        efforts = []
        for effort in effort_levels:
            effort = _text(effort, "effort level", 32)
            if effort not in efforts:
                efforts.append(effort)
        normalized.append(
            {
                "selector": selector,
                "resolved_model": resolved,
                "display_name": display_name,
                "description": description,
                "role_settings_compatible": _setting_supported(selector),
                "supports_effort": model.get("supportsEffort") is True,
                "supported_effort_levels": efforts,
                "configurable_effort_levels": [value for value in efforts if value in EFFORTS],
                "supports_adaptive_thinking": model.get("supportsAdaptiveThinking") is True,
                "supports_fast_mode": model.get("supportsFastMode") is True,
                "supports_auto_mode": model.get("supportsAutoMode") is True,
            }
        )
    return normalized


def _catalog_argv(binary):
    return [
        binary,
        "--output-format",
        "stream-json",
        "--verbose",
        "--system-prompt",
        "",
        "--tools",
        "",
        "--permission-mode",
        "dontAsk",
        "--safe-mode",
        "--disable-slash-commands",
        "--setting-sources",
        "",
        "--strict-mcp-config",
        "--mcp-config",
        '{"mcpServers":{}}',
        "--input-format",
        "stream-json",
    ]


def query_catalog(binary, cwd, timeout=15):
    """Use the public Agent SDK initialize exchange without sending a model prompt."""
    request = {
        "type": "control_request",
        "request_id": REQUEST_ID,
        "request": {"subtype": "initialize", "hooks": None},
    }
    try:
        completed = subprocess.run(
            _catalog_argv(binary),
            input=json.dumps(request, separators=(",", ":")) + "\n",
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            cwd=cwd,
            env=child_environment(),
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise ControlError(
            "model_catalog_unavailable", "Claude Code model catalog query timed out."
        ) from None
    if len(completed.stdout.encode("utf-8")) > MAX_OUTPUT_BYTES:
        raise ControlError(
            "invalid_model_catalog", "Claude Code model catalog response exceeded 1 MiB."
        )
    response = None
    for line in completed.stdout.splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        envelope = value.get("response") if isinstance(value, dict) else None
        if (
            value.get("type") == "control_response"
            and isinstance(envelope, dict)
            and envelope.get("request_id") == REQUEST_ID
        ):
            response = envelope
            break
    if completed.returncode != 0 or not isinstance(response, dict):
        raise ControlError(
            "model_catalog_unavailable", "Claude Code did not return a model catalog."
        )
    payload = response.get("response")
    if response.get("subtype") != "success" or not isinstance(payload, dict):
        raise ControlError(
            "model_catalog_unavailable", "Claude Code rejected the model catalog query."
        )
    models = normalize_catalog(payload.get("models"))
    return {
        "contract": CONTRACT,
        "source": SOURCE,
        "model_count": len(models),
        "models": models,
    }
