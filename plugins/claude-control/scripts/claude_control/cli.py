"""JSON command interface; no remote routing or implicit model selection."""

import argparse
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

from . import (
    __version__,
    composition_cli,
    message_cli,
    monitor_cli,
    task_cli,
    workflow_cli,
    workspace_cli,
)
from .assignments import CONTRACT, ROLE_PRESETS, load_assignment, render_assignment
from .execution_settings import EFFORTS
from .orchestration import observe, report
from .runner import child_environment, exec_claude, launch_worker, run_worker
from .store import ACTIVE, ControlError, Store


def parser():
    p = argparse.ArgumentParser(description="Manage host-local, tools-disabled Claude sessions.")
    p.add_argument(
        "--state-dir",
        type=Path,
        default=Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state")))
        / "claude-control",
    )
    p.add_argument("--version", action="version", version=__version__)
    commands = p.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init")
    init.add_argument("--claude-bin", required=True)
    init.add_argument("--allow-root", action="append", required=True)
    init.add_argument("--max-parallel", type=int, default=2)
    init.add_argument("--max-queued", type=int, default=100)
    doctor = commands.add_parser("doctor")
    doctor.add_argument(
        "--auth", action="store_true", help="Check login, excluding account identifiers."
    )
    commands.add_parser("list")
    commands.add_parser("roles")
    delegate = commands.add_parser("delegate")
    delegate.add_argument("--assignment-file", type=Path, required=True)
    delegate.add_argument("--request-id", required=True)
    observation = commands.add_parser("observe")
    observation.add_argument("--run", action="append", required=True)
    observation.add_argument("--seconds", type=float, default=30)
    for name in ("start", "followup", "resume", "restart"):
        cmd = commands.add_parser(name)
        if name == "start":
            cmd.add_argument("--name", required=True)
            cmd.add_argument("--model", choices=("sonnet", "fable"), required=True)
            cmd.add_argument("--role", required=True)
            cmd.add_argument("--project", required=True)
        else:
            cmd.add_argument("--session", required=True)
            cmd.add_argument("--acknowledge-context", action="store_true")
        cmd.add_argument("--prompt-file", type=Path, required=True)
        cmd.add_argument("--request-id", required=True)
        cmd.add_argument("--timeout", type=float, default=300)
        cmd.add_argument("--effort", choices=EFFORTS)
    for name in (
        "status",
        "result",
        "report",
        "logs",
        "stop",
        "reconcile",
        "wait",
        "_worker",
        "_exec",
    ):
        cmd = commands.add_parser(name)
        cmd.add_argument("--run", required=True)
        if name == "logs":
            cmd.add_argument("--stream", choices=("events", "stderr", "worker"), default="events")
            cmd.add_argument("--bytes", type=int, default=8192)
        if name == "wait":
            cmd.add_argument("--seconds", type=float, default=30)
    task_cli.register(commands)
    message_cli.register(commands)
    workflow_cli.register(commands)
    workspace_cli.register(commands)
    composition_cli.register(commands)
    monitor_cli.register(commands)
    command = commands.add_parser("_workspace_exec")
    command.add_argument("--run", required=True)
    command.add_argument("--seq", type=int, required=True)
    command.add_argument("--scratch", type=Path, required=True)
    return p


def doctor(store, check_auth):
    binary = store.config["claude_bin"]

    def call(args):
        return subprocess.run(
            [binary, *args],
            capture_output=True,
            text=True,
            timeout=15,
            cwd=store.path,
            env=child_environment(),
        )

    version = call(["--version"])
    help_result = call(["--help"])
    required = (
        "--safe-mode",
        "--setting-sources",
        "--strict-mcp-config",
        "--session-id",
        "--resume",
        "--tools",
        "--permission-prompts",
        "--output-format",
        "--disable-slash-commands",
        "--mcp-config",
        "--permission-mode",
        "--verbose",
    )
    missing = [flag for flag in required if flag not in help_result.stdout]
    result = dict(
        version=__version__,
        hostname=socket.gethostname(),
        installation_id=store.config["installation_id"],
        state_dir=str(store.path),
        claude_version=version.stdout.strip(),
        missing_options=missing,
        effort_supported=help_result.returncode == 0 and "--effort" in help_result.stdout.split(),
        ready=version.returncode == 0 and help_result.returncode == 0 and not missing,
        max_parallel=store.config["max_parallel"],
        max_queued=store.config.get("max_queued"),
        profile="text-only",
        systemd_run_available=bool(shutil.which("systemd-run")),
        hostname_changed=socket.gethostname() != store.config["hostname"],
    )
    if check_auth:
        auth = call(["auth", "status"])
        try:
            data = json.loads(auth.stdout)
        except json.JSONDecodeError:
            data = {}
        result["auth"] = {
            k: data.get(k) for k in ("loggedIn", "authMethod", "apiProvider", "subscriptionType")
        }
        result["ready"] = result["ready"] and auth.returncode == 0 and data.get("loggedIn") is True
    return result


def execute(args):
    if args.command == "monitor":
        return monitor_cli.execute(args)
    if args.command == "workspace":
        return workspace_cli.execute(args)
    if args.command == "composition":
        return composition_cli.execute(args)
    if args.command in ("workflow", "overview"):
        return workflow_cli.execute(args)
    if args.command == "message":
        return message_cli.execute(args)
    if args.command in ("task", "migrate", "dispatch"):
        return task_cli.execute(args)
    if args.command == "roles":
        return {"protocol": CONTRACT, "roles": [{"name": k, **v} for k, v in ROLE_PRESETS.items()]}
    if args.command == "init":
        return Store.initialize(
            args.state_dir, args.claude_bin, args.allow_root, args.max_parallel, args.max_queued
        )
    if args.command == "_worker":
        run_worker(args.state_dir, args.run)
        return {"worker_finished": True}
    if args.command == "_exec":
        exec_claude(args.state_dir, args.run)
        return None
    if args.command == "_workspace_exec":
        from .workspace_sandbox import exec_check

        exec_check(args.state_dir, args.run, args.seq, args.scratch)
        return None
    store = Store(args.state_dir)
    if args.command == "doctor":
        return doctor(store, args.auth)
    if args.command == "list":
        return store.list_all()
    if args.command == "delegate":
        assignment = load_assignment(args.assignment_file)
        assignment["project"] = store.project(assignment["project"])
        prompt = render_assignment(assignment)
        run_id, created = store.reserve(
            prompt=prompt,
            request_id=args.request_id,
            timeout=assignment["timeout"],
            name=assignment["name"],
            model=assignment["model"],
            role=assignment["role"],
            project=assignment["project"],
            effort=assignment.get("effort"),
        )
        if created:
            launch_worker(store, run_id)
        return {**store.get_run(run_id), "deduplicated": not created}
    if args.command == "observe":
        return observe(store, args.run, args.seconds)
    if args.command == "report":
        return report(store, args.run)
    if args.command in ("start", "followup", "resume", "restart"):
        if args.prompt_file.stat().st_size > 1024 * 1024:
            raise ControlError("invalid_prompt", "Prompt file exceeds 1 MiB.")
        options = dict(
            prompt=args.prompt_file.read_text(),
            request_id=args.request_id,
            timeout=args.timeout,
            effort=args.effort,
        )
        if args.command == "start":
            options.update(name=args.name, model=args.model, role=args.role, project=args.project)
        else:
            options.update(session_ref=args.session, acknowledge_context=args.acknowledge_context)
            options["restart"] = args.command == "restart"
        run_id, created = store.reserve(**options)
        if created:
            launch_worker(store, run_id)
        return {**store.get_run(run_id), "deduplicated": not created}
    if args.command == "stop":
        return store.stop(args.run)
    if args.command == "reconcile":
        return store.reconcile(args.run)
    if args.command == "wait":
        if not 0 <= args.seconds <= 60:
            raise ControlError("invalid_wait", "Wait must be 0–60 seconds.")
        deadline = time.monotonic() + args.seconds
        while True:
            store.refresh()
            row = store.get_run(args.run)
            if (
                row["status"] not in ACTIVE
                or row["status"] == "unknown"
                or time.monotonic() >= deadline
            ):
                return row
            time.sleep(0.2)
    store.refresh()
    row = store.get_run(args.run)
    if args.command == "status":
        return row
    if args.command == "result":
        path = store.run_dir(args.run) / "result.json"
        return {"run": row, "result": json.loads(path.read_text()) if path.exists() else None}
    if args.command == "logs":
        if not 1 <= args.bytes <= 65536:
            raise ControlError("invalid_limit", "Log tail must be 1–65536 bytes.")
        file = (
            store.run_dir(args.run)
            / {"events": "events.jsonl", "stderr": "stderr.txt", "worker": "worker.log"}[
                args.stream
            ]
        )
        if not file.exists():
            return {"run_id": args.run, "text": ""}
        with file.open("rb") as handle:
            handle.seek(max(0, file.stat().st_size - args.bytes))
            text = handle.read(args.bytes).decode(errors="replace")
        return {"run_id": args.run, "text": text}
    raise ControlError("invalid_command", "Unsupported command.")


def main():
    args = parser().parse_args()
    try:
        output = execute(args)
        if output is not None:
            print(json.dumps(output, ensure_ascii=False, indent=2))
        return 0
    except (ControlError, OSError, ValueError, sqlite3.Error, subprocess.TimeoutExpired) as exc:
        message = str(exc)
        if isinstance(exc, sqlite3.OperationalError):
            message += "; check state directory write access and database availability."
        print(
            json.dumps(
                {"error": getattr(exc, "code", "operation_failed"), "message": message},
                ensure_ascii=False,
            ),
            file=sys.stderr if args.command in ("_exec", "_workspace_exec") else sys.stdout,
        )
        return 2


if __name__ == "__main__":
    sys.exit(main())
