"""Explicit task/revision contracts; v1 delegate snapshots retain their old semantics."""

import hashlib
import json
import uuid

from .assignments import (
    CONTRACT as LEGACY_CONTRACT,
)
from .assignments import MAX_BYTES, _strip_fence, read_snapshot, render_assignment, strict_json
from .assignments import validate_report as validate_legacy_report
from .model_settings import model_matches
from .store import ControlError
from .workflow_contracts import PROTOCOL as WORKFLOW_CONTRACT
from .workflow_contracts import validate_review
from .workspace_contracts import LEGACY_PROTOCOL as LEGACY_WORKSPACE_CONTRACT
from .workspace_contracts import PROTOCOL as WORKSPACE_CONTRACT
from .workspace_contracts import operation_contract, validate_envelope, validate_operations
from .workspace_contracts import review_contract as workspace_review_contract
from .workspace_contracts import validate_review as validate_workspace_review

CONTRACT = "claude-control.task.v2"
EXECUTION_CONTRACT = "claude-control.task.v3"
MESSAGE_CONTRACT = "claude-control.task.v4"
WORKSPACE_CONTRACTS = (LEGACY_WORKSPACE_CONTRACT, WORKSPACE_CONTRACT)


def _nonempty_string():
    return {"type": "string", "minLength": 1}


def report_schema(snapshot):
    """Return the exact Claude CLI structured-output schema for a frozen task prompt."""
    properties = {
        "task_id": {"type": "string", "const": snapshot["task"]["id"]},
        "revision": {"type": "integer", "const": snapshot["task"]["revision"]},
        "role": {"type": "string", "const": snapshot["assignment"]["role"]},
        "status": {"type": "string", "enum": ["complete", "blocked"]},
        "summary": _nonempty_string(),
        "deliverable": _nonempty_string(),
        "evidence": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["claim", "basis", "reference"],
                "properties": {
                    "claim": _nonempty_string(),
                    "basis": {
                        "type": "string",
                        "enum": ["supplied_context", "reasoning"],
                    },
                    "reference": _nonempty_string(),
                },
            },
        },
        "limitations": {"type": "array", "items": _nonempty_string()},
        "handoff": {"type": "string"},
    }
    required = list(properties)
    if snapshot["protocol"] == WORKFLOW_CONTRACT and snapshot["workflow"]["phase"] == "review":
        source = snapshot["workflow"]["source"]
        criterion_ids = sorted(snapshot["workflow"]["criteria"])
        properties["review"] = {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "target",
                "recommendation",
                "criteria",
                "unverified",
                "revision_instructions",
            ],
            "properties": {
                "target": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["task_id", "revision", "run_id", "result_sha256"],
                    "properties": {
                        "task_id": {"type": "string", "const": source["task_id"]},
                        "revision": {"type": "integer", "const": source["revision"]},
                        "run_id": {"type": "string", "const": source["run_id"]},
                        "result_sha256": {
                            "type": "string",
                            "const": source["result_sha256"],
                        },
                    },
                },
                "recommendation": {
                    "type": "string",
                    "enum": ["approve", "revise", "blocked"],
                },
                "criteria": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": criterion_ids,
                    "properties": {
                        key: {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["verdict", "evidence"],
                            "properties": {
                                "verdict": {
                                    "type": "string",
                                    "enum": ["pass", "fail", "unknown"],
                                },
                                "evidence": _nonempty_string(),
                            },
                        }
                        for key in criterion_ids
                    },
                },
                "unverified": {"type": "array", "items": _nonempty_string()},
                "revision_instructions": {"type": "string"},
            },
        }
        required.append("review")
    if snapshot["protocol"] in WORKSPACE_CONTRACTS:
        policy = snapshot["workspace"]["policy"]
        variants = []
        if policy["read_paths"]:
            variants.append(
                {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["op", "path"],
                    "properties": {
                        "op": {"type": "string", "const": "read"},
                        "path": {"type": "string", "enum": policy["read_paths"]},
                    },
                }
            )
        if policy["write_paths"]:
            variants.append(
                {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["op", "path", "content"],
                    "properties": {
                        "op": {"type": "string", "const": "write"},
                        "path": {"type": "string", "enum": policy["write_paths"]},
                        "content": {"type": "string", "maxLength": 131072},
                    },
                }
            )
            if snapshot["protocol"] == WORKSPACE_CONTRACT:
                hunk = {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "old_start",
                        "old_count",
                        "new_start",
                        "new_count",
                        "lines",
                    ],
                    "properties": {
                        "old_start": {"type": "integer", "minimum": 1},
                        "old_count": {"type": "integer", "minimum": 0},
                        "new_start": {"type": "integer", "minimum": 1},
                        "new_count": {"type": "integer", "minimum": 0},
                        "lines": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": 4096,
                            "items": {"type": "string", "minLength": 1},
                        },
                    },
                }
                variants.append(
                    {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["op", "path", "base_sha256", "hunks"],
                        "properties": {
                            "op": {"type": "string", "const": "patch"},
                            "path": {"type": "string", "enum": policy["write_paths"]},
                            "base_sha256": {
                                "type": "string",
                                "pattern": "^[0-9a-f]{64}$",
                            },
                            "hunks": {
                                "type": "array",
                                "minItems": 1,
                                "maxItems": 64,
                                "items": hunk,
                            },
                        },
                    }
                )
        if policy["checks"]:
            variants.append(
                {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["op", "name"],
                    "properties": {
                        "op": {"type": "string", "const": "run_check"},
                        "name": {"type": "string", "enum": sorted(policy["checks"])},
                    },
                }
            )
        properties["operations"] = {
            "type": "array",
            "maxItems": 8,
            **({"items": {"oneOf": variants}} if variants else {"maxItems": 0}),
        }
        required.append("operations")
        review = snapshot["workspace"].get("review")
        if review is not None:
            criterion_ids = sorted(review["criteria"])
            target = review["target"]
            properties["review"] = {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "target",
                    "recommendation",
                    "criteria",
                    "unverified",
                    "revision_instructions",
                ],
                "properties": {
                    "target": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": list(target),
                        "properties": {
                            key: {"type": "string", "const": value}
                            for key, value in target.items()
                        },
                    },
                    "recommendation": {
                        "type": "string",
                        "enum": ["approve", "revise", "blocked"],
                    },
                    "criteria": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": criterion_ids,
                        "properties": {
                            key: {
                                "type": "object",
                                "additionalProperties": False,
                                "required": ["verdict", "evidence"],
                                "properties": {
                                    "verdict": {
                                        "type": "string",
                                        "enum": ["pass", "fail", "unknown"],
                                    },
                                    "evidence": _nonempty_string(),
                                },
                            }
                            for key in criterion_ids
                        },
                    },
                    "unverified": {
                        "type": "array",
                        "items": _nonempty_string(),
                        "description": (
                            "Acceptance-criterion evidence gaps only; empty for approve."
                        ),
                    },
                    "revision_instructions": {"type": "string"},
                },
            }
            required.append("review")
    return {
        "type": "object",
        "additionalProperties": False,
        "required": required,
        "properties": properties,
    }


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
        LEGACY_WORKSPACE_CONTRACT,
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
    if data["protocol"] in WORKSPACE_CONTRACTS:
        validate_envelope(data.get("workspace"), protocol=data["protocol"])
        if data.get("report_contract", {}).get("operations") != operation_contract(
            data["protocol"]
        ):
            raise ControlError("invalid_snapshot", "Workspace operation contract changed.")
        review = data["workspace"].get("review")
        if review is not None and data.get("report_contract", {}).get(
            "review"
        ) != workspace_review_contract(review):
            raise ControlError("invalid_snapshot", "Workspace review contract changed.")
        if review is None and "review" in data.get("report_contract", {}):
            raise ControlError("invalid_snapshot", "Unexpected workspace review contract.")
    elif "workspace" in data:
        raise ControlError("invalid_snapshot", "Workspace input requires a workspace contract.")
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
    workspace_review = (
        snapshot["protocol"] in WORKSPACE_CONTRACTS
        and snapshot["workspace"].get("review") is not None
    )
    if review_phase:
        validate_review(data.get("review"), snapshot["workflow"])
    elif workspace_review:
        validate_workspace_review(data.get("review"), snapshot["workspace"]["review"])
    elif "review" in data:
        raise ControlError(
            "invalid_report", "Review extension is only valid for workflow review turns."
        )
    if snapshot["protocol"] in WORKSPACE_CONTRACTS:
        validate_operations(
            data, snapshot["workspace"]["policy"], protocol=snapshot["protocol"]
        )
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
        LEGACY_WORKSPACE_CONTRACT,
        WORKSPACE_CONTRACT,
    ):
        execution = db.execute(
            "SELECT * FROM execution_inputs WHERE run_id=?", (row["id"],)
        ).fetchone()
        if not execution or execution["base_sha256"] != digest(prompt):
            raise ControlError("invalid_snapshot", "Execution base differs from its revision.")
        snapshot = read(execution["prompt"])
        revision_snapshot = read(prompt)
        base = {
            k: v
            for k, v in snapshot.items()
            if k not in ("dependencies", "messages", "workflow", "workspace")
        }
        if snapshot["protocol"] == WORKFLOW_CONTRACT and snapshot["workflow"]["phase"] == "review":
            base["report_contract"] = {
                k: v for k, v in base["report_contract"].items() if k != "review"
            }
        if snapshot["protocol"] in WORKSPACE_CONTRACTS:
            base["report_contract"] = {
                k: v
                for k, v in base["report_contract"].items()
                if k not in ("operations", "review")
            }
            # Workspace materialization replaces the generic text-only wording
            # with the fixed controller-operation contract.  Restore the frozen
            # revision fields before proving the immutable base is unchanged.
            base["instructions"] = revision_snapshot["instructions"]
            base["role"] = revision_snapshot["role"]
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
            LEGACY_WORKSPACE_CONTRACT,
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
    if link["contract"] in WORKSPACE_CONTRACTS:
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
    if (
        not isinstance(result, dict)
        or result.get("validated_success") is not True
        or not models
        or any(not model_matches(session["model"], model) for model in models)
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
