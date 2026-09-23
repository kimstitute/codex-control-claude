"""Read-only empirical evaluation of recorded composition outcomes and cost."""

import json
import math

from .store import ControlError
from .workspace import FORMAT_REPAIR_SCOPE

ATTRIBUTION_VERSION = "composition-runs.v1"
TOKEN_KEYS = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)


def _json(raw, label):
    try:
        return json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ControlError("invalid_ledger", f"Recorded {label} JSON is invalid.") from exc


def _rate(numerator, denominator):
    if denominator == 0:
        return {
            "value": None,
            "lower": None,
            "upper": None,
            "numerator": numerator,
            "denominator": 0,
        }
    value = numerator / denominator
    z = 1.959963984540054
    z2 = z * z
    scale = 1 + z2 / denominator
    center = (value + z2 / (2 * denominator)) / scale
    radius = (
        z
        * math.sqrt(value * (1 - value) / denominator + z2 / (4 * denominator * denominator))
        / scale
    )
    return {
        "value": value,
        "lower": max(0.0, center - radius),
        "upper": min(1.0, center + radius),
        "numerator": numerator,
        "denominator": denominator,
    }


def _accepted(db, composition_id):
    return db.execute(
        "SELECT d.id FROM composition_results e "
        "JOIN composition_results v ON v.composition_id=e.composition_id AND v.phase='reviewer' "
        "JOIN review_decisions d ON d.task_id=e.task_id AND d.revision=e.revision "
        "AND d.run_id=e.run_id AND d.result_sha256=e.result_sha256 AND d.kind='accept' "
        "WHERE e.composition_id=? AND e.phase='editor' LIMIT 1",
        (composition_id,),
    ).fetchone()


def _outcome(db, row):
    if _accepted(db, row["id"]):
        return "accepted"
    if row["state"] == "awaiting_codex" and row["reason"] == "final_review_ready":
        return "ready"
    pending = {None, "plan_acceptance_required"}
    if row["state"] == "stopped" or (
        row["state"] == "awaiting_codex" and row["reason"] not in pending
    ):
        return "failed"
    return "in_progress"


def _run_ids(db, row):
    result = {
        value[0]
        for value in db.execute(
            "SELECT run_id FROM workflow_runs WHERE workflow_id=?",
            (row["workflow_id"],),
        )
    }
    result.update(
        value[0]
        for value in db.execute(
            "SELECT c.run_id FROM workspace_calls c JOIN composition_members m "
            "ON m.workspace_id=c.workspace_id WHERE m.composition_id=?",
            (row["id"],),
        )
    )
    return sorted(result)


def _format_repaired(db, composition_id):
    prompts = db.execute(
        "SELECT r.prompt FROM composition_members m JOIN task_revisions r ON r.task_id=m.task_id "
        "WHERE m.composition_id=?",
        (composition_id,),
    )
    for prompt in prompts:
        snapshot = _json(prompt[0], "task revision")
        scope = snapshot.get("assignment", {}).get("scope", [])
        if FORMAT_REPAIR_SCOPE in scope:
            return True
    return False


def _operation(db, operation_id):
    row = db.execute(
        "SELECT response FROM task_operations WHERE operation_id=?", (operation_id,)
    ).fetchone()
    return _json(row[0], "operation response") if row else None


def _gate(db, row, policy):
    if not policy.get("test_contract"):
        return "n/a"
    prefix = "composition:" + row["id"] + ":test:"
    baseline = _operation(db, prefix + "baseline")
    post = _operation(db, prefix + "post")
    if baseline and baseline.get("status") != "passed":
        return "baseline_failed"
    if post:
        return "passed" if post.get("status") == "passed" else "failed"
    return "pending" if baseline and baseline.get("status") == "passed" else "not_run"


def _telemetry(db, run_ids):
    if not run_ids:
        return "missing", None, None
    measured = 0
    recorded = 0
    total_cost = 0.0
    totals = {key: 0 for key in TOKEN_KEYS}
    for run_id in run_ids:
        row = db.execute(
            "SELECT usage,provider_cost_usd FROM run_telemetry WHERE run_id=?", (run_id,)
        ).fetchone()
        if not row:
            continue
        recorded += 1
        usage = _json(row["usage"], "run telemetry usage")
        values = [usage.get(key) for key in TOKEN_KEYS]
        valid = row["provider_cost_usd"] is not None and all(
            isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0
            for value in values
        )
        if not valid:
            continue
        measured += 1
        total_cost += row["provider_cost_usd"]
        for key, value in zip(TOKEN_KEYS, values):
            totals[key] += value
    if measured == len(run_ids):
        return "measured", total_cost, totals
    return ("partial" if recorded else "missing"), None, None


def _entry(db, row):
    policy = _json(row["policy"], "composition policy")
    outcome = _outcome(db, row)
    run_ids = _run_ids(db, row)
    telemetry_status, cost, tokens = _telemetry(db, run_ids)
    workflow_row = db.execute(
        "SELECT round FROM workflows WHERE id=?", (row["workflow_id"],)
    ).fetchone()
    successful = outcome in ("ready", "accepted")
    unassisted = bool(
        successful
        and workflow_row
        and workflow_row["round"] == 0
        and not _format_repaired(db, row["id"])
    )
    return {
        "id": row["id"],
        "name": row["name"],
        "origin": "leader_spec" if policy.get("planning_mode") == "leader_spec" else "planned",
        "outcome": outcome,
        "gate": _gate(db, row, policy),
        "unassisted_success": unassisted,
        "run_ids": run_ids,
        "telemetry_status": telemetry_status,
        "provider_cost_usd": cost,
        "tokens": tokens,
    }


def evaluate(store, composition_ids=None):
    """Evaluate all or an explicit list without refreshing or mutating the store."""
    if composition_ids is not None:
        if not composition_ids or len(set(composition_ids)) != len(composition_ids):
            raise ControlError(
                "invalid_arguments", "Composition selections must be nonempty and unique."
            )
    with store.db() as db:
        if composition_ids is None:
            rows = db.execute("SELECT * FROM compositions ORDER BY created,id").fetchall()
        else:
            placeholders = ",".join("?" for _ in composition_ids)
            found = db.execute(
                f"SELECT * FROM compositions WHERE id IN ({placeholders}) ORDER BY created,id",
                tuple(composition_ids),
            ).fetchall()
            by_id = {row["id"]: row for row in found}
            missing = [value for value in composition_ids if value not in by_id]
            if missing:
                raise ControlError(
                    "composition_not_found", "Unknown composition UUID: " + missing[0]
                )
            rows = [by_id[value] for value in composition_ids]
        entries = [_entry(db, row) for row in rows]

    counts = {name: 0 for name in ("in_progress", "failed", "ready", "accepted")}
    telemetry_counts = {name: 0 for name in ("measured", "partial", "missing")}
    for entry in entries:
        counts[entry["outcome"]] += 1
        telemetry_counts[entry["telemetry_status"]] += 1
    successful = counts["ready"] + counts["accepted"]
    terminal = counts["failed"] + successful
    unassisted = sum(entry["unassisted_success"] for entry in entries)
    measured = [entry for entry in entries if entry["telemetry_status"] == "measured"]
    measured_successes = sum(entry["outcome"] in ("ready", "accepted") for entry in measured)
    measured_cost = sum(entry["provider_cost_usd"] for entry in measured)
    token_totals = {key: sum(entry["tokens"][key] for entry in measured) for key in TOKEN_KEYS}
    return {
        "schema_version": store.config["schema"],
        "attribution_version": ATTRIBUTION_VERSION,
        "summary": {
            "selected": len(entries),
            "terminal": terminal,
            **counts,
            "successful": successful,
            "unassisted_success": unassisted,
            "readiness_rate": _rate(successful, terminal),
            "acceptance_rate": _rate(counts["accepted"], terminal),
            "unassisted_success_rate": _rate(unassisted, terminal),
        },
        "telemetry": {
            **telemetry_counts,
            "measured_provider_cost_usd": measured_cost if measured else None,
            "measured_tokens": token_totals if measured else None,
            "successful_measured": measured_successes,
            "cost_per_success_usd": (
                measured_cost / measured_successes if measured_successes else None
            ),
        },
        "compositions": entries,
    }
