"""Process-level tests for the host-local Claude controller."""

from __future__ import annotations

import json
import os
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = PROJECT_ROOT / "plugins" / "claude-control" / "scripts"
CLI = SCRIPTS / "claude_control_cli.py"
FAKE = Path(__file__).with_name("fake_claude.py")
sys.path.insert(0, str(SCRIPTS))

from claude_control import runner  # noqa: E402
from claude_control.store import (  # noqa: E402
    ACTIVE,
    ControlError,
    Store,
    boot_id,
    pid_namespace,
    proc_identity,
)

TERMINAL = {"completed", "failed", "cancelled", "launch_failed", "interrupted"}


class EnvironmentSelectionTests(unittest.TestCase):
    def test_windows_child_environment_has_no_case_insensitive_duplicates(self) -> None:
        source = {
            "SYSTEMROOT": r"C:\Windows",
            "SystemRoot": r"C:\Windows",
            "COMSPEC": r"C:\Windows\System32\cmd.exe",
            "ComSpec": r"C:\Windows\System32\cmd.exe",
            "HTTPS_PROXY": "upper",
            "https_proxy": "lower",
        }
        with (
            mock.patch.object(runner.os, "name", "nt"),
            mock.patch.dict(runner.os.environ, source, clear=True),
        ):
            environment = runner.child_environment()

        folded = [name.casefold() for name in environment]
        self.assertEqual(len(folded), len(set(folded)))
        self.assertEqual(environment["SYSTEMROOT"], r"C:\Windows")
        self.assertEqual(environment["COMSPEC"], r"C:\Windows\System32\cmd.exe")
        self.assertEqual(environment["HTTPS_PROXY"], "upper")

    def test_non_windows_child_environment_preserves_distinct_case(self) -> None:
        source = {"HTTPS_PROXY": "upper", "https_proxy": "lower"}
        with (
            mock.patch.object(runner.os, "name", "posix"),
            mock.patch.dict(runner.os.environ, source, clear=True),
        ):
            environment = runner.child_environment()

        self.assertEqual(environment["HTTPS_PROXY"], "upper")
        self.assertEqual(environment["https_proxy"], "lower")


class ControllerTestCase(unittest.TestCase):
    max_parallel = 2

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="claude-control-test-")
        self.root = Path(self.temporary.name)
        self.state = self.root / "state"
        self.project = self.root / "projects" / "primary"
        self.project.mkdir(parents=True)
        initialized = self.cli(
            "init",
            "--claude-bin",
            str(FAKE),
            "--allow-root",
            str(self.root / "projects"),
            "--max-parallel",
            str(self.max_parallel),
        )
        self.assertEqual(initialized["max_parallel"], self.max_parallel)

    def tearDown(self) -> None:
        config_path = self.state / "config.json"
        if config_path.exists():
            config = json.loads(config_path.read_text(encoding="utf-8"))
            if hasattr(self, "original_host_id"):
                config["host_id"] = self.original_host_id
                config_path.write_text(json.dumps(config), encoding="utf-8")
                os.chmod(config_path, 0o600)
            try:
                listing = self.cli("list")
            except AssertionError:
                listing = {"runs": []}
            for run in listing.get("runs", []):
                if run["status"] in ACTIVE:
                    self.cli("stop", "--run", run["id"], expected=None)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                try:
                    runs = self.cli("list")["runs"]
                except AssertionError:
                    break
                if all(run["status"] not in ACTIVE or run["status"] == "unknown" for run in runs):
                    break
                time.sleep(0.1)
        self.temporary.cleanup()

    def cli(self, *arguments: str, expected: int | None = 0) -> dict:
        completed = subprocess.run(
            [sys.executable, str(CLI), "--state-dir", str(self.state), *arguments],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            self.fail(
                f"CLI emitted invalid JSON (exit {completed.returncode}): "
                f"stdout={completed.stdout!r} stderr={completed.stderr!r}: {exc}"
            )
        if expected is not None:
            self.assertEqual(
                completed.returncode,
                expected,
                f"stdout={completed.stdout!r} stderr={completed.stderr!r}",
            )
        payload["_exit_code"] = completed.returncode
        return payload

    def cli_no_output(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        completed = subprocess.run(
            [sys.executable, str(CLI), "--state-dir", str(self.state), *arguments],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        self.assertEqual(
            completed.returncode,
            0,
            f"stdout={completed.stdout!r} stderr={completed.stderr!r}",
        )
        self.assertEqual(completed.stdout, "")
        return completed

    def prompt(self, value: object, name: str | None = None) -> Path:
        path = self.root / (name or f"prompt-{uuid.uuid4()}.json")
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def start(
        self,
        name: str,
        prompt: object,
        request_id: str,
        *,
        model: str = "sonnet",
        timeout: float = 10,
        expected: int | None = 0,
        project: Path | None = None,
    ) -> dict:
        return self.cli(
            "start",
            "--name",
            name,
            "--model",
            model,
            "--role",
            "test",
            "--project",
            str(project or self.project),
            "--prompt-file",
            str(self.prompt(prompt)),
            "--request-id",
            request_id,
            "--timeout",
            str(timeout),
            expected=expected,
        )

    def followup(
        self,
        session_id: str,
        prompt: object,
        request_id: str,
        *,
        acknowledge: bool = False,
        expected: int | None = 0,
    ) -> dict:
        arguments = [
            "followup",
            "--session",
            session_id,
            "--prompt-file",
            str(self.prompt(prompt)),
            "--request-id",
            request_id,
            "--timeout",
            "10",
        ]
        if acknowledge:
            arguments.append("--acknowledge-context")
        return self.cli(*arguments, expected=expected)

    def wait_terminal(self, run_id: str, seconds: float = 8) -> dict:
        deadline = time.monotonic() + seconds
        last = None
        while time.monotonic() < deadline:
            last = self.cli("wait", "--run", run_id, "--seconds", "0.4")
            if last["status"] in TERMINAL or last["status"] == "unknown":
                return last
        self.fail(f"run {run_id} did not settle; last={last}")

    def result(self, run_id: str) -> dict:
        return self.cli("result", "--run", run_id)


class ControllerProcessTests(ControllerTestCase):
    def test_doctor_reports_fake_cli_and_sanitized_auth_ready(self) -> None:
        report = self.cli("doctor", "--auth")

        self.assertIs(report["ready"], True)
        self.assertEqual(report["claude_version"], "fake-claude 1.0")
        self.assertEqual(report["missing_options"], [])
        self.assertEqual(
            report["auth"],
            {
                "loggedIn": True,
                "authMethod": "fake",
                "apiProvider": "fixture",
                "subscriptionType": "test",
            },
        )

    def test_model_catalog_uses_sdk_initialize_and_removes_account_identity(self) -> None:
        report = self.cli("models", "catalog")

        self.assertEqual(report["contract"], "claude-control.model-catalog.v1")
        self.assertEqual(report["model_count"], 2)
        self.assertEqual(
            report["models"][0],
            {
                "selector": "claude-fable-test[1m]",
                "resolved_model": "claude-fable-test",
                "display_name": "Fable Test",
                "description": "Fixture frontier model",
                "role_settings_compatible": True,
                "supports_effort": True,
                "supported_effort_levels": ["low", "high", "max"],
                "configurable_effort_levels": ["low", "high", "max"],
                "supports_adaptive_thinking": True,
                "supports_fast_mode": False,
                "supports_auto_mode": True,
            },
        )
        serialized = json.dumps(report)
        self.assertNotIn("private@example.test", serialized)
        self.assertNotIn("Private Fixture Org", serialized)

    def test_parallel_sessions_preserve_distinct_nonce_results(self) -> None:
        first = self.start("parallel-a", {"text": "nonce-a"}, "parallel-a")
        second = self.start("parallel-b", {"text": "nonce-b"}, "parallel-b")

        first_done = self.wait_terminal(first["id"])
        second_done = self.wait_terminal(second["id"])

        self.assertEqual(first_done["status"], "completed")
        self.assertEqual(second_done["status"], "completed")
        self.assertEqual(self.result(first["id"])["result"]["response"], "nonce-a")
        self.assertEqual(self.result(second["id"])["result"]["response"], "nonce-b")
        self.assertNotEqual(first["session_id"], second["session_id"])

    def test_simultaneous_identical_request_deduplicates_to_one_run(self) -> None:
        prompt = self.prompt({"text": "same"}, "same.json")
        command = [
            sys.executable,
            str(CLI),
            "--state-dir",
            str(self.state),
            "start",
            "--name",
            "dedup",
            "--model",
            "sonnet",
            "--role",
            "test",
            "--project",
            str(self.project),
            "--prompt-file",
            str(prompt),
            "--request-id",
            "same-request",
        ]
        processes = [subprocess.Popen(command, stdout=subprocess.PIPE, text=True) for _ in range(2)]
        payloads = [json.loads(process.communicate(timeout=10)[0]) for process in processes]

        self.assertEqual({process.returncode for process in processes}, {0})
        self.assertEqual(len({payload["id"] for payload in payloads}), 1)
        self.assertEqual(sorted(payload["deduplicated"] for payload in payloads), [False, True])

    def test_reusing_request_id_for_different_input_is_rejected(self) -> None:
        created = self.start("conflict", {"text": "first"}, "conflict-id")
        self.wait_terminal(created["id"])

        rejected = self.start("other-name", {"text": "second"}, "conflict-id", expected=2)

        self.assertEqual(rejected["error"], "request_conflict")

    def test_session_reference_is_uuid_only_despite_name_collision(self) -> None:
        first = self.start("ordinary-name", {"remember": "first-session"}, "uuid-first")
        self.assertEqual(self.wait_terminal(first["id"])["status"], "completed")
        second = self.start(first["session_id"], {"remember": "second-session"}, "uuid-second")
        self.assertEqual(self.wait_terminal(second["id"])["status"], "completed")

        recalled = self.followup(first["session_id"], {"recall": True}, "uuid-recall")
        name_lookup = self.followup("ordinary-name", {"recall": True}, "name-lookup", expected=2)

        self.assertEqual(self.wait_terminal(recalled["id"])["status"], "completed")
        self.assertEqual(self.result(recalled["id"])["result"]["response"], "first-session")
        self.assertEqual(name_lookup["error"], "invalid_session")

    def test_capacity_and_busy_session_are_enforced(self) -> None:
        active = self.start("busy", {"behavior": "sleep", "sleep_seconds": 5}, "busy-start")
        second = self.start("also-busy", {"behavior": "sleep", "sleep_seconds": 5}, "busy-second")

        capacity = self.start("overflow", {"text": "no"}, "overflow", expected=2)
        busy = self.followup(active["session_id"], {"text": "no"}, "busy-follow", expected=2)

        self.assertEqual(capacity["error"], "capacity")
        self.assertEqual(busy["error"], "session_busy")
        self.cli("stop", "--run", active["id"])
        self.cli("stop", "--run", second["id"])
        self.assertEqual(self.wait_terminal(active["id"])["status"], "cancelled")
        self.assertEqual(self.wait_terminal(second["id"])["status"], "cancelled")

    def test_targeted_cancel_does_not_stop_another_session(self) -> None:
        target = self.start(
            "cancel-target", {"behavior": "sleep", "sleep_seconds": 5}, "cancel-target"
        )
        survivor = self.start("survivor", {"text": "survived"}, "survivor")

        self.cli("stop", "--run", target["id"])
        target_done = self.wait_terminal(target["id"])
        survivor_done = self.wait_terminal(survivor["id"])

        self.assertEqual(target_done["status"], "cancelled")
        self.assertEqual(survivor_done["status"], "completed")
        self.assertEqual(self.result(survivor["id"])["result"]["response"], "survived")

    def test_timeout_fails_only_the_timed_run(self) -> None:
        run = self.start("timeout", {"behavior": "sleep", "sleep_seconds": 5}, "timeout", timeout=1)

        done = self.wait_terminal(run["id"])

        self.assertEqual(done["status"], "failed")
        self.assertEqual(done["reason"], "timeout")

    def test_missing_or_tampered_result_demotes_completed_run(self) -> None:
        missing = self.start("missing-result-file", {"text": "first"}, "integrity-missing")
        tampered = self.start("tampered-result-file", {"text": "second"}, "integrity-tampered")
        self.assertEqual(self.wait_terminal(missing["id"])["status"], "completed")
        self.assertEqual(self.wait_terminal(tampered["id"])["status"], "completed")

        missing_path = self.state / "runs" / missing["id"] / "result.json"
        tampered_path = self.state / "runs" / tampered["id"] / "result.json"
        missing_path.unlink()
        tampered_value = json.loads(tampered_path.read_text(encoding="utf-8"))
        tampered_value["response"] = "altered"
        tampered_path.write_text(json.dumps(tampered_value), encoding="utf-8")

        missing_checked = self.cli("result", "--run", missing["id"])
        tampered_checked = self.cli("result", "--run", tampered["id"])

        self.assertEqual(missing_checked["run"]["status"], "failed")
        self.assertEqual(missing_checked["run"]["reason"], "result_integrity")
        self.assertIsNone(missing_checked["result"])
        self.assertEqual(tampered_checked["run"]["status"], "failed")
        self.assertEqual(tampered_checked["run"]["reason"], "result_integrity")

    def test_tampered_completed_result_blocks_followup_without_prior_status_read(self) -> None:
        completed = self.start(
            "tampered-admission", {"text": "trusted"}, "tampered-admission-start"
        )
        self.assertEqual(self.wait_terminal(completed["id"])["status"], "completed")
        result_path = self.state / "runs" / completed["id"] / "result.json"
        result = json.loads(result_path.read_text(encoding="utf-8"))
        result["response"] = "tampered-before-followup"
        result_path.write_text(json.dumps(result), encoding="utf-8")

        rejected = self.followup(
            completed["session_id"],
            {"text": "must not resume"},
            "tampered-admission-followup",
            expected=2,
        )

        self.assertEqual(rejected["error"], "context_uncertain")
        checked = self.cli("status", "--run", completed["id"])
        self.assertEqual(checked["status"], "failed")
        self.assertEqual(checked["reason"], "result_integrity")

    def test_model_mismatch_error_and_missing_result_fail_closed(self) -> None:
        cases = (
            ("bad-model", "mismatch_model"),
            ("error-result", "error"),
            ("no-result", "no_result"),
        )
        for name, behavior in cases:
            with self.subTest(behavior=behavior):
                run = self.start(name, {"behavior": behavior}, name)
                done = self.wait_terminal(run["id"])
                self.assertEqual(done["status"], "failed")
                self.assertTrue(self.result(run["id"])["result"]["errors"])

    def test_failed_turn_requires_explicit_context_acknowledgement(self) -> None:
        failed = self.start("context", {"behavior": "error"}, "context-start")
        self.assertEqual(self.wait_terminal(failed["id"])["status"], "failed")

        rejected = self.followup(
            failed["session_id"], {"text": "continue"}, "context-no-ack", expected=2
        )
        accepted = self.followup(
            failed["session_id"],
            {"text": "continued"},
            "context-ack",
            acknowledge=True,
        )

        self.assertEqual(rejected["error"], "context_uncertain")
        self.assertEqual(self.wait_terminal(accepted["id"])["status"], "completed")

    def test_restart_rotates_backend_after_cancelled_before_claim(self) -> None:
        store = Store(self.state)
        cancelled_id, created = store.reserve(
            prompt=json.dumps({"remember": "old-context"}),
            request_id="restart-cancelled-original",
            timeout=10,
            name="restart-after-cancel",
            model="sonnet",
            role="test",
            project=str(self.project),
        )
        self.assertTrue(created)
        cancelled = store.stop(cancelled_id)
        managed_session = cancelled["session_id"]
        original_backend = cancelled["backend_id"]
        prompt = self.prompt({"remember": "new-context"}, "restart.json")

        missing_ack = self.cli(
            "restart",
            "--session",
            managed_session,
            "--prompt-file",
            str(prompt),
            "--request-id",
            "restart-without-ack",
            expected=2,
        )
        restarted = self.cli(
            "restart",
            "--session",
            managed_session,
            "--acknowledge-context",
            "--prompt-file",
            str(prompt),
            "--request-id",
            "restart-with-ack",
            "--timeout",
            "10",
        )
        retried = self.cli(
            "restart",
            "--session",
            managed_session,
            "--acknowledge-context",
            "--prompt-file",
            str(prompt),
            "--request-id",
            "restart-with-ack",
            "--timeout",
            "10",
        )

        self.assertEqual(missing_ack["error"], "context_uncertain")
        self.assertNotEqual(restarted["backend_id"], original_backend)
        self.assertEqual(restarted["session_id"], managed_session)
        self.assertEqual(restarted["resume"], 0)
        self.assertEqual(retried["id"], restarted["id"])
        self.assertEqual(retried["backend_id"], restarted["backend_id"])
        self.assertIs(retried["deduplicated"], True)
        self.assertEqual(self.wait_terminal(restarted["id"])["status"], "completed")
        old_run = self.cli("status", "--run", cancelled_id)
        self.assertEqual(old_run["status"], "cancelled")
        self.assertEqual(old_run["reason"], "cancelled_before_claim")
        self.assertEqual(old_run["backend_id"], original_backend)

        resume_prompt = self.prompt({"recall": True}, "normal-resume.json")
        resumed = self.cli(
            "resume",
            "--session",
            managed_session,
            "--prompt-file",
            str(resume_prompt),
            "--request-id",
            "normal-resume-after-restart",
            "--timeout",
            "10",
        )
        self.assertEqual(resumed["backend_id"], restarted["backend_id"])
        self.assertEqual(resumed["resume"], 1)
        self.assertEqual(self.wait_terminal(resumed["id"])["status"], "completed")
        self.assertEqual(self.result(resumed["id"])["result"]["response"], "new-context")

    def test_restart_rejects_active_and_blocked_sessions(self) -> None:
        active = self.start(
            "restart-active",
            {"behavior": "sleep", "sleep_seconds": 5},
            "restart-active-start",
        )
        prompt = self.prompt({"text": "restart"}, "restart-rejected.json")
        active_rejected = self.cli(
            "restart",
            "--session",
            active["session_id"],
            "--acknowledge-context",
            "--prompt-file",
            str(prompt),
            "--request-id",
            "restart-active-rejected",
            expected=2,
        )
        self.assertEqual(active_rejected["error"], "session_busy")
        self.cli("stop", "--run", active["id"])
        self.assertEqual(self.wait_terminal(active["id"])["status"], "cancelled")

        blocked = self.start(
            "restart-blocked",
            {"behavior": "mismatch_session"},
            "restart-blocked-start",
        )
        self.assertEqual(self.wait_terminal(blocked["id"])["status"], "failed")
        blocked_rejected = self.cli(
            "restart",
            "--session",
            blocked["session_id"],
            "--acknowledge-context",
            "--prompt-file",
            str(prompt),
            "--request-id",
            "restart-blocked-rejected",
            expected=2,
        )
        self.assertEqual(blocked_rejected["error"], "session_busy")

    def test_session_mismatch_blocks_even_acknowledged_followup(self) -> None:
        divergent = self.start("divergent", {"behavior": "mismatch_session"}, "divergent-start")
        self.assertEqual(self.wait_terminal(divergent["id"])["status"], "failed")

        rejected = self.followup(
            divergent["session_id"],
            {"text": "unsafe"},
            "divergent-follow",
            acknowledge=True,
            expected=2,
        )

        self.assertEqual(rejected["error"], "session_busy")

    def test_host_mismatch_and_symlink_project_escape_fail_closed(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        escape = self.root / "projects" / "escape"
        escape.symlink_to(outside, target_is_directory=True)
        denied = self.start("escape", {"text": "no"}, "escape", project=escape, expected=2)
        self.assertEqual(denied["error"], "project_denied")

        config_path = self.state / "config.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        self.original_host_id = config["host_id"]
        config["host_id"] = "0" * 64
        config_path.write_text(json.dumps(config), encoding="utf-8")
        os.chmod(config_path, 0o600)

        rejected = self.cli("list", expected=2)
        self.assertEqual(rejected["error"], "host_mismatch")

    def test_killed_worker_becomes_unknown_and_is_not_relaunched(self) -> None:
        run = self.start(
            "killed-worker", {"behavior": "sleep", "sleep_seconds": 10}, "killed-worker"
        )
        deadline = time.monotonic() + 4
        row = None
        while time.monotonic() < deadline:
            row = self.cli("status", "--run", run["id"])
            if row["status"] == "running":
                break
            time.sleep(0.05)
        self.assertIsNotNone(row)
        self.assertEqual(row["status"], "running")
        worker_pid = row["worker_pid"]
        os.kill(worker_pid, signal.SIGKILL)

        unknown = self.wait_terminal(run["id"])

        self.assertEqual(unknown["status"], "unknown")
        self.assertEqual(self.cli("list")["runs"][0]["id"], run["id"])
        invocation = self.state / "runs" / run["id"] / "invocation.json"
        self.assertTrue(invocation.exists())

        deadline = time.monotonic() + 4
        reconciled = None
        while time.monotonic() < deadline:
            reconciled = self.cli("reconcile", "--run", run["id"], expected=None)
            if reconciled.get("status") == "interrupted":
                break
            time.sleep(0.1)
        self.assertEqual(reconciled.get("status"), "interrupted", reconciled)

    def test_foreign_pid_namespace_does_not_mark_active_run_dead(self) -> None:
        run = self.start("namespace", {"behavior": "sleep", "sleep_seconds": 5}, "namespace")
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            row = self.cli("status", "--run", run["id"])
            if row["status"] == "running":
                break
            time.sleep(0.05)
        self.assertEqual(row["status"], "running")
        with sqlite3.connect(self.state / "state.sqlite3") as database:
            database.execute(
                "UPDATE runs SET worker_pid=?,worker_start=?,worker_namespace=? WHERE id=?",
                (2_000_000_000, "missing", "pid:[foreign-test]", run["id"]),
            )

        observed = self.cli("status", "--run", run["id"])

        self.assertEqual(observed["status"], "running")
        self.cli("stop", "--run", run["id"])
        self.assertEqual(self.wait_terminal(run["id"])["status"], "cancelled")

    def test_late_exec_is_denied_after_cancellation_and_reconcile(self) -> None:
        store = Store(self.state)
        cancelled_id, _ = store.reserve(
            prompt=json.dumps({"text": "must not execute"}),
            request_id="late-cancelled",
            timeout=10,
            name="late-cancelled",
            model="sonnet",
            role="test",
            project=str(self.project),
        )
        parent_identity = proc_identity(os.getpid())
        with sqlite3.connect(self.state / "state.sqlite3") as database:
            database.execute(
                "UPDATE runs SET status='launching',cancel_requested=1,worker_pid=?,"
                "worker_start=?,worker_namespace=?,boot=? WHERE id=?",
                (
                    os.getpid(),
                    parent_identity["start"],
                    pid_namespace(),
                    boot_id(),
                    cancelled_id,
                ),
            )

        self.cli_no_output("_exec", "--run", cancelled_id)

        cancelled_row = self.cli("status", "--run", cancelled_id)
        self.assertIsNone(cancelled_row["child_pid"])
        with sqlite3.connect(self.state / "state.sqlite3") as database:
            database.execute(
                "UPDATE runs SET status='cancelled',finished=?,reason='test_cleanup' WHERE id=?",
                (time.time(), cancelled_id),
            )

        reconciled_id, _ = store.reserve(
            prompt=json.dumps({"text": "must not execute either"}),
            request_id="late-reconciled",
            timeout=10,
            name="late-reconciled",
            model="sonnet",
            role="test",
            project=str(self.project),
        )
        with sqlite3.connect(self.state / "state.sqlite3") as database:
            database.execute(
                "UPDATE runs SET status='unknown',worker_pid=?,worker_start=?,"
                "worker_namespace=?,boot=? WHERE id=?",
                (2_000_000_000, "missing", pid_namespace(), boot_id(), reconciled_id),
            )
        reconciled = self.cli("reconcile", "--run", reconciled_id)

        self.cli_no_output("_exec", "--run", reconciled_id)
        after = self.cli("status", "--run", reconciled_id)

        self.assertEqual(reconciled["status"], "interrupted")
        self.assertEqual(after["status"], "interrupted")
        self.assertIsNone(after["child_pid"])

    def test_failed_second_group_cleanup_quarantines_with_original_error(self) -> None:
        store = Store(self.state)
        run_id, _ = store.reserve(
            prompt=json.dumps({"text": "mock cleanup"}),
            request_id="mock-cleanup",
            timeout=10,
            name="mock-cleanup",
            model="sonnet",
            role="test",
            project=str(self.project),
        )
        fake_process = mock.Mock(pid=2_000_000_000)
        first_error = ControlError("first_cleanup", "first cleanup failed")
        second_error = ControlError("second_cleanup", "second cleanup failed")
        with (
            mock.patch.object(runner.signal, "signal"),
            mock.patch.object(runner.subprocess, "Popen", return_value=fake_process),
            mock.patch.object(runner, "child_exited", return_value=True),
            mock.patch.object(runner, "kill_group", side_effect=[first_error, second_error]),
        ):
            with self.assertRaisesRegex(ControlError, "first cleanup failed"):
                runner.run_worker(self.state, run_id)

        quarantined = self.cli("status", "--run", run_id)
        self.assertEqual(quarantined["status"], "unknown")
        self.assertIn("worker_error:ControlError:first cleanup failed", quarantined["reason"])

    def test_expired_pending_claim_rejects_late_worker(self) -> None:
        store = Store(self.state)
        run_id, created = store.reserve(
            prompt=json.dumps({"text": "never launch"}),
            request_id="expired-claim",
            timeout=10,
            name="expired-claim",
            model="sonnet",
            role="test",
            project=str(self.project),
        )
        self.assertTrue(created)
        with sqlite3.connect(self.state / "state.sqlite3") as database:
            database.execute("UPDATE runs SET created=? WHERE id=?", (time.time() - 60, run_id))

        expired = self.cli("status", "--run", run_id)
        late = self.cli("_worker", "--run", run_id)
        after = self.cli("status", "--run", run_id)

        self.assertEqual(expired["status"], "launch_failed")
        self.assertTrue(late["worker_finished"])
        self.assertEqual(after["status"], "launch_failed")
        self.assertIsNone(after["child_pid"])


class SingleCapacityTests(ControllerTestCase):
    max_parallel = 1

    def test_active_run_consumes_single_capacity_slot(self) -> None:
        active = self.start("only", {"behavior": "sleep", "sleep_seconds": 5}, "only-active")

        rejected = self.start("overflow", {"text": "no"}, "only-overflow", expected=2)

        self.assertEqual(rejected["error"], "capacity")
        self.cli("stop", "--run", active["id"])
        self.assertEqual(self.wait_terminal(active["id"])["status"], "cancelled")


if __name__ == "__main__":
    unittest.main()
