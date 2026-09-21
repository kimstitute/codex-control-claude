"""Controller operation requests; the Claude CLI itself still has no tools."""

from .store import ControlError
from .workspace_policy import check_access, normalize_policy

PROTOCOL = "claude-control.task.v6"
OPERATIONS = {
    "type": "array; 0–8 operations",
    "read": {"op": "read", "path": "allowed relative file"},
    "write": {"op": "write", "path": "exact writable file", "content": "full UTF-8 text"},
    "run_check": {"op": "run_check", "name": "policy check name; no argv allowed"},
    "status": "Nonempty operations require status blocked; finish with operations [] and status complete.",
    "authority": "Requests execute only through the controller. Never claim an unreturned operation succeeded.",
}


def validate_operations(data, policy):
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
            "run_check": {"op", "name"},
        }
        if not isinstance(kind, str) or kind not in keys or set(action) != keys[kind]:
            raise ControlError("invalid_report", "Unknown operation or extra operation fields.")
        if kind in ("read", "write"):
            check_access(policy, action["path"], write=kind == "write")
            if kind == "write" and (
                not isinstance(action["content"], str)
                or len(action["content"].encode()) > 128 * 1024
            ):
                raise ControlError(
                    "invalid_report", "Write content must be UTF-8 text up to 128 KiB."
                )
        elif not isinstance(action["name"], str) or action["name"] not in policy["checks"]:
            raise ControlError("invalid_report", "Check is outside the fixed command policy.")
    return operations


def validate_envelope(data):
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
    if (
        not isinstance(data, dict)
        or set(data) != expected
        or normalize_policy(data["policy"]) != data["policy"]
    ):
        raise ControlError("invalid_snapshot", "Invalid workspace execution envelope.")
