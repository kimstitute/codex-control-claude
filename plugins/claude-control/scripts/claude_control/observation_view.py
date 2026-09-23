"""Pure graph and timeline projections for observation replay UIs."""

from __future__ import annotations

from . import observation

PROFILE = "claude-control.observation-view.v1"
NODE_KINDS = ("composition", "workflow", "workspace", "task", "run", "session")


def _node_id(kind, identity):
    return f"{'agent' if kind == 'session' else kind}:{identity}"


def _short(identity):
    text = str(identity)
    return text if len(text) <= 12 else text[:8] + "…"


def _state(kind, node, runs):
    if kind == "session":
        owned = [run for run in runs.values() if run.get("session_id") == node.get("id")]
        if owned:
            latest = max(owned, key=lambda run: (run.get("created") or 0, run.get("id") or ""))
            return latest.get("state") or "idle"
        return "blocked" if node.get("blocked") else "idle"
    if kind == "task":
        run = runs.get(node.get("active_run_id"))
        return (run or {}).get("state") or "idle"
    return node.get("state") or "idle"


def graph(snapshot):
    """Convert a replay snapshot into the monitor graph renderer contract."""
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("nodes"), dict):
        raise ValueError("observation replay snapshot has no node graph")
    source_nodes = snapshot["nodes"]
    runs = source_nodes.get("run", {})
    runs = runs if isinstance(runs, dict) else {}
    nodes = []
    for kind in NODE_KINDS:
        group = source_nodes.get(kind, {})
        if not isinstance(group, dict):
            raise ValueError(f"observation replay {kind} nodes must be an object")
        for identity, node in sorted(group.items()):
            if not isinstance(node, dict) or node.get("id") != identity:
                raise ValueError(f"observation replay contains an invalid {kind} node")
            label = node.get("name") or _short(identity)
            projected = {
                "id": _node_id(kind, identity),
                "kind": "agent" if kind == "session" else kind,
                "label": label,
                "state": _state(kind, node, runs),
            }
            if kind == "session":
                projected.update(role=node.get("role"), model=node.get("model"))
            elif kind == "run":
                models = node.get("models")
                projected["model"] = (
                    sorted(value for value in models if isinstance(value, str))[-1]
                    if isinstance(models, list) and models
                    else None
                )
                projected["role"] = (source_nodes.get("session", {}).get(node.get("session_id")) or {}).get(
                    "role"
                )
            elif kind == "task":
                projected["work"] = node.get("name")
            elif kind in ("workflow", "composition"):
                projected["phase"] = node.get("phase")
            nodes.append(projected)

    edges = []
    source_edges = snapshot.get("edges")
    if not isinstance(source_edges, dict):
        raise ValueError("observation replay snapshot has no edge graph")
    for edge_kind, group in sorted(source_edges.items()):
        if not isinstance(group, dict):
            raise ValueError(f"observation replay {edge_kind} edges must be an object")
        for identity, edge in sorted(group.items()):
            if not isinstance(edge, dict):
                raise ValueError(f"observation replay contains an invalid {edge_kind} edge")
            from_kind, to_kind = edge.get("from_kind"), edge.get("to_kind")
            from_id, to_id = edge.get("from_id"), edge.get("to_id")
            if from_kind in NODE_KINDS and to_kind in NODE_KINDS and from_id and to_id:
                edges.append(
                    {
                        "from": _node_id(from_kind, from_id),
                        "to": _node_id(to_kind, to_id),
                        "label": edge_kind,
                        "id": identity,
                    }
                )

    # The durable ledger stores session ownership on nodes rather than as separate
    # relational rows. Derive display-only links so a run/task remains connected to
    # its agent without inventing durable history.
    for identity, run in sorted(runs.items()):
        session_id = run.get("session_id") if isinstance(run, dict) else None
        if session_id:
            edges.append(
                {
                    "from": _node_id("run", identity),
                    "to": _node_id("session", session_id),
                    "label": "session",
                    "id": f"{identity}>{session_id}",
                }
            )
    for identity, task in sorted((source_nodes.get("task") or {}).items()):
        session_id = task.get("session_id") if isinstance(task, dict) else None
        if session_id:
            edges.append(
                {
                    "from": _node_id("task", identity),
                    "to": _node_id("session", session_id),
                    "label": "assigned",
                    "id": f"{identity}>{session_id}",
                }
            )
    return {"nodes": nodes, "edges": edges}


def timeline_bar(cursor, latest, width=36):
    """Render a compact deterministic cursor minimap for terminal dashboards."""
    width = max(5, int(width))
    if latest <= 1:
        position = width - 1
    else:
        position = round((max(1, min(cursor, latest)) - 1) * (width - 1) / (latest - 1))
    cells = ["─"] * width
    cells[position] = "●"
    return "[" + "".join(cells) + "]"


def frame(store, through=None):
    """Read the latest high-water mark and reconstruct one selected replay frame."""
    latest_snapshot = observation.replay(store)
    latest = latest_snapshot["cursor"]
    if through is None or through >= latest:
        selected = latest_snapshot
    else:
        selected = observation.replay(store, through=max(1, through))
    cursor = selected["cursor"]
    page = observation.events(store, after=max(0, cursor - 1), limit=1, through=cursor)
    event = page["events"][0] if page["events"] else None
    return {
        "profile": PROFILE,
        "cursor": cursor,
        "latest_cursor": latest,
        "at_latest": cursor == latest,
        "fidelity": selected["fidelity"],
        "event": event,
        "graph": graph(selected),
        "snapshot": selected,
    }
