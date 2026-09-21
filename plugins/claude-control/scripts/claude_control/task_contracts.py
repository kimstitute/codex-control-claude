"""Explicit task/revision contracts; v1 delegate snapshots retain their old semantics."""

import hashlib
import json
import uuid

from .assignments import (
    CONTRACT as LEGACY_CONTRACT,
)
from .assignments import MAX_BYTES, _strip_fence, read_snapshot, render_assignment, strict_json
from .assignments import validate_report as validate_legacy_report
from .store import ControlError
from .workflow_contracts import PROTOCOL as WORKFLOW_CONTRACT
from .workflow_contracts import validate_review
from .workspace_contracts import OPERATIONS, validate_envelope, validate_operations
from .workspace_contracts import PROTOCOL as WORKSPACE_CONTRACT

CONTRACT = "claude-control.task.v2"
EXECUTION_CONTRACT = "claude-control.task.v3"
MESSAGE_CONTRACT = "claude-control.task.v4"


def canonical(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    )


def digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def render(assignment, task_id, revision, parent, backend, *, previous=None):
    snapshot = json.loads(render_assignment(assignment))
    if previous is not None:
        # Existing roles/instructions stay pinned even after a plugin update.
        snapshot["role"] = previous["role"]
        snapshot["instructions"] = previous["instructions"]
    snapshot["protocol"] = CONTRACT
    snapshot["task"] = dict(id=task_id, revision=revision, parent_run_id=parent, backend_id=backend)
    snapshot["profile"] = "text-only"
    snapshot["report_contract"]["task_id"] = (
        "string; must equal task.id exactly (not assignment.id)"
    )
    snapshot["report_contract"]["revision"] = "integer; must equal task.revision exactly"
    prompt = canonical(snapshot)
    if len(prompt.encode()) > MAX_BYTES:
        raise ControlError("invalid_assignment", "Final task prompt exceeds 1 MiB.")
    read(prompt)
    return prompt


def read(prompt):
    data = strict_json(prompt)
    if not isinstance(data, dict) or data.get("protocol") not in (
        CONTRACT,
        EXECUTION_CONTRACT,
        MESSAGE_CONTRACT,
        WORKFLOW_CONTRACT,
        WORKSPACE_CONTRACT,
    ):
        raise ControlError("invalid_snapshot", "Expected an explicit task contract.")
    if data["protocol"] in (EXECUTION_CONTRACT, MESSAGE_CONTRACT, WORKFLOW_CONTRACT):
        if not isinstance(data.get("dependencies"), list):
            raise ControlError("invalid_snapshot", "Execution dependencies must be a list.")
        for dependency in data["dependencies"]:
            if not isinstance(dependency, dict) or set(dependency) != {
                "task_id",
                "revision",
                "run_id",
                "result_sha256",
                "decision_id",
                "report",
            }:
                raise ControlError("invalid_snapshot", "Invalid execution dependency.")
    if data["protocol"] == WORKSPACE_CONTRACT:
        validate_envelope(data.get("workspace"))
        if data.get("report_contract", {}).get("operations") != OPERATIONS:
            raise ControlError("invalid_snapshot", "Workspace operation contract changed.")
    elif "workspace" in data:
        raise ControlError("invalid_snapshot", "Workspace input requires task contract v6.")
    if data["protocol"] == WORKFLOW_CONTRACT:
        flow = data.get("workflow")
        if not isinstance(flow, dict) or set(flow) != {
            "id",
            "phase",
            "round",
            "policy",
            "source",
            "criteria",
            "review_contract",
        }:
            raise ControlError("invalid_snapshot", "Invalid workflow envelope.")
        if (
            flow["phase"] not in ("worker", "review")
            or not isinstance(flow["criteria"], dict)
            or not flow["criteria"]
        ):
            raise ControlError("invalid_snapshot", "Invalid workflow phase or criteria.")
        if flow["phase"] == "review" and (
            not isinstance(flow["review_contract"], dict)
            or data.get("report_contract", {}).get("review")
            != flow["review_contract"].get("review")
        ):
            raise ControlError(
                "invalid_snapshot", "Review output contract differs from workflow contract."
            )
    elif "workflow" in data:
        raise ControlError("invalid_snapshot", "Workflow input requires task contract v5.")
    if data["protocol"] == MESSAGE_CONTRACT or (
        data["protocol"] == WORKFLOW_CONTRACT and "messages" in data
    ):
        payloads = data.get("messages")
        if not isinstance(payloads, list) or not 1 <= len(payloads) <= 64:
            raise ControlError("invalid_snapshot", "Expected 1–64 selected messages.")
        for message in payloads:
            if not isinstance(message, dict) or set(message) != {
                "id",
                "seq",
                "base_revision",
                "kind",
                "content",
                "content_sha256",
                "source",
                "previous_run_id",
            }:
                raise ControlError("invalid_snapshot", "Invalid message execution envelope.")
    elif "messages" in data:
        raise ControlError("invalid_snapshot", "Messages require task contract v4.")
    if data.get("profile") != "text-only":
        raise ControlError("invalid_snapshot", "Unsupported task execution profile.")
    task = data.get("task")
    if not isinstance(task, dict) or set(task) != {"id", "revision", "parent_run_id", "backend_id"}:
        raise ControlError("invalid_snapshot", "Invalid task identity fields.")
    try:
        for key in ("id", "parent_run_id", "backend_id"):
            value = task[key]
            if value is None and key != "id":
                continue
            if not isinstance(value, str) or str(uuid.UUID(value)) != value:
                raise ValueError()
        if type(task["revision"]) is not int or task["revision"] < 1:
            raise ValueError()
    except ValueError:
        raise ControlError("invalid_snapshot", "Invalid task UUID or revision.") from None
    legacy = {
        k: v
        for k, v in data.items()
        if k not in ("task", "profile", "dependencies", "messages", "workflow", "workspace")
    }
    legacy["protocol"] = LEGACY_CONTRACT
    read_snapshot(canonical(legacy))
    return data


def validate_report(text, snapshot):
    if not isinstance(text, str) or len(text.encode()) > MAX_BYTES:
        raise ControlError("invalid_report", "Task report must be text of at most 1 MiB.")
    try:
        data = strict_json(_strip_fence(text.replace("\r\n", "\n").strip()))
    except ControlError:
        raise ControlError("invalid_report", "Expected a single task report JSON object.") from None
    if (
        not isinstance(data, dict)
        or type(data.get("revision")) is not int
        or data.get("revision") != snapshot["task"]["revision"]
        or data.get("task_id") != snapshot["task"]["id"]
    ):
        raise ControlError("invalid_report", "Report task/revision does not match its run.")
    review_phase = (
        snapshot["protocol"] == WORKFLOW_CONTRACT and snapshot["workflow"]["phase"] == "review"
    )
    if review_phase:
        validate_review(data.get("review"), snapshot["workflow"])
    elif "review" in data:
        raise ControlError(
            "invalid_report", "Review extension is only valid for workflow review turns."
        )
    if snapshot["protocol"] == WORKSPACE_CONTRACT:
        validate_operations(data, snapshot["workspace"]["policy"])
    elif "operations" in data:
        raise ControlError("invalid_report", "Operations require an explicit workspace contract.")
    legacy_report = {k: v for k, v in data.items() if k not in ("revision", "review", "operations")}
    legacy_report["task_id"] = snapshot["assignment"]["id"]
    validate_legacy_report(canonical(legacy_report), snapshot)
    return data


def inspect_run(store, db, row):
    """Validate the exact bytes and explicit DB binding, using the caller's transaction."""
    link = db.execute(
        "SELECT tr.*,v.prompt,v.expected_parent_run_id,v.expected_backend_id "
        "FROM task_runs tr JOIN task_revisions v ON v.task_id=tr.task_id AND v.revision=tr.revision "
        "WHERE tr.run_id=?",
        (row["id"],),
    ).fetchone()
    if not link:
        return None
    with (store.run_dir(row["id"]) / "prompt.txt").open("rb") as handle:
        raw = handle.read(MAX_BYTES + 1)
    prompt = link["prompt"]
    if link["contract"] in (
        EXECUTION_CONTRACT,
        MESSAGE_CONTRACT,
        WORKFLOW_CONTRACT,
        WORKSPACE_CONTRACT,
    ):
        execution = db.execute(
            "SELECT * FROM execution_inputs WHERE run_id=?", (row["id"],)
        ).fetchone()
        if not execution or execution["base_sha256"] != digest(prompt):
            raise ControlError("invalid_snapshot", "Execution base differs from its revision.")
        snapshot = read(execution["prompt"])
        base = {
            k: v
            for k, v in snapshot.items()
            if k not in ("dependencies", "messages", "workflow", "workspace")
        }
        if snapshot["protocol"] == WORKFLOW_CONTRACT and snapshot["workflow"]["phase"] == "review":
            base["report_contract"] = {
                k: v for k, v in base["report_contract"].items() if k != "review"
            }
        if snapshot["protocol"] == WORKSPACE_CONTRACT:
            base["report_contract"] = {
                k: v for k, v in base["report_contract"].items() if k != "operations"
            }
        base["protocol"] = CONTRACT
        if canonical(base) != prompt or execution["prompt_sha256"] != link["prompt_sha256"]:
            raise ControlError("invalid_snapshot", "Execution input differs from its revision.")
        prompt = execution["prompt"]
    if (
        len(raw) > MAX_BYTES
        or hashlib.sha256(raw).hexdigest() != link["prompt_sha256"]
        or raw.decode() != prompt
        or link["contract"]
        not in (
            CONTRACT,
            EXECUTION_CONTRACT,
            MESSAGE_CONTRACT,
            WORKFLOW_CONTRACT,
            WORKSPACE_CONTRACT,
        )
    ):
        raise ControlError("invalid_snapshot", "Task prompt differs from the reserved snapshot.")
    snapshot = read(prompt)
    if snapshot["protocol"] != link["contract"]:
        raise ControlError("invalid_snapshot", "Execution protocol differs from its binding.")
    if link["contract"] == WORKFLOW_CONTRACT:
        from .workflow import inspect_binding

        inspect_binding(db, snapshot, row["id"])
    if link["contract"] == WORKSPACE_CONTRACT:
        from .workspace import inspect_binding

        inspect_binding(store, db, snapshot, row["id"])
    if "messages" in snapshot:
        from .messages import inspect_binding

        inspect_binding(db, snapshot, row["id"])
    session = db.execute("SELECT * FROM sessions WHERE id=?", (row["session_id"],)).fetchone()
    task = db.execute("SELECT session_id FROM tasks WHERE id=?", (link["task_id"],)).fetchone()
    if (
        snapshot["task"]["id"] != link["task_id"]
        or snapshot["task"]["revision"] != link["revision"]
        or task["session_id"] != row["session_id"]
        or any(snapshot["assignment"][k] != session[k] for k in ("model", "role", "project"))
        or snapshot["assignment"]["timeout"] != row["timeout"]
        or (
            store.config["schema"] >= 9
            and (
                snapshot["assignment"].get("effort") != row["effort"]
                or row["effort"] != session["effort"]
            )
        )
        or snapshot["task"]["parent_run_id"] != link["expected_parent_run_id"]
        or snapshot["task"]["backend_id"] != link["expected_backend_id"]
        or (link["expected_backend_id"] and row["backend_id"] != link["expected_backend_id"])
    ):
        raise ControlError("invalid_snapshot", "Task binding differs from its execution.")
    output = dict(snapshot=snapshot, report=None, format_status="unavailable", agent_status=None)
    if row["status"] != "completed":
        return output
    with (store.run_dir(row["id"]) / "result.json").open("rb") as handle:
        raw = handle.read(32 * 1024 * 1024 + 1)
    if len(raw) > 32 * 1024 * 1024 or hashlib.sha256(raw).hexdigest() != row["result_sha256"]:
        raise ControlError("result_integrity", "Result bytes differ from the completed run digest.")
    result = strict_json(raw.decode())
    models = (
        json.loads(row["actual_models"])
        if isinstance(row["actual_models"], str)
        else row["actual_models"]
    )
    prefix = "claude-" + session["model"] + "-"
    if (
        not isinstance(result, dict)
        or result.get("validated_success") is not True
        or not models
        or any(not m.startswith(prefix) for m in models)
        or result.get("actual_models") != models
        or result.get("observed_session_ids") != [row["backend_id"]]
        or result.get("tool_use_count") != 0
    ):
        raise ControlError(
            "result_integrity", "Missing validated model/session execution evidence."
        )
    try:
        parsed = validate_report(result.get("response"), snapshot)
    except ControlError as exc:
        output.update(format_status="invalid", errors=[str(exc)])
    else:
        output.update(format_status="valid", agent_status=parsed["status"], report=parsed)
    return output
