"""Bounded, opt-in local transcript inspection for the interactive viewer.

The durable observation, AG-UI, and OTLP contracts remain content-free. This
adapter is a separate local-only surface and never contacts a service.
"""

from __future__ import annotations

import hashlib
import heapq
import json
import math
import os
import time
from collections import deque
from datetime import datetime
from pathlib import Path

from . import observation, observation_agui, terminal_cli
from .store import ControlError

PROTOCOL = "claude-control.local-sessions.v1"
DETAIL_TYPE = "CLAUDE_CONTROL_DETAIL_SNAPSHOT"
MAX_FILES = 256
MAX_BYTES = 4 * 1024 * 1024
MAX_HEAD_BYTES = 256 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024
MAX_RECORDS = 4_000
MAX_HEAD_RECORDS = 128
MAX_TEXT = 4_000


def _text(value, limit=MAX_TEXT):
    if isinstance(value, str):
        return value[:limit]
    if isinstance(value, (int, float, bool)):
        return str(value)
    return ""


def _stamp(value, fallback=0.0):
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
        return float(value)
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    return fallback


def _content(value):
    if isinstance(value, str):
        return _text(value)
    if not isinstance(value, list):
        return ""
    parts = []
    for item in value[:64]:
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, dict) and item.get("type") in (
            "text",
            "input_text",
            "output_text",
            "summary_text",
        ):
            parts.append(_text(item.get("text")))
        elif isinstance(item, dict) and item.get("type") == "thinking":
            parts.append(_text(item.get("thinking")))
    return _text("\n".join(part for part in parts if part))


def _summary(value):
    if not isinstance(value, dict):
        return ""
    for name in ("summary", "description", "title", "name"):
        text = _text(value.get(name), 600)
        if text:
            return text.replace("\n", " ")
    return ""


def _path_key(path):
    return hashlib.sha256(str(path.resolve()).encode()).hexdigest()[:12]


def _files(root):
    root = Path(root).expanduser()
    try:
        if not root.is_dir():
            return [], {"truncated": False, "read_errors": 0}
        resolved_root = root.resolve(strict=True)
    except OSError:
        return [], {"truncated": False, "read_errors": 1}
    files, read_errors, discovered = [], 0, 0
    try:
        for path in root.rglob("*.jsonl"):
            try:
                resolved = path.resolve(strict=True)
                if (
                    path.is_symlink()
                    or not resolved.is_relative_to(resolved_root)
                    or not path.is_file()
                ):
                    continue
                modified = path.stat().st_mtime
            except OSError:
                read_errors += 1
                continue
            discovered += 1
            item = (modified, str(path), path)
            if len(files) < MAX_FILES:
                heapq.heappush(files, item)
            elif item > files[0]:
                heapq.heapreplace(files, item)
    except OSError:
        read_errors += 1
    files.sort(reverse=True)
    return [path for _, _, path in files], {
        "truncated": discovered > MAX_FILES,
        "read_errors": read_errors,
    }


def _mark_coverage(source, coverage, context):
    source["partial"] = source["partial"] or bool(
        coverage.get("malformed") or coverage.get("read_error") or coverage.get("read_errors")
    )
    source["truncated"] = source["truncated"] or bool(coverage.get("truncated"))
    if coverage.get("read_error"):
        message = f"{context}: {coverage['read_error']}"
        source["read_error"] = _text(
            "; ".join(filter(None, (source.get("read_error"), message))), 600
        )
    elif coverage.get("read_errors"):
        message = f"{context}: {coverage['read_errors']} unreadable path(s)"
        source["read_error"] = _text(
            "; ".join(filter(None, (source.get("read_error"), message))), 600
        )


def _records(path):
    head, tail = [], deque(maxlen=MAX_RECORDS)
    malformed, truncated, read_error = 0, False, None

    def decode(raw):
        nonlocal malformed
        try:
            value = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            malformed += 1
            return None
        return value if isinstance(value, dict) else None

    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            if size > MAX_BYTES:
                while handle.tell() < MAX_HEAD_BYTES and len(head) < MAX_HEAD_RECORDS:
                    raw = handle.readline()
                    if not raw:
                        break
                    value = decode(raw)
                    if value is not None:
                        head.append(value)
                head_end = handle.tell()
                tail_start = max(head_end, size - MAX_BYTES)
                if tail_start > head_end:
                    handle.seek(tail_start)
                    handle.readline()
                    truncated = True
            for raw in handle:
                value = decode(raw)
                if value is not None:
                    if len(tail) == MAX_RECORDS:
                        truncated = True
                    tail.append(value)
    except OSError as error:
        read_error = _text(str(error), 300) or error.__class__.__name__
    rows = head + list(tail)
    return rows, {
        "malformed": malformed,
        "truncated": truncated,
        "read_error": read_error,
    }


def _session_id(rows, path, provider):
    names = (
        ("sessionId", "parentSessionId", "session_id", "conversation_id")
        if provider == "claude"
        else ("session_id", "id", "thread_id")
    )
    for record in rows:
        payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
        for source in (record, payload):
            for name in names:
                value = _text(source.get(name), 256)
                if value:
                    return value
    return path.stem


def _source(provider, path, session_id, detail):
    return {
        "selector": f"{provider}:{session_id}@{_path_key(path)}",
        "provider": provider,
        "session_id": session_id,
        "title": None,
        "project": None,
        "mode": None,
        "permission_mode": None,
        "last_prompt": None,
        "queued_ops": 0,
        "file_edits": 0,
        "partial": bool(detail["malformed"] or detail.get("read_error")),
        "truncated": bool(detail["truncated"]),
        "read_error": detail.get("read_error"),
    }


def _agent(owner, session_id):
    return {
        "key": owner,
        "agent_id": session_id,
        "parent_key": None,
        "run_id": None,
        "session_id": session_id,
        "role": "agent",
        "model": None,
        "description": None,
        "prompt": None,
        "reasoning": None,
        "started": None,
        "finished": None,
    }


def _usage_count(value):
    return (
        value
        if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 2**53
        else None
    )


def _claude_usage(rows, coverage):
    fields = (
        "input_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
        "output_tokens",
        "thinking_tokens",
    )
    responses = {}
    skipped = {"unkeyed": 0, "invalid": 0, "conflicting": 0, "synthetic": 0}
    missing = {name: 0 for name in fields}
    for row in rows:
        if row.get("type") != "assistant":
            continue
        message = row.get("message") if isinstance(row.get("message"), dict) else {}
        usage = message.get("usage")
        if not isinstance(usage, dict):
            continue
        request_id, message_id = row.get("requestId"), message.get("id")
        if not all(isinstance(value, str) and value for value in (request_id, message_id)):
            skipped["unkeyed"] += 1
            continue
        model = message.get("model")
        if model == "<synthetic>":
            skipped["synthetic"] += 1
            continue
        if not isinstance(model, str) or not model:
            model = "unknown"
        values = {}
        invalid = False
        for name in (
            "input_tokens",
            "cache_creation_input_tokens",
            "cache_read_input_tokens",
            "output_tokens",
        ):
            if name not in usage:
                missing[name] += 1
                values[name] = 0
                continue
            values[name] = _usage_count(usage.get(name))
            invalid = invalid or values[name] is None
        details = (
            usage.get("output_tokens_details")
            if isinstance(usage.get("output_tokens_details"), dict)
            else {}
        )
        if "thinking_tokens" not in details:
            missing["thinking_tokens"] += 1
            values["thinking_tokens"] = 0
        else:
            values["thinking_tokens"] = _usage_count(details.get("thinking_tokens"))
            invalid = invalid or values["thinking_tokens"] is None
        if invalid:
            skipped["invalid"] += 1
            continue
        cost = row.get("costUSD")
        cost = (
            float(cost)
            if isinstance(cost, (int, float))
            and not isinstance(cost, bool)
            and math.isfinite(cost)
            and cost >= 0
            else None
        )
        key = (request_id, message_id)
        current = responses.get(key)
        candidate = {"model": model, "values": values, "cost": cost}
        if current is None:
            responses[key] = candidate
            continue
        stable = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
        if current["model"] != model or any(
            current["values"][name] != values[name] for name in stable
        ):
            skipped["conflicting"] += 1
            continue
        for name in ("output_tokens", "thinking_tokens"):
            if values[name] < current["values"][name]:
                skipped["conflicting"] += 1
            else:
                current["values"][name] = values[name]
        if cost is not None:
            current["cost"] = cost
    if not responses:
        return None
    totals = {name: 0 for name in fields}
    by_model = {}
    for item in responses.values():
        model_totals = by_model.setdefault(item["model"], {name: 0 for name in fields})
        for name in fields:
            totals[name] += item["values"][name]
            model_totals[name] += item["values"][name]
    costs = [item["cost"] for item in responses.values()]
    reported_cost = all(cost is not None for cost in costs)
    complete = (
        not any(skipped.values())
        and not any(missing.values())
        and not any(coverage.get(name) for name in ("malformed", "truncated", "read_error"))
    )
    return {
        "source": "claude_transcript",
        "complete": complete,
        "responses": len(responses),
        "totals": totals,
        "by_model": by_model,
        "fields_missing": {name: count for name, count in missing.items() if count},
        "skipped": skipped,
        "cost": {
            "status": "reported" if reported_cost else "unavailable",
            "usd": sum(costs) if reported_cost else None,
        },
    }


def _usage_telemetry(local_usage):
    totals = local_usage["totals"]
    return {
        "usage": {
            "input_tokens": totals["input_tokens"],
            "cache_creation_input_tokens": totals["cache_creation_input_tokens"],
            "cache_read_input_tokens": totals["cache_read_input_tokens"],
            "output_tokens": totals["output_tokens"],
            "output_tokens_details": {"thinking_tokens": totals["thinking_tokens"]},
        },
        "provider_cost_usd": local_usage["cost"]["usd"],
        "cost_status": local_usage["cost"]["status"],
        "complete": local_usage["complete"],
    }


def _touch(agent, timestamp):
    agent["started"] = timestamp if agent["started"] is None else min(agent["started"], timestamp)
    agent["finished"] = max(agent["finished"] or timestamp, timestamp)


def _tool_starts(content, owner, timestamp, tools, events):
    if not isinstance(content, list):
        return
    for item in content:
        if not isinstance(item, dict) or item.get("type") not in (
            "tool_use",
            "tool_call",
            "function_call",
        ):
            continue
        tool_id = _text(item.get("id") or item.get("call_id") or f"tool-{len(tools) + 1}", 256)
        name = _text(item.get("name") or item.get("tool_name") or "tool", 256)
        tools.append(
            {
                "id": tool_id,
                "owner_key": owner,
                "name": name,
                "summary": _summary(item.get("input")) or None,
                "state": "pending",
                "started": timestamp,
                "finished": None,
            }
        )
        events.append(
            {"kind": "tool_start", "owner_key": owner, "timestamp": timestamp, "summary": name}
        )


def _tool_end(record, owner, timestamp, tools, events):
    tool_id = _text(record.get("tool_use_id") or record.get("call_id"), 256)
    if not tool_id:
        return
    tool = next((item for item in reversed(tools) if item["id"] == tool_id), None)
    if tool is None:
        return
    failed = bool(record.get("is_error") or record.get("error"))
    tool["state"] = "error" if failed else "ok"
    tool["finished"] = timestamp
    events.append(
        {
            "kind": "failure" if failed else "tool_end",
            "owner_key": owner,
            "timestamp": timestamp,
            "summary": tool["name"],
        }
    )


def _parse_claude(path, rows, detail):
    root_session_id = _session_id(rows, path, "claude")
    agent_id = next(
        (_text(row.get("agentId"), 256) for row in rows if _text(row.get("agentId"), 256)),
        root_session_id,
    )
    owner = f"session:{agent_id}"
    source, agent = _source("claude", path, agent_id, detail), _agent(owner, root_session_id)
    agent["agent_id"] = agent_id
    if agent_id != root_session_id:
        agent["parent_key"] = f"session:{root_session_id}"
        agent["role"] = "subagent"
    tools, events = [], []
    local_usage = _claude_usage(rows, detail)
    if local_usage is not None:
        agent["local_usage"] = local_usage
    for index, row in enumerate(rows):
        kind = _text(row.get("type"), 128)
        timestamp = _stamp(row.get("timestamp"), float(index))
        _touch(agent, timestamp)
        message = row.get("message") if isinstance(row.get("message"), dict) else {}
        content = message.get("content")
        source["project"] = source["project"] or _text(row.get("cwd")) or None
        source["mode"] = source["mode"] or _text(row.get("entrypoint")) or None
        source["permission_mode"] = (
            source["permission_mode"] or _text(row.get("permissionMode")) or None
        )
        if kind == "user":
            prompt = _content(content)
            if prompt:
                agent["prompt"] = source["last_prompt"] = prompt
                source["title"] = prompt.splitlines()[0][:120]
                events.append(
                    {
                        "kind": "prompt",
                        "owner_key": owner,
                        "timestamp": timestamp,
                        "summary": "user prompt",
                        "text": prompt,
                    }
                )
            if isinstance(content, list):
                for item in content:
                    if isinstance(item, dict) and item.get("type") in (
                        "tool_result",
                        "tool_response",
                    ):
                        _tool_end(item, owner, timestamp, tools, events)
        elif kind == "assistant":
            agent["model"] = _text(message.get("model") or row.get("model")) or agent["model"]
            response = _content(content)
            agent["reasoning"] = response or agent["reasoning"]
            if response:
                events.append(
                    {
                        "kind": "response",
                        "owner_key": owner,
                        "timestamp": timestamp,
                        "summary": "assistant response",
                        "text": response,
                    }
                )
            _tool_starts(content, owner, timestamp, tools, events)
        elif kind == "queue-operation":
            source["queued_ops"] += 1
        elif kind in ("error", "failure"):
            events.append(
                {"kind": "failure", "owner_key": owner, "timestamp": timestamp, "summary": kind}
            )
        if kind == "file-history-snapshot":
            source["file_edits"] += 1
        if row.get("isSidechain"):
            agent["role"] = "subagent"
    return {"source": source, "agents": [agent], "tools": tools, "events": events}


def _parse_codex(path, rows, detail):
    session_id = _session_id(rows, path, "codex")
    owner = f"session:{session_id}"
    source, agent = _source("codex", path, session_id, detail), _agent(owner, session_id)
    tools, events = [], []
    for index, row in enumerate(rows):
        timestamp = _stamp(row.get("timestamp"), float(index))
        _touch(agent, timestamp)
        kind = row.get("type")
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        payload_kind = _text(payload.get("type"), 128)
        if kind == "session_meta":
            source["project"] = _text(payload.get("cwd")) or source["project"]
            source["mode"] = (
                _text(payload.get("originator") or payload.get("source")) or source["mode"]
            )
            agent["role"] = _text(payload.get("agent_role")) or agent["role"]
            agent["description"] = _text(payload.get("agent_nickname")) or agent["description"]
            parent = _text(payload.get("parent_thread_id"))
            if parent:
                agent["parent_key"] = f"session:{parent}"
        elif kind == "turn_context":
            agent["model"] = _text(payload.get("model")) or agent["model"]
            source["permission_mode"] = (
                _text(payload.get("approval_policy")) or source["permission_mode"]
            )
        elif kind == "response_item":
            text = _content(payload.get("content"))
            if payload.get("role") == "user" and text:
                agent["prompt"] = source["last_prompt"] = text
                source["title"] = text.splitlines()[0][:120]
                events.append(
                    {
                        "kind": "prompt",
                        "owner_key": owner,
                        "timestamp": timestamp,
                        "summary": "user prompt",
                        "text": text,
                    }
                )
            elif payload.get("role") == "assistant" and text:
                agent["reasoning"] = text
                events.append(
                    {
                        "kind": "response",
                        "owner_key": owner,
                        "timestamp": timestamp,
                        "summary": "assistant response",
                        "text": text,
                    }
                )
            if payload_kind in ("function_call", "custom_tool_call"):
                tool_id = _text(
                    payload.get("call_id") or payload.get("id") or f"tool-{len(tools) + 1}"
                )
                name = _text(payload.get("name") or payload.get("tool_name") or "tool")
                tools.append(
                    {
                        "id": tool_id,
                        "owner_key": owner,
                        "name": name,
                        "summary": None,
                        "state": "pending",
                        "started": timestamp,
                        "finished": None,
                    }
                )
                events.append(
                    {
                        "kind": "tool_start",
                        "owner_key": owner,
                        "timestamp": timestamp,
                        "summary": name,
                    }
                )
            elif payload_kind in ("function_call_output", "custom_tool_call_output"):
                _tool_end(payload, owner, timestamp, tools, events)
            elif payload_kind == "agent_message":
                response = _content(payload.get("content")) or _text(
                    payload.get("message") or payload.get("text")
                )
                if response:
                    agent["reasoning"] = response
                    events.append(
                        {
                            "kind": "response",
                            "owner_key": owner,
                            "timestamp": timestamp,
                            "summary": "agent message",
                            "text": response,
                        }
                    )
            elif payload_kind == "reasoning":
                reasoning = _content(payload.get("summary")) or _text(payload.get("text"))
                if reasoning:
                    agent["reasoning"] = reasoning
                    events.append(
                        {
                            "kind": "reasoning",
                            "owner_key": owner,
                            "timestamp": timestamp,
                            "summary": "agent reasoning",
                            "text": reasoning,
                        }
                    )
        elif kind == "event_msg" and payload_kind in ("agent_message", "agent_reasoning"):
            agent["reasoning"] = (
                _text(payload.get("message") or payload.get("text")) or agent["reasoning"]
            )
            if agent["reasoning"]:
                events.append(
                    {
                        "kind": "reasoning" if payload_kind == "agent_reasoning" else "response",
                        "owner_key": owner,
                        "timestamp": timestamp,
                        "summary": payload_kind.replace("agent_", "agent "),
                        "text": agent["reasoning"],
                    }
                )
        if payload_kind in ("collab_agent_spawn_begin", "collab_agent_spawn_end"):
            events.append(
                {
                    "kind": "spawn",
                    "owner_key": owner,
                    "timestamp": timestamp,
                    "summary": "agent spawn",
                }
            )
        if "error" in payload_kind or "fail" in payload_kind:
            events.append(
                {
                    "kind": "failure",
                    "owner_key": owner,
                    "timestamp": timestamp,
                    "summary": payload_kind,
                }
            )
    return {"source": source, "agents": [agent], "tools": tools, "events": events}


def _parse(provider, path, rows, detail):
    if provider == "claude":
        return _parse_claude(path, rows, detail)
    return _parse_codex(path, rows, detail)


def _native_catalog(provider, root):
    sessions = []
    stats = {"files": 0, "malformed": 0, "read_errors": 0, "truncated": False}
    consumed = 0
    paths, scan = _files(root)
    stats["read_errors"] += scan["read_errors"]
    stats["truncated"] = scan["truncated"]
    for path in paths:
        try:
            file_stat = path.stat()
        except OSError:
            stats["read_errors"] += 1
            continue
        budget = min(file_stat.st_size, MAX_BYTES + MAX_HEAD_BYTES)
        if consumed + budget > MAX_TOTAL_BYTES:
            stats["truncated"] = True
            break
        consumed += budget
        stats["files"] += 1
        rows, detail = _records(path)
        stats["malformed"] += detail["malformed"]
        stats["read_errors"] += int(bool(detail.get("read_error")))
        stats["truncated"] = stats["truncated"] or detail["truncated"]
        if not rows:
            continue
        parsed = _parse(provider, path, rows, detail)
        source = parsed["source"]
        agent = parsed["agents"][0]
        sessions.append(
            {
                "selector": source["selector"],
                "provider": provider,
                "session_id": source["session_id"],
                "title": source["title"] or source["last_prompt"],
                "project": source["project"],
                "file": str(path.resolve()),
                "modified": file_stat.st_mtime,
                "live": time.time() - file_stat.st_mtime < 30,
                "agents": len(parsed["agents"]),
                "events": len(parsed["events"]),
                "_key": agent["key"],
                "_parent_key": agent.get("parent_key"),
            }
        )
    relationships = {item["_key"]: item.get("_parent_key") for item in sessions if item.get("_key")}
    for session in sessions:
        connected = {session["_key"]}
        changed = True
        while changed:
            changed = False
            for key, parent in relationships.items():
                if key in connected or parent in connected:
                    before = len(connected)
                    connected.add(key)
                    if parent:
                        connected.add(parent)
                    changed = changed or len(connected) != before
        session["agents"] = len(connected)
        session.pop("_key", None)
        session.pop("_parent_key", None)
    return sessions, stats


def catalog(*, claude_root=None, codex_root=None, store=None, source="all"):
    """Return selectable sessions without transcript bodies."""
    sessions = []
    stats = {"files": 0, "malformed": 0, "read_errors": 0, "truncated": False}
    if source in ("all", "managed") and store is not None:
        for run in reversed(store.list_all().get("runs", [])):
            sessions.append(
                {
                    "selector": f"managed:{run['id']}",
                    "provider": "managed",
                    "session_id": run.get("session_id"),
                    "title": run.get("role") or run.get("model") or "managed run",
                    "project": None,
                    "file": None,
                    "modified": run.get("finished") or run.get("started") or run.get("created"),
                    "live": run.get("status") in {"pending", "claimed", "launching", "running"},
                    "agents": 1,
                    "events": None,
                }
            )
    terminal_by_session = {}
    if store is not None and source in ("all", "claude", "native_claude"):
        try:
            terminal_by_session = {
                item["session_id"]: item for item in terminal_cli.catalog(store)["terminals"]
            }
        except ControlError as error:
            stats["terminal_error"] = _text(str(error), 300)
    roots = (
        ("claude", claude_root or Path.home() / ".claude" / "projects"),
        ("codex", codex_root or Path.home() / ".codex" / "sessions"),
    )
    for provider, root in roots:
        aliases = {provider, "native_claude" if provider == "claude" else provider}
        if source != "all" and source not in aliases:
            continue
        found, detail = _native_catalog(provider, root)
        if provider == "claude":
            for item in found:
                terminal = terminal_by_session.get(item.get("session_id"))
                if terminal:
                    item.update(
                        live=terminal.get("status") in {"idle", "running", "active"},
                        terminal_id=terminal.get("id"),
                        terminal_status=terminal.get("status"),
                        terminal_kind=terminal.get("kind"),
                        terminal_attachable=terminal.get("attachable", False),
                    )
        sessions.extend(found)
        stats["files"] += detail["files"]
        stats["malformed"] += detail["malformed"]
        stats["read_errors"] += detail["read_errors"]
        stats["truncated"] = stats["truncated"] or detail["truncated"]
    known_claude = {item.get("session_id") for item in sessions if item.get("provider") == "claude"}
    for terminal in terminal_by_session.values():
        if terminal["session_id"] in known_claude:
            continue
        sessions.append(
            {
                "selector": f"terminal:{terminal['session_id']}",
                "provider": "claude",
                "session_id": terminal["session_id"],
                "title": terminal.get("name") or "Claude terminal",
                "project": terminal.get("cwd"),
                "file": None,
                "modified": terminal.get("started_at") or 0,
                "live": terminal.get("status") in {"idle", "running", "active"},
                "agents": 1,
                "events": 0,
                "terminal_id": terminal.get("id"),
                "terminal_status": terminal.get("status"),
                "terminal_kind": terminal.get("kind"),
                "terminal_attachable": terminal.get("attachable", False),
            }
        )
    sessions.sort(key=lambda item: float(item.get("modified") or 0), reverse=True)
    return {"protocol": PROTOCOL, "version": 1, "sessions": sessions[:MAX_RECORDS], "stats": stats}


def _merge_details(primary, related):
    agents = {item["key"]: item for item in primary["agents"]}
    tools = {(item["owner_key"], item["id"]): item for item in primary["tools"]}
    events = {
        (
            item.get("owner_key"),
            item["timestamp"],
            item["kind"],
            item.get("summary"),
            item.get("text"),
        ): item
        for item in primary["events"]
    }
    for detail in related:
        agents.update((item["key"], item) for item in detail["agents"])
        tools.update(((item["owner_key"], item["id"]), item) for item in detail["tools"])
        events.update(
            (
                (
                    item.get("owner_key"),
                    item["timestamp"],
                    item["kind"],
                    item.get("summary"),
                    item.get("text"),
                ),
                item,
            )
            for item in detail["events"]
        )
        primary["source"]["partial"] = primary["source"]["partial"] or detail["source"]["partial"]
        primary["source"]["truncated"] = (
            primary["source"]["truncated"] or detail["source"]["truncated"]
        )
        if detail["source"].get("read_error"):
            _mark_coverage(
                primary["source"],
                {"read_error": detail["source"]["read_error"]},
                "related transcript",
            )
    primary["agents"] = sorted(
        agents.values(), key=lambda item: (item.get("started") or 0.0, item["key"])
    )
    primary["tools"] = sorted(
        tools.values(), key=lambda item: (item.get("started") or 0.0, item["id"])
    )
    primary["events"] = sorted(events.values(), key=lambda item: (item["timestamp"], item["kind"]))
    return primary


def _related_claude(path, primary):
    agent = primary["agents"][0]
    root_session = agent.get("session_id") or primary["source"]["session_id"]
    project_dir = path.parent.parent.parent if path.parent.name == "subagents" else path.parent
    main = project_dir / f"{root_session}.jsonl"
    subagents = project_dir / root_session / "subagents"
    candidates = [main]
    children, scan = _files(subagents)
    _mark_coverage(primary["source"], scan, "subagent scan")
    candidates.extend(children)
    related, consumed = [], 0
    for candidate in candidates:
        if candidate == path:
            continue
        try:
            size = candidate.stat().st_size
        except OSError as error:
            _mark_coverage(
                primary["source"], {"read_error": _text(str(error), 300)}, "related transcript"
            )
            continue
        budget = min(size, MAX_BYTES + MAX_HEAD_BYTES)
        if consumed + budget > MAX_TOTAL_BYTES:
            primary["source"]["truncated"] = True
            break
        consumed += budget
        rows, coverage = _records(candidate)
        _mark_coverage(primary["source"], coverage, "related transcript")
        if rows:
            parsed = _parse_claude(candidate, rows, coverage)
            parsed_agent = parsed["agents"][0]
            if (
                parsed_agent["agent_id"] == root_session
                or parsed_agent.get("parent_key") == f"session:{root_session}"
            ):
                related.append(parsed)
    return _merge_details(primary, related)


def _related_codex(path, primary, root):
    selected = primary["agents"][0]
    selected_key = selected["key"]
    parsed_candidates, consumed = [], 0
    candidates, scan = _files(root)
    _mark_coverage(primary["source"], scan, "Codex transcript scan")
    for candidate in candidates:
        if candidate == path:
            continue
        try:
            size = candidate.stat().st_size
        except OSError as error:
            _mark_coverage(
                primary["source"], {"read_error": _text(str(error), 300)}, "related transcript"
            )
            continue
        budget = min(size, MAX_BYTES + MAX_HEAD_BYTES)
        if consumed + budget > MAX_TOTAL_BYTES:
            primary["source"]["truncated"] = True
            break
        consumed += budget
        rows, coverage = _records(candidate)
        _mark_coverage(primary["source"], coverage, "related transcript")
        if not rows:
            continue
        parsed = _parse_codex(candidate, rows, coverage)
        parsed_candidates.append(parsed)
    related_keys = {selected_key}
    if selected.get("parent_key"):
        related_keys.add(selected["parent_key"])
    changed = True
    while changed:
        changed = False
        for parsed in parsed_candidates:
            agent = parsed["agents"][0]
            if agent["key"] in related_keys or agent.get("parent_key") in related_keys:
                if agent["key"] not in related_keys:
                    related_keys.add(agent["key"])
                    changed = True
        for parsed in parsed_candidates:
            agent = parsed["agents"][0]
            if agent["key"] in related_keys and agent.get("parent_key"):
                if agent["parent_key"] not in related_keys:
                    related_keys.add(agent["parent_key"])
                    changed = True
    related = [parsed for parsed in parsed_candidates if parsed["agents"][0]["key"] in related_keys]
    return _merge_details(primary, related)


def _find_native(selector, provider, root):
    expected, _, path_key = selector.split(":", 1)[1].partition("@")
    paths, scan = _files(root)
    for path in paths:
        if path_key and _path_key(path) != path_key:
            continue
        rows, detail = _records(path)
        if not rows:
            continue
        parsed = _parse(provider, path, rows, detail)
        _mark_coverage(parsed["source"], scan, f"{provider} transcript scan")
        if parsed["source"]["session_id"] != expected:
            continue
        if provider == "claude":
            return _related_claude(path, parsed)
        return _related_codex(path, parsed, root)
    return None


def _managed_detail(store, selector):
    run_id = selector.split(":", 1)[1]
    run = store.get_run(run_id)
    session_id = run.get("session_id")
    prompt = None
    prompt_error = None
    try:
        prompt = (store.run_dir(run_id) / "prompt.txt").read_text(encoding="utf-8")[:MAX_TEXT]
    except OSError as error:
        prompt_error = _text(str(error), 300)
    source = {
        "selector": selector,
        "provider": "managed",
        "session_id": session_id,
        "title": run.get("role") or run.get("model") or "managed run",
        "project": None,
        "mode": run.get("role"),
        "permission_mode": None,
        "last_prompt": prompt,
        "queued_ops": 0,
        "file_edits": 0,
        "partial": bool(prompt_error),
        "truncated": prompt is not None and len(prompt) >= MAX_TEXT,
        "read_error": f"managed prompt: {prompt_error}" if prompt_error else None,
    }
    agent = _agent(f"run:{run_id}", run_id)
    agent.update(
        parent_key=f"session:{session_id}" if session_id else None,
        run_id=run_id,
        session_id=session_id,
        role=run.get("role"),
        model=run.get("model"),
        description=run.get("name"),
        prompt=prompt,
        started=run.get("started"),
        finished=run.get("finished"),
    )
    rows, detail = _records(store.run_dir(run_id) / "events.jsonl")
    _mark_coverage(source, detail, "managed event stream")
    tools, events = [], []
    if rows:
        parsed = _parse_claude(store.run_dir(run_id) / "events.jsonl", rows, detail)
        tools = parsed["tools"]
        events = parsed["events"]
        if parsed["agents"]:
            agent["reasoning"] = parsed["agents"][0].get("reasoning")
        for tool in tools:
            tool["owner_key"] = f"run:{run_id}"
        for event in events:
            event["owner_key"] = f"run:{run_id}"
        base = float(run.get("started") or run.get("created") or 0.0)
        for index, event in enumerate(events):
            if event["timestamp"] < 1_000_000_000:
                event["timestamp"] = base + (index + 1) / 1_000.0
        for index, tool in enumerate(tools):
            if tool.get("started") is not None and tool["started"] < 1_000_000_000:
                tool["started"] = base + (index + 1) / 1_000.0
            if tool.get("finished") is not None and tool["finished"] < 1_000_000_000:
                tool["finished"] = base + (index + 2) / 1_000.0
    if prompt:
        events.insert(
            0,
            {
                "kind": "prompt",
                "owner_key": f"run:{run_id}",
                "timestamp": float(run.get("started") or run.get("created") or 0.0),
                "summary": "managed prompt",
                "text": prompt,
            },
        )
    return {"source": source, "agents": [agent], "tools": tools, "events": events}


def _terminal_detail(store, selector):
    session_id = selector.split(":", 1)[1]
    terminal = terminal_cli.snapshot(store, session_id, include_output=True)
    if terminal is None:
        return None
    source = {
        "selector": selector,
        "provider": "claude",
        "session_id": session_id,
        "title": terminal.get("name") or "Claude terminal",
        "project": terminal.get("cwd"),
        "mode": "background terminal",
        "permission_mode": "dontAsk / tools disabled" if terminal.get("safe_profile") else None,
        "last_prompt": None,
        "queued_ops": 0,
        "file_edits": 0,
        "partial": False,
        "truncated": False,
        "read_error": None,
        "terminal": terminal,
    }
    agent = _agent(f"session:{session_id}", session_id)
    agent.update(
        session_id=session_id,
        role=terminal.get("role") or "interactive",
        model=terminal.get("requested_model"),
        description=terminal.get("name") or "Claude terminal",
        started=terminal.get("started_at"),
    )
    return {"source": source, "agents": [agent], "tools": [], "events": []}


def _with_terminal(store, detail):
    if detail is None or detail["source"].get("provider") != "claude":
        return detail
    session_id = detail["source"].get("session_id")
    if not session_id:
        return detail
    try:
        detail["source"]["terminal"] = terminal_cli.snapshot(store, session_id, include_output=True)
    except ControlError as error:
        detail["source"]["terminal"] = {
            "session_id": session_id,
            "attachable": False,
            "error": _text(str(error), 300),
        }
    return detail


def _detail(store, selector, claude_root, codex_root):
    if selector.startswith("managed:"):
        return _managed_detail(store, selector)
    if selector.startswith("claude:"):
        root = claude_root or Path.home() / ".claude" / "projects"
        return _with_terminal(store, _find_native(selector, "claude", root))
    if selector.startswith("terminal:"):
        return _terminal_detail(store, selector)
    if selector.startswith("codex:"):
        root = codex_root or Path.home() / ".codex" / "sessions"
        return _find_native(selector, "codex", root)
    return None


def _promote_terminal_selector(selector, claude_root, terminal=None):
    if not selector.startswith("terminal:"):
        return selector
    session_id = selector.split(":", 1)[1]
    root = claude_root or Path.home() / ".claude" / "projects"
    sessions, _ = _native_catalog("claude", root)
    matches = [item["selector"] for item in sessions if item["session_id"] == session_id]
    cwd = terminal.get("cwd") if isinstance(terminal, dict) else None
    if cwd:
        normalized = os.path.normcase(str(Path(cwd).resolve(strict=False)))
        matches = [
            item["selector"]
            for item in sessions
            if item["session_id"] == session_id
            and (
                not item.get("project")
                or os.path.normcase(str(Path(item["project"]).resolve(strict=False))) == normalized
            )
        ]
    return matches[0] if len(matches) == 1 else selector


def _local_events(detail):
    source, agents = detail["source"], detail["agents"]
    terminal = source.get("terminal") if isinstance(source.get("terminal"), dict) else None
    first_stamp = min(
        [item["timestamp"] for item in detail["events"]]
        + [item["started"] for item in agents if item.get("started") is not None]
        or [0.0]
    )
    entries = []
    known = {agent["key"] for agent in agents}
    for parent_key in sorted(
        {agent.get("parent_key") for agent in agents if agent.get("parent_key")} - known
    ):
        parent_id = parent_key.split(":", 1)[-1]
        entries.append(
            (
                first_stamp,
                0,
                "node_created",
                "session",
                parent_id,
                {
                    "id": parent_id,
                    "name": "parent session",
                    "state": "idle",
                    "role": "parent",
                    "model": None,
                    "session_id": parent_id,
                },
            )
        )
    for agent in agents:
        started = agent.get("started") if agent.get("started") is not None else first_stamp
        state = "active"
        if terminal and agent.get("session_id") == terminal.get("session_id"):
            status = terminal.get("status")
            if status == "idle":
                state = "idle"
            elif status in {"failed", "error"}:
                state = "failed"
            elif status in {"stopped", "completed", "finished"}:
                state = "completed"
            elif status not in {"running", "active"}:
                state = "unknown"
        payload = {
            "id": agent["agent_id"],
            "name": agent.get("description") or agent.get("role") or source["provider"],
            "state": state,
            "role": agent.get("role") or "agent",
            "model": agent.get("model"),
            "session_id": agent.get("session_id"),
        }
        if isinstance(agent.get("local_usage"), dict):
            payload["telemetry"] = _usage_telemetry(agent["local_usage"])
        entries.append((started, 0, "node_created", "session", agent["agent_id"], payload))
        if agent.get("parent_key"):
            parent_id = agent["parent_key"].split(":", 1)[-1]
            edge_id = f"{parent_id}->{agent['agent_id']}"
            entries.append(
                (
                    started,
                    1,
                    "edge_created",
                    "parent",
                    edge_id,
                    {
                        "id": edge_id,
                        "from_kind": "session",
                        "from_id": parent_id,
                        "to_kind": "session",
                        "to_id": agent["agent_id"],
                    },
                )
            )
        finished = agent.get("finished")
        if finished is not None and time.time() - finished >= 30:
            entries.append(
                (
                    finished,
                    3,
                    "node_state",
                    "session",
                    agent["agent_id"],
                    {**payload, "state": "idle", "status_basis": "last transcript activity"},
                )
            )
    for marker_index, marker in enumerate(detail["events"], 1):
        entries.append(
            {
                "marker_index": marker_index,
                "timestamp": marker["timestamp"],
                "kind": marker["kind"],
                "owner_key": marker.get("owner_key"),
                "summary": marker.get("summary") or marker["kind"],
            }
        )
    events = [
        {
            "type": "CUSTOM",
            "name": "claude-control.local-detail",
            "value": {
                "kind": "baseline",
                "entity_kind": "store",
                "entity_id": "baseline",
                "payload": {"fidelity": "local_detail", "nodes": {}, "edges": {}, "telemetry": []},
            },
            "timestamp": first_stamp,
            "metadata": {"claude-control": {"cursor": 1}},
        }
    ]
    entries.sort(
        key=lambda item: (
            item["timestamp"] if isinstance(item, dict) else item[0],
            2 if isinstance(item, dict) else item[1],
        )
    )
    for cursor, entry in enumerate(entries, 2):
        if isinstance(entry, dict):
            value = {
                "kind": "local_marker",
                "entity_kind": entry["kind"],
                "entity_id": f"marker-{entry['marker_index']}",
                "payload": {
                    "kind": entry["kind"],
                    "owner_key": entry["owner_key"],
                    "summary": entry["summary"],
                },
            }
            timestamp = entry["timestamp"]
        else:
            timestamp, _, kind, entity_kind, entity_id, payload = entry
            value = {
                "kind": kind,
                "entity_kind": entity_kind,
                "entity_id": entity_id,
                "payload": payload,
            }
        events.append(
            {
                "type": "CUSTOM",
                "name": "claude-control.local-detail",
                "value": value,
                "timestamp": timestamp,
                "metadata": {"claude-control": {"cursor": cursor}},
            }
        )
    return events


def _managed_events(store):
    converted, cursor = [], 0
    while True:
        page = observation.events(store, after=cursor, limit=observation.MAX_LIMIT)
        converted.extend(observation_agui.events(page["events"]))
        cursor = page["next_cursor"]
        if not page["has_more"]:
            return converted


def stream(
    store, *, source="managed", claude_root=None, codex_root=None, follow=False, poll_seconds=0.25
):
    """Yield graph events and a cursorless local detail snapshot."""
    selector = source
    if selector in ("all", "managed", "claude", "native_claude", "codex"):
        listed = catalog(
            store=store, source=selector, claude_root=claude_root, codex_root=codex_root
        )["sessions"]
        if not listed:
            raise ControlError(
                "session_not_found", "No local session matched the requested source."
            )
        selector = listed[0]["selector"]
    previous_graph = []
    previous_detail_signature = None
    previous_detail_snapshot = None
    while True:
        terminal = (
            previous_detail_snapshot["source"].get("terminal")
            if isinstance(previous_detail_snapshot, dict)
            else None
        )
        promoted_selector = (
            _promote_terminal_selector(selector, claude_root, terminal)
            if previous_detail_snapshot is not None
            else selector
        )
        promoted = promoted_selector != selector
        selector = promoted_selector
        detail = _detail(store, selector, claude_root, codex_root)
        if detail is None:
            raise ControlError("session_not_found", "The selected local session is unavailable.")
        graph = _managed_events(store) if selector.startswith("managed:") else _local_events(detail)
        detail_signature = hashlib.sha256(
            json.dumps(detail, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        prefix = (
            len(graph) >= len(previous_graph) and graph[: len(previous_graph)] == previous_graph
        )
        if previous_graph and (promoted or not prefix):
            yield {
                "type": "CLAUDE_CONTROL_DETAIL_RESET",
                "version": 1,
                "reason": "source_promoted" if promoted else "source_replaced",
            }
            yield from graph
        elif len(graph) > len(previous_graph):
            yield from graph[len(previous_graph) :]
        if detail_signature != previous_detail_signature:
            yield {"type": DETAIL_TYPE, "version": 1, **detail}
        previous_graph = graph
        previous_detail_signature = detail_signature
        previous_detail_snapshot = detail
        if not follow:
            return
        time.sleep(max(0.05, min(10.0, poll_seconds)))
