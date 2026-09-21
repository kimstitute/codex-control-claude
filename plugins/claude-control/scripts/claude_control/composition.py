"""Durable composition of the bounded P4 workflow and P5 workspaces."""

import fcntl
import json
import math
import sqlite3
import time
import uuid
from contextlib import contextmanager

from . import task_contracts as contract
from . import tasks, workflow, workspace
from . import workspace_files as files
from .store import ControlError
from .workspace_policy import normalize_policy

STATE_ERRORS = (ControlError, OSError, ValueError, KeyError, TypeError, sqlite3.IntegrityError)


def _require(store):
    if store.config["schema"] < 10:
        raise ControlError("migration_required", "Compositions require migrate --offline.")


def _get(db, composition_id):
    row = db.execute("SELECT * FROM compositions WHERE id=?", (composition_id,)).fetchone()
    if not row:
        raise ControlError("composition_not_found", "Unknown composition UUID.")
    return row


def _operation_id(composition_id, action):
    return f"composition:{composition_id}:{action}"


def _composition_id(store, operation_id):
    namespace = uuid.UUID(store.config["installation_id"])
    return str(uuid.uuid5(namespace, "composition:" + operation_id))


def _pin_source(store, composition_id, repo, requested_ref):
    operation_id = _operation_id(composition_id, "source")
    intent = {
        "kind": "composition_source",
        "repo": repo,
        "requested_ref": requested_ref,
    }
    with store.db() as db:
        fingerprint, prior = tasks._operation(db, operation_id, intent)
        if prior:
            return prior["commit"]
    resolved = files.resolve_commit(repo, requested_ref)
    with store.db(write=True) as db:
        fingerprint, prior = tasks._operation(db, operation_id, intent)
        if prior:
            return prior["commit"]
        tasks._record(db, operation_id, fingerprint, {"commit": resolved})
    return resolved


def _member(db, composition_id, phase):
    return db.execute(
        "SELECT * FROM composition_members WHERE composition_id=? AND phase=?",
        (composition_id, phase),
    ).fetchone()


def _result(db, composition_id, phase):
    return db.execute(
        "SELECT * FROM composition_results WHERE composition_id=? AND phase=?",
        (composition_id, phase),
    ).fetchone()


def _canonical_assignment(store, value, *, role=None, model=None):
    assignment = tasks._assignment(store, value)
    if role is not None and assignment["role"] not in role:
        raise ControlError("invalid_composition", "Assignment role is incompatible with its phase.")
    if model is not None and assignment["model"] != model:
        raise ControlError(
            "invalid_composition", "Assignment model is incompatible with its phase."
        )
    return assignment


def _reviewer_policy(editor_policy):
    return normalize_policy(
        {
            "version": 1,
            "role": "verifier",
            "read_paths": editor_policy["read_paths"],
            "write_paths": [],
            "checks": {},
            "max_actions": editor_policy["max_actions"],
            "max_calls": editor_policy["max_calls"],
        }
    )


def create(
    store,
    planning_assignment,
    editor_assignment,
    editor_policy,
    reviewer_assignment,
    operation_id,
    *,
    repo,
    ref="HEAD",
    max_revisions=2,
    max_calls=6,
    dispatch_window_seconds=900,
    reviewer_effort=None,
):
    """Create a composition, recovering a previously created P4 child by operation ID."""
    _require(store)
    source_repo = store.project(repo)
    composition_id = _composition_id(store, operation_id)
    pinned_commit = _pin_source(store, composition_id, source_repo, ref)
    planning = _canonical_assignment(
        store, planning_assignment, role=("planner", "architect", "executor")
    )
    editor = _canonical_assignment(store, editor_assignment, role=("executor",))
    reviewer = _canonical_assignment(
        store, reviewer_assignment, role=("critic", "verifier"), model="fable"
    )
    if any(a["project"] != source_repo for a in (planning, editor, reviewer)):
        raise ControlError(
            "invalid_composition", "Every composition assignment must use the source repository."
        )
    if reviewer["acceptance_criteria"] != editor["acceptance_criteria"]:
        raise ControlError(
            "invalid_composition",
            "The frozen reviewer must assess the editor's exact acceptance criteria.",
        )
    editor_policy = normalize_policy(editor_policy)
    if editor_policy["role"] != "executor":
        raise ControlError("invalid_composition", "The editor policy must use the executor role.")
    policy = {
        "version": 1,
        "repo": source_repo,
        "requested_ref": ref,
        "ref": pinned_commit,
        "planning_assignment": planning,
        "editor_assignment": editor,
        "editor_policy": editor_policy,
        "reviewer_assignment": reviewer,
        "reviewer_policy": _reviewer_policy(editor_policy),
        "workflow": {
            "max_revisions": max_revisions,
            "max_calls": max_calls,
            "dispatch_window_seconds": dispatch_window_seconds,
            "reviewer_effort": reviewer_effort,
        },
    }
    intent = {"kind": "composition_create", "policy": policy}
    with store.db() as db:
        fingerprint, prior = tasks._operation(db, operation_id, intent)
        if prior:
            return {**prior, "deduplicated": True}

    child = workflow.create(
        store,
        planning,
        _operation_id(composition_id, "plan:create"),
        max_revisions=max_revisions,
        max_calls=max_calls,
        dispatch_window_seconds=dispatch_window_seconds,
        reviewer_effort=reviewer_effort,
    )
    with store.db(write=True) as db:
        fingerprint, prior = tasks._operation(db, operation_id, intent)
        if prior:
            return {**prior, "deduplicated": True}
        existing = db.execute("SELECT * FROM compositions WHERE id=?", (composition_id,)).fetchone()
        if existing and existing["workflow_id"] != child["id"]:
            raise ControlError(
                "request_conflict", "Composition identity belongs to another workflow."
            )
        if not existing:
            db.execute(
                "INSERT INTO compositions"
                "(id,name,workflow_id,policy,state,reason,phase,created) "
                "VALUES(?,?,?,?, 'active',NULL,'plan',?)",
                (
                    composition_id,
                    planning["name"],
                    child["id"],
                    contract.canonical(policy),
                    time.time(),
                ),
            )
        response = {
            "id": composition_id,
            "workflow_id": child["id"],
            "state": "active",
            "phase": "plan",
        }
        tasks._record(db, operation_id, fingerprint, response)
    return {**response, "deduplicated": False}


def _completed_result(store, db, task_id, *, accepted=False):
    task = tasks._task(db, task_id)
    if not task["active_run_id"]:
        return None
    run = db.execute("SELECT * FROM runs WHERE id=?", (task["active_run_id"],)).fetchone()
    if not run or run["status"] != "completed" or not run["result_sha256"]:
        return None
    checked = contract.inspect_run(store, db, run)
    if checked["format_status"] != "valid" or checked["agent_status"] != "complete":
        return None
    decision = None
    if accepted:
        decision = db.execute(
            "SELECT * FROM review_decisions WHERE task_id=? AND revision=? AND run_id=? "
            "AND result_sha256=? AND kind='accept' ORDER BY rowid DESC LIMIT 1",
            (task_id, task["current_revision"], run["id"], run["result_sha256"]),
        ).fetchone()
        if not decision:
            return None
    return {
        "task_id": task_id,
        "revision": task["current_revision"],
        "run_id": run["id"],
        "result_sha256": run["result_sha256"],
        "report": checked["report"],
        "decision_id": decision["id"] if decision else None,
    }


def _record_result(db, composition_id, phase, source, *, workspace_id=None, exported=None):
    values = (
        composition_id,
        phase,
        source["task_id"],
        source["revision"],
        source["run_id"],
        source["result_sha256"],
        workspace_id,
        exported["manifest_sha256"] if exported else None,
        exported["manifest"]["tree_sha256"] if exported else None,
        time.time(),
    )
    existing = _result(db, composition_id, phase)
    if existing:
        pinned = tuple(
            existing[key]
            for key in (
                "task_id",
                "revision",
                "run_id",
                "result_sha256",
                "workspace_id",
                "manifest_sha256",
                "tree_sha256",
            )
        )
        if pinned != values[2:9]:
            raise ControlError("dependency_changed", "A composed phase result changed.")
        return
    db.execute(
        "INSERT INTO composition_results"
        "(composition_id,phase,task_id,revision,run_id,result_sha256,workspace_id,"
        "manifest_sha256,tree_sha256,created) VALUES(?,?,?,?,?,?,?,?,?,?)",
        values,
    )


def _append_context(assignment, envelope):
    result = dict(assignment)
    separator = "\n\n" if result["context"] else ""
    result["context"] += (
        separator + "claude-control composition provenance:\n" + contract.canonical(envelope)
    )
    return result


def _materialize_editor(store, row, policy, plan):
    composition_id = row["id"]
    assignment = _append_context(
        policy["editor_assignment"],
        {
            "protocol": "claude-control.composition.v1",
            "phase": "plan",
            "workflow_id": row["workflow_id"],
            "task_id": plan["task_id"],
            "revision": plan["revision"],
            "run_id": plan["run_id"],
            "result_sha256": plan["result_sha256"],
            "acceptance_decision_id": plan["decision_id"],
            "report": plan["report"],
        },
    )
    made = workspace.create(
        store,
        policy["editor_policy"],
        _operation_id(composition_id, "editor:create"),
        repo=policy["repo"],
        ref=policy["ref"],
    )
    bound = workspace.bind(
        store,
        made["id"],
        assignment,
        _operation_id(composition_id, "editor:bind"),
    )
    with store.db(write=True) as db:
        current = _get(db, composition_id)
        if current["state"] != "active" or current["phase"] != "plan":
            return
        _record_result(db, composition_id, "plan", plan)
        db.execute(
            "INSERT INTO composition_members"
            "(composition_id,phase,workspace_id,task_id,created) VALUES(?,?,?,?,?)",
            (composition_id, "editor", made["id"], bound["task_id"], time.time()),
        )
        db.execute(
            "UPDATE compositions SET phase='editor',state='active',reason=NULL "
            "WHERE id=? AND state='active' AND phase='plan'",
            (composition_id,),
        )


def _workspace_result(store, db, member):
    row = workspace._get(db, member["workspace_id"])
    if row["state"] != "finished":
        return None
    exported = workspace.export(store, row["id"])
    saved = db.execute(
        "SELECT * FROM workspace_exports WHERE workspace_id=?", (row["id"],)
    ).fetchone()
    source = _completed_result(store, db, member["task_id"])
    if not source or source["run_id"] != saved["run_id"]:
        raise ControlError(
            "dependency_changed", "Frozen export does not match the final task result."
        )
    return source, exported


def _materialize_reviewer(store, row, policy, editor, exported):
    composition_id = row["id"]
    assignment = _append_context(
        policy["reviewer_assignment"],
        {
            "protocol": "claude-control.composition.v1",
            "phase": "editor",
            "workspace_id": editor["workspace_id"],
            "task_id": editor["task_id"],
            "revision": editor["revision"],
            "run_id": editor["run_id"],
            "result_sha256": editor["result_sha256"],
            "manifest_sha256": exported["manifest_sha256"],
            "tree_sha256": exported["manifest"]["tree_sha256"],
        },
    )
    made = workspace.create(
        store,
        policy["reviewer_policy"],
        _operation_id(composition_id, "reviewer:create"),
        from_snapshot=editor["workspace_id"],
    )
    bound = workspace.bind(
        store,
        made["id"],
        assignment,
        _operation_id(composition_id, "reviewer:bind"),
    )
    with store.db(write=True) as db:
        current = _get(db, composition_id)
        if current["state"] != "active" or current["phase"] != "editor":
            return
        _record_result(
            db,
            composition_id,
            "editor",
            editor,
            workspace_id=editor["workspace_id"],
            exported=exported,
        )
        db.execute(
            "INSERT INTO composition_members"
            "(composition_id,phase,workspace_id,task_id,created) VALUES(?,?,?,?,?)",
            (composition_id, "reviewer", made["id"], bound["task_id"], time.time()),
        )
        db.execute(
            "UPDATE compositions SET phase='reviewer',state='active',reason=NULL "
            "WHERE id=? AND state='active' AND phase='editor'",
            (composition_id,),
        )


def _halt(store, composition_id, reason):
    with store.db(write=True) as db:
        db.execute(
            "UPDATE compositions SET state='awaiting_codex',reason=? WHERE id=? AND state='active'",
            (reason, composition_id),
        )


def _advance(store, composition_id):
    with store.db() as db:
        row = _get(db, composition_id)
        policy = json.loads(row["policy"])
    if row["state"] == "stopping":
        _finish_stop(store, composition_id)
        return []
    if row["state"] == "stopped":
        return []

    if row["phase"] == "plan":
        with store.db() as db:
            flow = workflow._get(db, row["workflow_id"])
            accepted = _completed_result(store, db, flow["worker_task_id"], accepted=True)
        reviewed = flow["state"] == "awaiting_codex" and flow["reason"] == "approve_recommended"
        if accepted and reviewed:
            with store.db(write=True) as db:
                db.execute(
                    "UPDATE compositions SET state='active',reason=NULL WHERE id=? "
                    "AND state='awaiting_codex' AND reason='plan_acceptance_required' "
                    "AND phase='plan'",
                    (composition_id,),
                )
            _materialize_editor(store, row, policy, accepted)
            return []
        if accepted and not reviewed:
            _halt(store, composition_id, "plan_review_required")
            return []
        if row["state"] == "awaiting_codex":
            return []
        child = workflow.run(store, row["workflow_id"], once=True)
        if child["state"] == "awaiting_codex":
            reason = child["reason"]
            _halt(
                store,
                composition_id,
                "plan_acceptance_required" if reason == "approve_recommended" else reason,
            )
        return child.get("started", [])

    with store.db() as db:
        member = _member(db, composition_id, row["phase"])
        if not member:
            raise ControlError("invalid_state", "Composition phase has no bound workspace.")
        completed = _workspace_result(store, db, member)
    if completed:
        source, exported = completed
        if row["phase"] == "editor":
            _materialize_reviewer(
                store,
                row,
                policy,
                {**source, "workspace_id": member["workspace_id"]},
                exported,
            )
        else:
            with store.db(write=True) as db:
                _record_result(
                    db,
                    composition_id,
                    "reviewer",
                    source,
                    workspace_id=member["workspace_id"],
                    exported=exported,
                )
                db.execute(
                    "UPDATE compositions SET state='awaiting_codex',"
                    "reason='final_review_ready' WHERE id=? "
                    "AND state='active' AND phase='reviewer'",
                    (composition_id,),
                )
        return []
    if row["state"] == "awaiting_codex":
        return []
    child = workspace.run(store, member["workspace_id"], once=True, max_seconds=0)
    if child["state"] in ("awaiting_codex", "preparing"):
        _halt(store, composition_id, child.get("reason") or "workspace_incomplete")
    return child.get("started", [])


def _accepted(db, composition_id):
    reviewer = _result(db, composition_id, "reviewer")
    editor = _result(db, composition_id, "editor")
    if not reviewer or not editor:
        return None
    return db.execute(
        "SELECT * FROM review_decisions WHERE task_id=? AND revision=? AND run_id=? "
        "AND result_sha256=? AND kind='accept' ORDER BY rowid DESC LIMIT 1",
        (editor["task_id"], editor["revision"], editor["run_id"], editor["result_sha256"]),
    ).fetchone()


def accept_guard(store, db, task_id, run_id):
    """Enforce both explicit composition acceptance boundaries."""
    plan = db.execute(
        "SELECT c.id,w.state,w.reason,w.worker_task_id FROM compositions c "
        "JOIN workflows w ON w.id=c.workflow_id WHERE w.worker_task_id=?",
        (task_id,),
    ).fetchone()
    if plan:
        if plan["state"] != "awaiting_codex" or plan["reason"] != "approve_recommended":
            raise ControlError(
                "composition_plan_review_required",
                "Accept the plan only after its independent workflow review recommends approval.",
            )
        return
    member = db.execute(
        "SELECT m.*,c.id AS owner_id,c.state AS owner_state,c.reason AS owner_reason "
        "FROM composition_members m "
        "JOIN compositions c ON c.id=m.composition_id "
        "WHERE m.task_id=? AND m.phase='editor'",
        (task_id,),
    ).fetchone()
    if not member:
        return
    if member["owner_state"] != "awaiting_codex" or member["owner_reason"] != "final_review_ready":
        raise ControlError(
            "composition_final_review_not_ready",
            "Accept the editor only while its completed independent review awaits Codex.",
        )
    editor = _result(db, member["composition_id"], "editor")
    reviewer = _result(db, member["composition_id"], "reviewer")
    if not editor or editor["run_id"] != run_id or not reviewer:
        raise ControlError(
            "composition_review_required",
            "Accept the exact editor result only after its independent frozen-snapshot review.",
        )
    saved = db.execute(
        "SELECT * FROM workspace_exports WHERE workspace_id=? AND run_id=?",
        (reviewer["workspace_id"], reviewer["run_id"]),
    ).fetchone()
    if (
        not saved
        or saved["manifest_sha256"] != reviewer["manifest_sha256"]
        or saved["result_sha256"] != reviewer["result_sha256"]
    ):
        raise ControlError(
            "composition_review_required", "The independent review export is missing or changed."
        )
    reviewed = workspace.export(store, reviewer["workspace_id"])
    if reviewed["manifest"]["tree_sha256"] != reviewer["tree_sha256"]:
        raise ControlError(
            "composition_review_required", "The independent review snapshot identity changed."
        )


def status(store, composition_id):
    _require(store)
    store.refresh()
    with store.db() as db:
        row = _get(db, composition_id)
        output = dict(row)
        output["policy"] = json.loads(row["policy"])
        members = {
            r["phase"]: dict(r)
            for r in db.execute(
                "SELECT * FROM composition_members WHERE composition_id=? ORDER BY rowid",
                (composition_id,),
            )
        }
        output["results"] = [
            dict(r)
            for r in db.execute(
                "SELECT * FROM composition_results WHERE composition_id=? ORDER BY rowid",
                (composition_id,),
            )
        ]
        accepted = _accepted(db, composition_id)
    output["workflow"] = workflow.status(store, row["workflow_id"])
    output["members"] = {}
    for phase, member in members.items():
        output["members"][phase] = {
            **member,
            "workspace": workspace.status(store, member["workspace_id"]),
        }
    output["accepted"] = bool(accepted)
    output["acceptance_decision_id"] = accepted["id"] if accepted else None
    if accepted and row["state"] not in ("stopping", "stopped"):
        output.update(state="accepted", reason="codex_accepted")
    return output


@contextmanager
def _coordinator(store, composition_id):
    lock_path = store.path / ("composition-" + composition_id + ".lock")
    with lock_path.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ControlError(
                "composition_busy", "Another coordinator owns this composition."
            ) from None
        yield


def run(store, composition_id, *, once=False, max_seconds=30):
    _require(store)
    if (
        isinstance(max_seconds, bool)
        or not isinstance(max_seconds, (int, float))
        or not math.isfinite(max_seconds)
        or not 0 <= max_seconds <= 3600
    ):
        raise ControlError("invalid_composition", "max-seconds must be finite, 0..3600.")
    deadline = None if once else time.monotonic() + max_seconds
    started = []
    with _coordinator(store, composition_id):
        while True:
            try:
                started.extend(_advance(store, composition_id))
            except STATE_ERRORS as exc:
                _halt(store, composition_id, getattr(exc, "code", "composition_failed"))
            output = status(store, composition_id)
            if once:
                reason = "once"
            elif output["state"] not in ("active", "stopping"):
                reason = "needs_attention" if output["state"] == "awaiting_codex" else "idle"
            elif time.monotonic() >= deadline:
                reason = "deadline"
            else:
                time.sleep(min(0.2, max(0, deadline - time.monotonic())))
                continue
            return {**output, "started": started, "loop_reason": reason}


def _finish_stop(store, composition_id):
    with store.db() as db:
        row = _get(db, composition_id)
        workspace_ids = {
            member["workspace_id"]
            for member in db.execute(
                "SELECT workspace_id FROM composition_members WHERE composition_id=?",
                (composition_id,),
            )
        }
        for phase in ("editor", "reviewer"):
            operation = db.execute(
                "SELECT response FROM task_operations WHERE operation_id=?",
                (_operation_id(composition_id, phase + ":create"),),
            ).fetchone()
            if operation:
                workspace_id = json.loads(operation["response"]).get("id")
                if (
                    workspace_id
                    and db.execute(
                        "SELECT 1 FROM workspaces WHERE id=?", (workspace_id,)
                    ).fetchone()
                ):
                    workspace_ids.add(workspace_id)
    children_stopped = True
    for workspace_id in workspace_ids:
        child = workspace.status(store, workspace_id)
        if child["state"] != "finished":
            child = workspace.stop(
                store,
                workspace_id,
                _operation_id(composition_id, "workspace:" + workspace_id + ":stop"),
            )
            children_stopped = children_stopped and child["state"] == "stopped"
    plan = workflow.stop(
        store,
        row["workflow_id"],
        _operation_id(composition_id, "plan:stop"),
    )
    if children_stopped and plan["state"] == "stopped":
        with store.db(write=True) as db:
            db.execute(
                "UPDATE compositions SET state='stopped',reason='stopped' "
                "WHERE id=? AND state='stopping'",
                (composition_id,),
            )


def stop(store, composition_id, operation_id):
    _require(store)
    with _coordinator(store, composition_id):
        with store.db(write=True) as db:
            fingerprint, prior = tasks._operation(
                db, operation_id, {"kind": "composition_stop", "composition": composition_id}
            )
            _get(db, composition_id)
            if not prior:
                db.execute(
                    "UPDATE compositions SET state='stopping',reason='stop_requested' "
                    "WHERE id=? AND state!='stopped'",
                    (composition_id,),
                )
                tasks._record(db, operation_id, fingerprint, {"id": composition_id})
        _finish_stop(store, composition_id)
        return {**status(store, composition_id), "deduplicated": bool(prior)}
