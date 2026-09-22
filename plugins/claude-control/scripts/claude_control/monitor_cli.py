"""CLI adapter for read-only live monitoring."""

import sys

from . import monitor
from .store import ControlError, Store


def register(commands):
    parser = commands.add_parser("monitor", help="Observe the local agent graph and run usage.")
    sub = parser.add_subparsers(dest="monitor_command", required=True)
    snapshot = sub.add_parser("snapshot", help="Return one read-only JSON snapshot.")
    snapshot.add_argument("--history", type=int, default=100)
    snapshot.add_argument("--no-live", action="store_true")
    tui = sub.add_parser("tui", help="Open the live terminal dashboard.")
    tui.add_argument("--history", type=int, default=100)
    tui.add_argument("--refresh-seconds", type=float, default=0.5)


def execute(args):
    store = Store(args.state_dir)
    if args.monitor_command == "snapshot":
        return monitor.snapshot(store, history=args.history, live=not args.no_live)
    if args.monitor_command == "tui":
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            raise ControlError("monitor_terminal", "Monitor TUI needs an interactive terminal.")
        monitor.run_tui(store, refresh_seconds=args.refresh_seconds, history=args.history)
        return None
    raise ControlError("invalid_command", "Unsupported monitor command.")

