"""Read-only runtime snapshots and a stdlib curses dashboard."""

from __future__ import annotations

import curses
import json
import math
import os
import threading
import time
from pathlib import Path

from . import provider_usage
from .store import ACTIVE, ControlError

PROTOCOL = "claude-control.monitor.v1"
MAX_LIVE_BYTES = 4 * 1024 * 1024
_LIVE_CACHE = {}


def _json(value, default):
    if value is None:
        return default
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default
    return parsed


def _number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else 0


def _usage(value):
    value = value if isinstance(value, dict) else {}
    details = value.get("output_tokens_details")
    details = details if isinstance(details, dict) else {}
    return {
        "input_tokens": int(_number(value.get("input_tokens"))),
        "cache_creation_input_tokens": int(_number(value.get("cache_creation_input_tokens"))),
        "cache_read_input_tokens": int(_number(value.get("cache_read_input_tokens"))),
        "output_tokens": int(_number(value.get("output_tokens"))),
        "thinking_tokens": int(_number(details.get("thinking_tokens"))),
    }


def _live_usage(path: Path):
    """Read only bounded counters from a possibly incomplete stream tail."""
    try:
        info = path.stat()
        identity = (info.st_mtime_ns, info.st_size)
        cached = _LIVE_CACHE.get(str(path))
        if cached and cached[0] == identity:
            return cached[1]
        with path.open("rb") as handle:
            size = info.st_size
            if size > MAX_LIVE_BYTES:
                handle.seek(size - MAX_LIVE_BYTES)
                handle.readline()
            data = handle.read(MAX_LIVE_BYTES)
    except (FileNotFoundError, OSError):
        return None
    estimated = None
    usage = {}
    model_usage = {}
    cost = None
    exact = False
    for raw in data.decode("utf-8", errors="replace").splitlines():
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        tokens = event.get("estimated_tokens")
        if isinstance(tokens, (int, float)) and not isinstance(tokens, bool) and tokens >= 0:
            estimated = int(tokens)
        if event.get("type") == "assistant" and isinstance(event.get("message"), dict):
            candidate = event["message"].get("usage")
            if isinstance(candidate, dict):
                usage = candidate
        if event.get("type") == "result":
            if isinstance(event.get("usage"), dict):
                usage = event["usage"]
                exact = True
            if isinstance(event.get("modelUsage"), dict):
                model_usage = event["modelUsage"]
            candidate = event.get("total_cost_usd")
            if isinstance(candidate, (int, float)) and not isinstance(candidate, bool):
                cost = float(candidate)
    result = {
        "usage": _usage(usage),
        "model_usage": model_usage,
        "provider_cost_usd": cost,
        "estimated_output_tokens": estimated,
        "exact": exact,
    }
    if len(_LIVE_CACHE) >= 2048:
        _LIVE_CACHE.clear()
    _LIVE_CACHE[str(path)] = (identity, result)
    return result


def _run(row, store, *, live=True):
    value = dict(row)
    value["actual_models"] = _json(value.get("actual_models"), [])
    telemetry = None
    if value.get("telemetry_usage") is not None:
        telemetry = {
            "usage": _usage(_json(value.pop("telemetry_usage"), {})),
            "model_usage": _json(value.pop("telemetry_model_usage"), {}),
            "provider_cost_usd": value.pop("telemetry_cost"),
            "duration_api_ms": value.pop("telemetry_api_ms"),
            "duration_ms": value.pop("telemetry_duration_ms"),
        }
    else:
        for key in (
            "telemetry_usage",
            "telemetry_model_usage",
            "telemetry_cost",
            "telemetry_api_ms",
            "telemetry_duration_ms",
        ):
            value.pop(key, None)
    current = None
    if live and (value["status"] in ACTIVE or telemetry is None):
        current = _live_usage(store.run_dir(value["id"]) / "events.jsonl")
    value["telemetry"] = telemetry
    value["live"] = current
    value["elapsed_ms"] = max(
        0,
        int(
            1000
            * ((value.get("finished") or time.time()) - (value.get("started") or value["created"]))
        ),
    )
    return value


def _node(kind, identifier, label, state, **fields):
    return {
        "id": f"{kind}:{identifier}",
        "kind": kind,
        "label": label,
        "state": state,
        **fields,
    }


def snapshot(store, *, history=100, live=True):
    """Return one consistent, read-only graph and usage snapshot."""
    if store.config["schema"] < 12:
        raise ControlError("migration_required", "Monitor requires schema 12.")
    if type(history) is not int or not 1 <= history <= 1000:
        raise ControlError("invalid_limit", "Monitor history must be between 1 and 1000.")
    run_select = (
        "SELECT r.rowid AS sequence,r.*,t.usage AS telemetry_usage,"
        "t.model_usage AS telemetry_model_usage,t.provider_cost_usd AS telemetry_cost,"
        "t.duration_api_ms AS telemetry_api_ms,t.duration_ms AS telemetry_duration_ms "
        "FROM runs r LEFT JOIN run_telemetry t ON t.run_id=r.id "
    )
    with store.db() as db:
        db.execute("BEGIN")
        sessions = [dict(row) for row in db.execute("SELECT rowid AS sequence,* FROM sessions")]
        tasks = [dict(row) for row in db.execute("SELECT rowid AS sequence,* FROM tasks")]
        workflows = [dict(row) for row in db.execute("SELECT rowid AS sequence,* FROM workflows")]
        workspaces = [
            dict(row)
            for row in db.execute(
                "SELECT w.rowid AS sequence,w.*,t.task_id,t.assignment "
                "FROM workspaces w LEFT JOIN workspace_tasks t ON t.workspace_id=w.id"
            )
        ]
        compositions = [
            dict(row) for row in db.execute("SELECT rowid AS sequence,* FROM compositions")
        ]
        members = [dict(row) for row in db.execute("SELECT * FROM composition_members")]
        dependencies = [dict(row) for row in db.execute("SELECT * FROM task_dependencies")]
        decisions = [
            dict(row)
            for row in db.execute(
                "SELECT task_id,revision,kind,recommendation,created FROM review_decisions "
                "ORDER BY rowid"
            )
        ]
        recent_rows = db.execute(
            run_select + "ORDER BY r.rowid DESC LIMIT ?", (history,)
        ).fetchall()
        latest_rows = db.execute(
            run_select + "WHERE r.rowid IN (SELECT max(rowid) FROM runs GROUP BY session_id) "
            "ORDER BY r.rowid DESC"
        ).fetchall()
        all_telemetry = db.execute(
            "SELECT usage,provider_cost_usd,duration_ms FROM run_telemetry"
        ).fetchall()
        db.rollback()

    recent = [_run(row, store, live=live) for row in recent_rows]
    latest = {row["session_id"]: _run(row, store, live=live) for row in latest_rows}
    run_by_id = {run["id"]: run for run in [*recent, *latest.values()]}
    task_by_id = {row["id"]: row for row in tasks}
    workspace_by_task = {row["task_id"]: row for row in workspaces if row.get("task_id")}
    decision_by_task = {}
    for row in decisions:
        task = task_by_id.get(row["task_id"])
        if task and row["revision"] == task["current_revision"]:
            decision_by_task[row["task_id"]] = row

    agents = []
    for session in sessions:
        current = latest.get(session["id"])
        owned = next((task for task in tasks if task["session_id"] == session["id"]), None)
        work = owned["name"] if owned else session["role"]
        if owned and owned["id"] in workspace_by_task:
            assignment = _json(workspace_by_task[owned["id"]].get("assignment"), {})
            work = assignment.get("name") or assignment.get("objective") or work
        agents.append(
            {
                "id": session["id"],
                "name": session["name"],
                "role": session["role"],
                "requested_model": session["model"],
                "actual_model": (
                    current["actual_models"][-1] if current and current["actual_models"] else None
                ),
                "effort": session.get("effort"),
                "project": Path(session["project"]).name,
                "state": current["status"] if current else "idle",
                "work": work,
                "task_id": owned["id"] if owned else None,
                "run": current,
            }
        )

    nodes = []
    edges = []
    for row in compositions:
        nodes.append(
            _node(
                "composition",
                row["id"],
                row["name"],
                row["state"],
                phase=row["phase"],
            )
        )
        edges.append(
            {
                "from": f"composition:{row['id']}",
                "to": f"workflow:{row['workflow_id']}",
                "label": "plan",
            }
        )
    for row in members:
        edges.append(
            {
                "from": f"composition:{row['composition_id']}",
                "to": f"workspace:{row['workspace_id']}",
                "label": row["phase"],
            }
        )
    for row in workflows:
        nodes.append(
            _node(
                "workflow",
                row["id"],
                row["name"],
                row["state"],
                phase=row["phase"],
                round=row["round"],
            )
        )
        for role, task_id in (
            ("worker", row["worker_task_id"]),
            ("reviewer", row["reviewer_task_id"]),
        ):
            edges.append({"from": f"workflow:{row['id']}", "to": f"task:{task_id}", "label": role})
    for row in workspaces:
        policy = _json(row["policy"], {})
        nodes.append(
            _node(
                "workspace",
                row["id"],
                Path(row["source_repo"]).name,
                row["state"],
                role=policy.get("role"),
            )
        )
        if row.get("task_id"):
            edges.append(
                {
                    "from": f"workspace:{row['id']}",
                    "to": f"task:{row['task_id']}",
                    "label": policy.get("role") or "task",
                }
            )
    for row in tasks:
        run = run_by_id.get(row["active_run_id"])
        decision = decision_by_task.get(row["id"])
        state = run["status"] if run else "awaiting_submit"
        if decision and decision["kind"] == "accept":
            state = "accepted"
        nodes.append(
            _node(
                "task",
                row["id"],
                row["name"],
                state,
                revision=row["current_revision"],
            )
        )
        if row["session_id"]:
            edges.append(
                {
                    "from": f"task:{row['id']}",
                    "to": f"agent:{row['session_id']}",
                    "label": "session",
                }
            )
    for agent in agents:
        run = agent["run"]
        nodes.append(
            _node(
                "agent",
                agent["id"],
                agent["name"],
                agent["state"],
                role=agent["role"],
                model=agent["actual_model"] or agent["requested_model"],
                work=agent["work"],
                estimated_output_tokens=(run.get("live") or {}).get("estimated_output_tokens")
                if run
                else None,
            )
        )
    for row in dependencies:
        edges.append(
            {
                "from": f"task:{row['parent_task_id']}",
                "to": f"task:{row['child_task_id']}",
                "label": "depends",
            }
        )

    totals = {
        "runs": len(all_telemetry),
        "input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "output_tokens": 0,
        "thinking_tokens": 0,
        "provider_cost_usd": 0.0,
        "duration_ms": 0.0,
    }
    for row in all_telemetry:
        usage = _usage(_json(row["usage"], {}))
        for key in usage:
            totals[key] += usage[key]
        totals["provider_cost_usd"] += _number(row["provider_cost_usd"])
        totals["duration_ms"] += _number(row["duration_ms"])

    return {
        "protocol": PROTOCOL,
        "captured_at": time.time(),
        "host": os.uname().nodename,
        "schema": store.config["schema"],
        "summary": {
            "agents": len(agents),
            "active_agents": sum(agent["state"] in ACTIVE for agent in agents),
            "active_runs": sum(run["status"] in ACTIVE for run in latest.values()),
            "awaiting_attention": sum(
                run["status"] in ("unknown", "failed", "launch_failed", "interrupted")
                for run in latest.values()
            ),
            "telemetry": totals,
        },
        "nodes": nodes,
        "edges": edges,
        "agents": agents,
        "runs": recent,
    }


def _attach_provider_usage(data, usage):
    """Attach account limits and label locally observed Claude token totals."""
    value = {
        "protocol": usage.get("protocol"),
        "captured_at": usage.get("captured_at"),
        "providers": [dict(item) for item in usage.get("providers", [])],
    }
    totals = data["summary"]["telemetry"]
    for item in value["providers"]:
        if item.get("provider") != "claude":
            continue
        tokens = dict(item.get("tokens") or {})
        tokens.update(
            {
                "managed_input_used": totals["input_tokens"],
                "managed_cache_read_used": totals["cache_read_input_tokens"],
                "managed_output_used": totals["output_tokens"],
                "managed_total_used": (
                    totals["input_tokens"]
                    + totals["cache_creation_input_tokens"]
                    + totals["cache_read_input_tokens"]
                    + totals["output_tokens"]
                ),
                "note": "tokens observed by this controller; they are not the subscription quota denominator",
            }
        )
        item["tokens"] = tokens
    data["provider_usage"] = value
    return data


def _short(value, width):
    value = str(value or "-").replace("\n", " ")
    return value if len(value) <= width else value[: max(1, width - 1)] + "…"


def _tokens(value):
    if value is None:
        return "-"
    value = int(value)
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}m"
    if value >= 1000:
        return f"{value / 1000:.1f}k"
    return str(value)


def _duration(milliseconds):
    seconds = max(0, int(_number(milliseconds) / 1000))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"


def graph_lines(data, *, active_only=False):
    nodes = {node["id"]: node for node in data["nodes"]}
    edges = [edge for edge in data["edges"] if edge["from"] in nodes and edge["to"] in nodes]
    if active_only:
        live_states = set(ACTIVE) | {"active", "operating", "stopping", "awaiting_codex"}
        keep = {key for key, node in nodes.items() if node["state"] in live_states}
        changed = True
        while changed:
            changed = False
            for edge in edges:
                if edge["from"] in keep or edge["to"] in keep:
                    before = len(keep)
                    keep.update((edge["from"], edge["to"]))
                    changed = changed or len(keep) != before
        if keep:
            nodes = {key: value for key, value in nodes.items() if key in keep}
            edges = [edge for edge in edges if edge["from"] in keep and edge["to"] in keep]
    incoming = {key: 0 for key in nodes}
    children = {key: [] for key in nodes}
    for edge in edges:
        incoming[edge["to"]] += 1
        children[edge["from"]].append((edge["label"], edge["to"]))
    roots = sorted((key for key, count in incoming.items() if count == 0), key=str)
    if not roots:
        roots = sorted(nodes)
    lines = []
    visited = set()

    def label(node):
        suffix = []
        for key in ("phase", "role", "model", "work"):
            if node.get(key):
                suffix.append(str(node[key]))
        if node.get("estimated_output_tokens") is not None:
            suffix.append("~" + _tokens(node["estimated_output_tokens"]) + " tok")
        detail = " · ".join(suffix)
        return f"[{node['kind']}] {node['label']}  {node['state']}" + (
            f"  {detail}" if detail else ""
        )

    def walk(key, prefix="", branch="", edge_label=None):
        node = nodes[key]
        relation = f"{edge_label} → " if edge_label else ""
        if key in visited:
            lines.append(prefix + branch + relation + "↩ " + label(node))
            return
        visited.add(key)
        lines.append(prefix + branch + relation + label(node))
        entries = sorted(children[key], key=lambda item: (item[0], item[1]))
        child_prefix = prefix + ("   " if not branch else ("│  " if branch == "├─ " else "   "))
        for index, (relation_name, child) in enumerate(entries):
            walk(
                child,
                child_prefix,
                "└─ " if index == len(entries) - 1 else "├─ ",
                relation_name,
            )

    for root in roots:
        if root not in visited:
            walk(root)
    for key in sorted(nodes):
        if key not in visited:
            walk(key)
    return lines or ["No managed agents or orchestration records."]


def _agent_lines(data):
    lines = ["STATE        MODEL                 ROLE          TOKENS       COST      CURRENT WORK"]
    for agent in sorted(
        data["agents"], key=lambda item: (item["state"] not in ACTIVE, item["name"])
    ):
        run = agent["run"] or {}
        telemetry = run.get("telemetry") or {}
        live = run.get("live") or {}
        usage = telemetry.get("usage") or live.get("usage") or {}
        output = usage.get("output_tokens")
        token_text = _tokens(output)
        if run.get("status") in ACTIVE and live.get("estimated_output_tokens") is not None:
            token_text = "~" + _tokens(live["estimated_output_tokens"])
        cost = telemetry.get("provider_cost_usd")
        if cost is None:
            cost = live.get("provider_cost_usd")
        lines.append(
            f"{agent['state']:<12} {_short(agent['actual_model'] or agent['requested_model'], 21):<21} "
            f"{_short(agent['role'], 13):<13} {token_text:>8} "
            f"{('$%.4f' % cost) if cost is not None else '-':>10}  {_short(agent['work'], 42)}"
        )
    return lines


def _history_lines(data):
    lines = ["STATUS       MODEL                 IN/CACHE/OUT       ELAPSED     COST      WORK"]
    agents = {agent["id"]: agent for agent in data["agents"]}
    for run in data["runs"]:
        agent = agents.get(run["session_id"], {})
        telemetry = run.get("telemetry") or {}
        live = run.get("live") or {}
        usage = telemetry.get("usage") or live.get("usage") or {}
        token_text = f"{_tokens(usage.get('input_tokens'))}/{_tokens(usage.get('cache_read_input_tokens'))}/{_tokens(usage.get('output_tokens'))}"
        if run["status"] in ACTIVE and live.get("estimated_output_tokens") is not None:
            token_text += f" (~{_tokens(live['estimated_output_tokens'])})"
        models = run["actual_models"]
        model = models[-1] if models else agent.get("requested_model", "-")
        cost = telemetry.get("provider_cost_usd")
        if cost is None:
            cost = live.get("provider_cost_usd")
        lines.append(
            f"{run['status']:<12} {_short(model, 21):<21} {token_text:<18} "
            f"{_duration(run['elapsed_ms']):>9} {('$%.4f' % cost) if cost is not None else '-':>10}  "
            f"{_short(agent.get('work') or agent.get('name') or run['id'], 36)}"
        )
    return lines


def _percent_bar(remaining, width=18):
    if remaining is None:
        return "[" + "?".center(width) + "]"
    remaining = max(0.0, min(100.0, float(remaining)))
    filled = round(width * remaining / 100)
    return "[" + "█" * filled + "·" * (width - filled) + "]"


def _reset_text(value, now=None):
    if value is None:
        return "-"
    now = time.time() if now is None else now
    seconds = max(0, int(float(value) - now))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"
    return f"{seconds // 86400}d{(seconds % 86400) // 3600:02d}h"


def _amount(value, unit):
    if value is None:
        return "-"
    if unit == "percent":
        return f"{float(value):.1f}%"
    return f"{_tokens(value)} {unit}"


def _limit_lines(data):
    report = data.get("provider_usage") or {}
    providers = report.get("providers") or []
    if not providers:
        return ["Provider limits are loading…"]
    lines = [
        "PROVIDER  STATUS       LIMIT / MODEL                    REMAINING CAPACITY        USED / LEFT        RESET"
    ]
    now = time.time()
    for provider in providers:
        name = str(provider.get("provider") or "?").upper()
        status = str(provider.get("status") or "unknown") + ("*" if provider.get("stale") else "")
        windows = provider.get("windows") or []
        if not windows:
            detail = provider.get("error") or provider.get("note") or "no reported quota"
            lines.append(f"{name:<9} {status:<12} {_short(detail, 76)}")
        for index, item in enumerate(windows):
            remaining = item.get("remaining_percent")
            unit = item.get("unit") or "percent"
            if item.get("exact_amounts"):
                amounts = (
                    f"{_amount(item.get('used'), unit)} / {_amount(item.get('remaining'), unit)}"
                )
            else:
                used = item.get("used_percent")
                amounts = (
                    f"{used:.1f}% / {remaining:.1f}%"
                    if used is not None and remaining is not None
                    else "absolute tokens unavailable"
                )
            label = item.get("label") or "quota"
            if item.get("model") and item["model"] not in str(label):
                label = f"{label} · {item['model']}"
            lines.append(
                f"{name if index == 0 else '':<9} {status if index == 0 else '':<12} "
                f"{_short(label, 32):<32} {_percent_bar(remaining)} "
                f"{_short(amounts, 23):<23} {_reset_text(item.get('resets_at'), now):>8}"
            )
        tokens = provider.get("tokens") or {}
        token_parts = []
        for key, label in (
            ("today_used", "today"),
            ("lifetime_used", "lifetime"),
            ("managed_total_used", "managed"),
        ):
            if tokens.get(key) is not None:
                token_parts.append(f"{label} {_tokens(tokens[key])}")
        if token_parts:
            lines.append(
                f"{'':<9} {'tokens':<12} {' · '.join(token_parts)} · remaining token ceiling unavailable"
            )
        spending = provider.get("spending") or {}
        if spending:
            spend_parts = []
            if spending.get("plan"):
                spend_parts.append("plan " + str(spending["plan"]))
            used_cents = spending.get("used_cents")
            limit_cents = spending.get("limit_cents")
            if used_cents is not None:
                text = f"on-demand ${used_cents / 100:.2f}"
                if limit_cents is not None:
                    text += f" / ${limit_cents / 100:.2f}"
                spend_parts.append(text)
            elif spending.get("used") is not None:
                spend_parts.append(f"extra {spending.get('currency', '')} {spending['used']:.2f}")
            if spend_parts:
                lines.append(f"{'':<9} {'spend':<12} {' · '.join(spend_parts)}")
        lines.append("")
    lines.extend(
        [
            "Semantics:",
            "  • Bars use provider-reported quota percentages; 100% means fully remaining.",
            "  • Token totals and quota percentages are separate metrics unless an exact amount is reported.",
            "  • A '*' after status means the last good reading is being shown after a refresh failure.",
        ]
    )
    return lines


def _safe_add(window, y, x, text, width, attribute=0):
    if y < 0 or x < 0 or width <= 0:
        return
    try:
        window.addnstr(y, x, text, width, attribute)
    except curses.error:
        pass


def _draw(window, data, view, offset, active_only):
    window.erase()
    height, width = window.getmaxyx()
    summary = data["summary"]
    totals = summary["telemetry"]
    title = (
        f" Claude Control Monitor  {data['host']}  schema {data['schema']}  "
        f"agents {summary['active_agents']}/{summary['agents']} active  "
        f"cost ${totals['provider_cost_usd']:.4f}  "
        f"tokens {_tokens(totals['output_tokens'])} out "
    )
    _safe_add(window, 0, 0, title.ljust(width), width, curses.A_REVERSE)
    tabs = ["1 Graph", "2 Agents", "3 History", "4 Limits"]
    tab_line = "   ".join(f"[{tab}]" if index == view else tab for index, tab in enumerate(tabs))
    _safe_add(window, 1, 0, tab_line, width, curses.A_BOLD)
    _safe_add(
        window,
        2,
        0,
        "q quit · 1/2/3/4 view · ↑↓/jk scroll · PgUp/PgDn · r refresh · a active graph",
        width,
    )
    if height < 8 or width < 50:
        _safe_add(window, 4, 0, "Terminal is too small (minimum 50×8).", width, curses.A_BOLD)
        window.refresh()
        return 0
    if view == 0:
        lines = graph_lines(data, active_only=active_only)
        mode = "active graph" if active_only else "all graph"
    elif view == 1:
        lines, mode = _agent_lines(data), "managed agents"
    elif view == 2:
        lines, mode = _history_lines(data), "recent run history"
    else:
        lines, mode = _limit_lines(data), "provider quotas and token activity"
    visible = max(1, height - 5)
    offset = max(0, min(offset, max(0, len(lines) - visible)))
    _safe_add(
        window,
        3,
        0,
        f"{mode} · rows {offset + 1}-{min(len(lines), offset + visible)}/{len(lines)}",
        width,
        curses.A_DIM,
    )
    for row, line in enumerate(lines[offset : offset + visible], start=4):
        attribute = curses.A_BOLD if row == 4 and view in (1, 2, 3) and offset == 0 else 0
        _safe_add(window, row, 0, line, width, attribute)
    captured = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(data["captured_at"]))
    _safe_add(window, height - 1, 0, f"updated {captured}".ljust(width), width, curses.A_REVERSE)
    window.refresh()
    return offset


class _UsagePoller:
    def __init__(self, *, interval, timeout):
        self.interval = interval
        self.timeout = timeout
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.stop = threading.Event()
        self.value = {"protocol": provider_usage.PROTOCOL, "captured_at": None, "providers": []}
        self.thread = threading.Thread(target=self._run, name="provider-usage", daemon=True)
        self.thread.start()

    def _run(self):
        while not self.stop.is_set():
            value = provider_usage.collect(timeout=self.timeout)
            with self.lock:
                self.value = value
            self.wake.wait(self.interval)
            self.wake.clear()

    def get(self):
        with self.lock:
            return {
                "protocol": self.value.get("protocol"),
                "captured_at": self.value.get("captured_at"),
                "providers": [dict(item) for item in self.value.get("providers", [])],
            }

    def refresh(self):
        self.wake.set()

    def close(self):
        self.stop.set()
        self.wake.set()
        self.thread.join(timeout=0.2)


def run_tui(store, *, refresh_seconds=0.5, history=100, limits_refresh_seconds=60.0):
    if not math.isfinite(refresh_seconds) or not 0.1 <= refresh_seconds <= 60:
        raise ControlError("invalid_limit", "Monitor refresh must be finite, 0.1–60 seconds.")
    if not math.isfinite(limits_refresh_seconds) or not 30 <= limits_refresh_seconds <= 3600:
        raise ControlError(
            "invalid_limit", "Provider-limit refresh must be finite, 30–3600 seconds."
        )

    poller = _UsagePoller(
        interval=limits_refresh_seconds, timeout=min(15, limits_refresh_seconds / 2)
    )

    def main(window):
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        window.keypad(True)
        window.timeout(max(100, int(refresh_seconds * 1000)))
        view = 0
        offsets = [0, 0, 0, 0]
        active_only = True
        data = _attach_provider_usage(snapshot(store, history=history), poller.get())
        while True:
            data = _attach_provider_usage(data, poller.get())
            offsets[view] = _draw(window, data, view, offsets[view], active_only)
            key = window.getch()
            if key in (ord("q"), ord("Q")):
                return
            if key in (ord("1"), ord("2"), ord("3"), ord("4")):
                view = key - ord("1")
            elif key == 9:
                view = (view + 1) % 4
            elif key in (curses.KEY_DOWN, ord("j")):
                offsets[view] += 1
            elif key in (curses.KEY_UP, ord("k")):
                offsets[view] = max(0, offsets[view] - 1)
            elif key == curses.KEY_NPAGE:
                offsets[view] += max(1, window.getmaxyx()[0] - 6)
            elif key == curses.KEY_PPAGE:
                offsets[view] = max(0, offsets[view] - max(1, window.getmaxyx()[0] - 6))
            elif key in (ord("a"), ord("A")) and view == 0:
                active_only = not active_only
                offsets[view] = 0
            if key in (-1, ord("r"), ord("R")):
                data = snapshot(store, history=history)
                if key in (ord("r"), ord("R")):
                    poller.refresh()

    try:
        curses.wrapper(main)
    except curses.error as exc:
        raise ControlError("monitor_terminal", "Monitor needs an interactive terminal.") from exc
    finally:
        poller.close()
