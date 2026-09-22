"""Platform capability and default-path behavior."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/claude-control/scripts"))

from claude_control import cli, monitor  # noqa: E402
from claude_control.platform import capability_report, default_state_dir  # noqa: E402
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


class DefaultPathTests(unittest.TestCase):
    def test_posix_preserves_xdg_and_home_fallbacks(self):
        self.assertEqual(
            default_state_dir({"XDG_STATE_HOME": "/state"}, "/home/user", "posix"),
            Path("/state/claude-control"),
        )
        self.assertEqual(
            default_state_dir({}, "/home/user", "posix"),
            Path("/home/user/.local/state/claude-control"),
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
