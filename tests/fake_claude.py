#!/usr/bin/env python3
"""Small deterministic Claude CLI stand-in used by the test suite."""

from __future__ import annotations

import json
import os
import sys
import time
import uuid
from pathlib import Path

ASSIGNMENT_PROTOCOL = "claude-control.assignment.v1"
WORKFLOW_PROTOCOL = "claude-control.task.v5"
WORKSPACE_PROTOCOLS = {"claude-control.task.v6", "claude-control.task.v7"}


def _print_json(value: object) -> None:
    print(json.dumps(value, separators=(",", ":")), flush=True)


def _option(arguments: list[str], name: str) -> str | None:
    try:
        return arguments[arguments.index(name) + 1]
    except (ValueError, IndexError):
        return None


def _prompt() -> dict[str, object]:
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {"text": raw}
    return value if isinstance(value, dict) else {"text": raw}


def _state_path(session_id: str) -> Path:
    state_root = Path(os.environ.get("FAKE_CLAUDE_HOME", str(Path.cwd() / ".fake-claude")))
    state_root.mkdir(parents=True, exist_ok=True)
    return state_root / f"{session_id}.json"


def _load_state(session_id: str) -> dict[str, object]:
    path = _state_path(session_id)
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _save_state(session_id: str, value: dict[str, object]) -> None:
    path = _state_path(session_id)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value), encoding="utf-8")
    os.replace(temporary, path)


def _save_invocation(session_id: str, arguments: list[str], prompt: dict[str, object]) -> None:
    """Persist fixture-only input evidence without changing conversation state."""
    path = _state_path(f"{session_id}.invocation")
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps({"argv": arguments, "prompt": prompt}), encoding="utf-8")
    os.replace(temporary, path)


def _assignment_fixture(context: object) -> dict[str, object]:
    """Read an opt-in fixture control from assignment context for controller tests."""
    if not isinstance(context, str):
        return {}
    try:
        value, _ = json.JSONDecoder().raw_decode(context.lstrip())
    except json.JSONDecodeError:
        return {}
    if not isinstance(value, dict):
        return {}
    fixture = value.get("__fake_assignment__")
    return fixture if isinstance(fixture, dict) else {}


def _assignment_text(prompt: dict[str, object]) -> tuple[str, dict[str, object]] | None:
    if prompt.get("protocol") not in {
        ASSIGNMENT_PROTOCOL,
        "claude-control.task.v2",
        "claude-control.task.v3",
        "claude-control.task.v4",
        WORKFLOW_PROTOCOL,
        *WORKSPACE_PROTOCOLS,
    }:
        return None
    assignment = prompt.get("assignment")
    role = prompt.get("role")
    if not isinstance(assignment, dict) or not isinstance(role, dict):
        return None
    task_id = assignment.get("id")
    role_name = assignment.get("role")
    if not isinstance(task_id, str) or not isinstance(role_name, str):
        return None
    fixture = _assignment_fixture(assignment.get("context"))
    report: dict[str, object] = {
        "task_id": task_id,
        "role": role_name,
        "status": "complete",
        "summary": f"Fixture report for {role_name}.",
        "deliverable": f"Fixture deliverable for {task_id}.",
        "evidence": [],
        "limitations": ["Fixture supplies no external evidence."],
        "handoff": "",
    }
    if prompt.get("protocol") in {
        "claude-control.task.v2",
        "claude-control.task.v3",
        "claude-control.task.v4",
        WORKFLOW_PROTOCOL,
        *WORKSPACE_PROTOCOLS,
    }:
        report["task_id"] = prompt["task"]["id"]
        report["revision"] = prompt["task"]["revision"]
    if prompt.get("protocol") in WORKSPACE_PROTOCOLS:
        rounds = fixture.get("workspace_operations", [])
        revision = prompt["task"]["revision"]
        operations: object = []
        if isinstance(rounds, list) and rounds:
            index = min(max(int(revision) - 1, 0), len(rounds) - 1)
            operations = rounds[index]
        report["operations"] = operations
        if isinstance(operations, list) and operations:
            report["status"] = "blocked"
    workflow = prompt.get("workflow")
    if (
        prompt.get("protocol") == WORKFLOW_PROTOCOL
        and isinstance(workflow, dict)
        and workflow.get("phase") == "review"
    ):
        round_number = workflow.get("round", 1)
        recommendations = fixture.get("workflow_recommendations", ["approve"])
        recommendation = (
            recommendations[min(max(int(round_number), 0), len(recommendations) - 1)]
            if isinstance(recommendations, list) and recommendations
            else "approve"
        )
        criteria = workflow.get("criteria", {})
        review_criteria = {
            key: {"verdict": "pass", "evidence": f"Fixture checked criterion {key}."}
            for key in criteria
        }
        unverified: list[str] = []
        instructions = ""
        if recommendation == "revise":
            if review_criteria:
                first = next(iter(review_criteria))
                review_criteria[first] = {
                    "verdict": "fail",
                    "evidence": "Fixture requests a bounded revision.",
                }
            instructions = "Address the failed criterion using only supplied context."
        elif recommendation == "blocked":
            review_criteria = {
                key: {"verdict": "unknown", "evidence": "Fixture lacks verification evidence."}
                for key in criteria
            }
            unverified = ["Fixture cannot verify the supplied result."]
        source = workflow.get("source")
        target = {
            key: source.get(key) if isinstance(source, dict) else None
            for key in ("task_id", "revision", "run_id", "result_sha256")
        }
        review = {
            "target": target,
            "recommendation": recommendation,
            "criteria": review_criteria,
            "unverified": unverified,
            "revision_instructions": instructions,
        }
        reviews = fixture.get("workflow_reviews")
        if isinstance(reviews, list) and reviews:
            override = reviews[min(max(int(round_number), 0), len(reviews) - 1)]
            if isinstance(override, dict):
                review.update(override)
        report["review"] = review
    workspace_envelope = prompt.get("workspace")
    if (
        prompt.get("protocol") in WORKSPACE_PROTOCOLS
        and isinstance(workspace_envelope, dict)
        and isinstance(workspace_envelope.get("review"), dict)
    ):
        review_envelope = workspace_envelope["review"]
        recommendation = fixture.get("workspace_recommendation", "approve")
        criteria = {
            key: {"verdict": "pass", "evidence": f"Fixture checked criterion {key}."}
            for key in review_envelope["criteria"]
        }
        unverified = []
        instructions = ""
        if recommendation == "revise":
            first = next(iter(criteria))
            criteria[first] = {"verdict": "fail", "evidence": "Fixture found a defect."}
            instructions = "Correct the failed criterion."
        elif recommendation == "blocked":
            first = next(iter(criteria))
            criteria[first] = {"verdict": "unknown", "evidence": "Fixture lacks evidence."}
            unverified = ["Required evidence is unavailable."]
        report["review"] = {
            "target": review_envelope["target"],
            "recommendation": recommendation,
            "criteria": criteria,
            "unverified": unverified,
            "revision_instructions": instructions,
        }
    override = fixture.get("report")
    reports = fixture.get("reports")
    if isinstance(reports, list) and reports:
        revision = prompt.get("task", {}).get("revision", 1)
        candidate = reports[min(max(int(revision) - 1, 0), len(reports) - 1)]
        if isinstance(candidate, dict):
            override = candidate
    if isinstance(override, dict):
        report.update(override)
    response = fixture.get("response")
    if isinstance(response, str):
        return response, fixture
    return json.dumps(report, separators=(",", ":")), fixture


def _assistant_text(prompt: dict[str, object], session_id: str) -> str:
    state = _load_state(session_id)
    remembered = prompt.get("remember")
    if isinstance(remembered, str):
        state["remembered"] = remembered
        _save_state(session_id, state)
        return remembered
    if prompt.get("recall") is True:
        value = state.get("remembered")
        return value if isinstance(value, str) else ""
    text = prompt.get("text", "OK")
    return text if isinstance(text, str) else "OK"


def _run(arguments: list[str]) -> int:
    if arguments == ["--version"]:
        print("fake-claude 1.0")
        return 0
    if "--help" in arguments:
        print(
            "fake Claude CLI: --model --effort --safe-mode --setting-sources "
            "--strict-mcp-config --session-id --resume --tools "
            "--output-format --disable-slash-commands "
            "--mcp-config --permission-mode --json-schema --verbose -p"
        )
        return 0
    if arguments[:2] == ["auth", "status"]:
        _print_json(
            {
                "loggedIn": True,
                "authMethod": "fake",
                "apiProvider": "fixture",
                "subscriptionType": "test",
            }
        )
        return 0

    model = _option(arguments, "--model")
    session_id = _option(arguments, "--session-id") or _option(arguments, "--resume")
    aliases = {"sonnet", "fable", "opus", "haiku"}
    if (model not in aliases and not str(model).startswith("claude-")) or session_id is None:
        print("fake Claude requires an explicit model and session", file=sys.stderr)
        return 2
    try:
        session_id = str(uuid.UUID(session_id))
    except ValueError:
        print("invalid session UUID", file=sys.stderr)
        return 2

    prompt = _prompt()
    _save_invocation(session_id, arguments, prompt)
    assignment = _assignment_text(prompt)
    fixture = assignment[1] if assignment else {}
    behavior = fixture.get("behavior", prompt.get("behavior", "success"))
    response_session = session_id
    if behavior == "mismatch_session":
        response_session = str(uuid.UUID(int=(uuid.UUID(session_id).int + 1) % (1 << 128)))

    _print_json(
        {
            "type": "system",
            "subtype": "init",
            "session_id": response_session,
            "model": f"claude-{model}-init-fixture",
        }
    )

    # Persist before a deliberate sleep so cancellation followed by resume is testable.
    text = assignment[0] if assignment else _assistant_text(prompt, session_id)
    if behavior == "sleep":
        seconds = fixture.get("sleep_seconds", prompt.get("sleep_seconds", 30))
        time.sleep(float(seconds) if isinstance(seconds, (int, float)) else 30.0)
    if behavior == "malformed":
        print("{not-json", flush=True)
        return 0
    if behavior == "exit_fail":
        print("fixture process failure", file=sys.stderr)
        return 7

    assistant_model = model if model.startswith("claude-") else f"claude-{model}-test"
    if behavior == "mismatch_model":
        assistant_model = "claude-fable-test" if model == "sonnet" else "claude-sonnet-test"
    elif behavior == "synthetic":
        assistant_model = "<synthetic>"

    _print_json(
        {
            "type": "assistant",
            "session_id": response_session,
            "message": {
                "model": assistant_model,
                "content": [{"type": "text", "text": text}],
            },
        }
    )
    if behavior == "no_result":
        return 0

    is_error = behavior == "error"
    result = {
        "type": "result",
        "subtype": "error" if is_error else "success",
        "session_id": response_session,
        "is_error": is_error,
        "result": "fixture error" if is_error else text,
        "usage": {"input_tokens": 4, "output_tokens": 2},
        "modelUsage": {
            assistant_model: {
                "inputTokens": 4,
                "outputTokens": 2,
                "costUSD": 0.001,
            }
        },
        "total_cost_usd": 0.001,
        "duration_api_ms": 25,
    }
    if not is_error and _option(arguments, "--json-schema") is not None:
        try:
            result["structured_output"] = json.loads(text)
            result["result"] = ""
        except json.JSONDecodeError:
            pass
    _print_json(result)
    return 1 if is_error else 0


if __name__ == "__main__":
    raise SystemExit(_run(sys.argv[1:]))
