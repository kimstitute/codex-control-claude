"""Task lifecycle CLI adapter: create/list/show/revise/submit/retry/review/accept/enqueue/dequeue/events/dispatch."""

from pathlib import Path

from . import scheduler, tasks
from .assignments import MAX_BYTES, load_assignment, strict_json
from .migration import migrate, migration_status
from .runner import launch_worker
from .store import ControlError, Store


def _evidence_args(cmd):
    cmd.add_argument("--task", required=True)
    cmd.add_argument("--revision", type=int, required=True)
    cmd.add_argument("--run", required=True)
    cmd.add_argument("--result-sha256", required=True)
    cmd.add_argument("--evidence-file", type=Path, required=True)
    cmd.add_argument("--operation-id", required=True)


def register(commands):
    migrate_cmd = commands.add_parser(
        "migrate", help="Inspect or perform the offline schema migration (3->4->5->6->7->8)."
    )
    mode = migrate_cmd.add_mutually_exclusive_group()
    mode.add_argument(
        "--status", action="store_true", help="Report migration status only; makes no changes."
    )
    mode.add_argument(
        "--offline",
        action="store_true",
        help="Perform the migration to the latest schema; all clients must already be stopped.",
    )

    dispatch_cmd = commands.add_parser(
        "dispatch", help="Admit queued work to available capacity (no daemon)."
    )
    dispatch_mode = dispatch_cmd.add_mutually_exclusive_group(required=True)
    dispatch_mode.add_argument(
        "--once",
        action="store_true",
        help="Attempt a single admission pass and return immediately.",
    )
    dispatch_mode.add_argument(
        "--until-idle",
        action="store_true",
        help="Admit repeatedly until no useful active work remains or --max-seconds elapses.",
    )
    dispatch_cmd.add_argument(
        "--max-seconds",
        type=float,
        default=None,
        help="Required with --until-idle; bounded admission deadline in seconds (0..3600).",
    )

    task = commands.add_parser("task", help="Manage delegated, review-gated tasks.")
    task_commands = task.add_subparsers(dest="task_command", required=True)

    create = task_commands.add_parser("create")
    create.add_argument("--assignment-file", type=Path, required=True)
    create.add_argument("--operation-id", required=True)
    create.add_argument("--session")
    create.add_argument("--parent-run")
    create.add_argument("--acknowledge-context", action="store_true")

    task_commands.add_parser("list")

    show = task_commands.add_parser("show")
    show.add_argument("--task", required=True)

    revise = task_commands.add_parser("revise")
    revise.add_argument("--task", required=True)
    revise.add_argument("--revision", type=int, required=True)
    revise.add_argument("--assignment-file", type=Path, required=True)
    revise.add_argument(
        "--parent-run",
        help="Optional; the runtime enforces this when the task's originating session still exists.",
    )
    revise.add_argument("--operation-id", required=True)
    revise.add_argument("--acknowledge-context", action="store_true")
    revise.add_argument("--message-id", action="append", default=[])
    revise.add_argument("--redeliver-messages", action="store_true")

    for name in ("submit", "retry"):
        cmd = task_commands.add_parser(name)
        cmd.add_argument("--task", required=True)
        cmd.add_argument("--revision", type=int, required=True)
        cmd.add_argument("--operation-id", required=True)

    review = task_commands.add_parser("review")
    _evidence_args(review)
    review.add_argument("--reviewer", required=True)
    review.add_argument("--recommendation", choices=("approve", "revise", "blocked"), required=True)

    accept = task_commands.add_parser("accept")
    _evidence_args(accept)

    enqueue = task_commands.add_parser("enqueue")
    enqueue.add_argument("--task", required=True)
    enqueue.add_argument("--revision", type=int, required=True)
    enqueue.add_argument("--operation-id", required=True)
    enqueue.add_argument(
        "--dependencies-file",
        type=Path,
        help="JSON array of {task_id, revision} objects this queue entry waits on; default none.",
    )

    dequeue = task_commands.add_parser("dequeue")
    dequeue.add_argument("--queue-id", type=int, required=True)
    dequeue.add_argument("--operation-id", required=True)

    events = task_commands.add_parser("events")
    events.add_argument("--task")
    events.add_argument("--after", type=int, default=0)
    events.add_argument("--limit", type=int, default=100)


def _load_evidence(path):
    try:
        with path.open("rb") as handle:
            raw = handle.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ControlError("invalid_evidence", "Evidence exceeds 1 MiB.")
        data = strict_json(raw.decode("utf-8"))
    except ControlError as exc:
        raise ControlError("invalid_evidence", str(exc)) from exc
    except (OSError, ValueError) as exc:
        message = f"Evidence file is invalid: {exc}"
        raise ControlError("invalid_evidence", message) from exc
    if not isinstance(data, dict) or not data:
        raise ControlError("invalid_evidence", "Evidence must be a non-empty JSON object.")
    for key, value in data.items():
        if not isinstance(key, str) or not key.isdigit() or int(key) < 1:
            message = f"Evidence key {key!r} must be a positive criterion id."
            raise ControlError("invalid_evidence", message)
        if not isinstance(value, str) or not value.strip():
            message = f"Evidence text for criterion {key!r} must be non-empty."
            raise ControlError("invalid_evidence", message)
    return data


def _load_dependencies(path):
    if path is None:
        return []
    try:
        with path.open("rb") as handle:
            raw = handle.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ControlError("invalid_dependencies", "Dependencies file exceeds 1 MiB.")
        data = strict_json(raw.decode("utf-8"))
    except ControlError as exc:
        raise ControlError("invalid_dependencies", str(exc)) from exc
    except (OSError, ValueError) as exc:
        message = f"Dependencies file is invalid: {exc}"
        raise ControlError("invalid_dependencies", message) from exc
    if not isinstance(data, list):
        raise ControlError("invalid_dependencies", "Dependencies must be a JSON array.")
    return data


def _submit(store, args, retry):
    run_id, created = tasks.submit(store, args.task, args.revision, args.operation_id, retry=retry)
    if created:
        launch_worker(store, run_id)
    return {**store.get_run(run_id), "deduplicated": not created}


def _dispatch(args):
    max_seconds = args.max_seconds
    if args.once and max_seconds is not None:
        raise ControlError("invalid_dispatch", "--max-seconds is not allowed with --once.")
    if args.until_idle:
        if max_seconds is None:
            raise ControlError(
                "invalid_dispatch", "--until-idle requires an explicit --max-seconds."
            )
        if not 0 <= max_seconds <= 3600:
            raise ControlError("invalid_dispatch", "--max-seconds must be between 0 and 3600.")
    store = Store(args.state_dir)
    return scheduler.dispatch(
        store, once=args.once, max_seconds=max_seconds if args.until_idle else 30
    )


def execute(args):
    if args.command == "migrate":
        if args.status:
            return migration_status(args.state_dir)
        return migrate(args.state_dir, offline=args.offline)

    if args.command == "dispatch":
        return _dispatch(args)

    store = Store(args.state_dir)
    sub = args.task_command

    if sub == "create":
        assignment = load_assignment(args.assignment_file, role_defaults=store.role_defaults())
        return tasks.create(
            store,
            assignment,
            args.operation_id,
            session_ref=args.session,
            parent_run_id=args.parent_run,
            acknowledge_context=args.acknowledge_context,
        )
    if sub == "list":
        return tasks.list_tasks(store)
    if sub == "show":
        return tasks.show(store, args.task)
    if sub == "revise":
        assignment = load_assignment(args.assignment_file, role_defaults=store.role_defaults())
        return tasks.revise(
            store,
            args.task,
            args.revision,
            assignment,
            args.operation_id,
            parent_run_id=args.parent_run,
            acknowledge_context=args.acknowledge_context,
            message_ids=args.message_id,
            redeliver_messages=args.redeliver_messages,
        )
    if sub in ("submit", "retry"):
        return _submit(store, args, retry=sub == "retry")
    if sub == "review":
        evidence = _load_evidence(args.evidence_file)
        return tasks.review(
            store,
            args.task,
            args.revision,
            args.run,
            args.result_sha256,
            evidence,
            args.operation_id,
            reviewer=args.reviewer,
            recommendation=args.recommendation,
        )
    if sub == "accept":
        evidence = _load_evidence(args.evidence_file)
        return tasks.accept(
            store,
            args.task,
            args.revision,
            args.run,
            args.result_sha256,
            evidence,
            args.operation_id,
        )
    if sub == "enqueue":
        dependencies = _load_dependencies(args.dependencies_file)
        return scheduler.enqueue(
            store, args.task, args.revision, args.operation_id, dependencies=dependencies
        )
    if sub == "dequeue":
        return scheduler.dequeue(store, args.queue_id, args.operation_id)
    if sub == "events":
        return scheduler.events(store, task_id=args.task, after=args.after, limit=args.limit)
    raise ControlError("invalid_command", "Unsupported task subcommand.")
