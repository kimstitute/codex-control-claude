"""CLI adapter for P3 next-turn message commands."""

from pathlib import Path

from . import messages
from .assignments import MAX_BYTES
from .store import ControlError, Store


def register(commands):
    parser = commands.add_parser(
        "message",
        help=(
            "Store instructions for a later, explicit revision selection. "
            "Never writes the active Claude context or creates a run."
        ),
    )
    sub = parser.add_subparsers(dest="message_command", required=True)

    enqueue = sub.add_parser(
        "enqueue",
        help="Queue a next-turn message for a task at a specific base revision.",
    )
    enqueue.add_argument("--task", required=True)
    enqueue.add_argument("--base-revision", required=True, type=int)
    enqueue.add_argument("--content-file", required=True, type=Path)
    enqueue.add_argument("--operation-id", required=True)
    enqueue.add_argument("--session", default=None)
    enqueue.add_argument("--source-run", default=None)
    enqueue.add_argument("--source-result-sha256", default=None)

    listing = sub.add_parser("list", help="List queued messages, optionally filtered by task.")
    listing.add_argument("--task", default=None)
    listing.add_argument("--after", type=int, default=0)
    listing.add_argument("--limit", type=int, default=100)

    cancel = sub.add_parser("cancel", help="Cancel a previously queued message.")
    cancel.add_argument("--message", required=True)
    cancel.add_argument("--operation-id", required=True)


def _read_content(content_file):
    try:
        with content_file.open("rb") as handle:
            raw = handle.read(MAX_BYTES + 1)
    except OSError as exc:
        raise ControlError("invalid_message", str(exc)) from exc

    if len(raw) > MAX_BYTES:
        raise ControlError("invalid_message", "content-file exceeds maximum allowed size")

    try:
        return raw.decode("utf-8", errors="strict")
    except (UnicodeDecodeError, ValueError) as exc:
        raise ControlError("invalid_message", str(exc)) from exc


def execute(args):
    store = Store(args.state_dir)

    if args.message_command == "enqueue":
        content = _read_content(args.content_file)
        return messages.enqueue(
            store,
            args.task,
            args.base_revision,
            content,
            args.operation_id,
            session_ref=args.session,
            source_run_id=args.source_run,
            source_result_sha256=args.source_result_sha256,
        )

    if args.message_command == "list":
        return messages.list_messages(
            store,
            task_id=args.task,
            after=args.after,
            limit=args.limit,
        )

    if args.message_command == "cancel":
        return messages.cancel(store, args.message, args.operation_id)

    raise ControlError("invalid_command", f"unsupported message command: {args.message_command}")
