"""AG-UI 1.0 events derived from the sanitized observation contract.

AG-UI is a live frontend protocol, not this controller's durable source of
truth.  Callers page the append-only observation ledger, convert those decoded
events here, and retain ledger cursors for replay.  Content-bearing AG-UI message,
reasoning and tool-call events are intentionally outside this adapter.
"""

from __future__ import annotations

import math

PROFILE = "claude-control.ag-ui.v1"
CUSTOM_NAME = "claude-control.observation"
ACTIVE = frozenset(("pending", "claimed", "launching", "running", "stopping"))
ERROR = frozenset(("failed", "cancelled", "interrupted", "unknown", "launch_failed"))
_PRIVATE_PARTS = frozenset(
    ("prompt", "result", "message", "policy", "path", "reason", "receipt", "account", "credential")
)


def _private(key):
    normalized = str(key).lower().replace("-", "_")
    return any(part in normalized for part in _PRIVATE_PARTS)


def _sanitize(value):
    if isinstance(value, dict):
        return {
            key: _sanitize(item)
            for key, item in sorted(value.items())
            if isinstance(key, str) and not _private(key)
        }
    if isinstance(value, list):
        return [_sanitize(item) for item in value]
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    raise ValueError("observation data contains a non-JSON value")


def _integer(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or value < 0 or not float(value).is_integer():
        return None
    return int(value)


def _count(source, *names):
    if not isinstance(source, dict):
        return None
    for name in names:
        value = _integer(source.get(name))
        if value is not None:
            return value
    return None


def _usage_item(source, model=None, fallback=None):
    source = source if isinstance(source, dict) else {}
    fallback = fallback if isinstance(fallback, dict) else {}
    base_input = _count(source, "inputTokens", "input_tokens")
    if base_input is None:
        base_input = _count(fallback, "input_tokens")
    cache_read = _count(
        source, "cacheReadInputTokens", "cache_read_input_tokens", "cachedInputTokens"
    )
    if cache_read is None:
        cache_read = _count(fallback, "cache_read_input_tokens")
    cache_write = _count(
        source,
        "cacheCreationInputTokens",
        "cache_creation_input_tokens",
        "cacheWriteInputTokens",
    )
    if cache_write is None:
        cache_write = _count(fallback, "cache_creation_input_tokens")
    output = _count(source, "outputTokens", "output_tokens")
    if output is None:
        output = _count(fallback, "output_tokens")
    details = fallback.get("output_tokens_details")
    reasoning = _count(source, "reasoningTokens", "thinkingTokens", "thinking_tokens")
    if reasoning is None:
        reasoning = _count(details, "thinking_tokens", "reasoning_tokens")

    item = {"provider": "anthropic"}
    if isinstance(model, str) and model:
        item["model"] = model
    if any(value is not None for value in (base_input, cache_read, cache_write)):
        item["inputTokens"] = (base_input or 0) + (cache_read or 0) + (cache_write or 0)
    if output is not None:
        item["outputTokens"] = output
    if "inputTokens" in item and "outputTokens" in item:
        item["totalTokens"] = item["inputTokens"] + item["outputTokens"]
    if reasoning is not None:
        item["reasoningTokens"] = reasoning
    if cache_read is not None:
        item["cachedInputTokens"] = cache_read
    if cache_write is not None:
        item["cacheWriteInputTokens"] = cache_write
    return item if len(item) > 1 else None


def token_usage(telemetry, models=None):
    """Return AG-UI numeric-only per-model usage without double counting subcounts."""
    if not isinstance(telemetry, dict):
        return None
    total = telemetry.get("usage")
    total = total if isinstance(total, dict) else {}
    per_model = telemetry.get("model_usage")
    per_model = per_model if isinstance(per_model, dict) else {}
    valid_models = sorted(
        (name, value)
        for name, value in per_model.items()
        if isinstance(name, str) and name and isinstance(value, dict)
    )
    if valid_models:
        single = len(valid_models) == 1
        usage = [
            _usage_item(value, model=name, fallback=total if single else None)
            for name, value in valid_models
        ]
    else:
        choices = sorted(value for value in (models or []) if isinstance(value, str) and value)
        usage = [_usage_item(total, model=choices[0] if choices else None)]
    result = [item for item in usage if item is not None]
    return result or None


def state_snapshot(snapshot):
    """Build one content-free AG-UI STATE_SNAPSHOT from a replay result."""
    if not isinstance(snapshot, dict):
        raise ValueError("observation snapshot must be an object")
    required = ("contract", "cursor", "fidelity", "nodes", "edges", "telemetry")
    if any(name not in snapshot for name in required):
        raise ValueError("observation snapshot is incomplete")
    state = {name: snapshot[name] for name in required}
    state["adapter_profile"] = PROFILE
    return {"type": "STATE_SNAPSHOT", "snapshot": _sanitize(state)}


def _metadata(cursor, thread_id=None, run_id=None):
    local = {"cursor": cursor, "profile": PROFILE}
    if thread_id is not None:
        local["threadId"] = thread_id
    if run_id is not None:
        local["runId"] = run_id
    return {"claude-control": local}


def _custom(event):
    return {
        "type": "CUSTOM",
        "name": CUSTOM_NAME,
        "value": _sanitize(event),
        "timestamp": event["effective_at"],
        "metadata": _metadata(event["cursor"]),
    }


def _lifecycle(event, telemetry):
    payload = event["payload"]
    run_id = payload.get("id")
    thread_id = payload.get("session_id")
    state = payload.get("state")
    if not isinstance(run_id, str) or not run_id:
        raise ValueError("run event has no run id")
    if not isinstance(thread_id, str) or not thread_id:
        raise ValueError("run event has no session id")
    common = {
        "timestamp": event["effective_at"],
        "metadata": _metadata(event["cursor"], thread_id, run_id),
    }
    usage = token_usage(telemetry, payload.get("models"))
    if event["kind"] == "node_created":
        return {"type": "RUN_STARTED", "threadId": thread_id, "runId": run_id, **common}
    if state == "completed":
        result = {"type": "RUN_FINISHED", "threadId": thread_id, "runId": run_id, **common}
        if usage is not None:
            result["usage"] = usage
        return result
    if state in ERROR:
        # RUN_ERROR has no threadId/runId fields in AG-UI 1.0.  The valid BaseEvent
        # metadata retains them so a multiplexed observation stream remains usable.
        result = {
            "type": "RUN_ERROR",
            "message": f"Agent run ended with state {state}.",
            "code": state,
            **common,
        }
        if usage is not None:
            result["usage"] = usage
        return result
    return _custom(event)


def events(decoded_events):
    """Convert decoded ledger events into a deterministic AG-UI event list."""
    if not isinstance(decoded_events, list):
        raise ValueError("decoded observation events must be a list")
    ordered = sorted(decoded_events, key=lambda event: event.get("cursor", -1))
    telemetry = {}
    for event in ordered:
        if not isinstance(event, dict):
            raise ValueError("decoded observation event must be an object")
        if event.get("kind") == "run_telemetry" and event.get("entity_kind") == "run":
            telemetry[event.get("entity_id")] = event.get("payload")

    converted = []
    previous = 0
    for event in ordered:
        cursor = event.get("cursor")
        if not isinstance(cursor, int) or isinstance(cursor, bool) or cursor <= previous:
            raise ValueError("decoded observation cursors must be positive and strictly increasing")
        previous = cursor
        timestamp = event.get("effective_at")
        if (
            isinstance(timestamp, bool)
            or not isinstance(timestamp, (int, float))
            or not math.isfinite(timestamp)
        ):
            raise ValueError("decoded observation event has an invalid timestamp")
        payload = event.get("payload")
        if not isinstance(payload, dict):
            raise ValueError("decoded observation event has no payload object")
        if (
            event.get("entity_kind") == "run"
            and event.get("kind") in ("node_created", "node_state")
        ):
            converted.append(_lifecycle(event, telemetry.get(event.get("entity_id"))))
        else:
            converted.append(_custom(event))
    return converted
