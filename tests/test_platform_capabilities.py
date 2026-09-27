"""Platform capability and default-path behavior."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/claude-control/scripts"))

from claude_control import cli, monitor, workspace_sandbox  # noqa: E402
from claude_control.platform import (  # noqa: E402
    capability_report,
    default_state_dir,
    operator_leases,
)
from claude_control.store import ControlError  # noqa: E402


class CapabilityTests(unittest.TestCase):
    @staticmethod
    def report(os_name, sys_platform, modules=(), programs=None, environ=None):
        programs = {} if programs is None else programs
        return capability_report(
            os_name,
            sys_platform,
            lambda name: name in modules,
            programs.get,
            {} if environ is None else environ,
        )

    def test_linux_reports_independent_optional_features(self):
        report = self.report("posix", "linux", {"curses"}, {"bwrap": "/usr/bin/bwrap"})

        self.assertEqual(report["platform"], "linux")
        self.assertFalse(report["wsl"])
        self.assertTrue(report["capabilities"]["session_control"]["supported"])
        self.assertTrue(report["capabilities"]["tui"]["supported"])
        self.assertTrue(report["capabilities"]["workspace"]["supported"])

    def test_linux_does_not_make_sessions_depend_on_bubblewrap(self):
        report = self.report("posix", "linux")

        self.assertTrue(report["capabilities"]["session_control"]["supported"])
        self.assertFalse(report["capabilities"]["workspace"]["supported"])
        self.assertFalse(report["capabilities"]["sandbox"]["supported"])

    def test_wsl_is_linux_with_an_explicit_marker(self):
        report = self.report("posix", "linux", environ={"WSL_DISTRO_NAME": "Ubuntu"})

        self.assertEqual(report["platform"], "linux")
        self.assertTrue(report["wsl"])

    def test_native_windows_reports_only_the_backends_that_have_landed(self):
        report = self.report("nt", "win32")

        self.assertEqual(report["platform"], "windows")
        self.assertTrue(report["capabilities"]["provider_usage"]["supported"])
        self.assertTrue(report["capabilities"]["tui"]["supported"])
        for name in ("session_control", "workspace", "sandbox"):
            self.assertFalse(report["capabilities"][name]["supported"])
            self.assertTrue(report["capabilities"][name]["reason"])

    def test_native_windows_enables_sessions_only_for_a_verified_helper(self):
        report = capability_report(
            "nt",
            "win32",
            lambda _name: False,
            lambda _name: None,
            {},
            helper_probe=lambda _env: (True, "windows-job-object", None, object()),
            wsl_probe=lambda: {
                "ready": False,
                "backend": None,
                "reason": "WSL unavailable",
            },
        )

        self.assertTrue(report["capabilities"]["session_control"]["supported"])
        self.assertEqual(
            report["capabilities"]["session_control"]["backend"], "windows-job-object"
        )

    def test_native_windows_enables_workspace_only_after_live_wsl_probe(self):
        report = capability_report(
            "nt",
            "win32",
            lambda _name: False,
            lambda _name: None,
            {},
            helper_probe=lambda _env: (True, "windows-job-object", None, object()),
            wsl_probe=lambda: {
                "ready": True,
                "backend": "wsl2-bubblewrap",
                "reason": None,
                "distro": "Ubuntu",
            },
        )

        self.assertTrue(report["capabilities"]["workspace"]["supported"])
        self.assertTrue(report["capabilities"]["sandbox"]["supported"])

    def test_macos_supports_local_control_but_fails_closed_for_workspace(self):
        report = self.report(
            "posix",
            "darwin",
            {"curses"},
            {"ps": "/bin/ps", "sysctl": "/usr/sbin/sysctl", "ioreg": "/usr/sbin/ioreg"},
        )

        self.assertEqual(report["platform"], "macos")
        self.assertTrue(report["capabilities"]["provider_usage"]["supported"])
        self.assertTrue(report["capabilities"]["tui"]["supported"])
        self.assertTrue(report["capabilities"]["session_control"]["supported"])
        self.assertFalse(report["capabilities"]["workspace"]["supported"])
        self.assertFalse(report["capabilities"]["sandbox"]["supported"])
        self.assertIn("fail-closed", report["capabilities"]["workspace"]["reason"])

    def test_macos_session_control_requires_base_system_process_tools(self):
        report = self.report("posix", "darwin", {"curses"}, {"ps": "/bin/ps"})

        self.assertFalse(report["capabilities"]["session_control"]["supported"])
        self.assertIn("sysctl", report["capabilities"]["session_control"]["reason"])

    def test_macos_finds_base_tools_by_absolute_path_when_gui_path_is_minimal(self):
        report = self.report(
            "posix",
            "darwin",
            {"curses"},
            {
                "/bin/ps": "/bin/ps",
                "/usr/sbin/sysctl": "/usr/sbin/sysctl",
                "/usr/sbin/ioreg": "/usr/sbin/ioreg",
            },
        )

        self.assertTrue(report["capabilities"]["session_control"]["supported"])

    def test_macos_workspace_probe_never_enters_linux_bubblewrap_path(self):
        with (
            mock.patch.object(workspace_sandbox.os, "name", "posix"),
            mock.patch.object(workspace_sandbox.sys, "platform", "darwin"),
        ):
            report = workspace_sandbox.probe()
            with self.assertRaises(ControlError) as caught:
                workspace_sandbox.require()

        self.assertFalse(report["ready"])
        self.assertIsNone(report["backend"])
        self.assertEqual(caught.exception.code, "sandbox_unavailable")


class DefaultPathTests(unittest.TestCase):
    def test_posix_preserves_xdg_and_home_fallbacks(self):
        self.assertEqual(
            default_state_dir(
                {"XDG_STATE_HOME": "/state"}, "/home/user", "posix", "linux"
            ),
            Path("/state/claude-control"),
        )
        self.assertEqual(
            default_state_dir({}, "/home/user", "posix", "linux"),
            Path("/home/user/.local/state/claude-control"),
        )

    def test_macos_uses_application_support_without_xdg_override(self):
        self.assertEqual(
            default_state_dir({}, "/Users/test", "posix", "darwin"),
            Path("/Users/test/Library/Application Support/codex-control-claude/state"),
        )
        self.assertEqual(
            default_state_dir({"XDG_STATE_HOME": "/state"}, "/Users/test", "posix", "darwin"),
            Path("/state/claude-control"),
        )

    def test_windows_prefers_localappdata_and_supports_profile_fallback(self):
        self.assertEqual(
            default_state_dir({"LOCALAPPDATA": "C:/Local"}, os_name="nt"),
            Path("C:/Local/codex-control-claude/state"),
        )
        self.assertEqual(
            default_state_dir({"USERPROFILE": "C:/Users/test"}, os_name="nt"),
            Path("C:/Users/test/AppData/Local/codex-control-claude/state"),
        )

    def test_windows_requires_a_user_local_base(self):
        with self.assertRaises(ValueError):
            default_state_dir({}, os_name="nt")

    def test_operator_lease_roots_are_store_independent_and_user_local(self):
        with mock.patch.object(operator_leases, "private_dir", side_effect=Path):
            self.assertEqual(
                operator_leases.root({"LOCALAPPDATA": "C:/Local"}, "nt"),
                Path("C:/Local/codex-control-claude/operator-leases"),
            )
            posix = operator_leases.root({}, "posix")
        self.assertEqual(posix.parent, Path("/tmp"))
        self.assertIn("operator-leases", posix.name)


class CliAndMonitorTests(unittest.TestCase):
    def test_doctor_accepts_explicit_platform_and_json_flags(self):
        args = cli.parser().parse_args(["doctor", "--platform", "--json"])

        self.assertTrue(args.platform)
        self.assertTrue(args.json)

    def test_tui_reports_missing_curses_without_import_failure(self):
        with mock.patch.object(monitor, "curses", None):
            with self.assertRaises(ControlError) as caught:
                monitor.run_tui(None)

        self.assertEqual(caught.exception.code, "tui_unavailable")


if __name__ == "__main__":
    unittest.main()
