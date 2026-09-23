"""CLI adapter for read-only live monitoring."""

import hashlib
import json
import math
import os
import subprocess
import sys
import time
from pathlib import Path

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
    agui_stream = agui_sub.add_parser(
        "stream", help="Write AG-UI events as JSON Lines, optionally following appends."
    )
    agui_stream.add_argument("--after", type=int, default=0, help="Exclusive event cursor.")
    agui_stream.add_argument("--limit", type=int, default=100)
    agui_stream.add_argument("--through", type=int, help="Inclusive high-water cursor.")
    agui_stream.add_argument("--follow", action="store_true", help="Wait for appended events.")
    agui_stream.add_argument("--poll-seconds", type=float, default=0.25)
    tui = sub.add_parser("tui", help="Open the live terminal dashboard.")
    tui.add_argument("--history", type=int, default=100)
    tui.add_argument("--refresh-seconds", type=float, default=0.5)
    tui.add_argument("--limits-refresh-seconds", type=float, default=60.0)
    viewer = sub.add_parser("viewer", help="Open the interactive graph and replay viewer.")
    viewer.add_argument("--viewer-bin", type=Path, help="Explicit ccc-viewer binary.")
    viewer.add_argument("--poll-seconds", type=float, default=0.25)
    viewer.add_argument("--no-follow", action="store_true")
    viewer.add_argument("--stream-file", type=Path, help="Open a saved AG-UI JSONL stream.")
    viewer.add_argument("--inspect", action="store_true", help="Print a headless summary.")


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


def _agui_stream(store, args, *, output=None, sleeper=time.sleep):
    """Drain cursor-ordered AG-UI events to one blocking, backpressure-safe JSONL stream."""
    if args.follow and args.through is not None:
        raise ControlError("invalid_cursor", "A followed AG-UI stream cannot pin --through.")
    if not math.isfinite(args.poll_seconds) or not 0.05 <= args.poll_seconds <= 10:
        raise ControlError("invalid_limit", "AG-UI poll interval must be finite, 0.05–10 seconds.")
    output = sys.stdout if output is None else output
    cursor = args.after
    while True:
        page = observation.events(
            store,
            after=cursor,
            limit=args.limit,
            through=args.through,
        )
        try:
            converted = observation_agui.events(page["events"])
        except ValueError as exc:
            raise ControlError("invalid_event", str(exc)) from exc
        try:
            for event in converted:
                output.write(json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n")
                output.flush()
        except BrokenPipeError:
            return None
        cursor = page["next_cursor"]
        if page["has_more"]:
            continue
        if not args.follow:
            return None
        sleeper(args.poll_seconds)


def _viewer_binary(explicit=None):
    bundled = explicit is None and not os.environ.get("CLAUDE_CONTROL_VIEWER")
    if explicit is not None:
        candidate = explicit.expanduser().resolve()
    elif os.environ.get("CLAUDE_CONTROL_VIEWER"):
        candidate = Path(os.environ["CLAUDE_CONTROL_VIEWER"]).expanduser().resolve()
    else:
        name = "ccc-viewer.exe" if os.name == "nt" else "ccc-viewer"
        candidate = Path(__file__).resolve().parents[2] / "bin" / name
    if not candidate.is_file():
        raise ControlError(
            "viewer_unavailable",
            f"Claude Control viewer binary is unavailable at {candidate}. "
            "Install a release bundle or pass --viewer-bin; monitor tui remains available.",
        )
    if bundled:
        _verify_bundled_viewer(candidate)
    return candidate


def _verify_bundled_viewer(candidate):
    manifest_path = candidate.parent / "viewer-manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        artifacts = manifest["artifacts"]
        item = artifacts[0]
        if (
            manifest.get("protocol") != 1
            or len(artifacts) != 1
            or set(item) != {"name", "sha256", "size"}
            or item["name"] != candidate.name
            or candidate.is_symlink()
            or isinstance(item["size"], bool)
            or not isinstance(item["size"], int)
            or not 1 <= item["size"] <= 64 * 1024 * 1024
        ):
            raise ValueError("invalid viewer manifest")
        data = candidate.read_bytes()
        if len(data) != item["size"] or hashlib.sha256(data).hexdigest() != item["sha256"]:
            raise ValueError("viewer bytes do not match the installation manifest")
    except (IndexError, KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ControlError(
            "viewer_integrity",
            f"Bundled Claude Control viewer failed integrity verification: {exc}",
        ) from exc


def _viewer_command(args):
    command = [
        str(_viewer_binary(args.viewer_bin)),
        *(["inspect"] if args.inspect else []),
    ]
    if args.stream_file is not None:
        command.extend(("--stream-file", str(args.stream_file.expanduser().resolve())))
    else:
        cli = Path(__file__).resolve().parents[1] / "claude_control_cli.py"
        command.extend(
            (
                "--python",
                sys.executable,
                "--cli",
                str(cli),
                "--state-dir",
                str(args.state_dir.resolve()),
                "--poll-seconds",
                str(args.poll_seconds),
            )
        )
        if args.no_follow:
            command.append("--no-follow")
    return command


def _run_viewer(args):
    if not args.inspect and (not sys.stdin.isatty() or not sys.stdout.isatty()):
        raise ControlError("monitor_terminal", "Monitor viewer needs an interactive terminal.")
    if not math.isfinite(args.poll_seconds) or not 0.05 <= args.poll_seconds <= 10:
        raise ControlError("invalid_limit", "Viewer poll interval must be finite, 0.05–10 seconds.")
    completed = subprocess.run(_viewer_command(args), check=False)
    if completed.returncode:
        raise ControlError(
            "viewer_failed", f"Claude Control viewer exited with code {completed.returncode}."
        )


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
        if args.agui_command == "stream":
            return _agui_stream(store, args)
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
    if args.monitor_command == "viewer":
        _run_viewer(args)
        return None
    raise ControlError("invalid_command", "Unsupported monitor command.")
