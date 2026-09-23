"""CLI adapter for read-only live monitoring."""

import sys

from . import monitor, observation, observation_agui, observation_otel, provider_usage
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
    events = sub.add_parser("events", help="Page through the append-only observation ledger.")
    events.add_argument("--after", type=int, default=0, help="Exclusive event cursor.")
    events.add_argument("--limit", type=int, default=100)
    events.add_argument("--through", type=int, help="Inclusive high-water cursor.")
    replay = sub.add_parser("replay", help="Reconstruct the graph at an event cursor.")
    replay.add_argument("--through", type=int, help="Inclusive replay cursor; defaults to latest.")
    export = sub.add_parser("export", help="Export replayed history for observability tools.")
    export.add_argument("--format", choices=("otlp-json",), default="otlp-json")
    export.add_argument("--through", type=int, help="Inclusive replay cursor; defaults to latest.")
    export.add_argument(
        "--metadata",
        action="store_true",
        help="Wrap the standard document with local profile and omission metadata.",
    )
    agui = sub.add_parser("agui", help="Convert observation history into AG-UI 1.0 events.")
    agui_sub = agui.add_subparsers(dest="agui_command", required=True)
    agui_snapshot = agui_sub.add_parser("snapshot", help="Return one AG-UI STATE_SNAPSHOT event.")
    agui_snapshot.add_argument(
        "--through", type=int, help="Inclusive replay cursor; defaults to latest."
    )
    agui_events = agui_sub.add_parser("events", help="Convert one ledger page into AG-UI events.")
    agui_events.add_argument("--after", type=int, default=0, help="Exclusive event cursor.")
    agui_events.add_argument("--limit", type=int, default=100)
    agui_events.add_argument("--through", type=int, help="Inclusive high-water cursor.")
    tui = sub.add_parser("tui", help="Open the live terminal dashboard.")
    tui.add_argument("--history", type=int, default=100)
    tui.add_argument("--refresh-seconds", type=float, default=0.5)
    tui.add_argument("--limits-refresh-seconds", type=float, default=60.0)


def _agui(store, args):
    """Convert one bounded observation read into standard AG-UI objects."""
    if args.agui_command == "snapshot":
        replayed = observation.replay(store, through=args.through)
        try:
            return observation_agui.state_snapshot(replayed)
        except ValueError as exc:
            raise ControlError("invalid_event", str(exc)) from exc
    if args.agui_command == "events":
        page = observation.events(
            store,
            after=args.after,
            limit=args.limit,
            through=args.through,
        )
        try:
            converted = observation_agui.events(page["events"])
        except ValueError as exc:
            raise ControlError("invalid_event", str(exc)) from exc
        # The AG-UI objects stay standard; only this envelope carries local paging.
        return {
            "profile": observation_agui.PROFILE,
            "after": page["after"],
            "limit": page["limit"],
            "through": page["through"],
            "next_cursor": page["next_cursor"],
            "has_more": page["has_more"],
            "events": converted,
        }
    raise ControlError("invalid_command", "Unsupported monitor agui command.")


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
    if args.monitor_command == "events":
        return observation.events(
            store,
            after=args.after,
            limit=args.limit,
            through=args.through,
        )
    if args.monitor_command == "replay":
        return observation.replay(store, through=args.through)
    if args.monitor_command == "export":
        snapshot = observation.replay(store, through=args.through)
        bundle = observation_otel.build_export(snapshot)
        return bundle if args.metadata else bundle["document"]
    if args.monitor_command == "agui":
        return _agui(store, args)
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
