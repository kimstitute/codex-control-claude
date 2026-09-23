"""Bounded queries and deterministic replay over the append-only observation ledger.

This module only reads observation_events. It never inserts, updates or deletes a
row, and it reads no private field: the schema-14 projections already exclude
prompts, results, message bodies, policies, project paths, receipts, account
identity and credentials, so folding them cannot leak any of those.

Two cursor conventions meet here and stay distinct. A page is taken *after* an
exclusive cursor, so a caller can walk history by feeding back next_cursor. A
fold is taken *through* an inclusive cursor, so a caller can reconstruct exactly
the state an observer would have seen at that event.

Fidelity is stated, never guessed. The single baseline event summarises whatever
history existed before schema 14 and declares itself 'baseline_only'; a fold that
starts from a summarising baseline keeps that declaration forever. A baseline that
summarised nothing lost nothing, so folds from it are 'complete'.
"""

from __future__ import annotations

import json
import math

from .schema import OBSERVATION_CONTRACT
from .store import ControlError

CONTRACT = OBSERVATION_CONTRACT
VERSION = 1
SCHEMA_REQUIRED = 14

BASELINE = "baseline"
NODE_CREATED = "node_created"
NODE_STATE = "node_state"
EDGE_CREATED = "edge_created"
RUN_TELEMETRY = "run_telemetry"
KINDS = (BASELINE, NODE_CREATED, NODE_STATE, EDGE_CREATED, RUN_TELEMETRY)
TELEMETRY_ENTITY = "run"
BASELINE_ENTITY = "store"

BASELINE_ONLY = "baseline_only"
COMPLETE = "complete"
FIDELITIES = (BASELINE_ONLY, COMPLETE)

DEFAULT_LIMIT = 100
MAX_LIMIT = 1000
MAX_CURSOR = 2**63 - 1
PAGE = 500

_COLUMNS = "cursor,contract,kind,entity_kind,entity_id,recorded_at,effective_at,payload"
_SELECT = (
    f"SELECT {_COLUMNS} FROM observation_events"
    " WHERE cursor>? AND cursor<=? ORDER BY cursor LIMIT ?"
)

# Appended edges carry their identity in the immutable row. The baseline summarises
# edges without one, so identity is rebuilt from exactly the projected fields the
# schema-14 triggers concatenate, in the same order.
_EDGE_IDENTITIES = {
    "dependency": "{from_id}:{from_revision}>{to_id}:{to_revision}",
    "binding": "{from_id}>{to_id}",
    "review": "{id}",
    "task_run": "{from_id}:{from_revision}>{to_id}",
    "workflow_run": "{from_id}:{phase}:{round}>{to_id}",
    "workspace_task": "{from_id}>{to_id}",
    "workspace_run": "{from_id}>{to_id}",
    "composition_member": "{from_id}:{phase}>{to_id}",
    "composition_run": "{from_id}:{phase}>{to_id}",
}


def _error(code, cursor, detail):
    return ControlError(code, f"Observation event {cursor} {detail}")


def _integer(value, name, code, *, low, high):
    if isinstance(value, bool) or not isinstance(value, int):
        raise ControlError(code, f"{name} must be an integer, not {type(value).__name__}.")
    if not low <= value <= high:
        raise ControlError(code, f"{name} must be between {low} and {high}.")
    return value


def _cursor(value, name):
    return _integer(value, name, "invalid_cursor", low=0, high=MAX_CURSOR)


def _require_schema(store):
    if store.config.get("schema", 0) < SCHEMA_REQUIRED:
        raise ControlError(
            "migration_required", "Observation history requires migrate --offline to schema 14."
        )


def _decode(row):
    """Decode one immutable ledger row; corrupted history is refused, never repaired."""
    cursor = row["cursor"]
    if isinstance(cursor, bool) or not isinstance(cursor, int) or cursor < 1:
        raise ControlError("invalid_event", "Observation ledger holds a non-positive cursor.")
    if row["contract"] != CONTRACT:
        raise _error(
            "invalid_contract", cursor, f"declares contract {row['contract']!r}, not {CONTRACT!r}."
        )
    kind = row["kind"]
    if kind not in KINDS:
        raise _error("unknown_event", cursor, f"declares unknown kind {kind!r}.")
    entity_kind, entity_id = row["entity_kind"], row["entity_id"]
    for name, value in (("entity_kind", entity_kind), ("entity_id", entity_id)):
        if not isinstance(value, str) or not value:
            raise _error("invalid_event", cursor, f"has an empty or non-text {name}.")
    stamps = {}
    for name in ("recorded_at", "effective_at"):
        stamp = row[name]
        if (
            isinstance(stamp, bool)
            or not isinstance(stamp, (int, float))
            or not math.isfinite(stamp)
        ):
            raise _error("invalid_event", cursor, f"has a non-numeric {name}.")
        stamps[name] = float(stamp)
    raw = row["payload"]
    if not isinstance(raw, str):
        raise _error("invalid_payload", cursor, "carries a non-text payload.")
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        raise _error("invalid_payload", cursor, "carries an undecodable payload.") from exc
    if not isinstance(payload, dict):
        raise _error("invalid_payload", cursor, "carries a payload that is not a JSON object.")
    return {
        "cursor": cursor,
        "contract": CONTRACT,
        "kind": kind,
        "entity_kind": entity_kind,
        "entity_id": entity_id,
        "recorded_at": stamps["recorded_at"],
        "effective_at": stamps["effective_at"],
        "payload": payload,
    }


def _select(db, after, through, limit):
    return db.execute(_SELECT, (after, through, limit)).fetchall()


def _edge_identity(kind, entry, cursor):
    template = _EDGE_IDENTITIES.get(kind)
    if template is None:
        raise _error("unknown_entity", cursor, f"summarises unknown edge kind {kind!r}.")
    values = {}
    for field in [part.split("}", 1)[0] for part in template.split("{")[1:]]:
        value = entry.get(field)
        if isinstance(value, bool) or not isinstance(value, (str, int)) or value == "":
            raise _error(
                "invalid_payload", cursor, f"summarises a {kind!r} edge without {field}."
            )
        values[field] = str(value)
    return template.format_map(values)


def _seed(event, nodes, edges, telemetry):
    """Seed the fold from the one baseline and report the fidelity it declares."""
    payload, cursor = event["payload"], event["cursor"]
    if event["entity_kind"] != BASELINE_ENTITY or event["entity_id"] != BASELINE:
        raise _error("invalid_payload", cursor, "does not identify the store baseline.")
    fidelity = payload.get("fidelity")
    if fidelity not in FIDELITIES:
        raise _error("invalid_payload", cursor, f"declares unknown fidelity {fidelity!r}.")
    summarised = False
    for section, bucket, marker, identify in (
        ("nodes", nodes, "node", None),
        ("edges", edges, "edge", _edge_identity),
    ):
        groups = payload.get(section)
        if not isinstance(groups, dict):
            raise _error("invalid_payload", cursor, f"has no {section} object.")
        for kind, listed in sorted(groups.items()):
            if not isinstance(listed, list):
                raise _error(
                    "invalid_payload", cursor, f"has a non-list {section} group {kind!r}."
                )
            group = bucket.setdefault(kind, {})
            for entry in listed:
                if not isinstance(entry, dict) or entry.get(marker) != kind:
                    raise _error(
                        "invalid_payload", cursor, f"has an unlabelled {kind!r} {marker}."
                    )
                if identify is None:
                    identity = entry.get("id")
                    if not isinstance(identity, str) or not identity:
                        raise _error(
                            "invalid_payload", cursor, f"summarises a {kind!r} node with no id."
                        )
                else:
                    identity = identify(kind, entry, cursor)
                if identity in group:
                    raise _error(
                        "duplicate_entity", cursor, f"summarises duplicate {kind!r} identity."
                    )
                group[identity] = entry
                summarised = True
    series = payload.get("telemetry")
    if not isinstance(series, list):
        raise _error("invalid_payload", cursor, "has no telemetry list.")
    for entry in series:
        if not isinstance(entry, dict) or entry.get("node") != RUN_TELEMETRY:
            raise _error("invalid_payload", cursor, "has an unlabelled telemetry entry.")
        run_id = entry.get("run_id")
        if not isinstance(run_id, str) or not run_id:
            raise _error("invalid_payload", cursor, "summarises telemetry with no run id.")
        if run_id in telemetry:
            raise _error("duplicate_entity", cursor, "summarises duplicate run telemetry.")
        telemetry[run_id] = entry
        summarised = True
    # A baseline that summarised nothing lost nothing, so folds from it are exact.
    return fidelity if summarised else COMPLETE


def _fold(event, nodes, edges, telemetry):
    """Apply one appended event; node state replaces its node instead of duplicating it."""
    kind, entity_kind = event["kind"], event["entity_kind"]
    identity, payload, cursor = event["entity_id"], event["payload"], event["cursor"]
    if kind in (NODE_CREATED, NODE_STATE):
        group = nodes.get(entity_kind)
        if group is None:
            raise _error(
                "unknown_entity", cursor, f"names node kind {entity_kind!r}, absent from baseline."
            )
        if payload.get("node") != entity_kind or payload.get("id") != identity:
            raise _error("invalid_payload", cursor, f"is not a {entity_kind!r} node.")
        if kind == NODE_CREATED and identity in group:
            raise _error("duplicate_entity", cursor, f"creates duplicate {entity_kind!r} node.")
        if kind == NODE_STATE and identity not in group:
            raise _error("missing_entity", cursor, f"updates an unknown {entity_kind!r} node.")
        group[identity] = payload
    elif kind == EDGE_CREATED:
        group = edges.get(entity_kind)
        if group is None:
            raise _error(
                "unknown_entity", cursor, f"names edge kind {entity_kind!r}, absent from baseline."
            )
        if payload.get("edge") != entity_kind:
            raise _error("invalid_payload", cursor, f"is not a {entity_kind!r} edge.")
        if _edge_identity(entity_kind, payload, cursor) != identity:
            raise _error("invalid_payload", cursor, f"identifies a different {entity_kind!r} edge.")
        if identity in group:
            raise _error("duplicate_entity", cursor, f"creates duplicate {entity_kind!r} edge.")
        group[identity] = payload
    else:
        if entity_kind != TELEMETRY_ENTITY:
            raise _error(
                "unknown_entity", cursor, f"records telemetry for {entity_kind!r}, not a run."
            )
        if payload.get("node") != RUN_TELEMETRY or payload.get("run_id") != identity:
            raise _error("invalid_payload", cursor, "carries telemetry for a different run.")
        if identity in telemetry:
            raise _error("duplicate_entity", cursor, "records duplicate run telemetry.")
        telemetry[identity] = payload


def _ordered(section):
    return {
        kind: {key: group[key] for key in sorted(group)} for kind, group in sorted(section.items())
    }


def events(store, after=0, limit=DEFAULT_LIMIT, through=None):
    """Return one bounded page of decoded events strictly after an exclusive cursor."""
    _require_schema(store)
    after = _cursor(after, "after")
    limit = _integer(limit, "limit", "invalid_limit", low=1, high=MAX_LIMIT)
    bound = MAX_CURSOR if through is None else _cursor(through, "through")
    with store.db() as db:
        # One extra row decides has_more; it is never decoded or returned.
        rows = _select(db, after, bound, limit + 1)
    page = [_decode(row) for row in rows[:limit]]
    return {
        "contract": CONTRACT,
        "version": VERSION,
        "after": after,
        "limit": limit,
        "through": None if through is None else bound,
        "events": page,
        "next_cursor": page[-1]["cursor"] if page else after,
        "has_more": len(rows) > limit,
    }


def replay(store, through=None):
    """Fold the baseline and every later event through an inclusive cursor into a snapshot."""
    _require_schema(store)
    requested = None if through is None else _cursor(through, "through")
    nodes, edges, telemetry = {}, {}, {}
    fidelity, cursor, seen = None, 0, False
    with store.db() as db:
        # Pin a high-water cursor once. Appends remain visible to later replays but
        # cannot move the boundary of this replay while it is paging.
        bound = (
            db.execute("SELECT coalesce(max(cursor),0) FROM observation_events").fetchone()[0]
            if requested is None
            else requested
        )
        while True:
            rows = _select(db, cursor, bound, PAGE)
            for row in rows:
                event = _decode(row)
                cursor = event["cursor"]
                if event["kind"] == BASELINE:
                    if seen:
                        raise _error(
                            "duplicate_baseline", cursor, "is a second baseline; history is unfoldable."
                        )
                    seen, fidelity = True, _seed(event, nodes, edges, telemetry)
                elif not seen:
                    raise _error(
                        "event_before_baseline", cursor, "precedes the baseline; history is unfoldable."
                    )
                else:
                    _fold(event, nodes, edges, telemetry)
            if len(rows) < PAGE:
                break
    if not seen:
        raise ControlError(
            "missing_baseline", "Observation history holds no baseline event through this cursor."
        )
    return {
        "contract": CONTRACT,
        "version": VERSION,
        "cursor": cursor,
        "fidelity": fidelity,
        "nodes": _ordered(nodes),
        "edges": _ordered(edges),
        "telemetry": {run_id: telemetry[run_id] for run_id in sorted(telemetry)},
    }
