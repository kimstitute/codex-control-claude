"""Deterministic OTLP/HTTP JSON export for replayed observation snapshots.

The exporter consumes only :func:`claude_control.observation.replay` output.  It
never reads operational tables and deliberately maps a small allow-list of
metadata and numeric usage fields.  Prompt, result, message, policy, path,
receipt, account and credential content therefore has no route into the export.

OpenTelemetry's GenAI agent conventions are still Development.  The resource
records the local profile used here instead of claiming stable conformance.
OpenInference attributes are carried on the same spans so compatible backends
can classify agent runs without a second trace format.
"""

from __future__ import annotations

import hashlib
import math

PROFILE = "claude-control.otel-genai-openinference.v1-development"
SCOPE_NAME = "claude-control.observation"
SCOPE_VERSION = "1"
TERMINAL = frozenset(
    ("completed", "failed", "cancelled", "interrupted", "unknown", "launch_failed")
)


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) and value >= 0 else None


def _token(value):
    number = _number(value)
    if number is None or not number.is_integer():
        return None
    return int(number)


def _identifier(prefix, value, length):
    digest = hashlib.sha256(f"{prefix}:{value}".encode("utf-8")).hexdigest()[:length]
    return digest if int(digest, 16) else "0" * (length - 1) + "1"


def _nanoseconds(value):
    # OTLP JSON follows ProtoJSON: fixed64 values, including timestamps, are strings.
    return str(int(value * 1_000_000_000))


def _any(value):
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, int):
        return {"intValue": str(value)}
    if isinstance(value, float):
        return {"doubleValue": value}
    return {"stringValue": str(value)}


def _attributes(values):
    return [{"key": key, "value": _any(values[key])} for key in sorted(values)]


def _usage(telemetry):
    usage = telemetry.get("usage") if isinstance(telemetry, dict) else None
    usage = usage if isinstance(usage, dict) else {}
    details = usage.get("output_tokens_details")
    details = details if isinstance(details, dict) else {}
    return {
        "input": _token(usage.get("input_tokens")),
        "cache_creation": _token(usage.get("cache_creation_input_tokens")),
        "cache_read": _token(usage.get("cache_read_input_tokens")),
        "output": _token(usage.get("output_tokens")),
        "reasoning": _token(details.get("thinking_tokens")),
    }


def _model(run):
    models = run.get("models")
    if not isinstance(models, list):
        return None
    candidates = sorted(value for value in models if isinstance(value, str) and value)
    return candidates[0] if candidates else None


def _span(run_id, run, session, telemetry):
    state = run.get("state")
    started, finished = _number(run.get("started")), _number(run.get("finished"))
    if state not in TERMINAL:
        return None, "non_terminal"
    if started is None or finished is None:
        return None, "missing_timestamps"
    if finished < started:
        return None, "invalid_timestamps"
    if not isinstance(session, dict):
        return None, "missing_session"

    role = session.get("role") if isinstance(session.get("role"), str) else "agent"
    name = session.get("name") if isinstance(session.get("name"), str) else role
    requested = session.get("model") if isinstance(session.get("model"), str) else None
    actual = _model(run)
    tokens = _usage(telemetry)
    attrs = {
        "agent.name": name,
        "claude_control.agent.role": role,
        "gen_ai.agent.id": session.get("id", run.get("session_id")),
        "gen_ai.agent.name": name,
        "gen_ai.operation.name": "invoke_agent",
        "gen_ai.provider.name": "anthropic",
        "graph.node.id": run_id,
        "llm.provider": "anthropic",
        "llm.system": "anthropic",
        "openinference.span.kind": "AGENT",
        "session.id": session.get("id", run.get("session_id")),
    }
    if requested:
        attrs["gen_ai.request.model"] = requested
        attrs["llm.model_name"] = requested
    if actual:
        attrs["gen_ai.response.model"] = actual
        attrs["llm.response.model_name"] = actual
    effort = run.get("effort") or session.get("effort")
    if isinstance(effort, str) and effort:
        attrs["claude_control.agent.effort"] = effort

    raw_input = tokens["input"]
    cache_creation = tokens["cache_creation"]
    cache_read = tokens["cache_read"]
    output = tokens["output"]
    reasoning = tokens["reasoning"]
    if raw_input is not None:
        attrs["claude_control.usage.uncached_input_tokens"] = raw_input
    if output is not None:
        attrs["gen_ai.usage.output_tokens"] = output
        attrs["llm.token_count.completion"] = output
    if cache_read is not None:
        attrs["gen_ai.usage.cache_read.input_tokens"] = cache_read
        attrs["llm.token_count.cache_read"] = cache_read
    if cache_creation is not None:
        attrs["gen_ai.usage.cache_creation.input_tokens"] = cache_creation
        attrs["llm.token_count.cache_write"] = cache_creation
    if reasoning is not None:
        attrs["gen_ai.usage.reasoning.output_tokens"] = reasoning
        attrs["llm.token_count.reasoning"] = reasoning

    # Claude reports non-cached input and cache read/write separately.  OpenInference
    # expects a prompt total, so fold those three counters together exactly once.
    prompt_parts = (raw_input, cache_creation, cache_read)
    if any(value is not None for value in prompt_parts):
        prompt = sum(value or 0 for value in prompt_parts)
        attrs["gen_ai.usage.input_tokens"] = prompt
        attrs["llm.token_count.prompt"] = prompt
        if output is not None:
            attrs["llm.token_count.total"] = prompt + output

    cost = _number(telemetry.get("provider_cost_usd")) if isinstance(telemetry, dict) else None
    if cost is not None:
        attrs["claude_control.provider.cost_usd"] = cost
        attrs["llm.cost.total"] = cost
    duration = _number(telemetry.get("duration_ms")) if isinstance(telemetry, dict) else None
    if duration is not None:
        attrs["claude_control.run.duration_ms"] = duration

    failed = state != "completed"
    if failed:
        attrs["error.type"] = f"claude_control.run.{state}"
    return (
        {
            "traceId": _identifier("session", session.get("id", run.get("session_id")), 32),
            "spanId": _identifier("run", run_id, 16),
            "name": f"invoke_agent {role}",
            "kind": "SPAN_KIND_INTERNAL",
            "startTimeUnixNano": _nanoseconds(started),
            "endTimeUnixNano": _nanoseconds(finished),
            "attributes": _attributes(attrs),
            "status": {"code": "STATUS_CODE_ERROR" if failed else "STATUS_CODE_OK"},
        },
        None,
    )


def build_export(snapshot):
    """Return a valid OTLP request plus separate deterministic omission metadata."""
    if not isinstance(snapshot, dict):
        raise ValueError("observation snapshot must be an object")
    nodes = snapshot.get("nodes")
    telemetry = snapshot.get("telemetry")
    if not isinstance(nodes, dict) or not isinstance(telemetry, dict):
        raise ValueError("observation snapshot is missing nodes or telemetry")
    runs = nodes.get("run", {})
    sessions = nodes.get("session", {})
    if not isinstance(runs, dict) or not isinstance(sessions, dict):
        raise ValueError("observation snapshot run and session nodes must be objects")

    spans, omitted = [], []
    for run_id in sorted(runs):
        run = runs[run_id]
        if not isinstance(run, dict):
            omitted.append({"run_id": run_id, "cause": "invalid_run"})
            continue
        session_id = run.get("session_id")
        session = sessions.get(session_id) if isinstance(session_id, str) else None
        span, cause = _span(run_id, run, session, telemetry.get(run_id, {}))
        if span is None:
            omitted.append({"run_id": run_id, "state": run.get("state"), "cause": cause})
        else:
            spans.append(span)

    resource = {
        "service.name": "claude-control",
        "claude_control.observation.contract": snapshot.get("contract", "unknown"),
        "claude_control.observation.cursor": int(snapshot.get("cursor", 0)),
        "claude_control.observation.fidelity": snapshot.get("fidelity", "unknown"),
        "claude_control.telemetry.profile": PROFILE,
    }
    document = {
        "resourceSpans": [
            {
                "resource": {"attributes": _attributes(resource)},
                "scopeSpans": [
                    {
                        "scope": {"name": SCOPE_NAME, "version": SCOPE_VERSION},
                        "spans": spans,
                    }
                ],
            }
        ]
    }
    return {
        "document": document,
        "metadata": {
            "profile": PROFILE,
            "gen_ai_semantic_conventions_status": "Development",
            "exported_run_count": len(spans),
            "omitted_run_count": len(omitted),
            "omitted_runs": omitted,
        },
    }


def otlp_document(snapshot):
    """Return only the ExportTraceServiceRequest-compatible OTLP JSON document."""
    return build_export(snapshot)["document"]
