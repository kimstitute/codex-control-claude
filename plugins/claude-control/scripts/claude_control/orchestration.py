"""Observe executions and inspect assignment reports without accepting their claims."""

import hashlib
import json
import math
import time
from collections import Counter

from .assignments import MAX_BYTES, read_snapshot, validate_report
from .store import TERMINAL, ControlError


def report(store, run_id):
    store.refresh()
    row = store.get_run(run_id)
    session = store.session(row["session_id"])
    output = dict(
        run_id=run_id,
        execution_status=row["status"],
        execution_reason=row["reason"],
        requested_model=session["model"],
        actual_models=row["actual_models"],
        contract_status="not_checked",
        format_status="pending",
        errors=[],
        agent_status=None,
        acceptance="unreviewed",
        report=None,
    )
    if row["status"] not in TERMINAL:
        return output
    output["format_status"] = "unavailable"
    # Only a session's original reservation can have a delegate contract.
    # Followups and restarts remain unstructured even if their input resembles one.
    if row["resume"]:
        output["contract_status"] = "unsupported"
        return output
    # Schema 3 uses an ordinary rowid table and never deletes/reinserts runs.
    # A future purge or WITHOUT ROWID migration must preserve first-run identity.
    with store.db() as db:
        first = db.execute(
            "SELECT id FROM runs WHERE session_id=? ORDER BY rowid LIMIT 1",
            (row["session_id"],),
        ).fetchone()
    if first["id"] != run_id:
        output["contract_status"] = "unsupported"
        return output
    try:
        with (store.run_dir(run_id) / "prompt.txt").open("rb") as handle:
            raw = handle.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ControlError("invalid_snapshot", "Saved prompt exceeds the size limit.")
        prompt = raw.decode("utf-8")
        snapshot = read_snapshot(prompt)
        if snapshot is None:
            output["contract_status"] = "unsupported"
            return output
        intent = dict(
            prompt=prompt,
            timeout=row["timeout"],
            name=session["name"],
            model=session["model"],
            role=session["role"],
            project=session["project"],
            session_id=None,
            acknowledge_context=False,
            restart=False,
        )
        digest = hashlib.sha256(json.dumps(intent, sort_keys=True).encode()).hexdigest()
        assignment = snapshot["assignment"]
        if (
            digest != row["fingerprint"]
            or any(assignment[key] != session[key] for key in ("name", "model", "role", "project"))
            or assignment["timeout"] != row["timeout"]
        ):
            raise ControlError(
                "invalid_snapshot", "Saved assignment does not match the run intent."
            )
    except (ControlError, OSError, ValueError) as exc:
        output.update(contract_status="invalid", errors=[str(exc)])
        return output
    output.update(
        contract_status="supported",
        task_id=assignment["id"],
        role=assignment["role"],
        role_version=snapshot["role"]["version"],
    )
    if row["status"] != "completed":
        return output
    try:
        # Verify the very bytes being read, including if the artifact changed after get_run.
        with (store.run_dir(run_id) / "result.json").open("rb") as handle:
            result_bytes = handle.read(32 * 1024 * 1024 + 1)
        if (
            len(result_bytes) > 32 * 1024 * 1024
            or hashlib.sha256(result_bytes).hexdigest() != row["result_sha256"]
        ):
            raise ControlError(
                "result_integrity", "Result integrity check failed during report read."
            )
        result = json.loads(result_bytes)
        if not isinstance(result, dict) or result.get("validated_success") is not True:
            raise ControlError("result_integrity", "Result is not a validated execution artifact.")
    except (ControlError, OSError, ValueError) as exc:
        output.update(errors=[str(exc)])
        return output
    try:
        parsed = validate_report(result.get("response"), snapshot)
    except ControlError as exc:
        output.update(format_status="invalid", errors=[str(exc)])
        return output
    output.update(format_status="valid", agent_status=parsed["status"], report=parsed)
    return output


def observe(store, run_ids, seconds):
    if not math.isfinite(seconds) or not 0 <= seconds <= 60:
        raise ControlError("invalid_wait", "Observation must be 0–60 seconds.")
    ids = list(dict.fromkeys(run_ids))
    if not 1 <= len(ids) <= 128:
        raise ControlError("invalid_runs", "Select between 1 and 128 distinct run IDs.")
    # Reject invalid/unowned IDs before waiting or refreshing other records.
    for run_id in ids:
        store.get_run(run_id)
    deadline = time.monotonic() + seconds
    while True:
        store.refresh()
        rows = [store.get_run(run_id) for run_id in ids]
        done = all(row["status"] in TERMINAL for row in rows)
        attention = [
            row["id"]
            for row in rows
            if row["status"] == "unknown"
            or row["status"] in TERMINAL
            and row["status"] != "completed"
        ]
        if done or attention or time.monotonic() >= deadline:
            return dict(
                runs=rows,
                counts=dict(Counter(row["status"] for row in rows)),
                execution_done=done,
                needs_attention=attention,
                wait_reason="attention" if attention else "terminal" if done else "deadline",
            )
        time.sleep(min(0.2, max(0, deadline - time.monotonic())))
