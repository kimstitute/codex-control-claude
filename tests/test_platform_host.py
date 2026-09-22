"""Portable host identity and private-state behavior."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/claude-control/scripts"))

from claude_control import store as store_module  # noqa: E402
from claude_control.platform import host as host_module  # noqa: E402
from claude_control.platform.host import (  # noqa: E402
    FILE_ALL_ACCESS,
    HostError,
    LinuxHost,
    WindowsHost,
)
from claude_control.store import ControlError, Store, write_json  # noqa: E402


class FakeWinApi:
    def __init__(
        self,
        *,
        sid="S-1-5-21-1111-2222-3333-1001",
        guid="11111111-1111-1111-1111-111111111111",
        reparse=False,
        owner=None,
        protected=True,
        entries=None,
    ):
        self.calls = []
        self.sid = sid
        self.guid = guid
        self.reparse = reparse
        self.owner = sid if owner is None else owner
        self.protected = protected
        self.entries = (
            [{"type": 0, "flags": 0, "mask": FILE_ALL_ACCESS, "sid": sid}]
            if entries is None
            else entries
        )
        self.process_times_by_handle = {}
        self.exit_codes = {}

    def machine_guid(self):
        return self.guid

    def current_user_sid(self):
        return self.sid

    def boot_marker(self):
        return "win-boot:1234"

    def open_process(self, pid):
        self.calls.append(("open_process", pid))
        return f"handle-{pid}"

    def process_times(self, handle):
        return self.process_times_by_handle.get(handle, (100, 0))

    def process_exit_code(self, handle):
        return self.exit_codes.get(handle, 259)

    def close_handle(self, handle):
        self.calls.append(("close_handle", handle))

    def is_reparse_point(self, path):
        self.calls.append(("is_reparse_point", str(path)))
        return self.reparse

    def set_private_dacl(self, path, sid):
        self.calls.append(("set_private_dacl", str(path), sid))

    def read_owner_and_dacl(self, path):
        self.calls.append(("read_owner_and_dacl", str(path)))
        return {"owner": self.owner, "protected": self.protected, "entries": self.entries}

    def open_directory(self, path):
        self.calls.append(("open_directory", str(path)))
        return f"dir-handle-{path}"

    def flush_file_buffers(self, handle):
        self.calls.append(("flush_file_buffers", handle))


class LinuxHostCompatibilityTests(unittest.TestCase):
    def test_linux_module_wrappers_preserve_identity_and_liveness(self):
        host = LinuxHost()
        self.assertEqual(host.host_identity(), store_module.host_identity())
        self.assertTrue(store_module.boot_id())
        self.assertTrue(store_module.pid_namespace())
        info = host.proc_identity(os.getpid())
        self.assertIsNotNone(info)
        self.assertNotIn(info["state"], ("Z", "X"))

    def test_new_config_records_platform_and_principal(self):
        with tempfile.TemporaryDirectory() as root:
            project = Path(root) / "project"
            project.mkdir()
            state = Path(root) / "state"
            config = Store.initialize(state, sys.executable, [project])
            self.assertEqual(config["platform"], "linux")
            self.assertEqual(config["principal_id"], f"uid:{os.getuid()}")
            self.assertEqual(Store(state).config["platform"], "linux")

    def test_legacy_linux_config_without_portable_identity_still_opens(self):
        with tempfile.TemporaryDirectory() as root:
            project = Path(root) / "project"
            project.mkdir()
            state = Path(root) / "state"
            Store.initialize(state, sys.executable, [project])
            config_path = state / "config.json"
            config = json.loads(config_path.read_text())
            config.pop("platform")
            config.pop("principal_id")
            write_json(config_path, config)
            self.assertNotIn("platform", Store(state).config)


class WindowsHostTests(unittest.TestCase):
    def test_identity_and_process_creation_time_are_stable(self):
        api = FakeWinApi(sid="S-1-5-21-9", guid="abc-guid")
        api.process_times_by_handle["handle-42"] = (5, 3)
        host = WindowsHost(api=api)
        info = host.proc_identity(42)
        self.assertEqual(host.principal_identity(), "S-1-5-21-9")
        self.assertEqual(len(host.host_identity()), 64)
        self.assertEqual(info["start"], "0000000300000005")
        self.assertTrue(host.alive(42, info["start"], host.boot_id()))

    def test_dead_or_reused_process_is_not_alive(self):
        api = FakeWinApi()
        api.exit_codes["handle-7"] = 0
        host = WindowsHost(api=api)
        self.assertFalse(host.alive(7, "0000000000000064", host.boot_id()))

    def test_existing_reparse_point_is_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "state"
            path.mkdir()
            with self.assertRaises(HostError) as raised:
                WindowsHost(api=FakeWinApi(reparse=True)).private_dir(path)
            self.assertEqual(raised.exception.code, "unsafe_state")

    def test_private_dacl_requires_owner_protection_and_known_full_access_aces(self):
        with tempfile.TemporaryDirectory() as root:
            entry = Path(root) / "state"
            entry.mkdir()
            WindowsHost(api=FakeWinApi()).verify_private_entry(entry)
            cases = (
                FakeWinApi(owner="S-1-5-21-999"),
                FakeWinApi(protected=False),
                FakeWinApi(
                    entries=[
                        {"type": 0, "flags": 0, "mask": FILE_ALL_ACCESS, "sid": "S-1-1-0"}
                    ]
                ),
                FakeWinApi(
                    entries=[{"type": 1, "flags": 0, "mask": FILE_ALL_ACCESS, "sid": None}]
                ),
            )
            for api in cases:
                with self.subTest(api=api), self.assertRaises(HostError):
                    WindowsHost(api=api).verify_private_entry(entry)

    def test_atomic_json_replace_applies_acl_and_flushes_parent(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "config.json"
            api = FakeWinApi()
            WindowsHost(api=api).write_json(path, {"answer": 42})
            self.assertEqual(json.loads(path.read_text()), {"answer": 42})
            self.assertEqual(
                [call[0] for call in api.calls],
                [
                    "is_reparse_point",
                    "set_private_dacl",
                    "open_directory",
                    "flush_file_buffers",
                    "close_handle",
                ],
            )

    def test_windows_state_requires_portable_principal_fields(self):
        fake = WindowsHost(api=FakeWinApi())
        with (
            mock.patch.object(host_module, "HOST", fake),
            mock.patch.object(store_module.os, "name", "nt"),
            mock.patch.object(store_module, "host_identity", lambda: "same-host"),
        ):
            with self.assertRaises(ControlError) as raised:
                store_module.check_host_and_principal({"host_id": "same-host"})
            self.assertEqual(raised.exception.code, "host_mismatch")
            store_module.check_host_and_principal(
                {
                    "host_id": "same-host",
                    "platform": "windows",
                    "principal_id": fake.principal_identity(),
                }
            )


if __name__ == "__main__":
    unittest.main()
