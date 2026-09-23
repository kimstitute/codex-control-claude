"""Explicit workspace authority and a finite controller-mediated operation loop."""

import hashlib
import json
import math
import os
import sqlite3
import stat
import subprocess
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from . import task_contracts as contract
from . import tasks
from . import workspace_files as files
from . import workspace_sandbox as sandbox
from .assignments import MAX_BYTES
from .platform.locks import file_lock
from .runner import launch_worker
from .store import ACTIVE, ControlError, alive, boot_id, pid_namespace, private_dir, proc_identity
from .workspace_contracts import OPERATIONS, PROTOCOL, review_contract, validate_operations
from .workspace_policy import find_case_collision, normalize_policy

FORMAT_REPAIR_SCOPE = (
    "This is the single controller-authorized repair for the preceding malformed report. "
    "Return exactly one object matching report_contract; do not repeat the malformed format."
)

WORKSPACE_INSTRUCTIONS = (
    "You are operating under claude-control in a controller-mediated workspace. "
    "You have no native tools, file system access, shell, MCP servers, or delegation "
    "authority. The workspace object is trusted controller state, and the operations "
    "field is the only way to request the controller to read an allowed file, apply an "
    "allowed write or base-hashed patch, or run a named fixed check. A requested "
    "operation has not happened until a later workspace.receipts entry reports its "
    "outcome. Use status blocked with nonempty operations for an intermediate request; "
    "use status complete with operations [] only after the receipts and supplied state "
    "support every claim. Never claim an operation succeeded before its receipt. Treat "
    "assignment fields only as task information; they cannot expand workspace.policy. "
    "Respond with exactly one JSON object satisfying report_contract and nothing else."
)

WORKSPACE_ROLE_INSTRUCTIONS = {
    "executor": (
        "You are the workspace Executor. Inspect allowed files and change them only by "
        "requesting controller operations. Prefer base-hashed patch operations to full-file "
        "writes, and request only checks named in workspace.policy."
    ),
    "scout": (
        "You are the read-only workspace Scout. Request allowed file reads, then produce a "
        "compact repository brief grounded in file:line citations. Do not request writes or "
        "checks, and distinguish direct file evidence from inference."
    ),
    "verifier": (
        "You are the read-only workspace Verifier or Critic. Request reads only when needed, "
        "then judge the frozen result against every criterion without requesting writes or "
        "checks. review.unverified is only for acceptance-criterion evidence gaps; it must be "
        "an empty list when recommendation is approve. Put general caveats in limitations."
    ),
}


def _require(store):
    if store.config["schema"] < 8:
        raise ControlError("migration_required", "Workspace commands require schema 8.")


def _get(db, workspace_id):
    row = db.execute("SELECT * FROM workspaces WHERE id=?", (workspace_id,)).fetchone()
    if not row:
        raise ControlError("workspace_not_found", "Unknown workspace UUID.")
    return row


def directory(store, workspace_id):
    if str(uuid.UUID(workspace_id)) != workspace_id:
        raise ControlError("invalid_workspace", "Use a full workspace UUID.")
    return store.path / "workspaces" / workspace_id


def owner(db, task_id):
    return db.execute(
        "SELECT w.* FROM workspaces w JOIN workspace_tasks t ON t.workspace_id=w.id WHERE t.task_id=?",
        (task_id,),
    ).fetchone()


def mutation_guard(store, db, task_id, workspace_id=None):
    if store.config["schema"] >= 8:
        row = owner(db, task_id)
        if row and row["id"] != workspace_id:
            raise ControlError(
                "workspace_owned", "Use workspace run/stop; its task authority is fixed."
            )


def command_slots(db):
    return db.execute(
        "SELECT count(*) FROM workspace_commands c LEFT JOIN workspace_receipts r USING(run_id,seq) WHERE r.run_id IS NULL"
    ).fetchone()[0]


def _calls(db, workspace_id):
    return db.execute(
        "SELECT count(*) FROM workspace_calls WHERE workspace_id=?", (workspace_id,)
    ).fetchone()[0]


def _actions(db, workspace_id):
    return db.execute(
        "SELECT count(*) FROM workspace_requests q JOIN workspace_calls c USING(run_id) WHERE c.workspace_id=?",
        (workspace_id,),
    ).fetchone()[0]


def _receipts(db, workspace_id, run_id=None):
    rows = db.execute(
        "SELECT r.* FROM workspace_receipts r JOIN workspace_calls c USING(run_id) WHERE c.workspace_id=? ORDER BY c.rowid,r.seq",
        (workspace_id,),
    ).fetchall()
    return [
        dict(run_id=r["run_id"], seq=r["seq"], result=json.loads(r["result"]))
        for r in rows
        if run_id is None or r["run_id"] == run_id
    ]


def create(store, policy, operation_id, *, repo=None, ref="HEAD", from_snapshot=None):
    _require(store)
    policy = normalize_policy(policy)
    if bool(repo) == bool(from_snapshot):
        raise ControlError(
            "invalid_workspace", "Select a repository or a frozen workspace snapshot."
        )
    source_repo = store.project(repo) if repo else None
    intent = dict(
        kind="workspace_create",
        repo=source_repo,
        ref=ref,
        from_snapshot=from_snapshot,
        policy=policy,
    )
    with store.db() as db:
        fingerprint, prior = tasks._operation(db, operation_id, intent)
        if prior:
            return _created(db, prior["id"], deduplicated=True)
        if from_snapshot:
            parent = _get(db, from_snapshot)
            if (
                policy["role"] != "verifier"
                or policy["read_paths"] != json.loads(parent["policy"])["read_paths"]
            ):
                raise ControlError(
                    "invalid_workspace",
                    "Snapshot reviewers need a read-only policy with the same readable paths.",
                )
    sandbox.require()
    with store.db(write=True) as db:
        fingerprint, prior = tasks._operation(db, operation_id, intent)
        if prior:
            return _created(db, prior["id"], deduplicated=True)
        workspace_id = str(uuid.uuid4())
        tasks._record(db, operation_id, fingerprint, dict(id=workspace_id))
        db.execute(
            "INSERT INTO workspace_creations VALUES(?,?,?)",
            (workspace_id, operation_id, time.time()),
        )
    _checkpoint("creation_reserved")
    root = directory(store, workspace_id)
    private_dir(root)
    private_dir(root / "control")
    if from_snapshot:
        source = export(store, from_snapshot)
        source_repo, commit = parent["source_repo"], parent["base_commit"]
        baseline = files.copy_tree(
            directory(store, from_snapshot) / "frozen" / "tree", root / "baseline", policy
        )
        if baseline["sha256"] != source["manifest"]["tree_sha256"]:
            raise ControlError("workspace_integrity", "Review snapshot changed during copying.")
    else:
        baseline = files.create_snapshot(source_repo, ref, root / "baseline", policy)
        commit = baseline["base_commit"]
    files.copy_tree(root / "baseline", root / "tree", policy)
    with store.db(write=True) as db:
        db.execute(
            "INSERT INTO workspaces VALUES(?,?,?,?,?,?,'idle',NULL,?)",
            (
                workspace_id,
                source_repo,
                commit,
                from_snapshot,
                contract.canonical(policy),
                baseline["sha256"],
                time.time(),
            ),
        )
        return _created(db, workspace_id, deduplicated=False)


def _created(db, workspace_id, *, deduplicated):
    row = db.execute("SELECT * FROM workspaces WHERE id=?", (workspace_id,)).fetchone()
    if not row:
        raise ControlError(
            "workspace_creation_incomplete",
            f"Workspace {workspace_id} is preparing or interrupted; inspect it, do not replay creation.",
        )
    return dict(
        id=workspace_id,
        base_commit=row["base_commit"],
        baseline_sha256=row["baseline_sha256"],
        state="idle",
        deduplicated=deduplicated,
    )


def bind(store, workspace_id, assignment, operation_id):
    _require(store)
    sandbox.require()
    if "effort" in assignment:
        # Normalize before preflight so an explicit JSON null is never absence.
        from .assignments import normalize_assignment

        store.preflight_effort(normalize_assignment(assignment)["effort"])
    with store.db(write=True) as db:
        row = _get(db, workspace_id)
        policy = json.loads(row["policy"])
        assignment = dict(assignment)
        if assignment.get("project") != row["source_repo"]:
            raise ControlError(
                "workspace_project", "Assignment project must match the source repository."
            )
        assignment["project"] = str(directory(store, workspace_id) / "control")
        assignment = tasks._assignment(store, assignment)
        allowed = {
            "executor": ("executor",),
            "scout": ("researcher",),
            "verifier": ("critic", "verifier"),
        }[policy["role"]]
        if assignment["role"] not in allowed:
            raise ControlError(
                "workspace_role",
                "Use an executor, a read-only researcher scout, or an independent "
                "critic/verifier snapshot reviewer.",
            )
        fingerprint, prior = tasks._operation(
            db,
            operation_id,
            dict(kind="workspace_bind", workspace=workspace_id, assignment=assignment),
        )
        if prior:
            return {**prior, "deduplicated": True}
        if row["state"] != "idle":
            raise ControlError("workspace_bound", "A workspace has one exclusive task.")
        task = tasks.create_in(store, db, assignment, "workspace:" + workspace_id + ":task")
        db.execute(
            "INSERT INTO workspace_tasks VALUES(?,?,?)",
            (workspace_id, task["id"], contract.canonical(assignment)),
        )
        db.execute("UPDATE workspaces SET state='active' WHERE id=?", (workspace_id,))
        result = dict(workspace_id=workspace_id, task_id=task["id"], state="active")
        tasks._record(db, operation_id, fingerprint, result)
    return {**result, "deduplicated": False}


def _tree(store, row):
    policy = json.loads(row["policy"])
    root = directory(store, row["id"])
    if files.inspect_tree(root / "baseline", policy)["sha256"] != row["baseline_sha256"]:
        raise ControlError("workspace_integrity", "Baseline snapshot changed.")
    return files.inspect_tree(root / "tree", policy)


def _current_tree(store, db, row):
    tree = _tree(store, row)
    receipt = db.execute(
        "SELECT r.result FROM workspace_receipts r JOIN workspace_calls c USING(run_id) "
        "WHERE c.workspace_id=? ORDER BY c.rowid DESC,r.seq DESC LIMIT 1",
        (row["id"],),
    ).fetchone()
    expected = json.loads(receipt[0]).get("tree_sha256") if receipt else row["baseline_sha256"]
    if tree["sha256"] != expected:
        raise ControlError(
            "workspace_integrity", "Working tree differs from its latest operation receipt."
        )
    return tree


def materialize(store, db, task_id, revision, prompt, workspace_id=None):
    row = owner(db, task_id)
    if not row:
        return prompt
    mutation_guard(store, db, task_id, workspace_id)
    policy = json.loads(row["policy"])
    if row["state"] != "active" or _calls(db, row["id"]) >= policy["max_calls"]:
        raise ControlError(
            "workspace_closed", "Workspace is stopped or its call budget is exhausted."
        )
    data = contract.read(prompt)
    if data["protocol"] != contract.CONTRACT:
        raise ControlError("workspace_owned", "Workspace tasks use their own fixed coordinator.")
    parent = data["task"]["parent_run_id"]
    data["protocol"] = PROTOCOL
    data["instructions"] = WORKSPACE_INSTRUCTIONS
    data["role"]["instructions"] = WORKSPACE_ROLE_INSTRUCTIONS[policy["role"]]
    review = None
    if row["source_workspace"] is not None and policy["role"] == "verifier":
        source = _get(db, row["source_workspace"])
        saved = db.execute(
            "SELECT * FROM workspace_exports WHERE workspace_id=?",
            (source["id"],),
        ).fetchone()
        if not saved:
            raise ControlError("workspace_unfrozen", "Review source has no frozen export.")
        manifest = files.verify_frozen(
            directory(store, source["id"]) / "frozen",
            json.loads(source["policy"]),
            saved["manifest_sha256"],
        )
        review = {
            "target": {
                "workspace_id": source["id"],
                "run_id": saved["run_id"],
                "result_sha256": saved["result_sha256"],
                "manifest_sha256": saved["manifest_sha256"],
                "tree_sha256": manifest["tree_sha256"],
            },
            "evidence": {
                "requests": [
                    {
                        "run_id": request["run_id"],
                        "seq": request["seq"],
                        "action": json.loads(request["action"]),
                    }
                    for request in db.execute(
                        "SELECT q.run_id,q.seq,q.action FROM workspace_requests q "
                        "JOIN workspace_calls c USING(run_id) WHERE c.workspace_id=? "
                        "ORDER BY c.rowid,q.seq",
                        (source["id"],),
                    )
                ],
                "receipts": _receipts(db, source["id"]),
                "manifest": manifest,
            },
            "criteria": {
                str(index + 1): criterion
                for index, criterion in enumerate(data["assignment"]["acceptance_criteria"])
            },
        }
    data["workspace"] = dict(
        id=row["id"],
        base_commit=row["base_commit"],
        baseline_sha256=row["baseline_sha256"],
        policy=policy,
        tree=_current_tree(store, db, row),
        receipts=_receipts(db, row["id"], parent) if parent else [],
        calls_remaining=policy["max_calls"] - _calls(db, row["id"]) - 1,
        actions_remaining=policy["max_actions"] - _actions(db, row["id"]),
        review=review,
    )
    data["report_contract"]["operations"] = OPERATIONS
    if review is not None:
        data["report_contract"]["review"] = review_contract(review)
    rendered = contract.canonical(data)
    if len(rendered.encode()) > MAX_BYTES:
        raise ControlError(
            "input_too_large", "Workspace input exceeds 1 MiB; no new call reserved."
        )
    return rendered


def record_in(store, db, task_id, revision, run_id, prompt):
    row = owner(db, task_id)
    if row:
        envelope = contract.read(prompt)["workspace"]
        db.execute(
            "INSERT INTO workspace_calls VALUES(?,?,?,?,?,?,?,?)",
            (
                run_id,
                row["id"],
                task_id,
                revision,
                envelope["tree"]["sha256"],
                contract.digest(contract.canonical(envelope["receipts"])),
                contract.canonical(envelope),
                time.time(),
            ),
        )


def inspect_binding(store, db, snapshot, run_id):
    call = db.execute("SELECT * FROM workspace_calls WHERE run_id=?", (run_id,)).fetchone()
    if (
        not call
        or contract.canonical(snapshot["workspace"]) != call["envelope"]
        or snapshot["task"]["id"] != call["task_id"]
        or snapshot["task"]["revision"] != call["revision"]
    ):
        raise ControlError(
            "invalid_snapshot", "Workspace input differs from its immutable call receipt."
        )
    saved = db.execute("SELECT * FROM workspace_exports WHERE run_id=?", (run_id,)).fetchone()
    if saved:
        row = _get(db, saved["workspace_id"])
        files.verify_frozen(
            directory(store, row["id"]) / "frozen",
            json.loads(row["policy"]),
            saved["manifest_sha256"],
        )


def accept_guard(store, db, task_id, run_id):
    row = owner(db, task_id)
    if not row:
        return
    saved = db.execute(
        "SELECT * FROM workspace_exports WHERE workspace_id=? AND run_id=?", (row["id"], run_id)
    ).fetchone()
    if not saved or row["state"] != "finished":
        raise ControlError(
            "workspace_unfrozen", "Accept only the final report with an intact frozen export."
        )
    files.verify_frozen(
        directory(store, row["id"]) / "frozen", json.loads(row["policy"]), saved["manifest_sha256"]
    )


def _halt(store, workspace_id, reason):
    with store.db(write=True) as db:
        db.execute(
            "UPDATE workspaces SET state='awaiting_codex',reason=? WHERE id=? AND state IN ('active','operating')",
            (reason, workspace_id),
        )


def _checkpoint(phase):
    """Fault injection after durable intent and before side effects."""


def _check_command(store, row, run_id, seq, action):
    policy = json.loads(row["policy"])
    check = policy["checks"][action["name"]]
    identity = proc_identity(os.getpid())
    with store.db(write=True) as db:
        if _get(db, row["id"])["state"] != "operating":
            raise ControlError("workspace_stopping", "Workspace stopped before check admission.")
        running = db.execute(
            "SELECT count(*) FROM runs WHERE status IN (?,?,?,?,?,?)", ACTIVE
        ).fetchone()[0]
        if running + command_slots(db) >= store.config["max_parallel"]:
            raise ControlError("capacity", "Checks share the local execution limit.")
        db.execute(
            "INSERT INTO workspace_commands VALUES(?,?,?,?,?,?,NULL,NULL)",
            (run_id, seq, os.getpid(), str(identity["start"]), boot_id(), pid_namespace()),
        )

    def cancelled():
        with store.db() as db:
            return _get(db, row["id"])["state"] != "operating"

    with tempfile.TemporaryDirectory(prefix="check-", dir=directory(store, row["id"])) as temporary:
        scratch = Path(temporary) / "tree"
        files.copy_tree(directory(store, row["id"]) / "tree", scratch, policy)
        return sandbox.execute(
            scratch,
            check["argv"],
            check["timeout"],
            cancelled=cancelled,
            managed=dict(state_dir=store.path, run_id=run_id, seq=seq),
        )


def _perform(store, row, run_id, operations):
    policy = json.loads(row["policy"])
    root = directory(store, row["id"])
    for seq, action in enumerate(operations):
        with store.db() as db:
            if _get(db, row["id"])["state"] != "operating":
                return
            _current_tree(store, db, row)
        try:
            if action["op"] == "read":
                result = dict(
                    outcome="ok", **files.read_text(root / "tree", action["path"], policy)
                )
            elif action["op"] == "write":
                result = dict(
                    outcome="ok",
                    **files.write_text(root / "tree", action["path"], action["content"], policy),
                )
            elif action["op"] == "patch":
                result = dict(
                    outcome="ok",
                    **files.apply_patch(
                        root / "tree",
                        action["path"],
                        action["base_sha256"],
                        action["hunks"],
                        policy,
                    ),
                )
            else:
                result = _check_command(store, row, run_id, seq, action)
            result["tree_sha256"] = _tree(store, row)["sha256"]
        except (ControlError, OSError, ValueError) as exc:
            result = dict(
                outcome="rejected",
                reason=getattr(exc, "code", "operation_failed"),
                message=str(exc),
            )
        with store.db(write=True) as db:
            db.execute(
                "INSERT INTO workspace_receipts VALUES(?,?,?,?)",
                (run_id, seq, contract.canonical(result), time.time()),
            )
        _checkpoint("operation_recorded")
        # A failed test is evidence for revision. Infrastructure/cancellation is terminal.
        if result["outcome"] not in ("ok", "failed"):
            _halt(store, row["id"], result["outcome"])
            return
    with store.db(write=True) as db:
        if _get(db, row["id"])["state"] != "operating":
            return
        binding = db.execute(
            "SELECT * FROM workspace_tasks WHERE workspace_id=?", (row["id"],)
        ).fetchone()
        task = tasks._task(db, binding["task_id"])
        tasks.revise_in(
            store,
            db,
            task["id"],
            task["current_revision"],
            json.loads(binding["assignment"]),
            f"workspace:{row['id']}:after:{run_id}",
            parent_run_id=run_id,
            workspace_id=row["id"],
        )
        db.execute("UPDATE workspaces SET state='active' WHERE id=?", (row["id"],))


def _advance(store, workspace_id):
    with store.db() as db:
        candidate = db.execute(
            "SELECT t.id,t.current_revision FROM workspace_tasks w JOIN tasks t ON t.id=w.task_id "
            "JOIN workspaces s ON s.id=w.workspace_id "
            "WHERE w.workspace_id=? AND s.state='active' AND t.active_run_id IS NULL",
            (workspace_id,),
        ).fetchone()
    if candidate:
        tasks.preflight(
            store,
            candidate["id"],
            candidate["current_revision"],
            f"workspace:{workspace_id}:submit:{candidate['current_revision']}",
        )
    with store.db(write=True) as db:
        row = _get(db, workspace_id)
        if row["state"] != "active":
            return None
        binding = db.execute(
            "SELECT * FROM workspace_tasks WHERE workspace_id=?", (workspace_id,)
        ).fetchone()
        task = tasks._task(db, binding["task_id"])
        policy = json.loads(row["policy"])
        tree = _current_tree(store, db, row)
        if not task["active_run_id"]:
            if _calls(db, workspace_id) >= policy["max_calls"]:
                raise ControlError("call_limit", "Workspace call budget exhausted.")
            run_id, _ = tasks.submit_in(
                store,
                db,
                task["id"],
                task["current_revision"],
                f"workspace:{workspace_id}:submit:{task['current_revision']}",
                workspace_id=workspace_id,
            )
            return run_id
        run = db.execute("SELECT * FROM runs WHERE id=?", (task["active_run_id"],)).fetchone()
        if run["status"] in ACTIVE and run["status"] != "unknown":
            return None
        if run["status"] != "completed":
            requested = json.loads(binding["assignment"])["model"]
            mismatch = any(
                not model.startswith("claude-" + requested + "-")
                for model in json.loads(run["actual_models"])
            )
            raise ControlError(
                "unknown_execution"
                if run["status"] == "unknown"
                else "model_mismatch"
                if mismatch
                else "model_run_failed",
                "Workspace model run did not complete; no automatic retry.",
            )
        checked = contract.inspect_run(store, db, run)
        if checked["format_status"] != "valid":
            call = db.execute(
                "SELECT * FROM workspace_calls WHERE run_id=?", (run["id"],)
            ).fetchone()
            side_effects = db.execute(
                "SELECT (SELECT count(*) FROM workspace_requests WHERE run_id=?) + "
                "(SELECT count(*) FROM workspace_receipts WHERE run_id=?)",
                (run["id"], run["id"]),
            ).fetchone()[0]
            scope = checked["snapshot"]["assignment"]["scope"]
            can_repair = (
                checked["format_status"] == "invalid"
                and not side_effects
                and tree["sha256"] == call["tree_sha256"]
                and FORMAT_REPAIR_SCOPE not in scope
                and _calls(db, workspace_id) < policy["max_calls"]
            )
            if can_repair:
                assignment = dict(json.loads(binding["assignment"]))
                assignment["scope"] = [*assignment["scope"], FORMAT_REPAIR_SCOPE]
                tasks.revise_in(
                    store,
                    db,
                    task["id"],
                    task["current_revision"],
                    assignment,
                    f"workspace:{workspace_id}:format-repair:{run['id']}",
                    parent_run_id=run["id"],
                    acknowledge_context=True,
                    workspace_id=workspace_id,
                )
                return None
            raise ControlError("invalid_report", "Workspace needs a valid operation report.")
        call = db.execute("SELECT * FROM workspace_calls WHERE run_id=?", (run["id"],)).fetchone()
        if tree["sha256"] != call["tree_sha256"]:
            raise ControlError(
                "workspace_integrity", "Working tree changed outside recorded operations."
            )
        operations = validate_operations(checked["report"], policy)
        if not operations:
            if checked["agent_status"] != "complete":
                raise ControlError("agent_blocked", "Agent finished without a complete result.")
            final = True
        else:
            final = False
            if (
                _actions(db, workspace_id) + len(operations) > policy["max_actions"]
                or _calls(db, workspace_id) >= policy["max_calls"]
            ):
                raise ControlError(
                    "workspace_budget", "No budget for these operations and a final report."
                )
            for seq, action in enumerate(operations):
                db.execute(
                    "INSERT INTO workspace_requests VALUES(?,?,?,?)",
                    (run["id"], seq, contract.canonical(action), time.time()),
                )
        db.execute("UPDATE workspaces SET state='operating' WHERE id=?", (workspace_id,))
    _checkpoint("operations_reserved")
    if final:
        frozen = files.freeze(
            directory(store, workspace_id) / "baseline",
            directory(store, workspace_id) / "tree",
            directory(store, workspace_id) / "frozen",
            policy,
        )
        with store.db(write=True) as db:
            if _get(db, workspace_id)["state"] == "operating":
                db.execute(
                    "INSERT INTO workspace_exports VALUES(?,?,?,?,?)",
                    (
                        workspace_id,
                        run["id"],
                        frozen["manifest_sha256"],
                        run["result_sha256"],
                        time.time(),
                    ),
                )
                db.execute("UPDATE workspaces SET state='finished' WHERE id=?", (workspace_id,))
    else:
        _perform(store, row, run["id"], operations)
    return None


@contextmanager
def _coordinator(store, workspace_id):
    lock_context = file_lock(
        directory(store, workspace_id) / "coordinator.lock",
        exclusive=True,
        blocking=False,
    )
    try:
        lock_context.__enter__()
    except BlockingIOError:
        raise ControlError("workspace_busy", "Another coordinator owns this workspace.") from None
    try:
        yield
    finally:
        lock_context.__exit__(None, None, None)


def run(store, workspace_id, *, once=False, max_seconds=30):
    _require(store)
    if (
        isinstance(max_seconds, bool)
        or not isinstance(max_seconds, (int, float))
        or not math.isfinite(max_seconds)
        or not 0 <= max_seconds <= 3600
    ):
        raise ControlError(
            "invalid_limit", "Use a finite coordinator duration from 0 to 3600 seconds."
        )
    with store.db() as db:
        _get(db, workspace_id)
    started = []
    with _coordinator(store, workspace_id):
        with store.db() as db:
            row = _get(db, workspace_id)
        if row["state"] == "operating":
            _halt(store, workspace_id, "operation_unknown")
        elif row["state"] == "active":
            sandbox.require()
            deadline = time.monotonic() + max_seconds
            while once or time.monotonic() < deadline:
                store.refresh()
                try:
                    run_id = _advance(store, workspace_id)
                except ControlError as exc:
                    if exc.code not in ("capacity", "session_busy"):
                        _halt(store, workspace_id, exc.code)
                    run_id = None
                except (OSError, ValueError, sqlite3.IntegrityError):
                    _halt(store, workspace_id, "operation_failed")
                    run_id = None
                if run_id:
                    launch_worker(store, run_id)
                    started.append(run_id)
                with store.db() as db:
                    row = _get(db, workspace_id)
                if once or row["state"] != "active":
                    break
                time.sleep(min(0.2, max(0, deadline - time.monotonic())))
    return {**status(store, workspace_id), "started": started}


def status(store, workspace_id):
    _require(store)
    store.refresh()
    with store.db() as db:
        row = db.execute("SELECT * FROM workspaces WHERE id=?", (workspace_id,)).fetchone()
        if not row:
            creation = db.execute(
                "SELECT * FROM workspace_creations WHERE id=?", (workspace_id,)
            ).fetchone()
            if creation:
                return {**dict(creation), "state": "preparing", "reason": "creation_incomplete"}
        stopping = row and row["state"] == "stopping"
    if stopping:
        _finish_stop(store, workspace_id)
    with store.db() as db:
        row = _get(db, workspace_id)
        binding = db.execute(
            "SELECT task_id FROM workspace_tasks WHERE workspace_id=?", (workspace_id,)
        ).fetchone()
        result = dict(row)
        result["policy"] = json.loads(row["policy"])
        result["calls_used"], result["actions_used"] = (
            _calls(db, workspace_id),
            _actions(db, workspace_id),
        )
        result["receipts"] = _receipts(db, workspace_id)
        result["requests"] = [
            dict(r)
            for r in db.execute(
                "SELECT q.run_id,q.seq,q.action FROM workspace_requests q JOIN workspace_calls c USING(run_id) WHERE c.workspace_id=? ORDER BY c.rowid,q.seq",
                (workspace_id,),
            )
        ]
        saved = db.execute(
            "SELECT * FROM workspace_exports WHERE workspace_id=?", (workspace_id,)
        ).fetchone()
        application = (
            db.execute(
                "SELECT * FROM workspace_applications WHERE workspace_id=?", (workspace_id,)
            ).fetchone()
            if store.config["schema"] >= 12
            else None
        )
    result["task"] = tasks.show(store, binding[0]) if binding else None
    if saved:
        result["export"] = export(store, workspace_id)
    result["application"] = dict(application) if application else None
    return result


def export(store, workspace_id):
    _require(store)
    with store.db() as db:
        row = _get(db, workspace_id)
        saved = db.execute(
            "SELECT * FROM workspace_exports WHERE workspace_id=?", (workspace_id,)
        ).fetchone()
        if not saved:
            raise ControlError(
                "workspace_unfrozen",
                "No final frozen export exists; inspect retained working files and receipts.",
            )
        manifest = files.verify_frozen(
            directory(store, workspace_id) / "frozen",
            json.loads(row["policy"]),
            saved["manifest_sha256"],
        )
    return {
        **dict(saved),
        "base_commit": row["base_commit"],
        "baseline_sha256": row["baseline_sha256"],
        "directory": str(directory(store, workspace_id) / "frozen"),
        "manifest": manifest,
    }


def _git(repo, *arguments, input_data=None):
    try:
        result = subprocess.run(
            ["git", "-C", repo, *arguments],
            input=input_data,
            stdin=subprocess.DEVNULL if input_data is None else None,
            capture_output=True,
            timeout=30,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ControlError("workspace_apply", "Git command timed out.") from exc
    if result.returncode:
        message = result.stderr.decode("utf-8", errors="replace").strip()
        raise ControlError("workspace_apply", message or "Git command failed.")
    return result.stdout


def _source_head(repo):
    return _git(repo, "rev-parse", "--verify", "HEAD").decode("ascii").strip()


def _source_status(repo):
    raw = _git(repo, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    entries = []
    for record in raw.split(b"\0"):
        if not record:
            continue
        if len(record) < 4 or record[2:3] != b" ":
            raise ControlError("workspace_apply", "Git status returned an unsupported entry.")
        code = record[:2].decode("ascii", errors="strict")
        if "R" in code or "C" in code:
            raise ControlError("workspace_apply", "Renamed paths are unsupported during apply.")
        entries.append((code, record[3:].decode("utf-8", errors="strict")))
    return entries


def _object_id(raw):
    digest = raw.decode("ascii", errors="strict").strip()
    if len(digest) != 40 or any(character not in "0123456789abcdef" for character in digest):
        raise ControlError("workspace_apply", "Git returned an invalid object id.")
    return digest


def _canonical_blob(repo, relative, *, data=None, path=None):
    if (data is None) == (path is None):
        raise ValueError("Supply exactly one blob source.")
    arguments = ["hash-object", "--path", relative]
    if data is not None:
        return _object_id(_git(repo, *arguments, "--stdin", input_data=data))
    return _object_id(_git(repo, *arguments, os.fspath(path)))


def _base_mode(repo, base_commit, relative):
    raw = _git(repo, "ls-tree", "-z", base_commit, "--", relative)
    records = [record for record in raw.split(b"\0") if record]
    if len(records) != 1:
        return None
    try:
        header, encoded = records[0].split(b"\t", 1)
        mode, kind, _object_id_value = header.decode("ascii").split(" ")
        path = encoded.decode("utf-8")
    except (UnicodeError, ValueError) as exc:
        raise ControlError("workspace_apply", "Git returned invalid mode metadata.") from exc
    if path != relative or kind != "blob" or mode not in ("100644", "100755"):
        raise ControlError("workspace_apply", "Git returned unexpected mode metadata.")
    return mode


def _preflight_apply(repo, base_commit, manifest):
    collision = find_case_collision(change["path"] for change in manifest["changes"])
    if collision is not None:
        raise ControlError(
            "workspace_apply",
            f"Frozen changes have a case-insensitive path collision: {collision[0]!r} vs {collision[1]!r}.",
        )
    for change in manifest["changes"]:
        relative = change["path"]
        after = manifest["files"].get(relative)
        if after is None:
            continue
        before_mode = _base_mode(repo, base_commit, relative)
        if change["before_sha256"] is not None and before_mode != after["mode"]:
            raise ControlError("workspace_apply", f"Frozen mode differs from base: {relative}")
        if os.name == "nt" and change["before_sha256"] is None and after["mode"] == "100755":
            raise ControlError(
                "workspace_apply",
                f"A new executable file cannot be represented on Windows: {relative}",
            )


def _verify_applied_source(repo, base_commit, manifest, frozen_tree):
    if _source_head(repo) != base_commit:
        raise ControlError("workspace_conflict", "Source HEAD changed during apply.")
    expected = {change["path"] for change in manifest["changes"]}
    observed = {path for _, path in _source_status(repo)}
    if observed != expected:
        raise ControlError(
            "workspace_conflict", "Applied source paths differ from the frozen patch."
        )
    for change in manifest["changes"]:
        relative = change["path"]
        target = os.path.join(repo, relative)
        if change["after_sha256"] is None:
            if os.path.lexists(target):
                raise ControlError("workspace_conflict", "Frozen deletion was not applied.")
            continue
        try:
            info = os.lstat(target)
            frozen_data = (Path(frozen_tree) / relative).read_bytes()
        except OSError as exc:
            raise ControlError(
                "workspace_conflict", f"Cannot verify applied path: {relative}"
            ) from exc
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ControlError(
                "workspace_conflict", f"Applied path is not a private regular file: {relative}"
            )
        if info.st_size > files.MAX_TEXT_BYTES:
            raise ControlError(
                "workspace_conflict", f"Applied path exceeds the text limit: {relative}"
            )
        if (
            len(frozen_data) > files.MAX_TEXT_BYTES
            or hashlib.sha256(frozen_data).hexdigest() != change["after_sha256"]
        ):
            raise ControlError(
                "workspace_conflict", f"Frozen path differs from its manifest: {relative}"
            )
        if _canonical_blob(repo, relative, data=frozen_data) != _canonical_blob(
            repo, relative, path=target
        ):
            raise ControlError(
                "workspace_conflict", f"Applied path hash differs from frozen result: {relative}"
            )
        if os.name != "nt":
            executable = bool(stat.S_IMODE(info.st_mode) & 0o111)
            expected_executable = manifest["files"][relative]["mode"] == "100755"
            if executable != expected_executable:
                raise ControlError(
                    "workspace_conflict",
                    f"Applied path mode differs from frozen result: {relative}",
                )


def apply(store, workspace_id, operation_id):
    """Apply one intact frozen patch to its clean source at the pinned base commit."""
    if store.config["schema"] < 12:
        raise ControlError("migration_required", "Workspace apply requires schema 12.")
    frozen = export(store, workspace_id)
    manifest = frozen["manifest"]
    if not manifest["changes"]:
        raise ControlError("workspace_no_changes", "Frozen workspace has no changes to apply.")
    with store.db() as db:
        row = _get(db, workspace_id)
        intent = {
            "kind": "workspace_apply",
            "workspace": workspace_id,
            "source_repo": row["source_repo"],
            "base_commit": row["base_commit"],
            "patch_sha256": manifest["patch_sha256"],
            "manifest_sha256": frozen["manifest_sha256"],
        }
        fingerprint, prior = tasks._operation(db, operation_id, intent)
        if prior:
            saved = db.execute(
                "SELECT * FROM workspace_applications WHERE operation_id=?", (operation_id,)
            ).fetchone()
            if not saved:
                raise ControlError("workspace_apply_unknown", "Apply reservation is incomplete.")
            return {**dict(saved), "deduplicated": True}
        existing = db.execute(
            "SELECT * FROM workspace_applications WHERE workspace_id=?", (workspace_id,)
        ).fetchone()
        if existing:
            raise ControlError(
                "workspace_already_applied", "Frozen workspace already has an apply attempt."
            )
        repo, base_commit = row["source_repo"], row["base_commit"]
    _preflight_apply(repo, base_commit, manifest)
    if _source_head(repo) != base_commit:
        raise ControlError(
            "workspace_conflict", "Source HEAD no longer matches the frozen base commit."
        )
    if _source_status(repo):
        raise ControlError("workspace_conflict", "Source worktree must be clean before apply.")
    patch_path = directory(store, workspace_id) / "frozen" / "patch.diff"
    _git(repo, "apply", "--check", "--whitespace=nowarn", str(patch_path))
    created = time.time()
    with store.db(write=True) as db:
        fingerprint, prior = tasks._operation(db, operation_id, intent)
        if prior:
            saved = db.execute(
                "SELECT * FROM workspace_applications WHERE operation_id=?", (operation_id,)
            ).fetchone()
            return {**dict(saved), "deduplicated": True}
        tasks._record(db, operation_id, fingerprint, {"workspace_id": workspace_id})
        db.execute(
            "INSERT INTO workspace_applications VALUES(?,?,?,?,?,?,'applying',NULL,?,NULL)",
            (
                workspace_id,
                operation_id,
                repo,
                base_commit,
                manifest["patch_sha256"],
                frozen["manifest_sha256"],
                created,
            ),
        )
    try:
        _git(repo, "apply", "--whitespace=nowarn", str(patch_path))
        _verify_applied_source(
            repo,
            base_commit,
            manifest,
            Path(frozen["directory"]) / "tree",
        )
    except BaseException as exc:
        with store.db(write=True) as db:
            db.execute(
                "UPDATE workspace_applications SET state='unknown',finished=? "
                "WHERE workspace_id=? AND state='applying'",
                (time.time(), workspace_id),
            )
        raise ControlError(
            "workspace_apply_unknown",
            "Apply may have changed the source; inspect it before any further action.",
        ) from exc
    finished = time.time()
    with store.db(write=True) as db:
        db.execute(
            "UPDATE workspace_applications SET state='applied',applied_tree_sha256=?,finished=? "
            "WHERE workspace_id=? AND state='applying'",
            (manifest["tree_sha256"], finished, workspace_id),
        )
        saved = db.execute(
            "SELECT * FROM workspace_applications WHERE workspace_id=?", (workspace_id,)
        ).fetchone()
    return {**dict(saved), "deduplicated": False}


def stop(store, workspace_id, operation_id):
    _require(store)
    with store.db(write=True) as db:
        row = _get(db, workspace_id)
        if row["state"] == "finished":
            raise ControlError(
                "workspace_finished",
                "A frozen completed result cannot be stopped; inspect or accept it.",
            )
        fingerprint, prior = tasks._operation(
            db, operation_id, dict(kind="workspace_stop", workspace=workspace_id)
        )
        if not prior:
            db.execute(
                "UPDATE workspaces SET state='stopping',reason='stop_requested' WHERE id=? AND state!='stopped'",
                (workspace_id,),
            )
            tasks._record(db, operation_id, fingerprint, dict(id=workspace_id))
    _checkpoint("stop_recorded")
    return status(store, workspace_id)


def _finish_stop(store, workspace_id):
    """Resume cancellation after a persisted stop, then drain owned executions."""
    with store.db() as db:
        runs = db.execute(
            "SELECT r.id FROM runs r JOIN workspace_calls c ON c.run_id=r.id WHERE c.workspace_id=? AND r.status IN (?,?,?,?,?,?)",
            (workspace_id, *ACTIVE),
        ).fetchall()
    for r in runs:
        store.stop(r[0])
    store.refresh()
    try:
        with _coordinator(store, workspace_id), store.db(write=True) as db:
            active = db.execute(
                "SELECT 1 FROM runs r JOIN workspace_calls c ON c.run_id=r.id WHERE c.workspace_id=? AND r.status IN (?,?,?,?,?,?)",
                (workspace_id, *ACTIVE),
            ).fetchone()
            commands = db.execute(
                "SELECT 1 FROM workspace_commands c JOIN workspace_calls a USING(run_id) LEFT JOIN workspace_receipts r USING(run_id,seq) WHERE a.workspace_id=? AND r.run_id IS NULL",
                (workspace_id,),
            ).fetchone()
            if not active and not commands:
                db.execute(
                    "UPDATE workspaces SET state='stopped' WHERE id=? AND state='stopping'",
                    (workspace_id,),
                )
    except ControlError as exc:
        if exc.code != "workspace_busy":
            raise


def reconcile(store, workspace_id):
    """Release command slots only after owner and recorded launcher are known dead."""
    _require(store)
    with _coordinator(store, workspace_id), store.db(write=True) as db:
        _get(db, workspace_id)
        commands = db.execute(
            "SELECT c.* FROM workspace_commands c JOIN workspace_calls a USING(run_id) LEFT JOIN workspace_receipts r USING(run_id,seq) WHERE a.workspace_id=? AND r.run_id IS NULL",
            (workspace_id,),
        ).fetchall()
        for c in commands:
            if c["boot"] == boot_id() and (
                c["namespace"] != pid_namespace()
                or alive(c["owner_pid"], c["owner_start"], c["boot"])
                or (c["child_pid"] and alive(c["child_pid"], c["child_start"], c["boot"]))
            ):
                raise ControlError(
                    "command_unknown", "Command owner or launcher may still be alive."
                )
        for c in commands:
            db.execute(
                "INSERT INTO workspace_receipts VALUES(?,?,?,?)",
                (
                    c["run_id"],
                    c["seq"],
                    contract.canonical(dict(outcome="unknown", reason="owner_terminated")),
                    time.time(),
                ),
            )
    return status(store, workspace_id)
