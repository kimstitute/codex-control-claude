"""Security and canonicalization contract for explicit workspace policies."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/claude-control/scripts"))

from claude_control import workspace_policy  # noqa: E402
from claude_control.store import ControlError  # noqa: E402


class WorkspacePolicyTests(unittest.TestCase):
    def policy(self, **changes) -> dict:
        value = {
            "version": 1,
            "role": "executor",
            "read_paths": ["src/", "README.md"],
            "write_paths": ["src/output.txt"],
            "checks": {"unit": {"argv": ["/usr/bin/python3", "-m", "unittest"], "timeout": 30}},
        }
        value.update(changes)
        return value

    def assert_error(self, code: str, call, *args, **kwargs) -> None:
        with self.assertRaises(ControlError) as raised:
            call(*args, **kwargs)
        self.assertEqual(raised.exception.code, code)

    def test_normalize_returns_canonical_fresh_policy_with_defaults(self) -> None:
        original = self.policy(
            read_paths=["z.txt", "src/", "a.txt"],
            write_paths=["src/z.txt", "a.txt"],
            checks={
                "z_check": {"argv": ["/usr/bin/true"], "timeout": 1},
                "a_check": {"argv": ["/usr/bin/python3", "-V"], "timeout": 2},
            },
        )

        normalized = workspace_policy.normalize_policy(original)

        self.assertEqual(normalized["read_paths"], ["a.txt", "src/", "z.txt"])
        self.assertEqual(normalized["write_paths"], ["a.txt", "src/z.txt"])
        self.assertEqual(list(normalized["checks"]), ["a_check", "z_check"])
        self.assertEqual((normalized["max_actions"], normalized["max_calls"]), (32, 8))
        self.assertIsNot(normalized, original)
        self.assertIsNot(
            normalized["checks"]["a_check"]["argv"], original["checks"]["a_check"]["argv"]
        )

    def test_normalize_rejects_invalid_shape_enums_and_boolean_limits(self) -> None:
        cases = {
            "not_object": [],
            "unknown_field": self.policy(extra=True),
            "wrong_version": self.policy(version=2),
            "wrong_role": self.policy(role="planner"),
            "boolean_actions": self.policy(max_actions=True),
            "boolean_calls": self.policy(max_calls=False),
            "too_many_reads": self.policy(read_paths=[f"f{i}" for i in range(65)]),
        }
        for name, value in cases.items():
            with self.subTest(name=name):
                self.assert_error(
                    "invalid_workspace_policy", workspace_policy.normalize_policy, value
                )

    def test_path_rejects_ambiguous_or_escaping_syntax(self) -> None:
        invalid = (
            "",
            "/absolute",
            "./file",
            "a/../file",
            "a//file",
            "a\\file",
            "a\x00file",
            "*.py",
            "file?",
            "name[0]",
            "a" * 241,
        )
        for value in invalid:
            with self.subTest(value=value):
                self.assert_error("invalid_workspace_policy", workspace_policy.path, value)
        self.assert_error("invalid_workspace_policy", workspace_policy.path, "src/")
        self.assertEqual(workspace_policy.path("src/", allow_directory=True), "src/")

    def test_protected_paths_and_secret_files_are_never_authorized(self) -> None:
        protected = (
            ".git/config",
            "nested/.claude/settings.json",
            ".codex/config.toml",
            "a/.omx/state",
            ".agents/prompt.md",
            ".env",
            ".env.local",
            "cert.pem",
            "private.key",
            "credentials.json",
            "nested/auth.json",
        )
        for relative in protected:
            with self.subTest(relative=relative):
                self.assert_error(
                    "workspace_path_denied",
                    workspace_policy.check_access,
                    self.policy(read_paths=["."], write_paths=[]),
                    relative,
                )

    def test_read_authorization_distinguishes_exact_files_prefixes_and_read_all(self) -> None:
        self.assertTrue(workspace_policy.permits(["README.md"], "README.md"))
        self.assertFalse(workspace_policy.permits(["README.md"], "README.md.bak"))
        self.assertTrue(workspace_policy.permits(["src/"], "src/pkg/module.py"))
        self.assertFalse(workspace_policy.permits(["src/"], "src2/module.py"))
        self.assertTrue(workspace_policy.permits(["."], "any/deep/file.txt"))

    def test_write_paths_must_be_exact_and_covered_by_read_authority(self) -> None:
        invalid = {
            "unread_write": self.policy(read_paths=["src/input.txt"]),
            "directory_write": self.policy(read_paths=["src/"], write_paths=["src/"], checks={}),
            "duplicate_read": self.policy(read_paths=["src/", "src/"]),
            "duplicate_write": self.policy(write_paths=["src/output.txt", "src/output.txt"]),
        }
        for name, value in invalid.items():
            with self.subTest(name=name):
                self.assert_error(
                    "invalid_workspace_policy", workspace_policy.normalize_policy, value
                )

    def test_verifier_policy_cannot_write_or_execute_checks(self) -> None:
        for name, value in {
            "write": self.policy(role="verifier", checks={}),
            "check": self.policy(role="verifier", write_paths=[]),
        }.items():
            with self.subTest(name=name):
                self.assert_error(
                    "invalid_workspace_policy", workspace_policy.normalize_policy, value
                )
        normalized = workspace_policy.normalize_policy(
            self.policy(role="verifier", write_paths=[], checks={})
        )
        self.assertEqual((normalized["write_paths"], normalized["checks"]), ([], {}))

    def test_check_allowlist_rejects_unsafe_names_programs_and_arguments(self) -> None:
        cases = {
            "bad_name": {"Bad": {"argv": ["/usr/bin/true"], "timeout": 1}},
            "relative_program": {"x": {"argv": ["python3"], "timeout": 1}},
            "nested_program": {"x": {"argv": ["/usr/bin/sub/python3"], "timeout": 1}},
            "dot_program": {"x": {"argv": ["/usr/bin/.."], "timeout": 1}},
            "empty_argv": {"x": {"argv": [], "timeout": 1}},
            "nul_argument": {"x": {"argv": ["/usr/bin/echo", "a\x00b"], "timeout": 1}},
            "long_argument": {"x": {"argv": ["/usr/bin/echo", "x" * 1025], "timeout": 1}},
            "boolean_timeout": {"x": {"argv": ["/usr/bin/true"], "timeout": True}},
            "too_many_checks": {
                f"c{i}": {"argv": ["/usr/bin/true"], "timeout": 1} for i in range(9)
            },
        }
        for name, checks in cases.items():
            with self.subTest(name=name):
                self.assert_error(
                    "invalid_workspace_policy",
                    workspace_policy.normalize_policy,
                    self.policy(checks=checks),
                )

    def test_check_access_enforces_read_and_write_permissions(self) -> None:
        policy = workspace_policy.normalize_policy(self.policy())

        self.assertEqual(workspace_policy.check_access(policy, "src/input.txt"), "src/input.txt")
        self.assertEqual(
            workspace_policy.check_access(policy, "src/output.txt", write=True),
            "src/output.txt",
        )
        self.assert_error(
            "workspace_path_denied", workspace_policy.check_access, policy, "other.txt"
        )
        self.assert_error(
            "workspace_path_denied",
            workspace_policy.check_access,
            policy,
            "src/another.txt",
            write=True,
        )
        self.assert_error(
            "workspace_path_denied", workspace_policy.check_access, policy, "../escape"
        )

    def test_path_rejects_lone_surrogate_code_points(self) -> None:
        high = chr(0xD800)
        low = chr(0xDFFF)
        surrogateescape = chr(0xDC80)
        surrogates = (
            f"src/{high}.py",
            f"src/{low}.py",
            f"src/{surrogateescape}.py",
            f"a{high}b{low}c",
        )
        for value in surrogates:
            with self.subTest(value=repr(value)):
                self.assert_error("invalid_workspace_policy", workspace_policy.path, value)

    def test_normalize_and_access_reject_surrogate_paths(self) -> None:
        for codepoint in (0xD800, 0xDFFF, 0xDC80):
            bad = f"src/{chr(codepoint)}.py"
            with self.subTest(codepoint=codepoint):
                for changes in (
                    {"read_paths": [bad], "write_paths": []},
                    {"read_paths": [f"src/{chr(codepoint)}/"], "write_paths": []},
                    {"write_paths": [bad]},
                ):
                    self.assert_error(
                        "invalid_workspace_policy",
                        workspace_policy.normalize_policy,
                        self.policy(**changes),
                    )
                self.assertFalse(workspace_policy.permits(["."], bad))
                self.assert_error(
                    "workspace_path_denied",
                    workspace_policy.check_access,
                    self.policy(read_paths=["."], write_paths=[]),
                    bad,
                )

    def test_check_argv_rejects_surrogate_arguments(self) -> None:
        for codepoint in (0xD800, 0xDC00, 0xDC80, 0xDFFF):
            for argv in (["/usr/bin/echo", chr(codepoint)], ["/usr/bin/" + chr(codepoint)]):
                with self.subTest(argv=repr(argv)):
                    self.assert_error(
                        "invalid_workspace_policy",
                        workspace_policy.normalize_policy,
                        self.policy(checks={"x": {"argv": argv, "timeout": 1}}),
                    )

    def test_valid_unicode_check_arguments_are_preserved(self) -> None:
        for value in ("한글", "café", "e\u0301", "\U0001f600"):
            with self.subTest(value=value):
                argv = ["/usr/bin/echo", value]
                policy = workspace_policy.normalize_policy(
                    self.policy(checks={"unit": {"argv": argv, "timeout": 1}})
                )
                self.assertEqual(policy["checks"]["unit"]["argv"], argv)

    def test_valid_unicode_paths_are_preserved(self) -> None:
        korean = chr(0xD55C) + chr(0xAE00) + chr(0xD30C) + chr(0xC77C)
        accented = "caf" + chr(0xE9)
        combining = "e" + chr(0x301)
        emoji = chr(0x1F600)
        unicode_paths = (
            f"src/{korean}.txt",
            f"src/{accented}.txt",
            f"src/{combining}.txt",
            f"src/{emoji}.txt",
        )
        for value in unicode_paths:
            with self.subTest(value=repr(value)):
                self.assertEqual(workspace_policy.path(value), value)
        write_path = f"src/{korean}.txt"
        normalized = workspace_policy.normalize_policy(
            self.policy(read_paths=["src/", "README.md"], write_paths=[write_path])
        )
        self.assertIn(write_path, normalized["write_paths"])


if __name__ == "__main__":
    unittest.main()
