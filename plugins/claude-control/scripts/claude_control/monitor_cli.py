"""CLI adapter for read-only live monitoring."""

import sys

from . import monitor, provider_usage
from .store import ControlError, Store


def register(commands):
    parser = commands.add_parser("monitor", help="Observe the local agent graph and run usage.")
    sub = parser.add_subparsers(dest="monitor_command", required=True)
    snapshot = sub.add_parser("snapshot", help="Return one read-only JSON snapshot.")
    snapshot.add_argument("--history", type=int, default=100)
    snapshot.add_argument("--no-live", action="store_true")
    snapshot.add_argument(
        "--limits", action="store_true", help="Also query signed-in provider limits."
    )
    limits = sub.add_parser("limits", help="Return Codex, Claude, Gemini and Cursor usage limits.")
    limits.add_argument("--timeout", type=float, default=12.0)
    limits.add_argument("--local-only", action="store_true")
    tui = sub.add_parser("tui", help="Open the live terminal dashboard.")
    tui.add_argument("--history", type=int, default=100)
    tui.add_argument("--refresh-seconds", type=float, default=0.5)
    tui.add_argument("--limits-refresh-seconds", type=float, default=60.0)


def execute(args):
    store = Store(args.state_dir)
    if args.monitor_command == "snapshot":
        data = monitor.snapshot(store, history=args.history, live=not args.no_live)
        if args.limits:
            data = monitor._attach_provider_usage(data, provider_usage.collect())
        return data
    if args.monitor_command == "limits":
        try:
            return provider_usage.collect(timeout=args.timeout, network=not args.local_only)
        except ValueError as exc:
            raise ControlError("invalid_limit", str(exc)) from exc
    if args.monitor_command == "tui":
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            raise ControlError("monitor_terminal", "Monitor TUI needs an interactive terminal.")
        monitor.run_tui(
            store,
            refresh_seconds=args.refresh_seconds,
            history=args.history,
            limits_refresh_seconds=args.limits_refresh_seconds,
        )
        return None
    raise ControlError("invalid_command", "Unsupported monitor command.")
