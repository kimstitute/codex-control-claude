"""Read-only status snapshot for the opt-in Claude harness."""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

USAGE_KEYS = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")
MAX_EVENTS_BYTES = 16 * 1024 * 1024


def _count(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _usage(value):
    if not isinstance(value, dict):
        return None
    return {key: _count(value.get(key)) for key in USAGE_KEYS}


def _events(path: Path):
    """Count each streamed assistant message once; prefer terminal result totals."""
    if not path.is_file() or path.is_symlink():
        return None, [], False
    if path.stat().st_size > MAX_EVENTS_BYTES:
        return None, [], False
    messages = {}
    models = set()
    terminal = None
    with path.open(encoding="utf-8", errors="replace") as stream:
        for raw in stream:
            try:
                event = json.loads(raw)
            except ValueError:
                continue  # An active writer may leave one incomplete line.
            if not isinstance(event, dict):
                continue
            kind = event.get("type")
            if kind == "assistant" and isinstance(event.get("message"), dict):
                message = event["message"]
                model = message.get("model")
                if isinstance(model, str) and model.startswith("claude-"):
                    models.add(model)
                message_id = message.get("id")
                usage = _usage(message.get("usage"))
                if isinstance(message_id, str) and usage:
                    previous = messages.get(message_id, {})
                    messages[message_id] = {key: max(previous.get(key, 0), usage[key])
                                            for key in USAGE_KEYS}
            elif kind == "result":
                terminal = _usage(event.get("usage"))
    if terminal is not None:
        return terminal, sorted(models), True
    if not messages:
        return None, sorted(models), False
    return {key: sum(row[key] for row in messages.values()) for key in USAGE_KEYS}, sorted(models), False


def _worker(path, data, now):
    from .harness_cli import _process_alive

    turns = data.get("turns") if isinstance(data.get("turns"), list) else []
    first = turns[0] if turns else {}
    latest = turns[-1] if turns else {}
    running = data.get("status") == "running"
    alive = _process_alive(latest) if running else False
    state = "running" if alive else "unknown" if running else data.get("status", "unknown")
    totals = {key: 0 for key in USAGE_KEYS}
    counted_turns = 0
    provisional = False
    models = set()
    for turn in turns:
        recorded_models = turn.get("actual_models")
        if isinstance(recorded_models, list):
            models.update(model for model in recorded_models
                          if isinstance(model, str) and model.startswith("claude-"))
        number = turn.get("number")
        if not isinstance(number, int) or number < 1:
            continue
        usage, observed, finished = _events(path / f"turn-{number}.jsonl")
        models.update(observed)
        if usage is not None:
            counted_turns += 1
            for key in USAGE_KEYS:
                totals[key] += usage[key]
            if not finished:
                provisional = True
        elif turn.get("status") == "running":
            provisional = True
    started = latest.get("started")
    if not isinstance(started, (int, float)) or isinstance(started, bool):
        started = None
    finished_at = latest.get("finished")
    end = now if state == "running" else finished_at if isinstance(finished_at, (int, float)) else now
    elapsed = max(0, int(end - started)) if started is not None else None
    first_started = first.get("started")
    age = max(0, int(now - first_started)) if isinstance(first_started, (int, float)) else None
    mission = latest.get("mission") or data.get("mission")
    return {
        "id": data["id"], "state": state, "mission": mission if isinstance(mission, str) else None,
        "requested_model": data.get("model"), "actual_models": sorted(models),
        "started_at": started, "elapsed_seconds": elapsed, "task_age_seconds": age,
        "tokens": {**totals, "total": sum(totals.values())} if counted_turns else None,
        "token_scope": "final" if counted_turns == len(turns) and not provisional else "provisional",
        "counted_turns": counted_turns, "turns": len(turns),
        "process_alive": alive if running else None,
    }


def snapshot(root: Path, *, include_all=False, now=None):
    """Read private task records without changing worker state or calling Claude."""
    from .harness_cli import CONTRACT

    now = time.time() if now is None else now
    workers = []
    warnings = []
    tasks = root / "tasks"
    if tasks.is_dir():
        for path in sorted(tasks.iterdir()):
            if not path.is_dir() or path.is_symlink():
                continue
            try:
                uuid.UUID(path.name)
            except ValueError:
                continue
            record = path / "task.json"
            if record.is_symlink():
                warnings.append(f"skipped unsafe task record: {path.name}")
                continue
            try:
                data = json.loads(record.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                warnings.append(f"could not read task record: {path.name}")
                continue
            if not isinstance(data, dict) or data.get("contract") != CONTRACT or data.get("id") != path.name:
                warnings.append(f"invalid task record: {path.name}")
                continue
            worker = _worker(path, data, now)
            if include_all or worker["state"] in ("running", "unknown"):
                workers.append(worker)
    workers.sort(key=lambda row: (row["state"] != "running", row["started_at"] or 0, row["id"]))
    return {"scope": "managed Claude harness workers", "observed_at": now,
            "workers": workers, "active_count": sum(row["state"] == "running" for row in workers),
            "warnings": warnings}
