"""CLI adapter for P4 workflow commands."""

import math
from pathlib import Path

from . import workflow
from .assignments import load_assignment
from .store import ControlError, Store


def register(commands):
    parser = commands.add_parser(
        "workflow",
        help="Create, run, and inspect bounded multi-turn workflows.",
    )
    sub = parser.add_subparsers(dest="workflow_command", required=True)

    create = sub.add_parser(
        "create",
        help="Create a new workflow from an assignment file.",
    )
    create.add_argument("--assignment-file", required=True, type=Path)
    create.add_argument("--operation-id", required=True)
    create.add_argument("--max-revisions", type=int, default=2)
    create.add_argument("--max-calls", type=int, default=6)
    create.add_argument("--dispatch-window-seconds", type=float, default=900)
    create.add_argument("--reviewer-effort", choices=("low", "medium", "high", "xhigh", "max"))
    create.add_argument("--reviewer-model")

    run = sub.add_parser(
        "run",
        help="Advance a workflow, either a single step or until idle.",
    )
    run.add_argument("--workflow", required=True)
    mode = run.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true")
    mode.add_argument("--until-idle", action="store_true")
    run.add_argument("--max-seconds", type=float, default=None)

    status = sub.add_parser("status", help="Show a single workflow's status.")
    status.add_argument("--workflow", required=True)

    stop = sub.add_parser("stop", help="Stop a workflow.")
    stop.add_argument("--workflow", required=True)
    stop.add_argument("--operation-id", required=True)

    overview = commands.add_parser("overview", help="Show status across all workflows.")
    overview.add_argument("--attention", action="store_true")


def _run(store, args):
    if args.once:
        if args.max_seconds is not None:
            raise ControlError("invalid_arguments", "--once does not accept --max-seconds")
        return workflow.run(store, args.workflow, once=True)

    if args.max_seconds is None:
        raise ControlError("invalid_arguments", "--until-idle requires an explicit --max-seconds")
    if not math.isfinite(args.max_seconds) or not (0 <= args.max_seconds <= 3600):
        raise ControlError(
            "invalid_arguments",
            "--max-seconds must be a finite number in [0, 3600] for --until-idle",
        )
    return workflow.run(store, args.workflow, once=False, max_seconds=args.max_seconds)


def execute(args):
    store = Store(args.state_dir)
    if args.command == "overview":
        return workflow.overview(store, attention=args.attention)

    if args.workflow_command == "create":
        assignment = load_assignment(args.assignment_file, role_defaults=store.role_defaults())
        return workflow.create(
            store,
            assignment,
            args.operation_id,
            max_revisions=args.max_revisions,
            max_calls=args.max_calls,
            dispatch_window_seconds=args.dispatch_window_seconds,
            reviewer_effort=args.reviewer_effort,
            reviewer_model=args.reviewer_model,
        )

    if args.workflow_command == "run":
        return _run(store, args)

    if args.workflow_command == "status":
        return workflow.status(store, args.workflow)

    if args.workflow_command == "stop":
        return workflow.stop(store, args.workflow, args.operation_id)

    raise ControlError("invalid_command", f"unsupported workflow command: {args.workflow_command}")
