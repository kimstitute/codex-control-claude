"""Explicit commands for bounded controller-mediated file work."""

from pathlib import Path

from . import workspace, workspace_sandbox
from .assignments import load_assignment, strict_json
from .store import ControlError, Store


def register(commands):
    group = commands.add_parser("workspace")
    sub = group.add_subparsers(dest="workspace_command", required=True)
    sub.add_parser("doctor")
    sub.add_parser("list")
    create = sub.add_parser("create")
    source = create.add_mutually_exclusive_group(required=True)
    source.add_argument("--repo")
    source.add_argument("--from-snapshot")
    create.add_argument("--ref", default="HEAD")
    create.add_argument("--policy-file", type=Path, required=True)
    create.add_argument("--operation-id", required=True)
    for name in ("task", "run", "status", "export", "stop", "reconcile"):
        command = sub.add_parser(name)
        command.add_argument("--workspace", required=True)
        if name in ("task", "stop"):
            command.add_argument("--operation-id", required=True)
        if name == "task":
            command.add_argument("--assignment-file", type=Path, required=True)
        if name == "run":
            mode = command.add_mutually_exclusive_group(required=True)
            mode.add_argument("--once", action="store_true")
            mode.add_argument("--until-idle", action="store_true")
            command.add_argument("--max-seconds", type=float)


def execute(args):
    command = args.workspace_command
    if command == "doctor":
        return workspace_sandbox.probe()
    store = Store(args.state_dir)
    if command == "create":
        with args.policy_file.open("rb") as handle:
            raw = handle.read(65537)
        if len(raw) > 65536:
            raise ControlError("invalid_workspace_policy", "Policy file exceeds 64 KiB.")
        return workspace.create(
            store,
            strict_json(raw.decode()),
            args.operation_id,
            repo=args.repo,
            ref=args.ref,
            from_snapshot=args.from_snapshot,
        )
    if command == "task":
        return workspace.bind(
            store, args.workspace, load_assignment(args.assignment_file), args.operation_id
        )
    if command == "run":
        if (args.once and args.max_seconds is not None) or (
            args.until_idle and args.max_seconds is None
        ):
            raise ControlError(
                "invalid_limit", "Use --once alone, or --until-idle with explicit --max-seconds."
            )
        return workspace.run(
            store, args.workspace, once=args.once, max_seconds=0 if args.once else args.max_seconds
        )
    if command == "stop":
        return workspace.stop(store, args.workspace, args.operation_id)
    if command == "list":
        workspace._require(store)
        with store.db() as db:
            return {
                "workspaces": [
                    dict(row)
                    for row in db.execute(
                        "SELECT c.id,w.base_commit,coalesce(w.state,'preparing') AS state,"
                        "coalesce(w.reason, CASE WHEN w.id IS NULL THEN 'creation_incomplete' END) AS reason,t.task_id "
                        "FROM workspace_creations c LEFT JOIN workspaces w ON w.id=c.id "
                        "LEFT JOIN workspace_tasks t ON t.workspace_id=w.id ORDER BY c.rowid"
                    )
                ]
            }
    return {
        "status": workspace.status,
        "export": workspace.export,
        "reconcile": workspace.reconcile,
    }[command](store, args.workspace)
