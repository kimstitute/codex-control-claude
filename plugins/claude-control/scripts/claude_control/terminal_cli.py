"""Explicit local terminal sessions backed by Claude Code background agents.

The ordinary Claude Control runner remains a non-interactive, tools-disabled
``-p`` process.  This module exposes a separate opt-in surface for sessions
created by Claude Code's native ``--bg`` runtime, which already owns the
platform PTY/ConPTY and implements ``attach``/``logs``/``stop``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path

from .execution_settings import EFFORTS, validate_effort
from .model_settings import validate_model
from .platform import project_operator_lease, terminal_operator_lease
from .platform.host import private_dir, verify_private_entry, write_json
from .platform.locks import file_lock
from .runner import child_environment
from .store import ControlError, Store

PROTOCOL = "claude-control.terminals.v1"
MAX_AGENTS = 4096
MAX_LOG_BYTES = 256 * 1024
CATALOG_TTL = 0.75
_SHORT_ID = re.compile(r"^[a-f0-9]{8}$")
_BACKGROUND_ID = re.compile(r"\b([a-f0-9]{8})\b")
_ANSI_OSC = re.compile(r"\x1b\].*?(?:\x07|\x1b\\)", re.DOTALL)
_ANSI_CSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_ANSI_ESC = re.compile(r"\x1b[@-_0-?]*")
_CATALOG_CACHE: dict[str, tuple[float, list[dict]]] = {}
_LOG_CACHE: dict[str, tuple[float, str]] = {}


def register(commands):
    parser = commands.add_parser(
        "terminal", help="Manage opt-in attachable Claude background terminals."
    )
    sub = parser.add_subparsers(dest="terminal_command", required=True)
    start = sub.add_parser("start", help="Create a safe-profile attachable terminal agent.")
    start.add_argument("--name", required=True)
    start.add_argument("--role", required=True)
    start.add_argument("--project", required=True)
    start.add_argument("--model")
    start.add_argument("--effort", choices=EFFORTS)
    start.add_argument("--request-id", required=True)
    sub.add_parser("list", help="List local Claude terminal sessions without transcript text.")
    for name in ("status", "logs", "attach", "stop"):
        command = sub.add_parser(name)
        selector = command.add_mutually_exclusive_group(required=True)
        selector.add_argument("--id", dest="terminal_id")
        selector.add_argument("--session", dest="session_id")
        if name == "logs":
            command.add_argument("--bytes", type=int, default=65536)
    shell = sub.add_parser(
        "shell", help="Open a separate user-operated shell in an authorized project."
    )
    shell.add_argument("--project", required=True)


def _root(store: Store) -> Path:
    return private_dir(store.path / "terminals")


def _request_path(store: Store, request_id: str) -> Path:
    digest = hashlib.sha256(request_id.encode("utf-8")).hexdigest()
    return _root(store) / f"request-{digest}.json"


def _descriptor_path(store: Store, session_id: str) -> Path:
    return _root(store) / f"session-{session_id}.json"


def _load(path: Path) -> dict | None:
    if not path.exists():
        return None
    verify_private_entry(path)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ControlError("terminal_state_invalid", str(exc)) from None
    if not isinstance(value, dict):
        raise ControlError("terminal_state_invalid", "Terminal state must be a JSON object.")
    return value


def _run(store: Store, arguments: list[str], *, cwd=None, timeout=30, inherit=False):
    options = {
        "cwd": cwd or store.path,
        "env": child_environment(),
        "timeout": timeout,
        "check": False,
    }
    if not inherit:
        options.update(capture_output=True)
    return subprocess.run([store.config["claude_bin"], *arguments], **options)


def _normalize_agent(item) -> dict | None:
    if not isinstance(item, dict):
        return None
    terminal_id = item.get("id")
    session_id = item.get("sessionId")
    if terminal_id is not None and (
        not isinstance(terminal_id, str) or not _SHORT_ID.fullmatch(terminal_id)
    ):
        terminal_id = None
    if not isinstance(session_id, str):
        return None
    try:
        session_id = str(uuid.UUID(session_id))
    except ValueError:
        return None
    cwd = item.get("cwd") if isinstance(item.get("cwd"), str) else None
    state = item.get("state") if isinstance(item.get("state"), str) else None
    status = item.get("status") if isinstance(item.get("status"), str) else None
    if status is None:
        if state in {"failed", "error", "stopped", "completed", "finished"}:
            status = state
        elif state in {"running", "active", "working"}:
            status = "running"
        elif state in {"blocked", "idle"}:
            status = "idle"
        else:
            status = "unknown"
    native_attachable = (
        terminal_id is not None
        and item.get("kind") == "background"
        and status not in {"failed", "error", "stopped", "completed", "finished"}
    )
    return {
        "id": terminal_id,
        "session_id": session_id,
        "name": item.get("name") if isinstance(item.get("name"), str) else None,
        "cwd": cwd,
        "kind": item.get("kind") if isinstance(item.get("kind"), str) else "unknown",
        "status": status,
        "state": state,
        "pid": item.get("pid") if isinstance(item.get("pid"), int) else None,
        "started_at": (
            item.get("startedAt") / 1000.0
            if isinstance(item.get("startedAt"), (int, float))
            and not isinstance(item.get("startedAt"), bool)
            else None
        ),
        "native_attachable": native_attachable,
    }


def agents(store: Store, *, refresh=False) -> list[dict]:
    """Return bounded, identity-only native Claude terminal metadata."""
    key = str(store.config["claude_bin"])
    cached = _CATALOG_CACHE.get(key)
    if not refresh and cached and time.monotonic() - cached[0] <= CATALOG_TTL:
        return [dict(item) for item in cached[1]]
    completed = _run(store, ["agents", "--json", "--all"], timeout=15)
    if completed.returncode != 0:
        message = completed.stderr.decode("utf-8", errors="replace")[:1000].strip()
        raise ControlError("terminal_catalog_failed", message or "Claude agents query failed.")
    try:
        value = json.loads(completed.stdout.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ControlError("terminal_catalog_failed", str(exc)) from None
    if not isinstance(value, list) or len(value) > MAX_AGENTS:
        raise ControlError("terminal_catalog_failed", "Claude agents returned an invalid list.")
    result = [normalized for item in value if (normalized := _normalize_agent(item))]
    _CATALOG_CACHE[key] = (time.monotonic(), result)
    return [dict(item) for item in result]


def _descriptors(store: Store) -> dict[str, dict]:
    records = {}
    paths = sorted(_root(store).glob("session-*.json"))
    if len(paths) > MAX_AGENTS:
        raise ControlError(
            "terminal_registry_full",
            f"Terminal descriptor registry exceeds its {MAX_AGENTS}-entry bound.",
        )
    for path in paths:
        value = _load(path)
        if value and isinstance(value.get("session_id"), str):
            records[value["session_id"]] = value
    return records


def catalog(store: Store, *, refresh=False) -> dict:
    descriptors = _descriptors(store)
    items = []
    for item in agents(store, refresh=refresh):
        descriptor = descriptors.get(item["session_id"], {})
        managed = bool(descriptor)
        safe_profile = descriptor.get("safe_profile") is True
        items.append(
            {
                **item,
                "name": descriptor.get("name") or item.get("name"),
                "attachable": bool(item.get("native_attachable") and managed and safe_profile),
                "loggable": bool(item.get("id") and managed and safe_profile),
                "managed": managed,
                "role": descriptor.get("role"),
                "requested_model": descriptor.get("model"),
                "requested_effort": descriptor.get("effort"),
                "safe_profile": safe_profile,
            }
        )
    return {"protocol": PROTOCOL, "version": 1, "terminals": items}


def resolve(store: Store, *, terminal_id=None, session_id=None, refresh=False) -> dict:
    if terminal_id is not None and not _SHORT_ID.fullmatch(terminal_id):
        raise ControlError("invalid_terminal", "Expected the exact 8-character terminal ID.")
    if session_id is not None:
        try:
            session_id = str(uuid.UUID(session_id))
        except ValueError:
            raise ControlError("invalid_session", "Expected a full terminal session UUID.") from None
    matches = [
        item
        for item in catalog(store, refresh=refresh)["terminals"]
        if (terminal_id is None or item["id"] == terminal_id)
        and (session_id is None or item["session_id"] == session_id)
    ]
    if not matches:
        raise ControlError("terminal_not_found", "No matching local Claude terminal exists.")
    if len(matches) != 1:
        raise ControlError("terminal_ambiguous", "Terminal selector matched multiple sessions.")
    return matches[0]


def _sanitize_output(data: bytes, limit: int) -> str:
    # Claude's native log stream can contain several complete screen repaints.
    # Keep the most recent frame rather than rendering duplicated historical
    # screens after ANSI removal. Streams without a clear-screen marker retain
    # their ordinary bounded tail behavior.
    if b"\x1b[2J" in data:
        data = data.rsplit(b"\x1b[2J", 1)[-1]
    text = data[-limit:].decode("utf-8", errors="replace")
    text = _ANSI_OSC.sub("", text)
    text = _ANSI_CSI.sub("", text)
    text = _ANSI_ESC.sub("", text)
    text = text.replace("\r", "\n")
    text = "".join(character for character in text if character in "\n\t" or ord(character) >= 32)
    lines = []
    for line in text.splitlines():
        line = line.rstrip()
        if not line and (not lines or not lines[-1]):
            continue
        if line and (not lines or line != lines[-1]):
            lines.append(line)
    return "\n".join(lines[-240:])[-limit:]


def recent_output(store: Store, item: dict, *, limit=16384) -> str:
    if not item.get("loggable"):
        return ""
    cached = _LOG_CACHE.get(item["id"])
    if cached and time.monotonic() - cached[0] <= CATALOG_TTL:
        return cached[1][-limit:]
    completed = _run(store, ["logs", item["id"]], cwd=item.get("cwd"), timeout=15)
    output = completed.stdout + (b"\n" + completed.stderr if completed.stderr else b"")
    if completed.returncode != 0:
        raise ControlError(
            "terminal_logs_failed", _sanitize_output(output, 1000) or "Claude logs failed."
        )
    text = _sanitize_output(output, MAX_LOG_BYTES)
    _LOG_CACHE[item["id"]] = (time.monotonic(), text)
    return text[-limit:]


def snapshot(store: Store, session_id: str, *, include_output=True) -> dict | None:
    try:
        item = resolve(store, session_id=session_id)
    except ControlError as exc:
        if exc.code == "terminal_not_found":
            return None
        raise
    return {
        **item,
        "recent_output": recent_output(store, item) if include_output else "",
        "observed_at": time.time(),
    }


def _validate_start(store: Store, args):
    if not args.name.strip() or len(args.name) > 120:
        raise ControlError("invalid_terminal", "Terminal name must be 1–120 characters.")
    if not args.request_id or len(args.request_id) > 200:
        raise ControlError("invalid_request", "Request ID must be 1–200 characters.")
    defaults = store.role_defaults()
    if args.role not in defaults:
        raise ControlError("invalid_role", "Role must match a configured Claude Control role.")
    model = args.model or defaults[args.role]["model"]
    effort = args.effort or defaults[args.role].get("effort")
    try:
        model = validate_model(model)
        effort = validate_effort(effort)
    except ValueError as exc:
        raise ControlError("invalid_terminal", str(exc)) from None
    store.preflight_effort(effort)
    project = store.project(args.project)
    return project, model, effort


def _managed_item(item: dict, descriptor: dict) -> dict:
    return {
        **item,
        **descriptor,
        "attachable": bool(item.get("native_attachable")),
        "loggable": bool(item.get("id")),
        "managed": True,
        "safe_profile": True,
        "role": descriptor["role"],
        "requested_model": descriptor["model"],
        "requested_effort": descriptor["effort"],
    }


def _native_name(label: str, launch_nonce: str) -> str:
    return f"{label[:96]} · ccc-{launch_nonce[:12]}"


def start(store: Store, args) -> dict:
    project, model, effort = _validate_start(store, args)
    intent = {
        "request_id": args.request_id,
        "name": args.name,
        "role": args.role,
        "project": project,
        "model": model,
        "effort": effort,
    }
    fingerprint = hashlib.sha256(
        json.dumps(intent, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    request_path = _request_path(store, args.request_id)
    lock_path = _root(store) / "registry.lock"
    with file_lock(lock_path, exclusive=True):
        prior = _load(request_path)
        if prior:
            if prior.get("fingerprint") != fingerprint:
                raise ControlError("request_conflict", "Request ID belongs to different input.")
            item = None
            session_id = prior.get("session_id")
            if isinstance(session_id, str):
                try:
                    item = resolve(store, session_id=session_id, refresh=True)
                except ControlError as exc:
                    if exc.code != "terminal_not_found":
                        raise
            if item is None and isinstance(prior.get("terminal_id"), str):
                try:
                    item = resolve(store, terminal_id=prior["terminal_id"], refresh=True)
                except ControlError as exc:
                    if exc.code != "terminal_not_found":
                        raise
            if item is None:
                native_name = prior.get("native_name")
                candidates = [
                    candidate
                    for candidate in agents(store, refresh=True)
                    if candidate.get("native_attachable")
                    and isinstance(native_name, str)
                    and candidate.get("name") == native_name
                    and candidate.get("cwd") == prior.get("project")
                    and (candidate.get("started_at") or 0) >= float(prior.get("created") or 0) - 5
                ]
                if len(candidates) == 1:
                    item = candidates[0]
            if item is None:
                raise ControlError(
                    "terminal_unknown",
                    "The recorded terminal launch is no longer observable; inspect before replacing it.",
                )
            descriptor = {
                **intent,
                "session_id": item["session_id"],
                "terminal_id": item["id"],
                "safe_profile": True,
                "created": prior.get("created"),
            }
            write_json(_descriptor_path(store, item["session_id"]), descriptor)
            prior.update(
                status="started", session_id=item["session_id"], terminal_id=item["id"]
            )
            write_json(request_path, prior)
            return {
                "protocol": PROTOCOL,
                "created": False,
                "terminal": _managed_item(item, descriptor),
            }
        if len(list(_root(store).glob("request-*.json"))) >= MAX_AGENTS:
            raise ControlError(
                "terminal_registry_full",
                f"Terminal request registry reached its {MAX_AGENTS}-entry bound.",
            )
        launch_nonce = str(uuid.uuid4())
        native_name = _native_name(args.name, launch_nonce)
        record = {
            **intent,
            "fingerprint": fingerprint,
            "launch_nonce": launch_nonce,
            "native_name": native_name,
            "status": "starting",
            "created": time.time(),
        }
        write_json(request_path, record)
        before = {item["session_id"] for item in agents(store, refresh=True)}
        arguments = [
            "--bg",
            "--safe-mode",
            "--setting-sources",
            "",
            "--strict-mcp-config",
            "--mcp-config",
            '{"mcpServers":{}}',
            "--disable-slash-commands",
            "--tools",
            "",
            "--permission-mode",
            "dontAsk",
            "--model",
            model,
            "--name",
            native_name,
        ]
        if effort is not None:
            arguments.extend(("--effort", effort))
        completed = _run(store, arguments, cwd=project, timeout=30)
        if completed.returncode != 0:
            record.update(
                status="launch_failed",
                error=_sanitize_output(completed.stderr, 1000) or "Claude background launch failed.",
            )
            write_json(request_path, record)
            raise ControlError("terminal_launch_failed", record["error"])
        output = completed.stdout.decode("utf-8", errors="replace")
        found = _BACKGROUND_ID.search(output)
        terminal_id = found.group(1) if found else None
        item = None
        for _ in range(20):
            try:
                if terminal_id:
                    item = resolve(store, terminal_id=terminal_id, refresh=True)
                else:
                    candidates = [
                        candidate
                        for candidate in agents(store, refresh=True)
                        if candidate["session_id"] not in before
                        and candidate.get("native_attachable")
                        and candidate.get("name") == native_name
                        and candidate.get("cwd") == project
                    ]
                    if len(candidates) == 1:
                        item = candidates[0]
                    elif len(candidates) > 1:
                        raise ControlError(
                            "terminal_ambiguous", "Multiple new Claude terminals matched the launch."
                        )
                    else:
                        raise ControlError("terminal_not_found", "Terminal is not visible yet.")
                break
            except ControlError as exc:
                if exc.code != "terminal_not_found":
                    raise
                time.sleep(0.1)
        if item is None:
            record.update(status="unknown")
            write_json(request_path, record)
            raise ControlError(
                "terminal_unknown",
                "Claude accepted the background launch but its terminal identity is not observable.",
            )
        descriptor = {
            **intent,
            "session_id": item["session_id"],
            "terminal_id": item["id"],
            "safe_profile": True,
            "created": record["created"],
        }
        write_json(_descriptor_path(store, item["session_id"]), descriptor)
        record.update(
            status="started", session_id=item["session_id"], terminal_id=item["id"]
        )
        write_json(request_path, record)
        return {
            "protocol": PROTOCOL,
            "created": True,
            "terminal": _managed_item(item, descriptor),
        }


def _attach(store: Store, item: dict) -> dict:
    if not item.get("attachable"):
        raise ControlError("terminal_unattachable", "This Claude session has no attach endpoint.")
    if not item.get("managed") or not item.get("safe_profile"):
        raise ControlError(
            "terminal_unmanaged", "Only safe-profile terminals created by this controller may attach."
        )
    if not os.isatty(0) or not os.isatty(1):
        raise ControlError("terminal_required", "Attach requires an interactive terminal.")
    lease = terminal_operator_lease(item["session_id"])
    try:
        with file_lock(lease, exclusive=True, blocking=False):
            completed = _run(
                store,
                ["attach", item["id"]],
                cwd=item.get("cwd"),
                timeout=None,
                inherit=True,
            )
    except BlockingIOError:
        raise ControlError(
            "terminal_busy", "Another local operator currently holds terminal control."
        ) from None
    if completed.returncode != 0:
        raise ControlError("terminal_attach_failed", "Claude terminal attach exited unsuccessfully.")
    return {"protocol": PROTOCOL, "detached": True, "terminal": item}


def _stop(store: Store, item: dict) -> dict:
    if not item.get("managed") or not item.get("safe_profile"):
        raise ControlError(
            "terminal_unmanaged", "Only safe-profile terminals created by this controller may stop."
        )
    if not item.get("id"):
        raise ControlError("terminal_unstoppable", "This session has no background terminal ID.")
    completed = _run(store, ["stop", item["id"]], cwd=item.get("cwd"), timeout=30)
    if completed.returncode != 0:
        output = completed.stdout + b"\n" + completed.stderr
        raise ControlError("terminal_stop_failed", _sanitize_output(output, 1000))
    return {
        "protocol": PROTOCOL,
        "stop_requested": True,
        "terminal": item,
        "message": _sanitize_output(completed.stdout, 2000),
    }


def _shell(store: Store, project: str) -> dict:
    project = store.project(project)
    if not os.isatty(0) or not os.isatty(1):
        raise ControlError("terminal_required", "Operator shell requires an interactive terminal.")
    if os.name == "nt":
        shell = os.environ.get("COMSPEC") or shutil.which("cmd.exe")
    else:
        shell = os.environ.get("SHELL") or shutil.which("bash") or shutil.which("sh")
    if not shell or not Path(shell).is_file():
        raise ControlError("shell_unavailable", "No interactive user shell is available.")
    lease = project_operator_lease(project)
    try:
        with file_lock(lease, exclusive=True, blocking=False):
            completed = subprocess.run([shell], cwd=project, env=os.environ.copy(), check=False)
    except BlockingIOError:
        raise ControlError("shell_busy", "An operator shell is already open for this project.") from None
    return {"protocol": PROTOCOL, "shell_exited": True, "exit_code": completed.returncode}


def execute(args):
    store = Store(args.state_dir)
    if args.terminal_command == "start":
        return start(store, args)
    if args.terminal_command == "list":
        return catalog(store, refresh=True)
    if args.terminal_command == "shell":
        _shell(store, args.project)
        return None
    item = resolve(
        store,
        terminal_id=getattr(args, "terminal_id", None),
        session_id=getattr(args, "session_id", None),
        refresh=True,
    )
    if args.terminal_command == "status":
        return {"protocol": PROTOCOL, "terminal": item}
    if args.terminal_command == "logs":
        if not 1 <= args.bytes <= MAX_LOG_BYTES:
            raise ControlError("invalid_limit", f"Terminal log limit must be 1–{MAX_LOG_BYTES}.")
        if not item.get("managed") or not item.get("safe_profile"):
            raise ControlError(
                "terminal_unmanaged",
                "Only safe-profile terminals created by this controller expose logs.",
            )
        return {
            "protocol": PROTOCOL,
            "terminal": item,
            "text": recent_output(store, item, limit=args.bytes),
        }
    if args.terminal_command == "attach":
        _attach(store, item)
        return None
    if args.terminal_command == "stop":
        return _stop(store, item)
    raise ControlError("invalid_command", "Unsupported terminal command.")
