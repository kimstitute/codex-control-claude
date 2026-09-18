"""Build Claude CLI argv and parse bounded stream-json session output.

This module has no side effects: it does not execute subprocesses, touch
the filesystem beyond reading the given stream file, or perform network I/O.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

MAX_STREAM_BYTES = 16 * 1024 * 1024
_ALIAS_TO_PREFIX = {"sonnet": "claude-sonnet-", "fable": "claude-fable-"}


def build_argv(binary: str, model: str, session_id: str, resume: bool = False) -> list[str]:
    """Build argv for a locked-down, non-interactive Claude CLI session invocation."""
    if model not in ("sonnet", "fable"):
        raise ValueError(f"unsupported model alias: {model!r}")
    try:
        uuid.UUID(session_id)
    except ValueError as exc:
        raise ValueError(f"invalid session_id: {session_id!r}") from exc

    session_flag = ["--resume", session_id] if resume else ["--session-id", session_id]

    return [
        binary,
        "--model",
        model,
        "--safe-mode",
        "--strict-mcp-config",
        "--mcp-config",
        '{"mcpServers":{}}',
        "--disable-slash-commands",
        "--tools",
        "",
        "--permission-mode",
        "dontAsk",
        "--permission-prompts",
        "none",
        *session_flag,
        "--output-format",
        "stream-json",
        "--verbose",
        "-p",
    ]


def _collect_text(content: list, errors: list[str], lineno: int) -> tuple[str, int]:
    """Extract text and count tool_use blocks from an assistant message content list."""
    text_parts: list[str] = []
    tool_uses = 0
    for block in content:
        if not isinstance(block, dict):
            errors.append(f"line {lineno}: non-object content block")
            continue
        btype = block.get("type")
        if btype == "text" and isinstance(block.get("text"), str):
            text_parts.append(block["text"])
        elif btype == "tool_use":
            tool_uses += 1
    return "".join(text_parts), tool_uses


def parse_stream(path: str | Path, expected_model: str, expected_session: str) -> dict:
    """Parse a bounded stream-json transcript produced by one finished CLI run."""
    with Path(path).open("rb") as handle:
        data = handle.read(MAX_STREAM_BYTES + 1)
    if len(data) > MAX_STREAM_BYTES:
        raise ValueError(f"stream file exceeds {MAX_STREAM_BYTES} bytes")

    expected_prefix = _ALIAS_TO_PREFIX.get(expected_model)
    if expected_prefix is None:
        raise ValueError(f"unsupported expected_model: {expected_model!r}")

    errors: list[str] = []
    actual_models: set[str] = set()
    bad_models: set[str] = set()
    observed_sessions: set[str] = set()
    bad_sessions: set[str] = set()
    assistant_texts: list[str] = []
    tool_use_count = 0
    result_events = 0
    has_result = False
    result_is_error = False
    result_text = None
    usage: dict = {}

    for lineno, raw in enumerate(data.decode("utf-8", errors="replace").splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            errors.append(f"line {lineno}: invalid JSON")
            continue
        if not isinstance(event, dict):
            errors.append(f"line {lineno}: non-object event")
            continue

        sid = event.get("session_id")
        if isinstance(sid, str) and sid:
            observed_sessions.add(sid)
            if sid != expected_session:
                bad_sessions.add(sid)
        elif sid is not None:
            errors.append(f"line {lineno}: invalid session_id")

        etype = event.get("type")
        if etype in ("assistant", "result") and sid != expected_session:
            errors.append(f"line {lineno}: assistant/result requires the exact session_id")
        if etype == "assistant":
            if has_result:
                errors.append(f"line {lineno}: assistant appeared after terminal result")
            message = event.get("message")
            if not isinstance(message, dict):
                errors.append(f"line {lineno}: assistant event missing message object")
                continue
            model = message.get("model")
            if isinstance(model, str) and model:
                if model.startswith("claude-"):
                    actual_models.add(model)
                if not model.startswith(expected_prefix):
                    bad_models.add(model)
            else:
                errors.append(f"line {lineno}: missing assistant model")
            content = message.get("content")
            if not isinstance(content, list):
                errors.append(f"line {lineno}: assistant message content is not a list")
                continue
            text, tool_uses = _collect_text(content, errors, lineno)
            if text:
                assistant_texts.append(text)
            tool_use_count += tool_uses
        elif etype == "result":
            result_events += 1
            has_result = True
            is_error = event.get("is_error")
            if not isinstance(is_error, bool):
                errors.append(f"line {lineno}: result is_error is not boolean")
            elif is_error:
                result_is_error = True
                errors.append(f"line {lineno}: terminal result reported an error")
            subtype = event.get("subtype")
            if subtype != "success":
                result_is_error = True
                errors.append(f"line {lineno}: result subtype {subtype!r} is not success")
            result_field = event.get("result")
            if isinstance(result_field, str):
                result_text = result_field
            else:
                errors.append(f"line {lineno}: terminal result must contain a string result")
            result_usage = event.get("usage")
            if isinstance(result_usage, dict):
                usage = result_usage
        # other event types (system/init/progress/etc.) are ignored

    if result_events > 1:
        errors.append(f"multiple result events: {result_events}")
    if not has_result:
        errors.append("missing terminal result event")
    if not actual_models:
        errors.append("missing actual assistant model")
    for model in sorted(bad_models):
        errors.append(
            f"model mismatch: {model!r} does not match expected prefix {expected_prefix!r}"
        )
    if tool_use_count:
        errors.append(f"tool_use blocks present: {tool_use_count}")
    for sid in sorted(bad_sessions):
        errors.append(f"session_id mismatch: {sid!r} != {expected_session!r}")
    if expected_session not in observed_sessions:
        errors.append(f"expected session_id {expected_session!r} not observed")

    response = result_text if result_text is not None else "".join(assistant_texts)

    return {
        "response": response,
        "actual_models": sorted(actual_models),
        "observed_session_ids": sorted(observed_sessions),
        "tool_use_count": tool_use_count,
        "has_result": has_result,
        "result_is_error": result_is_error,
        "errors": errors,
        "usage": usage,
        "validated_success": not errors,
    }
