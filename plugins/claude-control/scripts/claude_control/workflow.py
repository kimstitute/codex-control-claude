"""A fixed, bounded proposal/review template over tasks, messages and FIFO admission."""

import json
import math
import sqlite3
import time
import uuid

from . import messages, scheduler, tasks
from . import task_contracts as contract
from .assignments import MAX_BYTES
from .store import ACTIVE, ControlError, alive, execution_alive
from .workflow_contracts import PROTOCOL, envelope

STATE_ERRORS = (ControlError, OSError, ValueError, KeyError, TypeError, sqlite3.IntegrityError)


def _error_reason(exc):
    return getattr(
        exc, "code", "result_unreadable" if isinstance(exc, OSError) else "invalid_state"
    )


def _require(store):
    if store.config["schema"] < 7:
        raise ControlError("migration_required", "Workflows require migrate --offline.")


def _get(db, workflow_id):
    row = db.execute("SELECT * FROM workflows WHERE id=?", (workflow_id,)).fetchone()
    if not row:
        raise ControlError("workflow_not_found", "Unknown workflow UUID.")
    return row


def owner(db, task_id):
    return db.execute(
        "SELECT * FROM workflows WHERE worker_task_id=? OR reviewer_task_id=?",
        (task_id, task_id),
    ).fetchone()


def mutation_guard(store, db, task_id, workflow_id=None, workspace_id=None):
    if store.config["schema"] >= 8:
        from .workspace import mutation_guard as workspace_guard

        workspace_guard(store, db, task_id, workspace_id)
    if store.config["schema"] >= 7:
        row = owner(db, task_id)
        if row and row["id"] != workflow_id:
            raise ControlError(
                "workflow_owned",
                "This task is owned by a fixed workflow; inspect workflow status or stop it.",
            )


def _count(db, workflow_id):
    return db.execute(
        "SELECT count(*) FROM workflow_runs WHERE workflow_id=?", (workflow_id,)
    ).fetchone()[0]


def _window(row, policy):
    start = row["first_reserved_at"]
    if start is not None:
        now = time.time()
        if now < start:
            raise ControlError(
                "clock_changed", "Clock precedes the first reservation; inspect manually."
            )
        if now >= start + policy["dispatch_window_seconds"]:
            raise ControlError("window_expired", "Workflow dispatch window expired.")


def _step(db, row):
    return db.execute(
        "SELECT * FROM workflow_steps WHERE workflow_id=? AND phase=? AND round=?",
        (row["id"], row["phase"], row["round"]),
    ).fetchone()


def _members(db, row):
    for key in ("worker_task_id", "reviewer_task_id"):
        task = tasks._task(db, row[key])
        expected = (
            db.execute(
                "SELECT max(revision) FROM workflow_steps WHERE task_id=?", (task["id"],)
            ).fetchone()[0]
            or 1
        )
        if task["current_revision"] != expected:
            raise ControlError("context_changed", "Workflow member revision changed.")
        if (
            not task["session_id"]
            and db.execute("SELECT 1 FROM workflow_runs WHERE task_id=?", (task["id"],)).fetchone()
        ):
            raise ControlError(
                "invalid_state", "A reserved workflow member is missing its session."
            )
        if task["session_id"]:
            session = db.execute(
                "SELECT * FROM sessions WHERE id=?", (task["session_id"],)
            ).fetchone()
            latest = db.execute(
                "SELECT * FROM runs WHERE session_id=? ORDER BY rowid DESC LIMIT 1",
                (task["session_id"],),
            ).fetchone()
            version = tasks._revision(db, task["id"], expected)
            expected_run = task["active_run_id"] or version["expected_parent_run_id"]
            if (
                not latest
                or latest["id"] != expected_run
                or latest["backend_id"] != session["backend_id"]
            ):
                raise ControlError("context_changed", "Workflow session advanced or diverged.")
    worker = tasks._task(db, row["worker_task_id"])
    reviewer = tasks._task(db, row["reviewer_task_id"])
    if worker["session_id"] and worker["session_id"] == reviewer["session_id"]:
        raise ControlError("context_changed", "Worker and reviewer must have independent sessions.")


def _accepted(db, row):
    return db.execute(
        "SELECT d.* FROM review_decisions d JOIN tasks t ON t.id=d.task_id "
        "JOIN runs r ON r.id=t.active_run_id WHERE t.id=? AND d.kind='accept' "
        "AND d.revision=t.current_revision AND d.run_id=t.active_run_id AND d.result_sha256=r.result_sha256",
        (row["worker_task_id"],),
    ).fetchone()


def guard_in(store, db, task_id, revision):
    row = owner(db, task_id)
    if not row:
        return None
    if row["state"] != "active":
        raise ControlError(
            "workflow_inactive", "Workflow automation is stopped; inspect its recorded reason."
        )
    step = _step(db, row)
    if not step or step["task_id"] != task_id or step["revision"] != revision:
        raise ControlError("workflow_step", "Only the current workflow step may reserve a run.")
    if db.execute(
        "SELECT 1 FROM workflow_runs WHERE workflow_id=? AND phase=? AND round=?",
        (row["id"], step["phase"], step["round"]),
    ).fetchone():
        raise ControlError(
            "workflow_retry_denied",
            "Workflow reservations are never retried automatically or through task retry.",
        )
    _members(db, row)
    if _accepted(db, row):
        raise ControlError("codex_accepted", "Explicit Codex acceptance ends automation.")
    policy = json.loads(row["policy"])
    _window(row, policy)
    remaining = policy["max_calls"] - _count(db, row["id"])
    if remaining < (2 if step["phase"] == "worker" else 1):
        raise ControlError(
            "call_limit", "Insufficient calls for this step and its required review."
        )
    return row


def _complete(store, db, run):
    if not run or run["status"] != "completed":
        if run and run["status"] == "unknown":
            reason = "unknown_execution"
        else:
            reason = (run["reason"] or run["status"]) if run else "missing_run"
            if run:
                session = db.execute(
                    "SELECT model FROM sessions WHERE id=?", (run["session_id"],)
                ).fetchone()
                if any(
                    not model.startswith("claude-" + session[0] + "-")
                    for model in json.loads(run["actual_models"])
                ):
                    reason = "model_mismatch"
        raise ControlError(reason, "Workflow source did not complete successfully.")
    checked = contract.inspect_run(store, db, run)
    if not checked or checked["format_status"] != "valid":
        raise ControlError("invalid_report", "Workflow requires a valid structured report.")
    if checked["agent_status"] != "complete":
        raise ControlError("agent_blocked", "A blocked report cannot release a workflow edge.")
    return checked


def _source(store, db, step):
    if not step["source_run_id"]:
        return
    source_task = tasks._task(db, step["source_task_id"], step["source_revision"])
    if source_task["active_run_id"] != step["source_run_id"]:
        raise ControlError("dependency_changed", "Workflow source run changed.")
    run = db.execute("SELECT * FROM runs WHERE id=?", (step["source_run_id"],)).fetchone()
    checked = _complete(store, db, run)
    if (
        run["result_sha256"] != step["source_result_sha256"]
        or contract.canonical(checked["report"]) != step["source_report"]
    ):
        raise ControlError("dependency_changed", "Workflow source differs from the pinned result.")


def materialize(store, db, task_id, revision, prompt):
    row = guard_in(store, db, task_id, revision)
    if not row:
        return prompt
    step = _step(db, row)
    _source(store, db, step)
    snapshot = contract.read(prompt)
    snapshot.update(protocol=PROTOCOL, workflow=envelope(row, step, json.loads(row["policy"])))
    if step["phase"] == "review":
        snapshot["report_contract"] = {
            **snapshot["report_contract"],
            "review": snapshot["workflow"]["review_contract"]["review"],
        }
    snapshot.setdefault("dependencies", [])
    output = contract.canonical(snapshot)
    if len(output.encode()) > MAX_BYTES:
        raise ControlError("input_too_large", "Workflow input exceeds 1 MiB.")
    contract.read(output)
    return output


def record_in(store, db, task_id, revision, run_id):
    row = guard_in(store, db, task_id, revision)
    if not row:
        return
    now = time.time()
    db.execute(
        "INSERT INTO workflow_runs(workflow_id,phase,round,task_id,revision,run_id,created) VALUES(?,?,?,?,?,?,?)",
        (row["id"], row["phase"], row["round"], task_id, revision, run_id, now),
    )
    if row["first_reserved_at"] is None:
        db.execute("UPDATE workflows SET first_reserved_at=? WHERE id=?", (now, row["id"]))


def inspect_binding(db, snapshot, run_id):
    receipt = db.execute("SELECT * FROM workflow_runs WHERE run_id=?", (run_id,)).fetchone()
    if not receipt:
        raise ControlError("invalid_snapshot", "Workflow reservation receipt missing.")
    row = _get(db, receipt["workflow_id"])
    step = db.execute(
        "SELECT * FROM workflow_steps WHERE workflow_id=? AND phase=? AND round=?",
        (row["id"], receipt["phase"], receipt["round"]),
    ).fetchone()
    if (
        snapshot["workflow"] != envelope(row, step, json.loads(row["policy"]))
        or snapshot["task"]["id"] != receipt["task_id"]
        or snapshot["task"]["revision"] != receipt["revision"]
    ):
        raise ControlError(
            "invalid_snapshot", "Workflow input differs from its immutable step and receipt."
        )


def _add_step(db, row, phase, number, task_id, revision, source=None, report=None, message_id=None):
    db.execute(
        "INSERT INTO workflow_steps VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            row["id"],
            phase,
            number,
            task_id,
            revision,
            source["task_id"] if source else None,
            source["revision"] if source else None,
            source["run_id"] if source else None,
            source["result_sha256"] if source else None,
            contract.canonical(report) if source else None,
            message_id,
            time.time(),
        ),
    )


def create(
    store,
    assignment,
    operation_id,
    *,
    max_revisions=2,
    max_calls=6,
    dispatch_window_seconds=900,
    reviewer_effort=None,
    reviewer_model=None,
):
    _require(store)
    assignment = tasks._assignment(store, assignment)
    recorded = tasks._operation_recorded(store, operation_id)
    if "effort" in assignment and not recorded:
        store.preflight_effort(assignment["effort"])
    reviewer_defaults = store.role_defaults()["critic"]
    reviewer_model = reviewer_defaults["model"] if reviewer_model is None else reviewer_model
    effective_reviewer_effort = (
        reviewer_effort if reviewer_effort is not None else reviewer_defaults.get("effort")
    )
    from .model_settings import validate_model

    try:
        reviewer_model = validate_model(reviewer_model)
    except ValueError as exc:
        raise ControlError("invalid_workflow", str(exc)) from None
    if effective_reviewer_effort is not None:
        from .execution_settings import validate_effort

        try:
            validate_effort(effective_reviewer_effort)
        except ValueError as exc:
            raise ControlError("invalid_workflow", str(exc)) from None
        if not recorded:
            store.preflight_effort(effective_reviewer_effort)
    if assignment["role"] not in ("executor", "planner", "architect", "critic"):
        raise ControlError(
            "invalid_workflow",
            "Worker role must be executor, planner, architect or critic.",
        )
    if (
        type(max_revisions) is not int
        or not 0 <= max_revisions <= 10
        or type(max_calls) is not int
        or not 2 <= max_calls <= 64
    ):
        raise ControlError("invalid_workflow", "Use max-revisions 0..10 and max-calls 2..64.")
    if (
        isinstance(dispatch_window_seconds, bool)
        or not isinstance(dispatch_window_seconds, (int, float))
        or not math.isfinite(dispatch_window_seconds)
        or not 1 <= dispatch_window_seconds <= 86400
    ):
        raise ControlError("invalid_workflow", "Dispatch window must be finite, 1..86400 seconds.")
    policy = dict(
        max_revisions=max_revisions,
        max_calls=max_calls,
        dispatch_window_seconds=dispatch_window_seconds,
        worker_assignment=assignment,
        reviewer_model=reviewer_model,
        reviewer_role="critic",
    )
    if effective_reviewer_effort is not None:
        policy["reviewer_effort"] = effective_reviewer_effort
    with store.db(write=True) as db:
        fingerprint, prior = tasks._operation(
            db, operation_id, dict(kind="workflow_create", policy=policy)
        )
        if prior:
            return {**prior, "deduplicated": True}
        workflow_id = str(uuid.uuid4())
        prefix = "workflow:" + workflow_id
        worker = tasks.create_in(store, db, assignment, prefix + ":worker")
        review_assignment = dict(
            {key: value for key, value in assignment.items() if key != "effort"},
            id="review-" + workflow_id,
            name="Review: " + assignment["name"][:110],
            model=reviewer_model,
            role="critic",
            objective="Find the message whose id equals workflow.source.message_id and review its source.report against workflow.criteria. "
            "Pin review.target to workflow.source identity fields. "
            "Return the task report plus the top-level review object required by workflow.review_contract. "
            "Do not accept the task or change the workflow policy.",
            deliverable="Concise review summary; also include the structured top-level review object.",
            acceptance_criteria=[
                "Review every frozen workflow criterion against the exact source and return a structured recommendation."
            ],
        )
        if effective_reviewer_effort is not None:
            review_assignment["effort"] = effective_reviewer_effort
        reviewer = tasks.create_in(store, db, review_assignment, prefix + ":reviewer")
        db.execute(
            "INSERT INTO workflows VALUES(?,?,?,?,?,'active',NULL,'worker',0,NULL,?)",
            (
                workflow_id,
                assignment["name"],
                worker["id"],
                reviewer["id"],
                contract.canonical(policy),
                time.time(),
            ),
        )
        row = _get(db, workflow_id)
        _add_step(db, row, "worker", 0, worker["id"], 1)
        scheduler.enqueue_in(
            store, db, worker["id"], 1, prefix + ":queue:worker:0", workflow_id=workflow_id
        )
        result = dict(
            id=workflow_id,
            worker_task_id=worker["id"],
            reviewer_task_id=reviewer["id"],
            state="active",
        )
        tasks._record(db, operation_id, fingerprint, result)
    return {**result, "deduplicated": False}


def _halt(db, row, reason):
    db.execute(
        "UPDATE workflows SET state='awaiting_codex',reason=? WHERE id=? AND state='active'",
        (reason, row["id"]),
    )
    db.execute(
        "UPDATE queue_entries SET state='cancelled' WHERE state='waiting' AND task_id IN (?,?)",
        (row["worker_task_id"], row["reviewer_task_id"]),
    )


def _prepare(store, db, row, phase, number, source, checked, instructions):
    task_id = row["worker_task_id"] if phase == "worker" else row["reviewer_task_id"]
    task = tasks._task(db, task_id)
    current = task["current_revision"]
    assignment = contract.read(tasks._revision(db, task_id, current)["prompt"])["assignment"]
    prefix = f"workflow:{row['id']}:{phase}:{number}"
    message = messages.enqueue_in(
        store,
        db,
        task_id,
        current,
        instructions,
        prefix + ":message",
        source_run_id=source["run_id"],
        source_result_sha256=source["result_sha256"],
        workflow_id=row["id"],
    )
    revision = tasks.revise_in(
        store,
        db,
        task_id,
        current,
        assignment,
        prefix + ":revise",
        parent_run_id=task["active_run_id"],
        message_ids=[message["id"]],
        workflow_id=row["id"],
    )
    _add_step(
        db,
        row,
        phase,
        number,
        task_id,
        revision["current_revision"],
        source,
        checked["report"],
        message["id"],
    )
    db.execute("UPDATE workflows SET phase=?,round=? WHERE id=?", (phase, number, row["id"]))
    scheduler.enqueue_in(
        store, db, task_id, revision["current_revision"], prefix + ":queue", workflow_id=row["id"]
    )


def _advance_in(store, db, row):
    _members(db, row)
    if _accepted(db, row):
        _halt(db, row, "codex_accepted")
        return
    policy = json.loads(row["policy"])
    if row["first_reserved_at"] is not None and time.time() < row["first_reserved_at"]:
        raise ControlError("clock_changed", "Clock precedes first reservation.")
    step = _step(db, row)
    receipt = db.execute(
        "SELECT * FROM workflow_runs WHERE workflow_id=? AND phase=? AND round=?",
        (row["id"], row["phase"], row["round"]),
    ).fetchone()
    if not receipt:
        queue = db.execute(
            "SELECT blocked_reason FROM queue_entries WHERE task_id=? AND revision=? AND state='waiting'",
            (step["task_id"], step["revision"]),
        ).fetchone()
        if queue and queue[0] and queue[0] not in ("capacity", "session_busy"):
            raise ControlError(queue[0], "Workflow admission is blocked.")
        guard_in(store, db, step["task_id"], step["revision"])
        return
    run = db.execute("SELECT * FROM runs WHERE id=?", (receipt["run_id"],)).fetchone()
    if run["status"] in ACTIVE and run["status"] != "unknown":
        _window(row, policy)
        return
    checked = _complete(store, db, run)
    source = dict(
        task_id=step["task_id"],
        revision=step["revision"],
        run_id=run["id"],
        result_sha256=run["result_sha256"],
    )
    if row["phase"] == "review":
        review = checked["report"]["review"]
        target = review["target"]
        tasks._decision_in(
            store,
            db,
            target["task_id"],
            target["revision"],
            target["run_id"],
            target["result_sha256"],
            {
                key: value["verdict"] + ": " + value["evidence"]
                for key, value in review["criteria"].items()
            },
            f"workflow:{row['id']}:decision:{row['round']}",
            kind="review",
            reviewer="workflow:" + run["id"],
            recommendation=review["recommendation"],
        )
        if review["recommendation"] != "revise":
            _halt(
                db,
                row,
                "approve_recommended"
                if review["recommendation"] == "approve"
                else "review_blocked",
            )
            return
        if row["round"] >= policy["max_revisions"]:
            _halt(db, row, "revise_limit")
            return
        if policy["max_calls"] - _count(db, row["id"]) < 2:
            _halt(db, row, "call_limit")
            return
        phase, number, instructions = "worker", row["round"] + 1, review["revision_instructions"]
    else:
        if _count(db, row["id"]) >= policy["max_calls"]:
            _halt(db, row, "call_limit")
            return
        phase, number, instructions = (
            "review",
            row["round"],
            "Review the attached exact worker result against the frozen workflow criteria.",
        )
    _window(row, policy)
    db.execute("SAVEPOINT workflow_stage")
    try:
        _prepare(store, db, row, phase, number, source, checked, instructions)
    except STATE_ERRORS:
        db.execute("ROLLBACK TO workflow_stage")
        raise
    finally:
        db.execute("RELEASE workflow_stage")


def _checkpoint(phase, workflow_id):
    """Process-death injection boundary; never a model callback."""


def advance(store, workflow_id):
    _require(store)
    store.refresh()
    with store.db(write=True) as db:
        row = _get(db, workflow_id)
        if row["state"] == "active":
            try:
                _advance_in(store, db, row)
            except STATE_ERRORS as exc:
                _halt(
                    db,
                    row,
                    _error_reason(exc),
                )
    _checkpoint("advanced", workflow_id)


def _summary_in(store, db, task_id):
    task = tasks._task(db, task_id)
    runs = db.execute(
        "SELECT r.*,t.revision FROM runs r JOIN task_runs t ON t.run_id=r.id WHERE t.task_id=? ORDER BY r.rowid",
        (task_id,),
    ).fetchall()
    state, reason = tasks._state(store, db, task, runs)
    return dict(task, state=state, reason=reason)


def status(store, workflow_id):
    _require(store)
    store.refresh()
    with store.db() as db:
        row = _get(db, workflow_id)
        policy = json.loads(row["policy"])
        output = dict(row)
        output.pop("policy")
        output["worker"] = _summary_in(store, db, row["worker_task_id"])
        output["reviewer"] = _summary_in(store, db, row["reviewer_task_id"])
        output["steps"] = [
            dict(s)
            for s in db.execute(
                "SELECT workflow_id,phase,round,task_id,revision,source_run_id,source_result_sha256 FROM workflow_steps WHERE workflow_id=? ORDER BY rowid",
                (workflow_id,),
            )
        ]
        output["runs"] = [
            dict(r)
            for r in db.execute(
                "SELECT w.*,r.status,r.reason,r.actual_models FROM workflow_runs w JOIN runs r ON r.id=w.run_id WHERE w.workflow_id=? ORDER BY w.seq",
                (workflow_id,),
            )
        ]
        output["budget"] = dict(
            calls_used=len(output["runs"]),
            max_calls=policy["max_calls"],
            revisions_used=row["round"],
            max_revisions=policy["max_revisions"],
            dispatch_window_seconds=policy["dispatch_window_seconds"],
        )
        output["deadline"] = (
            (row["first_reserved_at"] + policy["dispatch_window_seconds"])
            if row["first_reserved_at"] is not None
            else None
        )
        output["accepted_revision"] = (
            output["worker"]["current_revision"]
            if output["worker"]["state"] == "accepted"
            else None
        )
        if output["accepted_revision"] is not None and row["state"] not in ("stopping", "stopped"):
            output.update(state="accepted", reason="codex_accepted")
        elif row["state"] == "active":
            try:
                _members(db, row)
                _window(row, policy)
                if output["runs"]:
                    latest = output["runs"][-1]
                    if latest["status"] == "unknown":
                        raise ControlError(
                            "unknown_execution", "Inspect and reconcile the owned run."
                        )
                    member = output["worker"] if row["phase"] == "worker" else output["reviewer"]
                    if member["state"] == "blocked":
                        raise ControlError(member["reason"], "Workflow member needs attention.")
            except STATE_ERRORS as exc:
                output.update(state="awaiting_codex", reason=_error_reason(exc))
    for run in output["runs"]:
        run["actual_models"] = json.loads(run["actual_models"])
    return output


def _finish_stop(store, workflow_id):
    with store.db() as db:
        run_ids = [
            r[0]
            for r in db.execute(
                "SELECT run_id FROM workflow_runs WHERE workflow_id=?", (workflow_id,)
            )
        ]
    for run_id in run_ids:
        store.stop(run_id)
    store.refresh()
    with store.db(write=True) as db:
        runs = db.execute(
            "SELECT r.* FROM runs r JOIN workflow_runs w ON w.run_id=r.id WHERE w.workflow_id=?",
            (workflow_id,),
        ).fetchall()
        unresolved = any(
            r["status"] in ACTIVE
            or alive(r["worker_pid"], r["worker_start"], r["boot"])
            or execution_alive(r)
            for r in runs
        )
        if not unresolved:
            db.execute(
                "UPDATE workflows SET state='stopped',reason='stopped' WHERE id=? AND state='stopping'",
                (workflow_id,),
            )


def stop(store, workflow_id, operation_id):
    _require(store)
    with store.db(write=True) as db:
        fingerprint, prior = tasks._operation(
            db, operation_id, dict(kind="workflow_stop", workflow=workflow_id)
        )
        row = _get(db, workflow_id)
        if not prior:
            db.execute(
                "UPDATE workflows SET state='stopping',reason='stop_requested' WHERE id=? AND state!='stopped'",
                (workflow_id,),
            )
            db.execute(
                "UPDATE queue_entries SET state='cancelled' WHERE state='waiting' AND task_id IN (?,?)",
                (row["worker_task_id"], row["reviewer_task_id"]),
            )
            tasks._record(db, operation_id, fingerprint, dict(id=workflow_id))
    _checkpoint("stop_recorded", workflow_id)
    _finish_stop(store, workflow_id)
    return dict(status(store, workflow_id), deduplicated=bool(prior))


def run(store, workflow_id, *, once=False, max_seconds=30):
    _require(store)
    if (
        isinstance(max_seconds, bool)
        or not isinstance(max_seconds, (int, float))
        or not math.isfinite(max_seconds)
        or not 0 <= max_seconds <= 3600
    ):
        raise ControlError("invalid_workflow", "max-seconds must be finite, 0..3600.")
    deadline = None if once else time.monotonic() + max_seconds
    started = []
    while True:
        capacity_wait = False
        with store.db() as db:
            state = _get(db, workflow_id)["state"]
        if state == "stopping":
            _finish_stop(store, workflow_id)
        elif deadline is None or time.monotonic() < deadline:
            advance(store, workflow_id)
            admission = scheduler.tick(store, deadline=deadline, workflow_id=workflow_id)
            started.extend(admission["started"])
            capacity_wait = any(
                q["blocked_reason"] in ("capacity", "session_busy") for q in admission["waiting"]
            ) and any(r["status"] != "unknown" for r in admission["active"])
            advance(store, workflow_id)
        output = status(store, workflow_id)
        if once:
            reason = "once"
        elif time.monotonic() >= deadline:
            reason = "deadline"
        elif output["state"] not in ("active", "stopping"):
            reason = "needs_attention" if output["state"] == "awaiting_codex" else "idle"
        elif (
            not any(r["status"] in ACTIVE and r["status"] != "unknown" for r in output["runs"])
            and not started
            and not capacity_wait
        ):
            reason = "needs_attention"
        else:
            time.sleep(min(0.2, max(0, deadline - time.monotonic())))
            continue
        return dict(output, started=started, loop_reason=reason)


def overview(store, attention=False):
    _require(store)
    with store.db() as db:
        ids = [r[0] for r in db.execute("SELECT id FROM workflows ORDER BY created,id")]
    flows = [status(store, i) for i in ids]
    task_rows = tasks.list_tasks(store)["tasks"]
    runs = store.list_all()["runs"]
    if attention:
        flows = [r for r in flows if r["state"] in ("awaiting_codex", "stopping")]
        task_rows = [r for r in task_rows if r["state"] in ("blocked", "awaiting_review")]
        runs = [r for r in runs if r["status"] == "unknown"]
    with store.db() as db:
        cursor = db.execute("SELECT coalesce(max(id),0) FROM task_events").fetchone()[0]
    return dict(
        workflows=flows,
        tasks=task_rows,
        runs=[{k: r[k] for k in ("id", "session_id", "status", "reason")} for r in runs],
        next_cursor=cursor,
    )
