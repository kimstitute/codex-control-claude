"""Durable FIFO admission with exact dependencies and an explicitly bounded loop."""

import math
import time
import uuid

from . import task_contracts as contract
from . import tasks
from .assignments import MAX_BYTES
from .runner import launch_worker
from .store import ACTIVE, ControlError

TERMINAL_BLOCKS = frozenset(
    (
        "dependency_changed",
        "input_too_large",
        "input_changed",
        "invalid_snapshot",
        "context_changed",
        "stale_revision",
        "already_submitted",
        "request_conflict",
    )
)


def _require(store):
    if store.config["schema"] < 5:
        raise ControlError("migration_required", "Queue commands require migrate --offline.")


def _dependencies(value):
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > 64:
        raise ControlError(
            "invalid_dependencies", "Supply at most 64 exact task/revision references."
        )
    nodes = []
    for ref in value:
        if not isinstance(ref, dict) or set(ref) != {"task_id", "revision"}:
            raise ControlError("invalid_dependencies", "Expected task_id and revision fields.")
        try:
            if str(uuid.UUID(ref["task_id"])) != ref["task_id"]:
                raise ValueError()
        except (ValueError, TypeError, AttributeError):
            raise ControlError("invalid_dependencies", "Use canonical task UUIDs.") from None
        if type(ref["revision"]) is not int or ref["revision"] < 1:
            raise ControlError("invalid_dependencies", "Revision must be a positive integer.")
        nodes.append((ref["task_id"], ref["revision"]))
    if len(nodes) != len(set(nodes)):
        raise ControlError("invalid_dependencies", "Duplicate dependency reference.")
    return [dict(task_id=t, revision=r) for t, r in sorted(nodes)]


def _check_graph(db, task_id, revision, dependencies):
    target = (task_id, revision)
    for ref in dependencies:
        if ref["task_id"] == task_id:
            raise ControlError("dependency_self", "A task cannot depend on itself.")
        parent = db.execute(
            "SELECT current_revision FROM tasks WHERE id=?", (ref["task_id"],)
        ).fetchone()
        if not parent:
            raise ControlError("dependency_not_found", "Dependency task does not exist.")
        if parent[0] != ref["revision"]:
            raise ControlError("stale_dependency", "Pin the parent's current revision explicitly.")
    frontier = [(ref["task_id"], ref["revision"]) for ref in dependencies]
    seen = set()
    while frontier:
        node = frontier.pop()
        if node == target:
            raise ControlError("dependency_cycle", "Dependency would create a cycle.")
        if node in seen:
            continue
        seen.add(node)
        frontier.extend(
            tuple(row)
            for row in db.execute(
                "SELECT parent_task_id,parent_revision FROM task_dependencies "
                "WHERE child_task_id=? AND child_revision=?",
                node,
            )
        )


def enqueue(store, task_id, revision, operation_id, dependencies=None):
    with store.db(write=True) as db:
        return enqueue_in(store, db, task_id, revision, operation_id, dependencies=dependencies)


def enqueue_in(store, db, task_id, revision, operation_id, dependencies=None, *, workflow_id=None):
    _require(store)
    dependencies = _dependencies(dependencies)
    fingerprint, prior = tasks._operation(
        db,
        operation_id,
        dict(kind="enqueue", task=task_id, revision=revision, dependencies=dependencies),
    )
    if prior:
        return {**prior, "deduplicated": True}
    from .workflow import mutation_guard

    mutation_guard(store, db, task_id, workflow_id)
    tasks._task(db, task_id, revision)
    if db.execute(
        "SELECT 1 FROM task_runs WHERE task_id=? AND revision=?", (task_id, revision)
    ).fetchone():
        raise ControlError("already_submitted", "Use task retry or revise after a reservation.")
    _check_graph(db, task_id, revision, dependencies)
    digest = contract.digest(contract.canonical(dependencies))
    pinned = db.execute(
        "SELECT fingerprint FROM dependency_sets WHERE task_id=? AND revision=?",
        (task_id, revision),
    ).fetchone()
    if pinned and pinned[0] != digest:
        raise ControlError("dependency_conflict", "Dependencies are fixed; create a new revision.")
    existing = db.execute(
        "SELECT * FROM queue_entries WHERE task_id=? AND revision=? AND state='waiting'",
        (task_id, revision),
    ).fetchone()
    if existing:
        result = dict(
            queue_id=existing["id"], state=existing["state"], task_id=task_id, revision=revision
        )
    else:
        if (
            db.execute("SELECT count(*) FROM queue_entries WHERE state='waiting'").fetchone()[0]
            >= store.config["max_queued"]
        ):
            raise ControlError("queue_full", "Waiting queue is full; dequeue or dispatch work.")
        if not pinned:
            db.execute("INSERT INTO dependency_sets VALUES(?,?,?)", (task_id, revision, digest))
            db.executemany(
                "INSERT INTO task_dependencies VALUES(?,?,?,?)",
                [(task_id, revision, ref["task_id"], ref["revision"]) for ref in dependencies],
            )
        entry = db.execute(
            "INSERT INTO queue_entries(task_id,revision,state,created) VALUES(?,?,'waiting',?)",
            (task_id, revision, time.time()),
        )
        result = dict(queue_id=entry.lastrowid, state="waiting", task_id=task_id, revision=revision)
    tasks._record(db, operation_id, fingerprint, result)
    return {**result, "deduplicated": False}


def dequeue(store, queue_id, operation_id):
    _require(store)
    if type(queue_id) is not int or queue_id < 1:
        raise ControlError("invalid_queue", "Queue ID must be a positive integer.")
    with store.db(write=True) as db:
        fingerprint, prior = tasks._operation(
            db, operation_id, dict(kind="dequeue", queue_id=queue_id)
        )
        if prior:
            return {**prior, "deduplicated": True}
        row = db.execute("SELECT * FROM queue_entries WHERE id=?", (queue_id,)).fetchone()
        if not row or row["state"] != "waiting":
            raise ControlError("queue_not_waiting", "Only a waiting queue entry can be removed.")
        from .workflow import mutation_guard

        mutation_guard(store, db, row["task_id"])
        db.execute("UPDATE queue_entries SET state='cancelled' WHERE id=?", (queue_id,))
        result = dict(queue_id=queue_id, state="cancelled")
        tasks._record(db, operation_id, fingerprint, result)
    return {**result, "deduplicated": False}


def materialize(store, db, task_id, revision, base):
    """Copy approved parent evidence under the same write lock as run reservation."""
    if not db.execute(
        "SELECT 1 FROM dependency_sets WHERE task_id=? AND revision=?", (task_id, revision)
    ).fetchone():
        return base
    dependencies = []
    for ref in db.execute(
        "SELECT parent_task_id,parent_revision FROM task_dependencies "
        "WHERE child_task_id=? AND child_revision=? ORDER BY parent_task_id,parent_revision",
        (task_id, revision),
    ).fetchall():
        parent = tasks._task(db, ref["parent_task_id"])
        if parent["current_revision"] != ref["parent_revision"]:
            raise ControlError(
                "dependency_changed", "Pinned parent revision changed; revise this child."
            )
        row = db.execute(
            "SELECT r.* FROM runs r JOIN task_runs t ON t.run_id=r.id "
            "WHERE r.id=? AND t.task_id=? AND t.revision=?",
            (parent["active_run_id"], parent["id"], parent["current_revision"]),
        ).fetchone()
        if not row or row["status"] != "completed":
            status = row["status"] if row else "unsubmitted"
            code = (
                "dependency_" + status
                if status in ("unknown", "failed", "cancelled", "launch_failed", "interrupted")
                else "dependency_not_accepted"
            )
            raise ControlError(code, f"Parent {parent['id']} is {status}.")
        accepted = db.execute(
            "SELECT id FROM review_decisions WHERE task_id=? AND revision=? AND run_id=? "
            "AND result_sha256=? AND kind='accept' ORDER BY rowid LIMIT 1",
            (parent["id"], parent["current_revision"], row["id"], row["result_sha256"]),
        ).fetchone()
        if not accepted:
            raise ControlError(
                "dependency_not_accepted", f"Parent {parent['id']} needs Codex acceptance."
            )
        checked = contract.inspect_run(store, db, row)
        if checked["format_status"] != "valid" or checked["agent_status"] != "complete":
            raise ControlError("dependency_not_accepted", "Parent result no longer validates.")
        dependencies.append(
            dict(
                task_id=parent["id"],
                revision=parent["current_revision"],
                run_id=row["id"],
                result_sha256=row["result_sha256"],
                decision_id=accepted["id"],
                report=checked["report"],
            )
        )
    snapshot = contract.read(base)
    prompt = contract.canonical(
        {**snapshot, "protocol": contract.EXECUTION_CONTRACT, "dependencies": dependencies}
    )
    if len(prompt.encode()) > MAX_BYTES:
        raise ControlError(
            "input_too_large", "Final execution input exceeds 1 MiB; revise the child."
        )
    return prompt


def events(store, task_id=None, after=0, limit=100):
    _require(store)
    if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 1000:
        raise ControlError("invalid_limit", "Use a nonnegative cursor and a limit of 1–1000.")
    with store.db() as db:
        if task_id is not None:
            tasks._task(db, task_id)
        rows = [
            dict(row)
            for row in db.execute(
                "SELECT * FROM task_events WHERE id>? "
                + ("AND task_id=? " if task_id else "")
                + "ORDER BY id LIMIT ?",
                (after, task_id, limit) if task_id else (after, limit),
            )
        ]
    return dict(events=rows, next_cursor=rows[-1]["id"] if rows else after)


def _checkpoint(phase, run_ids):
    """Crash-injection boundary for process-lifecycle regression tests."""


def _snapshot(store):
    with store.db() as db:
        waiting = [
            dict(row)
            for row in db.execute("SELECT * FROM queue_entries WHERE state='waiting' ORDER BY id")
        ]
        active = [
            dict(row)
            for row in db.execute(
                "SELECT id,status,session_id FROM runs WHERE status IN (?,?,?,?,?,?) ORDER BY rowid",
                ACTIVE,
            )
        ]
        attention = [
            dict(row)
            for row in db.execute(
                "SELECT q.id AS queue_id,q.task_id,q.revision,r.id AS run_id,r.status,r.reason "
                "FROM queue_entries q JOIN runs r ON r.id=q.run_id "
                "JOIN tasks t ON t.id=q.task_id AND t.current_revision=q.revision "
                "AND t.active_run_id=q.run_id WHERE q.state='reserved' "
                "AND r.status NOT IN ('pending','claimed','launching','running','stopping','completed') ORDER BY q.id"
            )
        ]
    attention.extend(
        dict(
            queue_id=row["id"],
            task_id=row["task_id"],
            reason=row["blocked_reason"],
            terminal_block=row["terminal_block"],
        )
        for row in waiting
        if row["blocked_reason"]
    )
    return dict(waiting=waiting, active=active, attention=attention)


def tick(store, *, deadline=None, workflow_id=None):
    _require(store)
    store.refresh()
    started = []
    # Probe executable capabilities before admission, never while holding a writer lock.
    with store.db() as db:
        candidates = db.execute(
            "SELECT id,task_id,revision FROM queue_entries "
            "WHERE state='waiting' AND terminal_block=0 ORDER BY id"
        ).fetchall()
    preflight_errors = {}
    preflighted = set()
    for candidate in candidates:
        if deadline is not None and time.monotonic() >= deadline:
            break
        if workflow_id is not None:
            from .workflow import owner

            with store.db() as db:
                owning = owner(db, candidate["task_id"])
            if owning is None or owning["id"] != workflow_id:
                continue
        key = (candidate["id"], candidate["task_id"], candidate["revision"])
        operation = f"dispatch:{store.config['installation_id']}:{candidate['id']}"
        try:
            tasks.preflight(store, candidate["task_id"], candidate["revision"], operation)
        except (ControlError, OSError, ValueError) as exc:
            preflight_errors[key] = exc
        else:
            preflighted.add(key)
    with store.db(write=True) as db:
        entries = db.execute(
            "SELECT * FROM queue_entries WHERE state='waiting' ORDER BY id"
        ).fetchall()
        for entry in entries:
            if workflow_id is not None:
                from .workflow import owner

                owning = owner(db, entry["task_id"])
                if owning is None or owning["id"] != workflow_id:
                    continue
            if deadline is not None and time.monotonic() >= deadline:
                break
            if entry["terminal_block"]:
                continue
            key = (entry["id"], entry["task_id"], entry["revision"])
            if key not in preflighted and key not in preflight_errors:
                continue
            db.execute("SAVEPOINT admission")
            try:
                preflight_error = preflight_errors.get(key)
                if preflight_error:
                    raise preflight_error
                operation = f"dispatch:{store.config['installation_id']}:{entry['id']}"
                run_id, created = tasks.submit_in(
                    store, db, entry["task_id"], entry["revision"], operation
                )
                if not created:
                    raise ControlError("request_conflict", "Queue operation was already reserved.")
                # Recheck after artifact I/O, immediately before admitting this candidate.
                if deadline is not None and time.monotonic() >= deadline:
                    db.execute("ROLLBACK TO admission")
                    db.execute("RELEASE admission")
                    break
            except (ControlError, OSError, ValueError) as exc:
                db.execute("ROLLBACK TO admission")
                db.execute("RELEASE admission")
                code = getattr(
                    exc,
                    "code",
                    "result_unreadable" if isinstance(exc, OSError) else "invalid_snapshot",
                )
                db.execute(
                    "UPDATE queue_entries SET blocked_reason=?,terminal_block=? WHERE id=?",
                    (code, int(code in TERMINAL_BLOCKS), entry["id"]),
                )
            else:
                db.execute("RELEASE admission")
                started.append(run_id)
    _checkpoint("reserved", started)
    for run_id in started:
        launch_worker(store, run_id)
        _checkpoint("launched", [run_id])
    return dict(started=started, **_snapshot(store))


def dispatch(store, *, once=False, max_seconds=30):
    if (
        isinstance(max_seconds, bool)
        or not isinstance(max_seconds, (int, float))
        or not math.isfinite(max_seconds)
        or not 0 <= max_seconds <= 3600
    ):
        raise ControlError("invalid_dispatch", "max-seconds must be finite and between 0 and 3600.")
    deadline = None if once else time.monotonic() + max_seconds
    started = []
    ticks = 0
    while True:
        result = tick(store, deadline=deadline)
        ticks += 1
        started.extend(result["started"])
        active = [row for row in result["active"] if row["status"] != "unknown"]
        if once:
            reason = "once"
        elif time.monotonic() >= deadline:
            reason = "deadline"
        elif not active:
            reason = (
                "needs_attention"
                if result["waiting"] or result["attention"] or result["active"]
                else "idle"
            )
        else:
            time.sleep(min(0.2, max(0, deadline - time.monotonic())))
            continue
        return dict(result, started=started, ticks=ticks, reason=reason)
