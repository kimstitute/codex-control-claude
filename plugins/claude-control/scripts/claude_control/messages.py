"""Explicit next-turn instructions, frozen handoffs and append-only delivery receipts."""

import json
import re
import time
import uuid

from . import task_contracts as contract
from . import tasks
from .assignments import MAX_BYTES
from .store import ACTIVE, ControlError, alive, boot_id, group_alive, pid_namespace

MAX_SELECTED = 64
MAX_QUEUED = 100


def _require(store):
    if store.config["schema"] < 6:
        raise ControlError("migration_required", "Messages require migrate --offline.")


def normalize_ids(ids):
    if ids is None:
        return []
    if not isinstance(ids, (list, tuple)) or len(ids) > MAX_SELECTED:
        raise ControlError("invalid_messages", "Select at most 64 distinct message UUIDs.")
    for value in ids:
        try:
            if not isinstance(value, str) or str(uuid.UUID(value)) != value:
                raise ValueError()
        except (ValueError, TypeError, AttributeError):
            raise ControlError("invalid_messages", "Use canonical message UUIDs.") from None
    if len(set(ids)) != len(ids):
        raise ControlError("invalid_messages", "Duplicate message selection.")
    return sorted(ids)


def _message(db, message_id):
    row = db.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone()
    if not row:
        raise ControlError("message_not_found", "Unknown message UUID.")
    return row


def _cancelled(db, message_id):
    return (
        db.execute(
            "SELECT 1 FROM message_cancellations WHERE message_id=?", (message_id,)
        ).fetchone()
        is not None
    )


def _latest(db, message_id):
    return db.execute(
        "SELECT r.*,b.seq AS binding_seq,b.revision AS binding_revision FROM message_bindings b "
        "JOIN runs r ON r.id=b.run_id WHERE b.message_id=? ORDER BY b.seq DESC LIMIT 1",
        (message_id,),
    ).fetchone()


def _target(db, message, task):
    if message["task_id"] != task["id"]:
        raise ControlError("message_target_changed", "Message belongs to another task.")
    if message["target_session_id"] and message["target_session_id"] != task["session_id"]:
        raise ControlError("message_target_changed", "Message belongs to another session.")
    if task["session_id"]:
        session = db.execute(
            "SELECT backend_id FROM sessions WHERE id=?", (task["session_id"],)
        ).fetchone()
        backend = message["target_backend_id"]
        if backend is None:
            # A previously unassigned task may acquire its first session, never a replacement.
            first = db.execute(
                "SELECT r.backend_id FROM runs r JOIN task_runs t ON t.run_id=r.id "
                "WHERE t.task_id=? ORDER BY r.rowid LIMIT 1",
                (task["id"],),
            ).fetchone()
            backend = first[0] if first else None
        if backend is not None and session["backend_id"] != backend:
            raise ControlError(
                "message_target_changed", "Message targets a previous backend conversation."
            )


def _payload(message, previous_run_id=None):
    source = None
    if message["source_run_id"]:
        source = dict(
            run_id=message["source_run_id"],
            result_sha256=message["source_result_sha256"],
            report=json.loads(message["source_report"]),
        )
    return dict(
        id=message["id"],
        seq=message["seq"],
        base_revision=message["base_revision"],
        kind=message["kind"],
        content=message["content"],
        content_sha256=message["content_sha256"],
        source=source,
        previous_run_id=previous_run_id,
    )


def _source(store, db, run_id, digest):
    if run_id is None and digest is None:
        return None
    if (
        not isinstance(run_id, str)
        or not isinstance(digest, str)
        or not re.fullmatch(r"[0-9a-f]{64}", digest)
    ):
        raise ControlError(
            "message_source_invalid", "Supply both source run and exact result SHA-256."
        )
    row = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    if not row or row["status"] != "completed" or row["result_sha256"] != digest:
        raise ControlError(
            "message_source_invalid", "Source must be a completed task run with its exact digest."
        )
    try:
        checked = contract.inspect_run(store, db, row)
        if checked is None or checked["format_status"] != "valid":
            raise ControlError(
                "message_source_invalid", "Source needs a valid structured task report."
            )
        return contract.canonical(checked["report"])
    except (ControlError, OSError, ValueError) as exc:
        raise ControlError("message_source_invalid", f"Source cannot be verified: {exc}") from exc


def enqueue(
    store,
    task_id,
    base_revision,
    content,
    operation_id,
    *,
    session_ref=None,
    source_run_id=None,
    source_result_sha256=None,
):
    with store.db(write=True) as db:
        return enqueue_in(
            store,
            db,
            task_id,
            base_revision,
            content,
            operation_id,
            session_ref=session_ref,
            source_run_id=source_run_id,
            source_result_sha256=source_result_sha256,
        )


def enqueue_in(
    store,
    db,
    task_id,
    base_revision,
    content,
    operation_id,
    *,
    session_ref=None,
    source_run_id=None,
    source_result_sha256=None,
    workflow_id=None,
):
    _require(store)
    if not isinstance(content, str) or not content.strip() or len(content.encode()) > MAX_BYTES:
        raise ControlError(
            "invalid_message", "Message content must be nonempty text of at most 1 MiB."
        )
    fingerprint, prior = tasks._operation(
        db,
        operation_id,
        dict(
            kind="message_enqueue",
            task=task_id,
            base=base_revision,
            content=content,
            session=session_ref,
            source_run=source_run_id,
            source_digest=source_result_sha256,
        ),
    )
    if prior:
        return {**prior, "deduplicated": True}
    from .workflow import mutation_guard

    mutation_guard(store, db, task_id, workflow_id)
    task = tasks._task(db, task_id)
    if type(base_revision) is not int or task["current_revision"] != base_revision:
        raise ControlError("stale_message", "Select the current base revision explicitly.")
    if session_ref is not None and session_ref != task["session_id"]:
        raise ControlError("message_target_changed", "Supplied session must match the task.")
    count = db.execute(
        "SELECT count(*) FROM messages m WHERE m.task_id=? "
        "AND NOT EXISTS(SELECT 1 FROM message_bindings b WHERE b.message_id=m.id) "
        "AND NOT EXISTS(SELECT 1 FROM message_cancellations c WHERE c.message_id=m.id)",
        (task_id,),
    ).fetchone()[0]
    if count >= MAX_QUEUED:
        raise ControlError(
            "message_full", "Task has 100 unbound messages; cancel stale instructions first."
        )
    backend = None
    if task["session_id"]:
        backend = db.execute(
            "SELECT backend_id FROM sessions WHERE id=?", (task["session_id"],)
        ).fetchone()[0]
    source_report = _source(store, db, source_run_id, source_result_sha256)
    message = dict(
        id=str(uuid.uuid4()),
        task_id=task_id,
        base_revision=base_revision,
        target_session_id=task["session_id"],
        target_backend_id=backend,
        content=content,
        content_sha256=contract.digest(content),
        kind="handoff" if source_report else "instruction",
        source_run_id=source_run_id,
        source_result_sha256=source_result_sha256,
        source_report=source_report,
        created=time.time(),
    )
    db.execute(
        "INSERT INTO messages(id,task_id,base_revision,target_session_id,target_backend_id,"
        "content,content_sha256,kind,source_run_id,source_result_sha256,source_report,created) "
        "VALUES(:id,:task_id,:base_revision,:target_session_id,:target_backend_id,:content,"
        ":content_sha256,:kind,:source_run_id,:source_result_sha256,:source_report,:created)",
        message,
    )
    message = _message(db, message["id"])
    if len(contract.canonical(_payload(message)).encode()) > MAX_BYTES:
        raise ControlError("input_too_large", "Message and copied source exceed 1 MiB.")
    result = {k: message[k] for k in ("id", "seq", "task_id", "base_revision")}
    tasks._record(db, operation_id, fingerprint, result)
    return {**result, "deduplicated": False}


def cancel(store, message_id, operation_id):
    _require(store)
    with store.db(write=True) as db:
        fingerprint, prior = tasks._operation(
            db, operation_id, dict(kind="message_cancel", message=message_id)
        )
        if prior:
            return {**prior, "deduplicated": True}
        message = _message(db, message_id)
        from .workflow import mutation_guard

        mutation_guard(store, db, message["task_id"])
        if _latest(db, message_id):
            raise ControlError(
                "message_bound", "A bound message retains its delivery history; inspect its run."
            )
        if not _cancelled(db, message_id):
            db.execute("INSERT INTO message_cancellations VALUES(?,?)", (message_id, time.time()))
        result = dict(id=message_id, state="cancelled")
        tasks._record(db, operation_id, fingerprint, result)
    return {**result, "deduplicated": False}


def _stopped_failure(row):
    if row["status"] not in ("failed", "cancelled", "launch_failed", "interrupted"):
        return False
    if row["worker_pid"] or row["child_pid"]:
        if row["boot"] == boot_id() and row["worker_namespace"] != pid_namespace():
            return False
        if alive(row["worker_pid"], row["worker_start"], row["boot"]):
            return False
        if row["boot"] == boot_id() and row["child_pid"] and group_alive(row["child_pid"]):
            return False
    return True


def select_in(store, db, task, revision, ids, redeliver=False):
    _require(store)
    for message_id in ids:
        message = _message(db, message_id)
        _target(db, message, task)
        if _cancelled(db, message_id):
            raise ControlError("message_cancelled", "Cancelled messages cannot be selected.")
        previous = _latest(db, message_id)
        if previous:
            if not redeliver:
                raise ControlError(
                    "message_already_bound", "Explicit redeliver-messages is required."
                )
            if not _stopped_failure(previous):
                raise ControlError(
                    "redelivery_denied", "Redelivery needs an inspected, stopped failed attempt."
                )
        elif message["base_revision"] != task["current_revision"]:
            raise ControlError(
                "stale_message", "Message base changed; cancel and enqueue for the current base."
            )
        db.execute(
            "INSERT INTO revision_messages VALUES(?,?,?,?)",
            (task["id"], revision, message_id, previous["id"] if previous else None),
        )
    # Reject impossible selections without advancing the task or consuming any message.
    base = tasks._revision(db, task["id"], revision)["prompt"]
    _render(base, selected_payloads(db, task["id"], revision))


def selected_payloads(db, task_id, revision):
    return [
        _payload(row, row["expected_prior_run_id"])
        for row in db.execute(
            "SELECT m.*,s.expected_prior_run_id FROM revision_messages s JOIN messages m ON m.id=s.message_id "
            "WHERE s.task_id=? AND s.revision=? ORDER BY m.seq",
            (task_id, revision),
        )
    ]


def _render(prompt, payloads):
    if not payloads:
        return prompt
    snapshot = contract.read(prompt)
    snapshot.update(protocol=contract.MESSAGE_CONTRACT, messages=payloads)
    snapshot.setdefault("dependencies", [])
    result = contract.canonical(snapshot)
    if len(result.encode()) > MAX_BYTES:
        raise ControlError("input_too_large", "Final input with selected messages exceeds 1 MiB.")
    return result


def materialize(store, db, task_id, revision, prompt, attempts):
    task = tasks._task(db, task_id, revision)
    ids = {row["id"] for row in attempts}
    payloads = selected_payloads(db, task_id, revision)
    for payload in payloads:
        message = _message(db, payload["id"])
        _target(db, message, task)
        if _cancelled(db, message["id"]):
            raise ControlError(
                "message_cancelled", "A selected message was cancelled; revise before submitting."
            )
        latest = _latest(db, message["id"])
        previous = payload["previous_run_id"]
        if latest:
            if latest["id"] not in ids and (
                latest["id"] != previous or not _stopped_failure(latest)
            ):
                raise ControlError(
                    "message_already_bound", "Message delivery advanced after selection."
                )
        elif previous:
            raise ControlError("invalid_snapshot", "Previous message delivery is missing.")
        if contract.digest(message["content"]) != message["content_sha256"]:
            raise ControlError("invalid_snapshot", "Message content digest changed.")
    return _render(prompt, payloads)


def bind_in(db, task_id, revision, run_id):
    db.execute(
        "INSERT INTO message_bindings(message_id,run_id,task_id,revision,created) "
        "SELECT message_id,?,task_id,revision,? FROM revision_messages WHERE task_id=? AND revision=?",
        (run_id, time.time(), task_id, revision),
    )


def inspect_binding(db, snapshot, run_id):
    task = snapshot["task"]
    expected = selected_payloads(db, task["id"], task["revision"])
    actual_ids = [
        row[0]
        for row in db.execute(
            "SELECT message_id FROM message_bindings WHERE run_id=? ORDER BY message_id", (run_id,)
        )
    ]
    if (
        snapshot.get("messages") != expected
        or actual_ids != sorted(item["id"] for item in expected)
        or any(contract.digest(item["content"]) != item["content_sha256"] for item in expected)
    ):
        raise ControlError(
            "invalid_snapshot", "Message input differs from its selection or delivery receipt."
        )


def _state(store, db, message, bindings, selections):
    if _cancelled(db, message["id"]):
        return "cancelled", None
    if bindings:
        run = db.execute("SELECT * FROM runs WHERE id=?", (bindings[-1]["run_id"],)).fetchone()
        if run["status"] == "unknown":
            return "needs_attention", "unknown_execution"
        if run["status"] in ACTIVE:
            return "bound_to_run", run["status"]
        if run["status"] == "completed":
            try:
                if store.result_valid(run):
                    return "run_completed", None
                return "needs_attention", "result_integrity"
            except ControlError as exc:
                return "needs_attention", exc.code
        return "needs_attention", run["reason"] or run["status"]
    task = tasks._task(db, message["task_id"])
    try:
        _target(db, message, task)
    except ControlError:
        return "queued", "target_changed"
    if any(row["revision"] == task["current_revision"] for row in selections):
        return "queued", "selected_for_revision"
    if message["base_revision"] != task["current_revision"]:
        return "queued", "revision_changed"
    return "queued", "awaiting_selection"


def list_messages(store, task_id=None, after=0, limit=100):
    _require(store)
    if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 1000:
        raise ControlError("invalid_limit", "Use a nonnegative cursor and limit 1–1000.")
    store.refresh()
    with store.db() as db:
        if task_id is not None:
            tasks._task(db, task_id)
        result = []
        for row in db.execute(
            "SELECT * FROM messages WHERE seq>? "
            + ("AND task_id=? " if task_id else "")
            + "ORDER BY seq LIMIT ?",
            (after, task_id, limit) if task_id else (after, limit),
        ).fetchall():
            bindings = [
                dict(b)
                for b in db.execute(
                    "SELECT * FROM message_bindings WHERE message_id=? ORDER BY seq", (row["id"],)
                )
            ]
            selections = [
                dict(s)
                for s in db.execute(
                    "SELECT * FROM revision_messages WHERE message_id=? ORDER BY revision",
                    (row["id"],),
                )
            ]
            state, reason = _state(store, db, row, bindings, selections)
            result.append(
                dict(
                    _payload(row),
                    task_id=row["task_id"],
                    target_session_id=row["target_session_id"],
                    target_backend_id=row["target_backend_id"],
                    created=row["created"],
                    state=state,
                    reason=reason,
                    selections=selections,
                    bindings=bindings,
                    redelivered=len(bindings) > 1,
                )
            )
    return dict(messages=result, next_cursor=result[-1]["seq"] if result else after)
