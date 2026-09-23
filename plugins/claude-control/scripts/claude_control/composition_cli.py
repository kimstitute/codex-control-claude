"""CLI adapter for P4-to-P5 compositions."""

import math
from pathlib import Path

from . import composition, evaluation
from .assignments import load_assignment, strict_json
from .store import ControlError, Store


def register(commands):
    parser = commands.add_parser("composition", help="Compose planning, editing, and review.")
    sub = parser.add_subparsers(dest="composition_command", required=True)

    create = sub.add_parser("create")
    planning = create.add_mutually_exclusive_group(required=True)
    planning.add_argument("--planning-assignment-file", type=Path)
    planning.add_argument("--leader-spec-file", type=Path)
    create.add_argument("--critic-assignment-file", type=Path)
    create.add_argument("--editor-assignment-file", type=Path, required=True)
    create.add_argument("--editor-policy-file", type=Path, required=True)
    create.add_argument("--reviewer-assignment-file", type=Path, required=True)
    create.add_argument("--test-contract-file", type=Path)
    create.add_argument("--repo", required=True)
    create.add_argument("--ref", default="HEAD")
    create.add_argument("--operation-id", required=True)
    create.add_argument("--max-revisions", type=int, default=2)
    create.add_argument("--max-calls", type=int, default=6)
    create.add_argument("--dispatch-window-seconds", type=float, default=900)
    create.add_argument("--reviewer-effort", choices=("low", "medium", "high", "xhigh", "max"))
    create.add_argument("--scout-workspace")

    run = sub.add_parser("run")
    run.add_argument("--composition", required=True)
    mode = run.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true")
    mode.add_argument("--until-idle", action="store_true")
    run.add_argument("--max-seconds", type=float)

    status = sub.add_parser("status")
    status.add_argument("--composition", required=True)

    stop = sub.add_parser("stop")
    stop.add_argument("--composition", required=True)
    stop.add_argument("--operation-id", required=True)

    evaluate = sub.add_parser("evaluate")
    selection = evaluate.add_mutually_exclusive_group(required=True)
    selection.add_argument("--all", action="store_true")
    selection.add_argument("--composition", action="append")


def _policy(path):
    with path.open("rb") as handle:
        raw = handle.read(65537)
    if len(raw) > 65536:
        raise ControlError("invalid_workspace_policy", "Policy file exceeds 64 KiB.")
    return strict_json(raw.decode("utf-8"))


def execute(args):
    store = Store(args.state_dir)
    command = args.composition_command
    if command == "create":
        leader_spec = _policy(args.leader_spec_file) if args.leader_spec_file else None
        critic = (
            load_assignment(args.critic_assignment_file) if args.critic_assignment_file else None
        )
        if leader_spec is not None and critic is None:
            raise ControlError(
                "invalid_arguments", "--leader-spec-file requires --critic-assignment-file"
            )
        if leader_spec is None and critic is not None:
            raise ControlError(
                "invalid_arguments", "--critic-assignment-file requires --leader-spec-file"
            )
        return composition.create(
            store,
            (
                load_assignment(args.planning_assignment_file)
                if args.planning_assignment_file
                else None
            ),
            load_assignment(args.editor_assignment_file),
            _policy(args.editor_policy_file),
            load_assignment(args.reviewer_assignment_file),
            args.operation_id,
            repo=args.repo,
            ref=args.ref,
            max_revisions=args.max_revisions,
            max_calls=args.max_calls,
            dispatch_window_seconds=args.dispatch_window_seconds,
            reviewer_effort=args.reviewer_effort,
            scout_workspace=args.scout_workspace,
            leader_spec=leader_spec,
            critic_assignment=critic,
            test_contract=(_policy(args.test_contract_file) if args.test_contract_file else None),
        )
    if command == "evaluate":
        return evaluation.evaluate(store, None if args.all else args.composition)
    if command == "status":
        return composition.status(store, args.composition)
    if command == "stop":
        return composition.stop(store, args.composition, args.operation_id)
    if command == "run":
        if args.once:
            if args.max_seconds is not None:
                raise ControlError("invalid_arguments", "--once does not accept --max-seconds")
            return composition.run(store, args.composition, once=True)
        if args.max_seconds is None:
            raise ControlError("invalid_arguments", "--until-idle requires --max-seconds")
        if not math.isfinite(args.max_seconds) or not 0 <= args.max_seconds <= 3600:
            raise ControlError("invalid_arguments", "--max-seconds must be finite, 0..3600")
        return composition.run(store, args.composition, once=False, max_seconds=args.max_seconds)
    raise ControlError("invalid_command", f"unsupported composition command: {command}")
