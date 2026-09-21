"""Unit tests for execution_settings validation and build_argv integration."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = PROJECT_ROOT / "plugins" / "claude-control" / "scripts"
sys.path.insert(0, str(PACKAGE_ROOT))

from claude_control.execution_settings import EFFORTS, validate_effort  # noqa: E402
from claude_control.protocol import build_argv  # noqa: E402

SESSION_ID = "00000000-0000-4000-8000-000000000001"


class ValidateEffortTests(unittest.TestCase):
    def test_none_returns_none(self) -> None:
        self.assertIsNone(validate_effort(None))

    def test_each_supported_level_returned_unchanged(self) -> None:
        for level in EFFORTS:
            self.assertEqual(validate_effort(level), level)

    def test_rejects_unknown_string(self) -> None:
        with self.assertRaises(ValueError):
            validate_effort("ultra")

    def test_rejects_empty_string(self) -> None:
        with self.assertRaises(ValueError):
            validate_effort("")

    def test_rejects_bool(self) -> None:
        with self.assertRaises(ValueError):
            validate_effort(True)

    def test_rejects_list_and_dict(self) -> None:
        with self.assertRaises(ValueError):
            validate_effort(["high"])
        with self.assertRaises(ValueError):
            validate_effort({"level": "high"})


class BuildArgvEffortTests(unittest.TestCase):
    def test_legacy_argv_unchanged_when_effort_none(self) -> None:
        argv = build_argv("claude", "sonnet", SESSION_ID)
        self.assertNotIn("--effort", argv)

    def test_start_session_emits_single_effort_pair(self) -> None:
        for level in EFFORTS:
            argv = build_argv("claude", "sonnet", SESSION_ID, effort=level)
            self.assertEqual(argv.count("--effort"), 1)
            idx = argv.index("--effort")
            self.assertEqual(argv[idx + 1], level)

    def test_resume_session_emits_single_effort_pair(self) -> None:
        for level in EFFORTS:
            argv = build_argv("claude", "sonnet", SESSION_ID, True, effort=level)
            self.assertEqual(argv.count("--effort"), 1)
            idx = argv.index("--effort")
            self.assertEqual(argv[idx + 1], level)
            self.assertIn("--resume", argv)

    def test_invalid_effort_raises_in_build_argv(self) -> None:
        with self.assertRaises(ValueError):
            build_argv("claude", "sonnet", SESSION_ID, effort="bogus")


if __name__ == "__main__":
    unittest.main()
