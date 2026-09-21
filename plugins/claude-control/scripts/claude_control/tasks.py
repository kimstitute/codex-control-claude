"""Logical tasks, immutable revisions, atomic dispatch and explicit review decisions."""

import json
import re
import time
import uuid

from . import task_contracts as contract
from .assignments import MAX_BYTES, normalize_assignment
from .store import ACTIVE, ControlError, alive, boot_id, pid_namespace


def _require_schema(store):
    if store.config["schema"] < 4:
        raise ControlError(
            "migration_required", "Task commands require explicit migrate --offline."
        )


def _operation(db, operation_id, intent):
    if not isinstance(operation_id, str) or not operation_id.strip() or len(operation_id) > 200:
        raise ControlError("invalid_request", "Provide an operation ID of 1–200 characters.")
    fingerprint = contract.digest(contract.canonical(intent))
    row = db.execute(
        "SELECT * FROM task_operations WHERE operation_id=?", (operation_id,)
    ).fetchone()
    if row and row["fingerprint"] != fingerprint:
        raise ControlError("request_conflict", "Operation ID already belongs to different input.")
    return fingerprint, json.loads(row["response"]) if row else None


def _record(db, operation_id, fingerprint, response):
    db.execute(
        "INSERT INTO task_operations VALUES(?,?,?)",
        (
            operation_id,
            fingerprint,
            contract.canonical(response),
        ),
    )


def _operation_recorded(store, operation_id):
    """Let an existing operation reach its transactional deduplication path."""
    if store.config["schema"] < 4:
        return False
    with store.db() as db:
        return bool(
            db.execute(
                "SELECT 1 FROM task_operations WHERE operation_id=?", (operation_id,)
            ).fetchone()
        )


def _task(db, task_id, revision=None):
    row = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    if not row:
        raise ControlError("task_not_found", "Unknown task UUID.")
    if revision is not None and (type(revision) is not int or revision != row["current_revision"]):
        raise ControlError("stale_revision", "Select the current task revision explicitly.")
    return row


def _revision(db, task_id, revision):
    row = db.execute(
        "SELECT * FROM task_revisions WHERE task_id=? AND revision=?",
        (
            task_id,
            revision,
        ),
    ).fetchone()
    if not row:
        raise ControlError("revision_not_found", "Unknown task revision.")
    return row


def _assignment(store, assignment):
    assignment = normalize_assignment(assignment)
    assignment["project"] = store.project(assignment["project"])
    return assignment


def _context(store, db, session_id, parent, assignment, *, inherit_effort):
    if not session_id:
        if parent:
            raise ControlError("context_changed", "A parent run requires an explicit session.")
        return None
    session = db.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
    if not session:
        raise ControlError("session_not_found", "Unknown managed session UUID.")
    session_effort = session["effort"] if store.config["schema"] >= 9 else None
    if "effort" in assignment:
        if assignment["effort"] != session_effort:
            raise ControlError(
                "session_incompatible", "Task effort must match the session's pinned effort."
            )
    elif inherit_effort and session_effort is not None:
        assignment["effort"] = session_effort
    elif not inherit_effort and assignment.get("effort") != session_effort:
        raise ControlError(
            "session_incompatible", "Frozen task effort must match the session's pinned effort."
        )
    if any(session[k] != assignment[k] for k in ("model", "role", "project")):
        raise ControlError(
            "session_incompatible", "Task model, role and project must match session."
        )
    latest = db.execute(
        "SELECT * FROM runs WHERE session_id=? ORDER BY rowid DESC LIMIT 1", (session_id,)
    ).fetchone()
    if not latest or latest["id"] != parent:
        raise ControlError(
            "context_changed", "Select the exact latest parent run; context changed."
        )
    if latest["status"] in ACTIVE or session["blocked"]:
        raise ControlError("session_busy", "Active or ambiguous session cannot be revised.")
    return session["backend_id"]


def _insert_revision(db, task_id, revision, prompt, parent, backend, acknowledge):
    db.execute(
        "INSERT INTO task_revisions VALUES(?,?,?,?,?,?,?,?)",
        (
            task_id,
            revision,
            prompt,
            contract.digest(prompt),
            parent,
            backend,
            int(acknowledge),
            time.time(),
        ),
    )


def create(
    store,
    assignment,
    operation_id,
    *,
    session_ref=None,
    parent_run_id=None,
    acknowledge_context=False,
):
    assignment = _assignment(store, assignment)
    if "effort" in assignment and not _operation_recorded(store, operation_id):
        store.preflight_effort(assignment["effort"])
    store.refresh()
    with store.db(write=True) as db:
        return create_in(
            store,
            db,
            assignment,
            operation_id,
            session_ref=session_ref,
            parent_run_id=parent_run_id,
            acknowledge_context=acknowledge_context,
        )


def create_in(
    store,
    db,
    assignment,
    operation_id,
    *,
    session_ref=None,
    parent_run_id=None,
    acknowledge_context=False,
):
    _require_schema(store)
    assignment = _assignment(store, assignment)
    intent = dict(
        kind="create",
        assignment=assignment,
        session=session_ref,
        parent=parent_run_id,
        acknowledge_context=acknowledge_context,
    )
    fingerprint, prior = _operation(db, operation_id, intent)
    if prior:
        return {**prior, "deduplicated": True}
    backend = _context(store, db, session_ref, parent_run_id, assignment, inherit_effort=True)
    task_id = str(uuid.uuid4())
    prompt = contract.render(assignment, task_id, 1, parent_run_id, backend)
    db.execute(
        "INSERT INTO tasks VALUES(?,?,?,1,NULL,?)",
        (
            task_id,
            assignment["name"],
            session_ref,
            time.time(),
        ),
    )
    _insert_revision(db, task_id, 1, prompt, parent_run_id, backend, acknowledge_context)
    result = dict(id=task_id, current_revision=1, session_id=session_ref)
    _record(db, operation_id, fingerprint, result)
    return {**result, "deduplicated": False}


def revise(
    store,
    task_id,
    base_revision,
    assignment,
    operation_id,
    *,
    parent_run_id=None,
    acknowledge_context=False,
    message_ids=None,
    redeliver_messages=False,
):
    assignment = _assignment(store, assignment)
    if "effort" in assignment and not _operation_recorded(store, operation_id):
        store.preflight_effort(assignment["effort"])
    store.refresh()
    with store.db(write=True) as db:
        return revise_in(
            store,
            db,
            task_id,
            base_revision,
            assignment,
            operation_id,
            parent_run_id=parent_run_id,
            acknowledge_context=acknowledge_context,
            message_ids=message_ids,
            redeliver_messages=redeliver_messages,
        )


def revise_in(
    store,
    db,
    task_id,
    base_revision,
    assignment,
    operation_id,
    *,
    parent_run_id=None,
    acknowledge_context=False,
    message_ids=None,
    redeliver_messages=False,
    workflow_id=None,
    workspace_id=None,
):
    _require_schema(store)
    assignment = _assignment(store, assignment)
    intent = dict(
        kind="revise",
        task=task_id,
        base=base_revision,
        assignment=assignment,
        parent=parent_run_id,
        acknowledge_context=acknowledge_context,
    )
    from . import messages

    selected_ids = messages.normalize_ids(message_ids)
    if type(redeliver_messages) is not bool:
        raise ControlError("invalid_messages", "redeliver-messages must be a boolean.")
    if selected_ids or redeliver_messages:
        messages._require(store)
        intent.update(message_ids=selected_ids, redeliver_messages=redeliver_messages)
    fingerprint, prior = _operation(db, operation_id, intent)
    if prior:
        return {**prior, "deduplicated": True}
    from .workflow import mutation_guard

    mutation_guard(store, db, task_id, workflow_id, workspace_id)
    task = _task(db, task_id, base_revision)
    previous = contract.read(_revision(db, task_id, base_revision)["prompt"])
    previous_effort = previous["assignment"].get("effort")
    if "effort" in assignment:
        if assignment["effort"] != previous_effort:
            raise ControlError("revision_conflict", "Effort is fixed across task revisions.")
    elif previous_effort is not None:
        assignment["effort"] = previous_effort
    backend = _context(
        store, db, task["session_id"], parent_run_id, assignment, inherit_effort=False
    )
    if any(previous["assignment"][k] != assignment[k] for k in ("model", "role", "project", "id")):
        raise ControlError("revision_conflict", "Model, role, project and assignment ID are fixed.")
    number = base_revision + 1
    prompt = contract.render(assignment, task_id, number, parent_run_id, backend, previous=previous)
    _insert_revision(db, task_id, number, prompt, parent_run_id, backend, acknowledge_context)
    if selected_ids:
        messages.select_in(store, db, task, number, selected_ids, redeliver_messages)
    db.execute(
        "UPDATE tasks SET current_revision=?,active_run_id=NULL WHERE id=?", (number, task_id)
    )
    if store.config["schema"] >= 5:
        db.execute(
            "UPDATE queue_entries SET state='superseded' WHERE task_id=? AND state='waiting'",
            (task_id,),
        )
    result = dict(id=task_id, current_revision=number, session_id=task["session_id"])
    _record(db, operation_id, fingerprint, result)
    return {**result, "deduplicated": False}


def _never_started(row):
    if row["status"] not in ("launch_failed", "cancelled") or row["child_pid"] or row["started"]:
        return False
    if row["worker_pid"]:
        if row["boot"] == boot_id() and row["worker_namespace"] != pid_namespace():
            return False
        if alive(row["worker_pid"], row["worker_start"], row["boot"]):
            return False
    return True


def preflight(store, task_id, revision, operation_id=None):
    """Probe an explicitly pinned effort outside any write transaction."""
    _require_schema(store)
    if operation_id is not None and _operation_recorded(store, operation_id):
        return None
    with store.db() as db:
        task = _task(db, task_id, revision)
        snapshot = contract.read(_revision(db, task["id"], revision)["prompt"])
    effort = snapshot["assignment"].get("effort")
    if effort is not None:
        store.preflight_effort(effort)
    return effort


def submit(store, task_id, revision, operation_id, *, retry=False):
    _require_schema(store)
    preflight(store, task_id, revision, operation_id)
    store.refresh()  # Materializes claim expiry before reservation; workers use status CAS.
    with store.db(write=True) as db:
        from .workflow import mutation_guard

        mutation_guard(store, db, task_id)
        return submit_in(store, db, task_id, revision, operation_id, retry=retry)


def submit_in(store, db, task_id, revision, operation_id, *, retry=False, workspace_id=None):
    """Reserve in the caller's write transaction; launch only after it commits."""
    fingerprint, prior = _operation(
        db,
        operation_id,
        dict(
            kind="retry" if retry else "submit",
            task=task_id,
            revision=revision,
        ),
    )
    if prior:
        return prior["run_id"], False
    task = _task(db, task_id, revision)
    version = _revision(db, task_id, revision)
    snapshot = contract.read(version["prompt"])
    if contract.digest(version["prompt"]) != version["prompt_sha256"]:
        raise ControlError("invalid_snapshot", "Revision digest changed; create a new revision.")
    attempts = db.execute(
        "SELECT r.* FROM runs r JOIN task_runs t ON t.run_id=r.id "
        "WHERE t.task_id=? AND t.revision=? ORDER BY r.rowid",
        (task_id, revision),
    ).fetchall()
    if attempts and not retry:
        raise ControlError(
            "already_submitted", "Reuse the operation ID, or explicitly retry/revise."
        )
    if retry and (not attempts or not all(_never_started(row) for row in attempts)):
        raise ControlError(
            "retry_denied", "Retry requires proof that every attempt never started Claude."
        )
    session_ref = task["session_id"]
    if session_ref:
        session = db.execute("SELECT * FROM sessions WHERE id=?", (session_ref,)).fetchone()
        latest = db.execute(
            "SELECT * FROM runs WHERE session_id=? ORDER BY rowid DESC LIMIT 1",
            (session_ref,),
        ).fetchone()
        expected = attempts[-1]["id"] if attempts else version["expected_parent_run_id"]
        expected_backend = (
            attempts[-1]["backend_id"] if attempts else version["expected_backend_id"]
        )
        if not latest or latest["id"] != expected or session["backend_id"] != expected_backend:
            raise ControlError(
                "context_changed", "Session advanced or restarted after this revision."
            )
        if session["blocked"] or latest["status"] in ACTIVE:
            raise ControlError("session_busy", "Session has an active or ambiguous run.")
    assignment = snapshot["assignment"]
    # Retry or a new revision after a stopped, never-executed turn keeps the
    # same reserved backend UUID, but must not resume a nonexistent transcript.
    fresh_backend = bool(session_ref) and store.backend_unstarted(
        db, session_ref, session["backend_id"]
    )
    acknowledge = bool(version["acknowledge_context"])
    if retry and version["expected_parent_run_id"]:
        parent = db.execute(
            "SELECT * FROM runs WHERE id=?", (version["expected_parent_run_id"],)
        ).fetchone()
        if parent["status"] != "completed" or not store.result_valid(parent):
            if not acknowledge:
                raise ControlError("context_uncertain", "Inspect parent context before revising.")
    prompt = version["prompt"]
    if store.config["schema"] >= 5:
        from .scheduler import materialize

        prompt = materialize(store, db, task_id, revision, prompt)
        if store.config["schema"] >= 6:
            from . import messages

            prompt = messages.materialize(store, db, task_id, revision, prompt, attempts)
        if store.config["schema"] >= 7:
            from . import workflow

            prompt = workflow.materialize(store, db, task_id, revision, prompt)
        if store.config["schema"] >= 8:
            from . import workspace

            prompt = workspace.materialize(store, db, task_id, revision, prompt, workspace_id)
        for attempt in attempts:
            previous = db.execute(
                "SELECT prompt FROM execution_inputs WHERE run_id=?", (attempt["id"],)
            ).fetchone()
            previous_prompt = previous["prompt"] if previous else version["prompt"]
            if prompt != previous_prompt:
                raise ControlError("input_changed", "Retry input changed; create a new revision.")
    run_id, created = store.reserve_in(
        db,
        prompt=prompt,
        request_id=operation_id,
        timeout=assignment["timeout"],
        name=None if session_ref else "task-" + task_id,
        model=assignment["model"],
        effort=assignment.get("effort"),
        role=assignment["role"],
        project=assignment["project"],
        session_ref=session_ref,
        acknowledge_context=acknowledge or retry,
        resume_unstarted=fresh_backend,
    )
    if not created:
        raise ControlError("request_conflict", "Operation ID was already used by a legacy run.")
    row = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    db.execute(
        "INSERT INTO task_runs VALUES(?,?,?,?,?)",
        (
            run_id,
            task_id,
            revision,
            contract.read(prompt)["protocol"],
            contract.digest(prompt),
        ),
    )
    if store.config["schema"] >= 5:
        if contract.read(prompt)["protocol"] in (
            contract.EXECUTION_CONTRACT,
            contract.MESSAGE_CONTRACT,
            contract.WORKFLOW_CONTRACT,
            contract.WORKSPACE_CONTRACT,
        ):
            db.execute(
                "INSERT INTO execution_inputs VALUES(?,?,?,?)",
                (run_id, prompt, contract.digest(prompt), version["prompt_sha256"]),
            )
        db.execute(
            "UPDATE queue_entries SET state='reserved',run_id=?,blocked_reason=NULL,"
            "terminal_block=0 WHERE task_id=? AND revision=? AND state IN ('waiting','reserved')",
            (run_id, task_id, revision),
        )
    if store.config["schema"] >= 6:
        messages.bind_in(db, task_id, revision, run_id)
    changed = db.execute(
        "UPDATE tasks SET session_id=?,active_run_id=? WHERE id=? AND current_revision=? "
        "AND active_run_id IS ?",
        (row["session_id"], run_id, task_id, revision, task["active_run_id"]),
    ).rowcount
    if changed != 1:
        raise ControlError("task_conflict", "Task changed during reservation.")
    if store.config["schema"] >= 7:
        workflow.record_in(store, db, task_id, revision, run_id)
    if store.config["schema"] >= 8:
        workspace.record_in(store, db, task_id, revision, run_id, prompt)
    _record(db, operation_id, fingerprint, dict(run_id=run_id))
    return run_id, True


def _evidence(evidence, criteria):
    required = {str(i + 1) for i in range(len(criteria))}
    if (
        not isinstance(evidence, dict)
        or set(evidence) != required
        or any(not isinstance(v, str) or not v.strip() for v in evidence.values())
        or len(contract.canonical(evidence).encode()) > MAX_BYTES
    ):
        raise ControlError(
            "invalid_evidence", "Supply nonempty evidence for every 1-based criterion ID."
        )


def _decision(
    store,
    task_id,
    revision,
    run_id,
    result_sha256,
    evidence,
    operation_id,
    *,
    kind,
    reviewer,
    recommendation,
):
    store.refresh()
    with store.db(write=True) as db:
        return _decision_in(
            store,
            db,
            task_id,
            revision,
            run_id,
            result_sha256,
            evidence,
            operation_id,
            kind=kind,
            reviewer=reviewer,
            recommendation=recommendation,
        )


def _decision_in(
    store,
    db,
    task_id,
    revision,
    run_id,
    result_sha256,
    evidence,
    operation_id,
    *,
    kind,
    reviewer,
    recommendation,
):
    _require_schema(store)
    if not isinstance(reviewer, str) or not reviewer.strip() or len(reviewer) > 120:
        raise ControlError("invalid_reviewer", "Reviewer must be 1–120 characters.")
    if recommendation not in ("approve", "revise", "blocked"):
        raise ControlError("invalid_review", "Select approve, revise or blocked.")
    if not isinstance(result_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", result_sha256):
        raise ControlError("invalid_digest", "Use the exact SHA-256 from the completed run.")
    fingerprint, prior = _operation(
        db,
        operation_id,
        dict(
            kind=kind,
            task=task_id,
            revision=revision,
            run=run_id,
            digest=result_sha256,
            evidence=evidence,
            reviewer=reviewer,
            recommendation=recommendation,
        ),
    )
    if prior:
        return {**prior, "deduplicated": True}
    task = _task(db, task_id, revision if kind == "accept" else None)
    version = _revision(db, task_id, revision)
    snapshot = contract.read(version["prompt"])
    _evidence(evidence, snapshot["assignment"]["acceptance_criteria"])
    row = db.execute(
        "SELECT r.* FROM runs r JOIN task_runs t ON t.run_id=r.id "
        "WHERE r.id=? AND t.task_id=? AND t.revision=?",
        (run_id, task_id, revision),
    ).fetchone()
    if not row or row["status"] != "completed" or row["result_sha256"] != result_sha256:
        raise ControlError(
            "result_not_reviewable", "Select a completed run and its exact result digest."
        )
    checked = contract.inspect_run(store, db, row)
    if checked["format_status"] != "valid":
        raise ControlError("invalid_report", "A well-formed report is required for a decision.")
    if kind == "accept" and (
        checked["agent_status"] != "complete" or task["active_run_id"] != run_id
    ):
        raise ControlError(
            "result_not_acceptable", "Only the current complete result can be accepted."
        )
    if kind == "accept" and store.config["schema"] >= 8:
        from .workspace import accept_guard

        accept_guard(store, db, task_id, run_id)
    decision = dict(
        id=str(uuid.uuid4()),
        task_id=task_id,
        revision=revision,
        run_id=run_id,
        result_sha256=result_sha256,
        kind=kind,
        reviewer=reviewer,
        recommendation=recommendation,
        evidence=evidence,
        created=time.time(),
    )
    db.execute(
        "INSERT INTO review_decisions VALUES(:id,:task_id,:revision,:run_id,:result_sha256,"
        ":kind,:reviewer,:recommendation,:evidence,:created)",
        {**decision, "evidence": contract.canonical(evidence)},
    )
    _record(db, operation_id, fingerprint, decision)
    return {**decision, "deduplicated": False}


def review(
    store,
    task_id,
    revision,
    run_id,
    result_sha256,
    evidence,
    operation_id,
    *,
    reviewer,
    recommendation,
):
    return _decision(
        store,
        task_id,
        revision,
        run_id,
        result_sha256,
        evidence,
        operation_id,
        kind="review",
        reviewer=reviewer,
        recommendation=recommendation,
    )


def accept(store, task_id, revision, run_id, result_sha256, evidence, operation_id):
    return _decision(
        store,
        task_id,
        revision,
        run_id,
        result_sha256,
        evidence,
        operation_id,
        kind="accept",
        reviewer="codex",
        recommendation="approve",
    )


def _state(store, db, task, runs):
    current = [r for r in runs if r["revision"] == task["current_revision"]]
    if not current:
        version = _revision(db, task["id"], task["current_revision"])
        if task["session_id"]:
            latest = db.execute(
                "SELECT id,backend_id FROM runs WHERE session_id=? ORDER BY rowid DESC LIMIT 1",
                (task["session_id"],),
            ).fetchone()
            if latest and (
                latest["id"] != version["expected_parent_run_id"]
                or latest["backend_id"] != version["expected_backend_id"]
            ):
                return "blocked", "context_changed"
        if store.config["schema"] >= 5:
            queued = db.execute(
                "SELECT * FROM queue_entries WHERE task_id=? AND revision=? AND state='waiting'",
                (task["id"], task["current_revision"]),
            ).fetchone()
            if queued:
                return (
                    ("blocked", queued["blocked_reason"])
                    if queued["blocked_reason"]
                    else ("queued", "awaiting_dispatch")
                )
        return "queued", "awaiting_submit"
    row = current[-1]
    if row["status"] == "unknown":
        return "blocked", "unknown_execution"
    if row["status"] in ACTIVE:
        return "running", row["status"]
    if row["status"] != "completed":
        reason = row["reason"] or row["status"]
        model = db.execute(
            "SELECT model FROM sessions WHERE id=?", (row["session_id"],)
        ).fetchone()[0]
        wrong_models = any(
            not value.startswith("claude-" + model + "-")
            for value in json.loads(row["actual_models"])
        )
        if wrong_models or "model mismatch" in reason:
            reason = "model_mismatch"
        elif reason == "claim_deadline":
            reason = "reserve_expired"
        return "blocked", reason
    try:
        checked = contract.inspect_run(store, db, row)
        if checked["format_status"] != "valid" or checked["agent_status"] != "complete":
            return "blocked", "invalid_report" if checked[
                "format_status"
            ] != "valid" else "agent_blocked"
    except FileNotFoundError:
        return "blocked", "result_integrity"
    except OSError:
        return "blocked", "result_unreadable"
    except (ControlError, ValueError) as exc:
        return "blocked", getattr(exc, "code", "result_integrity")
    accepted = db.execute(
        "SELECT 1 FROM review_decisions WHERE task_id=? AND revision=? AND run_id=? "
        "AND result_sha256=? AND kind='accept'",
        (task["id"], task["current_revision"], row["id"], row["result_sha256"]),
    ).fetchone()
    return ("accepted", None) if accepted else ("awaiting_review", None)


def show(store, task_id):
    _require_schema(store)
    store.refresh()
    with store.db() as db:
        task = dict(_task(db, task_id))
        versions = [
            dict(row)
            for row in db.execute(
                "SELECT * FROM task_revisions WHERE task_id=? ORDER BY revision",
                (task_id,),
            )
        ]
        runs = [
            dict(row)
            for row in db.execute(
                "SELECT r.*,t.revision FROM task_runs t JOIN runs r ON r.id=t.run_id "
                "WHERE t.task_id=? ORDER BY r.rowid",
                (task_id,),
            )
        ]
        state, reason = _state(store, db, task, runs)
        queue = []
        if store.config["schema"] >= 5:
            queue = [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM queue_entries WHERE task_id=? ORDER BY id", (task_id,)
                )
            ]
        decisions = [
            dict(row)
            for row in db.execute(
                "SELECT * FROM review_decisions WHERE task_id=? ORDER BY rowid",
                (task_id,),
            )
        ]
    for row in runs:
        row["actual_models"] = json.loads(row["actual_models"])
    for decision in decisions:
        decision["evidence"] = json.loads(decision["evidence"])
    return {
        **task,
        "state": state,
        "reason": reason,
        "revisions": versions,
        "runs": runs,
        "decisions": decisions,
        "queue": queue,
    }


def list_tasks(store):
    _require_schema(store)
    with store.db() as db:
        ids = [row[0] for row in db.execute("SELECT id FROM tasks ORDER BY rowid")]
    return {
        "tasks": [
            {
                k: v
                for k, v in show(store, task_id).items()
                if k not in ("runs", "revisions", "decisions", "queue")
            }
            for task_id in ids
        ]
    }
