import json
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins" / "claude-control" / "scripts"))

from claude_control import terminal_cli  # noqa: E402
from claude_control.platform.locks import file_lock  # noqa: E402
from claude_control.store import ControlError  # noqa: E402

FAKE_CLAUDE = r'''#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

state = Path(__file__).with_name("agents.json")
marker = Path(__file__).with_name("attach.txt")
agents = json.loads(state.read_text()) if state.exists() else []
args = sys.argv[1:]
if args[:3] == ["agents", "--json", "--all"]:
    print(json.dumps(agents))
    raise SystemExit(0)
if "--bg" in args:
    session = "11111111-1111-4111-8111-111111111111"
    name = args[args.index("--name") + 1]
    item = {
        "pid": os.getpid(),
        "id": session[:8],
        "cwd": os.getcwd(),
        "kind": "background",
        "startedAt": 1234000,
        "sessionId": session,
        "name": name,
        "status": "idle",
        "state": "blocked"
    }
    agents.append(item)
    state.write_text(json.dumps(agents))
    print(f"backgrounded · {item['id']} · {name}")
    raise SystemExit(0)
if args and args[0] == "logs":
    sys.stdout.write("\x1b[31mterminal sentinel\x1b[0m\r\nready\n")
    raise SystemExit(0)
if args and args[0] == "attach":
    marker.write_text(args[1])
    raise SystemExit(0)
if args and args[0] == "stop":
    for item in agents:
        if item.get("id") == args[1]:
            item["status"] = "stopped"
    state.write_text(json.dumps(agents))
    print("stopped")
    raise SystemExit(0)
raise SystemExit(3)
'''


class FakeStore:
    def __init__(self, root, binary, project):
        self.path = root / "state"
        self.path.mkdir()
        self.config = {"claude_bin": str(binary)}
        self._project = str(project)

    def role_defaults(self):
        return {"executor": {"model": "opus", "effort": "high"}}

    def preflight_effort(self, effort):
        self.checked_effort = effort

    def project(self, value):
        resolved = str(Path(value).resolve())
        if resolved != self._project:
            raise ControlError("project_denied", "outside root")
        return resolved


class TerminalTests(unittest.TestCase):
    def setUp(self):
        terminal_cli._CATALOG_CACHE.clear()
        terminal_cli._LOG_CACHE.clear()

    def fixture(self, root):
        root = Path(root)
        project = root / "project"
        project.mkdir()
        binary = root / "claude"
        binary.write_text(FAKE_CLAUDE, encoding="utf-8")
        binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
        return FakeStore(root, binary, project), project, binary

    def start_args(self, project, **changes):
        values = dict(
            name="interactive executor",
            role="executor",
            project=str(project),
            model=None,
            effort=None,
            request_id="terminal-start-1",
        )
        values.update(changes)
        return SimpleNamespace(**values)

    def test_start_is_idempotent_and_uses_safe_native_background_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            store, project, _ = self.fixture(directory)
            first = terminal_cli.start(store, self.start_args(project))
            second = terminal_cli.start(store, self.start_args(project))
            self.assertTrue(first["created"])
            self.assertFalse(second["created"])
            self.assertTrue(first["terminal"]["attachable"])
            self.assertTrue(first["terminal"]["managed"])
            self.assertEqual(first["terminal"]["name"], "interactive executor")
            self.assertEqual(first["terminal"]["session_id"], second["terminal"]["session_id"])
            native = terminal_cli.agents(store, refresh=True)
            self.assertEqual(len(native), 1)
            self.assertIn("ccc-", native[0]["name"])
            descriptor = next((store.path / "terminals").glob("session-*.json"))
            saved = json.loads(descriptor.read_text())
            self.assertTrue(saved["safe_profile"])
            self.assertEqual(saved["model"], "opus")
            self.assertEqual(saved["effort"], "high")

    def test_changed_request_is_rejected_and_terminal_logs_are_sanitized(self):
        with tempfile.TemporaryDirectory() as directory:
            store, project, _ = self.fixture(directory)
            result = terminal_cli.start(store, self.start_args(project))
            with self.assertRaisesRegex(ControlError, "different input"):
                terminal_cli.start(store, self.start_args(project, name="changed"))
            output = terminal_cli.recent_output(store, result["terminal"])
            self.assertIn("terminal sentinel", output)
            self.assertIn("ready", output)
            self.assertNotIn("\x1b", output)

    def test_sanitizer_keeps_only_the_latest_complete_screen(self):
        output = terminal_cli._sanitize_output(
            b"old screen\x1b[2Jnew screen\x1b[31m ready\x1b[0m", 4096
        )
        self.assertNotIn("old screen", output)
        self.assertIn("new screen", output)
        self.assertNotIn("\x1b", output)

    def test_attach_holds_one_local_operator_lease(self):
        with tempfile.TemporaryDirectory() as directory:
            store, project, binary = self.fixture(directory)
            item = terminal_cli.start(store, self.start_args(project))["terminal"]
            lease = store.path / "host-wide-terminal.lock"
            with (
                mock.patch.object(terminal_cli.os, "isatty", return_value=True),
                mock.patch.object(terminal_cli, "terminal_operator_lease", return_value=lease),
            ):
                attached = terminal_cli._attach(store, item)
            self.assertTrue(attached["detached"])
            self.assertEqual(binary.with_name("attach.txt").read_text(), item["id"])
            with file_lock(lease, exclusive=True, blocking=False):
                with (
                    mock.patch.object(terminal_cli.os, "isatty", return_value=True),
                    mock.patch.object(
                        terminal_cli, "terminal_operator_lease", return_value=lease
                    ),
                ):
                    with self.assertRaisesRegex(ControlError, "holds terminal control"):
                        terminal_cli._attach(store, item)

    def test_unmanaged_native_background_session_is_observable_but_not_controllable(self):
        with tempfile.TemporaryDirectory() as directory:
            store, _, binary = self.fixture(directory)
            session = "22222222-2222-4222-8222-222222222222"
            binary.with_name("agents.json").write_text(
                json.dumps(
                    [
                        {
                            "id": session[:8],
                            "sessionId": session,
                            "name": "external",
                            "cwd": str(Path(directory)),
                            "kind": "background",
                            "state": "blocked",
                        }
                    ]
                )
            )
            item = terminal_cli.catalog(store, refresh=True)["terminals"][0]
            self.assertEqual(item["status"], "idle")
            self.assertTrue(item["native_attachable"])
            self.assertFalse(item["attachable"])
            self.assertFalse(item["managed"])
            with self.assertRaisesRegex(ControlError, "created by this controller"):
                terminal_cli._stop(store, item)

    def test_catalog_omits_terminal_output_and_stop_is_explicit(self):
        with tempfile.TemporaryDirectory() as directory:
            store, project, _ = self.fixture(directory)
            item = terminal_cli.start(store, self.start_args(project))["terminal"]
            catalog = terminal_cli.catalog(store, refresh=True)
            self.assertNotIn("terminal sentinel", json.dumps(catalog))
            stopped = terminal_cli._stop(store, item)
            self.assertTrue(stopped["stop_requested"])
            current = terminal_cli.resolve(store, session_id=item["session_id"], refresh=True)
            self.assertEqual(current["status"], "stopped")
            self.assertFalse(current["attachable"])
            self.assertTrue(current["loggable"])
            self.assertIn("terminal sentinel", terminal_cli.recent_output(store, current))


if __name__ == "__main__":
    unittest.main()
