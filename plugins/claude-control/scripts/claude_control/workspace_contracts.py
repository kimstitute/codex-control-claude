"""Controller operation requests; the Claude CLI itself still has no tools."""

import json
import re

from .store import ControlError
from .workspace_policy import check_access, normalize_policy

LEGACY_PROTOCOL = "claude-control.task.v6"
PROTOCOL = "claude-control.task.v7"
LEGACY_OPERATIONS = {
    "type": "array; 0–8 operations",
    "read": {"op": "read", "path": "allowed relative file"},
    "write": {"op": "write", "path": "exact writable file", "content": "full UTF-8 text"},
    "run_check": {"op": "run_check", "name": "policy check name; no argv allowed"},
    "status": "Nonempty operations require status blocked; finish with operations [] and status complete.",
    "authority": "Requests execute only through the controller. Never claim an unreturned operation succeeded.",
}
OPERATIONS = {
    **LEGACY_OPERATIONS,
    "patch": {
        "op": "patch",
        "path": "exact existing writable UTF-8 file",
        "base_sha256": "lowercase SHA-256 of the current file",
        "hunks": [
            {
                "old_start": "1-based source line",
                "old_count": "context plus removed line count",
                "new_start": "1-based result line",
                "new_count": "context plus added line count",
                "lines": "exact lines prefixed by space, +, or -",
            }
        ],
    },
}


def operation_contract(protocol):
    if protocol == LEGACY_PROTOCOL:
        return LEGACY_OPERATIONS
    if protocol == PROTOCOL:
        return OPERATIONS
    raise ControlError("invalid_snapshot", "Unknown workspace operation contract.")


def review_contract(envelope):
    return {
        "target": envelope["target"],
        "recommendation": "approve|revise|blocked",
        "criteria": {
            key: {"verdict": "pass|fail|unknown", "evidence": "non-empty string"}
            for key in envelope["criteria"]
        },
        "unverified": "list of non-empty strings",
        "revision_instructions": "string",
    }


def validate_review(review, envelope):
    if not isinstance(review, dict) or set(review) != {
        "target",
        "recommendation",
        "criteria",
        "unverified",
        "revision_instructions",
    }:
        raise ControlError("invalid_report", "Invalid workspace review fields.")
    if review["target"] != envelope["target"]:
        raise ControlError("invalid_report", "Workspace review target changed.")
    recommendation = review["recommendation"]
    if recommendation not in ("approve", "revise", "blocked"):
        raise ControlError("invalid_report", "Invalid workspace review recommendation.")
    if not isinstance(review["criteria"], dict) or set(review["criteria"]) != set(
        envelope["criteria"]
    ):
        raise ControlError("invalid_report", "Workspace review criteria changed.")
    verdicts = []
    for item in review["criteria"].values():
        if (
            not isinstance(item, dict)
            or set(item) != {"verdict", "evidence"}
            or item["verdict"] not in ("pass", "fail", "unknown")
            or not isinstance(item["evidence"], str)
            or not item["evidence"].strip()
        ):
            raise ControlError("invalid_report", "Invalid workspace review criterion verdict.")
        verdicts.append(item["verdict"])
    if (
        not isinstance(review["unverified"], list)
        or any(not isinstance(v, str) or not v.strip() for v in review["unverified"])
        or not isinstance(review["revision_instructions"], str)
    ):
        raise ControlError("invalid_report", "Invalid workspace review explanation.")
    if recommendation == "approve" and (
        any(verdict != "pass" for verdict in verdicts) or review["unverified"]
    ):
        raise ControlError("invalid_report", "Approval requires all criteria pass and no unknowns.")
    if recommendation == "revise" and (
        "fail" not in verdicts or not review["revision_instructions"].strip()
    ):
        raise ControlError("invalid_report", "Revision requires a failed criterion and instructions.")
    if recommendation == "blocked" and (
        "unknown" not in verdicts or not review["unverified"]
    ):
        raise ControlError("invalid_report", "Blocked review requires unknown evidence.")


def _validate_patch(action):
    if not isinstance(action["base_sha256"], str) or not re.fullmatch(
        r"[0-9a-f]{64}", action["base_sha256"]
    ):
        raise ControlError("invalid_report", "Patch base_sha256 must be lowercase SHA-256.")
    hunks = action["hunks"]
    if not isinstance(hunks, list) or not 1 <= len(hunks) <= 64:
        raise ControlError("invalid_report", "Patch requires 1–64 hunks.")
    encoded = json.dumps(hunks, ensure_ascii=True, separators=(",", ":")).encode("ascii")
    if len(encoded) > 128 * 1024:
        raise ControlError("invalid_report", "Patch hunks exceed the 128 KiB limit.")
    previous_end = 0
    for hunk in hunks:
        expected = {"old_start", "old_count", "new_start", "new_count", "lines"}
        if not isinstance(hunk, dict) or set(hunk) != expected:
            raise ControlError("invalid_report", "Invalid patch hunk fields.")
        for key in ("old_start", "old_count", "new_start", "new_count"):
            if type(hunk[key]) is not int or hunk[key] < (1 if key.endswith("start") else 0):
                raise ControlError("invalid_report", "Invalid patch hunk range.")
        lines = hunk["lines"]
        if not isinstance(lines, list) or not lines or len(lines) > 4096:
            raise ControlError("invalid_report", "Patch hunk requires 1–4096 lines.")
        if any(not isinstance(line, str) or not line or line[0] not in " +-" for line in lines):
            raise ControlError("invalid_report", "Patch lines require space, +, or - prefixes.")
        old_count = sum(line[0] in " -" for line in lines)
        new_count = sum(line[0] in " +" for line in lines)
        if old_count != hunk["old_count"] or new_count != hunk["new_count"]:
            raise ControlError("invalid_report", "Patch hunk counts do not match its lines.")
        if hunk["old_start"] - 1 < previous_end:
            raise ControlError("invalid_report", "Patch hunks overlap or are out of order.")
        previous_end = hunk["old_start"] - 1 + hunk["old_count"]


def validate_operations(data, policy, *, protocol=PROTOCOL):
    operations = data.get("operations")
    if not isinstance(operations, list) or len(operations) > 8:
        raise ControlError("invalid_report", "Workspace report requires 0–8 operations.")
    if operations and data.get("status") != "blocked":
        raise ControlError(
            "invalid_report", "Requested operations are intermediate blocked reports."
        )
    for action in operations:
        if not isinstance(action, dict):
            raise ControlError("invalid_report", "Operation must be an object.")
        kind = action.get("op")
        keys = {
            "read": {"op", "path"},
            "write": {"op", "path", "content"},
            **(
                {"patch": {"op", "path", "base_sha256", "hunks"}}
                if protocol == PROTOCOL
                else {}
            ),
            "run_check": {"op", "name"},
        }
        if not isinstance(kind, str) or kind not in keys or set(action) != keys[kind]:
            raise ControlError("invalid_report", "Unknown operation or extra operation fields.")
        if kind in ("read", "write", "patch"):
            check_access(policy, action["path"], write=kind in ("write", "patch"))
            if kind == "write" and (
                not isinstance(action["content"], str)
                or len(action["content"].encode()) > 128 * 1024
            ):
                raise ControlError(
                    "invalid_report", "Write content must be UTF-8 text up to 128 KiB."
                )
            if kind == "patch":
                _validate_patch(action)
        elif not isinstance(action["name"], str) or action["name"] not in policy["checks"]:
            raise ControlError("invalid_report", "Check is outside the fixed command policy.")
    return operations


def validate_envelope(data, *, protocol=PROTOCOL):
    expected = {
        "id",
        "base_commit",
        "baseline_sha256",
        "policy",
        "tree",
        "receipts",
        "calls_remaining",
        "actions_remaining",
    }
    if protocol == PROTOCOL:
        expected.add("review")
    if (
        not isinstance(data, dict)
        or set(data) != expected
        or normalize_policy(data["policy"]) != data["policy"]
    ):
        raise ControlError("invalid_snapshot", "Invalid workspace execution envelope.")
    review = data.get("review")
    if review is not None:
        if protocol != PROTOCOL or not isinstance(review, dict) or set(review) != {
            "target",
            "criteria",
        }:
            raise ControlError("invalid_snapshot", "Invalid workspace review envelope.")
        target = review["target"]
        if not isinstance(target, dict) or set(target) != {
            "workspace_id",
            "run_id",
            "result_sha256",
            "manifest_sha256",
            "tree_sha256",
        }:
            raise ControlError("invalid_snapshot", "Invalid workspace review target.")
        criteria = review["criteria"]
        if (
            not isinstance(criteria, dict)
            or not criteria
            or any(not isinstance(k, str) or not isinstance(v, str) or not v for k, v in criteria.items())
        ):
            raise ControlError("invalid_snapshot", "Invalid workspace review criteria.")
