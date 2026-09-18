#!/usr/bin/env python3
"""Small deterministic Claude CLI stand-in used by the test suite."""

from __future__ import annotations

import json
import os
import sys
import time
import uuid
from pathlib import Path


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
            "fake Claude CLI: --model --safe-mode --setting-sources "
            "--strict-mcp-config --session-id --resume --tools "
            "--permission-prompts --output-format --disable-slash-commands "
            "--mcp-config --permission-mode --verbose -p"
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
    if model not in {"sonnet", "fable"} or session_id is None:
        print("fake Claude requires an explicit model and session", file=sys.stderr)
        return 2
    try:
        session_id = str(uuid.UUID(session_id))
    except ValueError:
        print("invalid session UUID", file=sys.stderr)
        return 2

    prompt = _prompt()
    behavior = prompt.get("behavior", "success")
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
    text = _assistant_text(prompt, session_id)
    if behavior == "sleep":
        seconds = prompt.get("sleep_seconds", 30)
        time.sleep(float(seconds) if isinstance(seconds, (int, float)) else 30.0)
    if behavior == "malformed":
        print("{not-json", flush=True)
        return 0
    if behavior == "exit_fail":
        print("fixture process failure", file=sys.stderr)
        return 7

    assistant_model = f"claude-{model}-test"
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
    _print_json(
        {
            "type": "result",
            "subtype": "error" if is_error else "success",
            "session_id": response_session,
            "is_error": is_error,
            "result": "fixture error" if is_error else text,
            "usage": {"input_tokens": 4, "output_tokens": 2},
        }
    )
    return 1 if is_error else 0


if __name__ == "__main__":
    raise SystemExit(_run(sys.argv[1:]))
