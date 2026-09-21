"""Durable launch-boundary tests for managed workspace checks."""

from __future__ import annotations

import ctypes
import json
import os
import select
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/claude-control/scripts"))

from claude_control import workspace, workspace_sandbox  # noqa: E402
from claude_control.store import ControlError, Store  # noqa: E402
from test_controller import ControllerTestCase  # noqa: E402


class BoundaryReached(BaseException):
    """Stop a coordinator after the command row exists without simulating recovery."""


class WorkspaceLaunchTests(ControllerTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.repo = self.project
        self.git("init", "-q")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.invalid")
        (self.repo / "README.md").write_text("base\n", encoding="utf-8")
        self.git("add", "README.md")
        self.git("commit", "-qm", "base")

    def git(self, *arguments: str) -> None:
        completed = subprocess.run(
            ["/usr/bin/git", "-C", str(self.repo), *arguments],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    @staticmethod
    def policy() -> dict:
        return {
            "version": 1,
            "role": "executor",
            "read_paths": ["."],
            "write_paths": [],
            "checks": {"unit": {"argv": ["/usr/bin/true"], "timeout": 2}},
        }

    def assignment(self, label: str) -> dict:
        fixture = {"workspace_operations": [[{"op": "run_check", "name": "unit"}], []]}
        return {
            "id": label,
            "name": label,
            "role": "executor",
            "model": "sonnet",
            "project": str(self.repo),
            "objective": "Request one bounded named check.",
            "context": json.dumps({"__fake_assignment__": fixture}),
            "scope": ["Use controller-mediated operations only."],
            "acceptance_criteria": ["Return a valid report."],
            "deliverable": "A check receipt.",
            "timeout": 10,
        }

    def prepare_command(self, callback) -> tuple[dict, dict]:
        store = Store(self.state)
        with mock.patch.object(workspace.sandbox, "require"):
            created = workspace.create(store, self.policy(), "launch-create", repo=str(self.repo))
            workspace.bind(store, created["id"], self.assignment("launch"), "launch-bind")
            run_id = workspace.run(store, created["id"], once=True, max_seconds=8)["started"][0]
        self.assertEqual(self.wait_terminal(run_id)["status"], "completed")
        captured: dict = {}

        def intercept(directory, argv, timeout, **kwargs):
            captured.update(
                directory=Path(directory), argv=argv, timeout=timeout, managed=kwargs["managed"]
            )
            return callback(created, captured)

        with (
            mock.patch.object(workspace.sandbox, "require"),
            mock.patch.object(workspace.sandbox, "execute", side_effect=intercept),
            self.assertRaises(BoundaryReached),
        ):
            workspace.run(store, created["id"], once=True, max_seconds=8)
        return created, captured

    def ready_workspace(self) -> dict:
        store = Store(self.state)
        with mock.patch.object(workspace.sandbox, "require"):
            created = workspace.create(store, self.policy(), "launch-create", repo=str(self.repo))
            workspace.bind(store, created["id"], self.assignment("launch"), "launch-bind")
            run_id = workspace.run(store, created["id"], once=True, max_seconds=8)["started"][0]
        self.assertEqual(self.wait_terminal(run_id)["status"], "completed")
        return created

    def command(self, captured: dict) -> dict:
        managed = captured["managed"]
        with Store(self.state).db() as db:
            return dict(
                db.execute(
                    "SELECT * FROM workspace_commands WHERE run_id=? AND seq=?",
                    (managed["run_id"], managed["seq"]),
                ).fetchone()
            )

    def retained_scratch(self, created: dict, source: Path, name: str) -> Path:
        target = workspace.directory(Store(self.state), created["id"]) / name / "tree"
        target.parent.mkdir()
        shutil.copytree(source, target)
        return target

    def assert_closed_without_exec(self, captured: dict, scratch: Path) -> None:
        managed = captured["managed"]
        with mock.patch.object(workspace_sandbox.os, "execve") as execute:
            with self.assertRaises(ControlError) as raised:
                workspace_sandbox.exec_check(self.state, managed["run_id"], managed["seq"], scratch)
        self.assertEqual(raised.exception.code, "workspace_command_closed")
        execute.assert_not_called()

    def test_reconciled_receipt_rejects_late_wrapper_without_child_update(self) -> None:
        retained: dict[str, Path] = {}

        def pause(created, captured):
            retained["scratch"] = self.retained_scratch(
                created, captured["directory"], "check-late"
            )
            raise BoundaryReached()

        created, captured = self.prepare_command(pause)
        with mock.patch.object(workspace, "alive", return_value=False):
            reconciled = workspace.reconcile(Store(self.state), created["id"])

        self.assertEqual(reconciled["receipts"][0]["result"]["outcome"], "unknown")
        self.assert_closed_without_exec(captured, retained["scratch"])
        self.assertIsNone(self.command(captured)["child_pid"])

    def test_dead_owner_or_wrong_parent_rejects_wrapper_before_exec(self) -> None:
        retained: dict[str, Path] = {}

        def pause(created, captured):
            retained["scratch"] = self.retained_scratch(
                created, captured["directory"], "check-owner"
            )
            raise BoundaryReached()

        _, captured = self.prepare_command(pause)
        command = self.command(captured)
        cases = (
            ("dead-owner", {"alive": False, "parent": command["owner_pid"]}),
            ("wrong-parent", {"alive": True, "parent": command["owner_pid"] + 1}),
        )
        for name, condition in cases:
            with (
                self.subTest(case=name),
                mock.patch.object(workspace_sandbox, "alive", return_value=condition["alive"]),
                mock.patch.object(
                    workspace_sandbox.os, "getppid", return_value=condition["parent"]
                ),
            ):
                self.assert_closed_without_exec(captured, retained["scratch"])
        self.assertIsNone(self.command(captured)["child_pid"])

    def test_child_identity_is_durable_before_exec_and_blocks_reconciliation(self) -> None:
        fake_child_pid = 900_001

        def inspect_identity(_created, captured):
            command = self.command(captured)
            with (
                mock.patch.object(workspace_sandbox.os, "getpid", return_value=fake_child_pid),
                mock.patch.object(
                    workspace_sandbox.os, "getppid", return_value=command["owner_pid"]
                ),
                mock.patch.object(workspace_sandbox, "alive", return_value=True),
                mock.patch.object(
                    workspace_sandbox, "proc_identity", return_value={"start": "777"}
                ),
                mock.patch.object(workspace_sandbox, "argv_for", return_value=["/usr/bin/true"]),
                mock.patch.object(workspace, "_checkpoint", side_effect=BoundaryReached),
            ):
                workspace_sandbox.exec_check(
                    self.state,
                    captured["managed"]["run_id"],
                    captured["managed"]["seq"],
                    captured["directory"],
                )

        created, captured = self.prepare_command(inspect_identity)
        command = self.command(captured)
        self.assertEqual((command["child_pid"], command["child_start"]), (fake_child_pid, "777"))

        def only_child_alive(pid, _start, _boot):
            return pid == fake_child_pid

        with mock.patch.object(workspace, "alive", side_effect=only_child_alive):
            with self.assertRaises(ControlError) as raised:
                workspace.reconcile(Store(self.state), created["id"])
        self.assertEqual(raised.exception.code, "command_unknown")

        with mock.patch.object(workspace, "alive", return_value=False):
            released = workspace.reconcile(Store(self.state), created["id"])
        self.assertEqual(released["receipts"][0]["result"]["outcome"], "unknown")

    def test_owner_death_after_identity_checkpoint_exits_125_before_exec(self) -> None:
        created = self.ready_workspace()
        child_ready_r, child_ready_w = os.pipe()
        main_ready_r, main_ready_w = os.pipe()
        child_pid_r, child_pid_w = os.pipe()
        libc = ctypes.CDLL(None, use_errno=True)
        previous_subreaper = ctypes.c_int()
        self.assertEqual(libc.prctl(37, ctypes.byref(previous_subreaper), 0, 0, 0), 0)
        self.assertEqual(libc.prctl(36, 1, 0, 0, 0), 0)  # PR_SET_CHILD_SUBREAPER
        wrapper_pid: int | None = None
        reaped: set[int] = set()

        def wait_owned(pid: int, timeout: float) -> int:
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                waited, status = os.waitpid(pid, os.WNOHANG)
                if waited == pid:
                    reaped.add(pid)
                    return os.waitstatus_to_exitcode(status)
                time.sleep(0.02)
            self.fail(f"owned subprocess {pid} did not exit within {timeout} seconds")

        owner_pid = os.fork()
        if owner_pid == 0:
            try:
                os.close(main_ready_r)
                os.close(child_pid_r)

                def launch_wrapper(directory, _argv, _timeout, **kwargs):
                    wrapper_pid = os.fork()
                    if wrapper_pid == 0:
                        try:
                            os.close(child_ready_r)
                            os.close(main_ready_w)
                            os.close(child_pid_w)

                            def checkpoint(phase: str) -> None:
                                if phase == "command_identity_recorded":
                                    os.write(child_ready_w, b"1")
                                    time.sleep(0.5)

                            with (
                                mock.patch.object(workspace, "_checkpoint", checkpoint),
                                mock.patch.object(
                                    workspace_sandbox, "argv_for", return_value=["/usr/bin/true"]
                                ),
                            ):
                                managed = kwargs["managed"]
                                workspace_sandbox.exec_check(
                                    self.state,
                                    managed["run_id"],
                                    managed["seq"],
                                    directory,
                                )
                            os._exit(0)
                        except BaseException:
                            os._exit(126)
                    os.write(child_pid_w, str(wrapper_pid).encode() + b"\n")
                    os.close(child_pid_w)
                    os.close(child_ready_w)
                    ready, _, _ = select.select([child_ready_r], [], [], 5)
                    if not ready or os.read(child_ready_r, 1) != b"1":
                        os._exit(124)
                    os.write(main_ready_w, b"1")
                    os._exit(0)

                with (
                    mock.patch.object(workspace.sandbox, "require"),
                    mock.patch.object(workspace.sandbox, "execute", side_effect=launch_wrapper),
                ):
                    workspace.run(Store(self.state), created["id"], once=True, max_seconds=8)
                os._exit(123)
            except BaseException:
                os._exit(122)

        os.close(child_ready_r)
        os.close(child_ready_w)
        os.close(main_ready_w)
        os.close(child_pid_w)
        try:
            ready, _, _ = select.select([child_pid_r], [], [], 5)
            self.assertTrue(ready, "owner did not publish its wrapper PID")
            wrapper_pid = int(os.read(child_pid_r, 64).strip())
            ready, _, _ = select.select([main_ready_r], [], [], 5)
            self.assertTrue(ready, "wrapper did not reach command_identity_recorded")
            self.assertEqual(os.read(main_ready_r, 1), b"1")
            with Store(self.state).db() as db:
                recorded = db.execute(
                    "SELECT child_pid FROM workspace_commands WHERE child_pid=?",
                    (wrapper_pid,),
                ).fetchone()
            self.assertIsNotNone(recorded)
            self.assertEqual(wait_owned(owner_pid, 5), 0)
            self.assertEqual(wait_owned(wrapper_pid, 5), 125)
        finally:
            os.close(main_ready_r)
            os.close(child_pid_r)
            for pid in (owner_pid, wrapper_pid):
                if pid is None or pid in reaped:
                    continue
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                try:
                    os.waitpid(pid, 0)
                except ChildProcessError:
                    pass
            libc.prctl(36, previous_subreaper.value, 0, 0, 0)

    def test_stop_before_wrapper_rejects_launch_without_child_update(self) -> None:
        retained: dict[str, Path] = {}

        def pause(created, captured):
            retained["scratch"] = self.retained_scratch(
                created, captured["directory"], "check-stopped"
            )
            raise BoundaryReached()

        created, captured = self.prepare_command(pause)
        stopped = workspace.stop(Store(self.state), created["id"], "stop-before-wrapper")

        self.assertEqual(stopped["state"], "stopping")
        self.assert_closed_without_exec(captured, retained["scratch"])
        self.assertIsNone(self.command(captured)["child_pid"])


if __name__ == "__main__":
    import unittest

    unittest.main()
