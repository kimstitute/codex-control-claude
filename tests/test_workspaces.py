"""P5 regression tests for controller-mediated workspaces."""

from __future__ import annotations

import fcntl
import hashlib
import json
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/claude-control/scripts"))

from claude_control import messages, scheduler, task_contracts, tasks, workspace  # noqa: E402
from claude_control.store import ControlError, Store  # noqa: E402
from test_controller import ControllerTestCase  # noqa: E402


class WorkspaceTests(ControllerTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.repo = self.project
        self.git("init", "-q")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.invalid")
        (self.repo / "README.md").write_text("base\n", encoding="utf-8")
        self.git("add", "README.md")
        self.git("commit", "-qm", "base")
        self.sequence = 0

    def git(self, *arguments: str) -> str:
        completed = subprocess.run(
            ["/usr/bin/git", "-C", str(self.repo), *arguments],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return completed.stdout.strip()

    @staticmethod
    def policy(*, role="executor", read=None, write=None, checks=None, **limits) -> dict:
        return {
            "version": 1,
            "role": role,
            "read_paths": read or ["."],
            "write_paths": write or [],
            "checks": checks or {},
            **limits,
        }

    def assignment(
        self,
        label: str,
        *,
        fixture: dict | None = None,
        role: str = "executor",
        model: str = "sonnet",
    ) -> dict:
        return {
            "id": label,
            "name": label,
            "role": role,
            "model": model,
            "project": str(self.repo),
            "objective": "Make only the requested bounded workspace changes.",
            "context": json.dumps({"__fake_assignment__": fixture or {}}),
            "scope": ["Use controller-mediated operations only."],
            "acceptance_criteria": ["Return a valid final report."],
            "deliverable": "A frozen workspace result.",
            "timeout": 10,
        }

    def create(self, *, policy=None, from_snapshot=None) -> dict:
        self.sequence += 1
        kwargs = (
            {"from_snapshot": from_snapshot}
            if from_snapshot
            else {"repo": str(self.repo), "ref": "HEAD"}
        )
        with mock.patch.object(workspace.sandbox, "require"):
            return workspace.create(
                Store(self.state),
                policy or self.policy(),
                f"workspace-create-{self.sequence}",
                **kwargs,
            )

    def bind(self, created: dict, assignment: dict) -> dict:
        self.sequence += 1
        with mock.patch.object(workspace.sandbox, "require"):
            return workspace.bind(
                Store(self.state), created["id"], assignment, f"workspace-bind-{self.sequence}"
            )

    def run_workspace(self, workspace_id: str, *, once=False, max_seconds=8) -> dict:
        with mock.patch.object(workspace.sandbox, "require"):
            return workspace.run(
                Store(self.state), workspace_id, once=once, max_seconds=max_seconds
            )

    def assert_error(self, expected: str, call, *args, **kwargs) -> None:
        with self.assertRaises(ControlError) as raised:
            call(*args, **kwargs)
        self.assertEqual(raised.exception.code, expected)

    def test_create_is_call_free_and_snapshots_committed_source_only(self) -> None:
        (self.repo / "README.md").write_text("dirty\n", encoding="utf-8")
        (self.repo / "untracked.txt").write_text("outside\n", encoding="utf-8")

        created = self.create()
        status = workspace.status(Store(self.state), created["id"])
        baseline = workspace.directory(Store(self.state), created["id"]) / "baseline"

        self.assertEqual((created["state"], status["calls_used"]), ("idle", 0))
        self.assertEqual((baseline / "README.md").read_text(encoding="utf-8"), "base\n")
        self.assertFalse((baseline / "untracked.txt").exists())
        self.assertEqual((self.repo / "README.md").read_text(encoding="utf-8"), "dirty\n")

    def test_concurrent_same_create_operation_allocates_one_workspace_root(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        outcomes: list[dict | ControlError | BaseException] = []
        checkpoint_calls = 0
        checkpoint_lock = threading.Lock()

        def checkpoint(phase: str) -> None:
            nonlocal checkpoint_calls
            if phase != "creation_reserved":
                return
            with checkpoint_lock:
                checkpoint_calls += 1
                first = checkpoint_calls == 1
            if first:
                entered.set()
                release.wait(timeout=5)

        def create_same() -> None:
            try:
                outcomes.append(
                    workspace.create(
                        Store(self.state),
                        self.policy(),
                        "concurrent-create",
                        repo=str(self.repo),
                    )
                )
            except (ControlError, BaseException) as exc:
                outcomes.append(exc)

        with (
            mock.patch.object(workspace.sandbox, "require"),
            mock.patch.object(workspace, "_checkpoint", side_effect=checkpoint),
        ):
            first = threading.Thread(target=create_same)
            first.start()
            self.assertTrue(entered.wait(timeout=5))
            second = threading.Thread(target=create_same)
            second.start()
            second.join(timeout=5)
            release.set()
            first.join(timeout=5)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        successes = [value for value in outcomes if isinstance(value, dict)]
        failures = [value for value in outcomes if isinstance(value, ControlError)]
        self.assertEqual(len(successes), 1)
        self.assertEqual([value.code for value in failures], ["workspace_creation_incomplete"])
        workspace_id = successes[0]["id"]
        roots = [entry.name for entry in (self.state / "workspaces").iterdir() if entry.is_dir()]
        self.assertEqual(roots, [workspace_id])
        with mock.patch.object(workspace.sandbox, "require"):
            repeated = workspace.create(
                Store(self.state),
                self.policy(),
                "concurrent-create",
                repo=str(self.repo),
            )
        self.assertEqual(repeated["id"], workspace_id)
        self.assertTrue(repeated["deduplicated"])

    def test_crash_after_create_reservation_cannot_allocate_or_copy_again(self) -> None:
        class SimulatedCrash(BaseException):
            pass

        def crash(phase: str) -> None:
            if phase == "creation_reserved":
                raise SimulatedCrash()

        with (
            mock.patch.object(workspace.sandbox, "require"),
            mock.patch.object(workspace, "_checkpoint", side_effect=crash),
            self.assertRaises(SimulatedCrash),
        ):
            workspace.create(
                Store(self.state),
                self.policy(),
                "crashed-create",
                repo=str(self.repo),
            )

        with Store(self.state).db() as db:
            creation = db.execute(
                "SELECT id FROM workspace_creations WHERE operation_id='crashed-create'"
            ).fetchone()
        workspace_id = creation[0]
        workspace_root = self.state / "workspaces"
        before = sorted(workspace_root.iterdir()) if workspace_root.exists() else []
        self.assertEqual(workspace.status(Store(self.state), workspace_id)["state"], "preparing")

        with (
            mock.patch.object(workspace.sandbox, "require"),
            mock.patch.object(workspace.files, "create_snapshot") as copy,
        ):
            self.assert_error(
                "workspace_creation_incomplete",
                workspace.create,
                Store(self.state),
                self.policy(),
                "crashed-create",
                repo=str(self.repo),
            )

        after = sorted(workspace_root.iterdir()) if workspace_root.exists() else []
        self.assertEqual(after, before)
        copy.assert_not_called()

    def test_bind_is_exclusive_and_uses_private_control_directory(self) -> None:
        created = self.create()
        bound = self.bind(created, self.assignment("exclusive"))

        with Store(self.state).db() as db:
            revision = db.execute(
                "SELECT prompt FROM task_revisions WHERE task_id=? AND revision=1",
                (bound["task_id"],),
            ).fetchone()
        prompt = json.loads(revision[0])
        self.assertEqual(
            prompt["assignment"]["project"],
            str(workspace.directory(Store(self.state), created["id"]) / "control"),
        )
        with mock.patch.object(workspace.sandbox, "require"):
            self.assert_error(
                "workspace_bound",
                workspace.bind,
                Store(self.state),
                created["id"],
                self.assignment("second"),
                "second-bind",
            )

    def test_public_task_queue_and_message_mutations_reject_owned_task(self) -> None:
        created = self.create()
        bound = self.bind(created, self.assignment("guarded"))
        store = Store(self.state)
        task_id = bound["task_id"]

        calls = (
            (tasks.submit, (store, task_id, 1, "public-submit"), {}),
            (scheduler.enqueue, (store, task_id, 1, "public-queue"), {}),
            (messages.enqueue, (store, task_id, 1, "follow up", "public-message"), {}),
            (
                tasks.revise,
                (store, task_id, 1, self.assignment("guarded"), "public-revise"),
                {},
            ),
        )
        for call, args, kwargs in calls:
            with self.subTest(call=call.__module__ + "." + call.__name__):
                self.assert_error("workspace_owned", call, *args, **kwargs)

    def test_write_then_finish_preserves_session_and_exports_frozen_tree(self) -> None:
        fixture = {
            "workspace_operations": [
                [{"op": "write", "path": "README.md", "content": "changed\n"}],
                [],
            ]
        }
        created = self.create(policy=self.policy(write=["README.md"]))
        bound = self.bind(created, self.assignment("write-finish", fixture=fixture))

        result = self.run_workspace(created["id"])
        exported = workspace.export(Store(self.state), created["id"])
        runs = result["task"]["runs"]

        self.assertEqual(result["state"], "finished")
        self.assertEqual((result["calls_used"], result["actions_used"]), (2, 1))
        self.assertEqual(len({run["session_id"] for run in runs}), 1)
        self.assertEqual(
            (Path(exported["directory"]) / "tree/README.md").read_text(encoding="utf-8"),
            "changed\n",
        )
        self.assertEqual((self.repo / "README.md").read_text(encoding="utf-8"), "base\n")
        self.assertEqual(result["task"]["id"], bound["task_id"])

    def test_base_hashed_hunk_patch_is_applied_and_receipted(self) -> None:
        base = hashlib.sha256(b"base\n").hexdigest()
        fixture = {
            "workspace_operations": [
                [
                    {
                        "op": "patch",
                        "path": "README.md",
                        "base_sha256": base,
                        "hunks": [
                            {
                                "old_start": 1,
                                "old_count": 1,
                                "new_start": 1,
                                "new_count": 1,
                                "lines": ["-base\n", "+patched\n"],
                            }
                        ],
                    }
                ],
                [],
            ]
        }
        created = self.create(policy=self.policy(write=["README.md"]))
        self.bind(created, self.assignment("patch-finish", fixture=fixture))

        result = self.run_workspace(created["id"])
        exported = workspace.export(Store(self.state), created["id"])
        receipt = result["receipts"][0]["result"]

        self.assertEqual(result["state"], "finished")
        self.assertEqual(receipt["before_sha256"], base)
        self.assertEqual(receipt["hunks"], 1)
        self.assertEqual(
            (Path(exported["directory"]) / "tree/README.md").read_text(encoding="utf-8"),
            "patched\n",
        )

    def test_apply_requires_exact_clean_head_and_is_idempotent(self) -> None:
        fixture = {
            "workspace_operations": [
                [{"op": "write", "path": "README.md", "content": "applied\n"}],
                [],
            ]
        }
        created = self.create(policy=self.policy(write=["README.md"]))
        self.bind(created, self.assignment("apply-frozen", fixture=fixture))
        self.assertEqual(self.run_workspace(created["id"])["state"], "finished")

        applied = workspace.apply(Store(self.state), created["id"], "apply-frozen-result")
        repeated = workspace.apply(Store(self.state), created["id"], "apply-frozen-result")

        self.assertEqual(applied["state"], "applied")
        self.assertFalse(applied["deduplicated"])
        self.assertTrue(repeated["deduplicated"])
        self.assertEqual((self.repo / "README.md").read_text(encoding="utf-8"), "applied\n")
        self.assertEqual(
            workspace.status(Store(self.state), created["id"])["application"]["state"],
            "applied",
        )

    def test_applied_source_uses_git_line_ending_normalization(self) -> None:
        base_commit = self.git("rev-parse", "HEAD")
        frozen_tree = self.state / "canonical-frozen"
        frozen_tree.mkdir()
        frozen = b"line one\nline two\n"
        digest = hashlib.sha256(frozen).hexdigest()
        (frozen_tree / "README.md").write_bytes(frozen)
        manifest = {
            "files": {
                "README.md": {"sha256": digest, "size": len(frozen), "mode": "100644"}
            },
            "changes": [
                {
                    "path": "README.md",
                    "before_sha256": hashlib.sha256(b"base\n").hexdigest(),
                    "after_sha256": digest,
                }
            ],
        }
        for setting, working in (
            ("false", frozen),
            ("input", frozen),
            ("true", b"line one\r\nline two\r\n"),
        ):
            with self.subTest(core_autocrlf=setting):
                self.git("config", "core.autocrlf", setting)
                (self.repo / "README.md").write_bytes(working)
                workspace._verify_applied_source(
                    str(self.repo), base_commit, manifest, frozen_tree
                )

        (self.repo / "README.md").write_bytes(b"line one\nDIFFERENT\n")
        self.assert_error(
            "workspace_conflict",
            workspace._verify_applied_source,
            str(self.repo),
            base_commit,
            manifest,
            frozen_tree,
        )

    def test_windows_rejects_only_new_executable_files(self) -> None:
        base_commit = self.git("rev-parse", "HEAD")
        existing = {
            "files": {
                "README.md": {"sha256": "0" * 64, "size": 1, "mode": "100644"}
            },
            "changes": [
                {
                    "path": "README.md",
                    "before_sha256": "1" * 64,
                    "after_sha256": "0" * 64,
                }
            ],
        }
        workspace._preflight_apply(str(self.repo), base_commit, existing)

        added = {
            "files": {
                "script.sh": {"sha256": "0" * 64, "size": 1, "mode": "100755"}
            },
            "changes": [
                {
                    "path": "script.sh",
                    "before_sha256": None,
                    "after_sha256": "0" * 64,
                }
            ],
        }
        with mock.patch.object(workspace.os, "name", "nt"):
            self.assert_error(
                "workspace_apply",
                workspace._preflight_apply,
                str(self.repo),
                base_commit,
                added,
            )

    def test_apply_rejects_dirty_or_advanced_source_before_reservation(self) -> None:
        fixture = {
            "workspace_operations": [
                [{"op": "write", "path": "README.md", "content": "candidate\n"}],
                [],
            ]
        }
        dirty = self.create(policy=self.policy(write=["README.md"]))
        self.bind(dirty, self.assignment("apply-dirty", fixture=fixture))
        self.run_workspace(dirty["id"])
        (self.repo / "README.md").write_text("local\n", encoding="utf-8")
        self.assert_error(
            "workspace_conflict",
            workspace.apply,
            Store(self.state),
            dirty["id"],
            "apply-dirty",
        )

        self.git("checkout", "--", "README.md")
        advanced = self.create(policy=self.policy(write=["README.md"]))
        self.bind(advanced, self.assignment("apply-advanced", fixture=fixture))
        self.run_workspace(advanced["id"])
        self.git("commit", "--allow-empty", "-qm", "advance")
        self.assert_error(
            "workspace_conflict",
            workspace.apply,
            Store(self.state),
            advanced["id"],
            "apply-advanced",
        )

    def test_accept_requires_intact_frozen_export(self) -> None:
        created = self.create()
        bound = self.bind(created, self.assignment("accept-frozen"))
        result = self.run_workspace(created["id"])
        run = result["task"]["runs"][-1]
        evidence = {"1": "Codex inspected the frozen manifest."}

        accepted = tasks.accept(
            Store(self.state),
            bound["task_id"],
            1,
            run["id"],
            run["result_sha256"],
            evidence,
            "accept-frozen",
        )
        self.assertEqual(accepted["kind"], "accept")

        frozen = Path(result["export"]["directory"]) / "tree/README.md"
        frozen.write_text("tampered\n", encoding="utf-8")
        self.assert_error(
            "workspace_integrity",
            tasks.accept,
            Store(self.state),
            bound["task_id"],
            1,
            run["id"],
            run["result_sha256"],
            evidence,
            "accept-after-tamper",
        )

    def test_accept_rejects_complete_report_before_freeze(self) -> None:
        created = self.create()
        bound = self.bind(created, self.assignment("accept-too-early"))
        run_id = self.run_workspace(created["id"], once=True)["started"][0]
        run = self.wait_terminal(run_id)

        self.assert_error(
            "workspace_unfrozen",
            tasks.accept,
            Store(self.state),
            bound["task_id"],
            1,
            run_id,
            run["result_sha256"],
            {"1": "The report is complete but has not been frozen."},
            "accept-too-early",
        )

    def test_read_receipt_is_delivered_on_the_next_turn(self) -> None:
        fixture = {"workspace_operations": [[{"op": "read", "path": "README.md"}], []]}
        created = self.create()
        self.bind(created, self.assignment("read-receipt", fixture=fixture))

        result = self.run_workspace(created["id"])
        backend_id = result["task"]["runs"][-1]["backend_id"]
        invocation = json.loads(
            (
                workspace.directory(Store(self.state), created["id"])
                / "control/.fake-claude"
                / f"{backend_id}.invocation.json"
            ).read_text(encoding="utf-8")
        )
        receipts = invocation["prompt"]["workspace"]["receipts"]

        self.assertEqual(len(receipts), 1)
        self.assertEqual(receipts[0]["result"]["content"], "base\n")
        self.assertEqual(receipts[0]["result"]["outcome"], "ok")
        self.assertIn("controller-mediated workspace", invocation["prompt"]["instructions"])
        self.assertIn("requesting controller operations", invocation["prompt"]["role"]["instructions"])
        self.assertNotIn("cannot execute code", invocation["prompt"]["instructions"])

    def test_invalid_operation_batch_has_no_file_effect(self) -> None:
        fixture = {
            "workspace_operations": [
                [
                    {"op": "write", "path": "README.md", "content": "must-not-run\n"},
                    {"op": "write", "path": "forbidden.txt", "content": "bad\n"},
                ]
            ]
        }
        created = self.create(policy=self.policy(write=["README.md"]))
        self.bind(created, self.assignment("invalid-batch", fixture=fixture))

        result = self.run_workspace(created["id"])
        tree = workspace.directory(Store(self.state), created["id"]) / "tree"

        self.assertEqual((result["state"], result["reason"]), ("awaiting_codex", "invalid_report"))
        self.assertEqual((tree / "README.md").read_text(encoding="utf-8"), "base\n")
        self.assertEqual(result["actions_used"], 0)

    def test_action_budget_rejects_entire_oversized_request(self) -> None:
        fixture = {
            "workspace_operations": [
                [
                    {"op": "write", "path": "README.md", "content": "one\n"},
                    {"op": "write", "path": "other.txt", "content": "two\n"},
                ]
            ]
        }
        created = self.create(policy=self.policy(write=["README.md", "other.txt"], max_actions=1))
        self.bind(created, self.assignment("action-limit", fixture=fixture))

        result = self.run_workspace(created["id"])
        tree = workspace.directory(Store(self.state), created["id"]) / "tree"

        self.assertEqual(
            (result["state"], result["reason"]), ("awaiting_codex", "workspace_budget")
        )
        self.assertEqual(result["actions_used"], 0)
        self.assertEqual((tree / "README.md").read_text(encoding="utf-8"), "base\n")
        self.assertFalse((tree / "other.txt").exists())

    def test_final_call_budget_is_reserved_before_any_operation(self) -> None:
        fixture = {
            "workspace_operations": [
                [{"op": "write", "path": "README.md", "content": "must-not-run\n"}]
            ]
        }
        created = self.create(policy=self.policy(write=["README.md"], max_calls=1, max_actions=2))
        self.bind(created, self.assignment("call-limit", fixture=fixture))

        result = self.run_workspace(created["id"])
        tree = workspace.directory(Store(self.state), created["id"]) / "tree/README.md"

        self.assertEqual(
            (result["state"], result["reason"]), ("awaiting_codex", "workspace_budget")
        )
        self.assertEqual((result["calls_used"], result["actions_used"]), (1, 0))
        self.assertEqual(tree.read_text(encoding="utf-8"), "base\n")

    def test_check_receipt_is_delivered_without_changing_working_tree(self) -> None:
        fixture = {"workspace_operations": [[{"op": "run_check", "name": "unit"}], []]}
        policy = self.policy(checks={"unit": {"argv": ["/usr/bin/true"], "timeout": 2}})
        created = self.create(policy=policy)
        self.bind(created, self.assignment("check", fixture=fixture))
        receipt = {
            "outcome": "ok",
            "exit_code": 0,
            "duration": 0.01,
            "truncated": False,
            "stdout": "",
            "stderr": "",
            "stdout_sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            "stderr_sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        }

        with (
            mock.patch.object(workspace.sandbox, "require"),
            mock.patch.object(workspace.sandbox, "execute", return_value=receipt) as execute,
        ):
            result = workspace.run(Store(self.state), created["id"], max_seconds=8)

        self.assertEqual(result["state"], "finished")
        self.assertEqual(result["receipts"][0]["result"]["outcome"], "ok")
        self.assertEqual(result["actions_used"], 1)
        execute.assert_called_once()

    def test_typed_check_receipt_evidence_is_verified_against_the_ledger(self) -> None:
        fixture = {"workspace_operations": [[{"op": "run_check", "name": "unit"}], []]}
        policy = self.policy(checks={"unit": {"argv": ["/usr/bin/true"], "timeout": 2}})
        created = self.create(policy=policy)
        bound = self.bind(created, self.assignment("typed-check-evidence", fixture=fixture))
        receipt = {
            "outcome": "ok",
            "exit_code": 0,
            "duration": 0.01,
            "truncated": False,
            "stdout": "",
            "stderr": "",
            "stdout_sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            "stderr_sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        }
        with (
            mock.patch.object(workspace.sandbox, "require"),
            mock.patch.object(workspace.sandbox, "execute", return_value=receipt),
        ):
            result = workspace.run(Store(self.state), created["id"], max_seconds=8)
        final_run = result["task"]["runs"][-1]
        recorded = result["receipts"][0]
        evidence = {
            "1": [
                {
                    "type": "check_receipt",
                    "run_id": recorded["run_id"],
                    "seq": recorded["seq"],
                    "receipt_sha256": task_contracts.digest(
                        task_contracts.canonical(recorded["result"])
                    ),
                }
            ]
        }

        decision = tasks.accept(
            Store(self.state),
            bound["task_id"],
            result["task"]["current_revision"],
            final_run["id"],
            final_run["result_sha256"],
            evidence,
            "accept-typed-check-evidence",
        )

        self.assertEqual(decision["evidence"], evidence)

    def test_malformed_workspace_report_halts_without_operation(self) -> None:
        fixture = {"report": {"operations": "not-a-list"}}
        created = self.create()
        self.bind(created, self.assignment("malformed-report", fixture=fixture))

        result = self.run_workspace(created["id"])

        self.assertEqual((result["state"], result["reason"]), ("awaiting_codex", "invalid_report"))
        self.assertEqual((result["calls_used"], result["actions_used"]), (2, 0))
        self.assertEqual(len({run["session_id"] for run in result["task"]["runs"]}), 1)

    def test_side_effect_free_invalid_report_gets_one_bounded_repair(self) -> None:
        fixture = {"reports": [{"operations": "not-a-list"}, {"operations": []}]}
        created = self.create()
        self.bind(created, self.assignment("repaired-report", fixture=fixture))

        result = self.run_workspace(created["id"])

        self.assertEqual(result["state"], "finished")
        self.assertEqual((result["calls_used"], result["actions_used"]), (2, 0))
        self.assertEqual(len({run["session_id"] for run in result["task"]["runs"]}), 1)
        self.assertIn(
            workspace.FORMAT_REPAIR_SCOPE,
            result["task"]["revisions"][1]["prompt"],
        )

    def test_invalid_report_is_not_repaired_without_call_budget(self) -> None:
        fixture = {"report": {"operations": "not-a-list"}}
        created = self.create(policy=self.policy(max_calls=1))
        self.bind(created, self.assignment("repair-budget", fixture=fixture))

        result = self.run_workspace(created["id"])

        self.assertEqual((result["state"], result["reason"]), ("awaiting_codex", "invalid_report"))
        self.assertEqual((result["calls_used"], result["actions_used"]), (1, 0))

    def test_reserved_operation_crash_is_never_replayed(self) -> None:
        fixture = {
            "workspace_operations": [
                [{"op": "write", "path": "README.md", "content": "must-not-run\n"}]
            ]
        }
        created = self.create(policy=self.policy(write=["README.md"]))
        self.bind(created, self.assignment("reserved-crash", fixture=fixture))
        started = self.run_workspace(created["id"], once=True)["started"][0]
        self.wait_terminal(started)

        class SimulatedCrash(BaseException):
            pass

        with (
            mock.patch.object(workspace.sandbox, "require"),
            mock.patch.object(workspace, "_checkpoint", side_effect=SimulatedCrash),
        ):
            with self.assertRaises(SimulatedCrash):
                workspace.run(Store(self.state), created["id"], once=True)

        result = self.run_workspace(created["id"], once=True)
        tree = workspace.directory(Store(self.state), created["id"]) / "tree"
        self.assertEqual(
            (result["state"], result["reason"]), ("awaiting_codex", "operation_unknown")
        )
        self.assertEqual((tree / "README.md").read_text(encoding="utf-8"), "base\n")
        self.assertEqual(result["receipts"], [])

    def test_out_of_band_edit_after_batch_blocks_next_model_call(self) -> None:
        fixture = {
            "workspace_operations": [
                [{"op": "write", "path": "README.md", "content": "recorded\n"}],
                [],
            ]
        }
        created = self.create(policy=self.policy(write=["README.md"]))
        self.bind(created, self.assignment("tree-guard", fixture=fixture))
        first = self.run_workspace(created["id"], once=True)["started"][0]
        self.wait_terminal(first)
        after_batch = self.run_workspace(created["id"], once=True)
        self.assertEqual((after_batch["state"], after_batch["calls_used"]), ("active", 1))
        tree = workspace.directory(Store(self.state), created["id"]) / "tree/README.md"
        tree.write_text("out-of-band\n", encoding="utf-8")

        result = self.run_workspace(created["id"], once=True)

        self.assertEqual(
            (result["state"], result["reason"]),
            ("awaiting_codex", "workspace_integrity"),
        )
        self.assertEqual(result["calls_used"], 1)
        self.assertEqual(len(result["task"]["runs"]), 1)

    def test_model_mismatch_halts_without_retry(self) -> None:
        created = self.create()
        self.bind(
            created,
            self.assignment("model-mismatch", fixture={"behavior": "mismatch_model"}),
        )

        result = self.run_workspace(created["id"])

        self.assertEqual((result["state"], result["reason"]), ("awaiting_codex", "model_mismatch"))
        self.assertEqual(result["calls_used"], 1)

    def test_stop_leaves_source_and_working_tree_unchanged(self) -> None:
        created = self.create(policy=self.policy(write=["README.md"]))
        self.bind(created, self.assignment("stopped"))

        result = workspace.stop(Store(self.state), created["id"], "stop-workspace")

        self.assertEqual(result["state"], "stopped")
        self.assertEqual((self.repo / "README.md").read_text(encoding="utf-8"), "base\n")
        tree = workspace.directory(Store(self.state), created["id"]) / "tree/README.md"
        self.assertEqual(tree.read_text(encoding="utf-8"), "base\n")

    def test_stop_during_coordinator_run_finishes_after_lock_owner_exits(self) -> None:
        created = self.create()
        self.bind(
            created,
            self.assignment("stop-race", fixture={"behavior": "sleep", "sleep_seconds": 30}),
        )
        outcome: list[dict | BaseException] = []

        def coordinate() -> None:
            try:
                outcome.append(self.run_workspace(created["id"], max_seconds=8))
            except BaseException as exc:  # Preserve thread failure for the test assertion.
                outcome.append(exc)

        thread = threading.Thread(target=coordinate)
        thread.start()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            current = workspace.status(Store(self.state), created["id"])
            if current["calls_used"] == 1:
                break
            time.sleep(0.02)
        else:
            self.fail("workspace coordinator did not reserve its model call")

        stopped = workspace.stop(Store(self.state), created["id"], "stop-racing-run")
        thread.join(timeout=8)

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            final = workspace.status(Store(self.state), created["id"])
            if final["state"] == "stopped":
                break
            time.sleep(0.05)

        self.assertFalse(thread.is_alive())
        self.assertIn(stopped["state"], ("stopping", "stopped"))
        self.assertEqual(final["state"], "stopped")
        self.assertEqual(len(outcome), 1)
        self.assertIsInstance(outcome[0], dict)
        self.assertIn(outcome[0]["state"], ("stopping", "stopped"))

    def test_stop_record_crash_is_recovered_without_second_stop(self) -> None:
        created = self.create()
        self.bind(
            created,
            self.assignment(
                "stop-record-crash", fixture={"behavior": "sleep", "sleep_seconds": 30}
            ),
        )
        outcome: list[dict | BaseException] = []

        def coordinate() -> None:
            try:
                outcome.append(self.run_workspace(created["id"], max_seconds=8))
            except BaseException as exc:
                outcome.append(exc)

        thread = threading.Thread(target=coordinate)
        thread.start()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if workspace.status(Store(self.state), created["id"])["calls_used"] == 1:
                break
            time.sleep(0.02)
        else:
            self.fail("workspace coordinator did not reserve its model call")

        class SimulatedCrash(BaseException):
            pass

        def crash(phase: str) -> None:
            if phase == "stop_recorded":
                raise SimulatedCrash()

        with mock.patch.object(workspace, "_checkpoint", side_effect=crash):
            with self.assertRaises(SimulatedCrash):
                workspace.stop(Store(self.state), created["id"], "crashed-stop")

        thread.join(timeout=8)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            recovered = workspace.status(Store(self.state), created["id"])
            if recovered["state"] == "stopped":
                break
            time.sleep(0.05)

        self.assertFalse(thread.is_alive())
        self.assertEqual(recovered["state"], "stopped")
        self.assertEqual(len(outcome), 1)
        self.assertIsInstance(outcome[0], dict)
        self.assertIn(outcome[0]["state"], ("stopping", "stopped"))

    def test_exclusive_coordinator_lock_rejects_competing_run(self) -> None:
        created = self.create()
        self.bind(created, self.assignment("lock"))
        lock_path = workspace.directory(Store(self.state), created["id"]) / "coordinator.lock"

        with lock_path.open("a") as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assert_error(
                "workspace_busy",
                workspace.run,
                Store(self.state),
                created["id"],
                once=True,
            )

    def test_readonly_verifier_can_review_a_frozen_workspace(self) -> None:
        source = self.create()
        self.bind(
            source,
            self.assignment(
                "source",
                fixture={"workspace_operations": [[{"op": "read", "path": "README.md"}], []]},
            ),
        )
        self.assertEqual(self.run_workspace(source["id"])["state"], "finished")
        verifier = self.create(
            policy=self.policy(role="verifier", read=["."]), from_snapshot=source["id"]
        )

        bound = self.bind(
            verifier,
            self.assignment("verify", role="verifier", model="fable"),
        )
        result = self.run_workspace(verifier["id"])

        self.assertEqual(result["state"], "finished")
        self.assertEqual(result["task"]["id"], bound["task_id"])
        self.assertEqual(result["policy"]["write_paths"], [])
        backend_id = result["task"]["runs"][-1]["backend_id"]
        invocation = json.loads(
            (
                workspace.directory(Store(self.state), verifier["id"])
                / "control/.fake-claude"
                / f"{backend_id}.invocation.json"
            ).read_text(encoding="utf-8")
        )
        evidence = invocation["prompt"]["workspace"]["review"]["evidence"]
        self.assertEqual(evidence["requests"][0]["action"], {"op": "read", "path": "README.md"})
        self.assertEqual(evidence["receipts"][0]["result"]["outcome"], "ok")
        self.assertEqual(
            evidence["manifest"]["manifest_sha256"],
            invocation["prompt"]["workspace"]["review"]["target"]["manifest_sha256"],
        )

    def test_readonly_sonnet_scout_can_inspect_source(self) -> None:
        scout = self.create(policy=self.policy(role="scout", read=["README.md"]))
        bound = self.bind(
            scout,
            self.assignment(
                "scout-source",
                role="researcher",
                model="sonnet",
                fixture={"workspace_operations": [[{"op": "read", "path": "README.md"}], []]},
            ),
        )

        result = self.run_workspace(scout["id"])

        self.assertEqual(result["state"], "finished")
        self.assertEqual(result["task"]["id"], bound["task_id"])
        self.assertEqual(result["receipts"][0]["result"]["content"], "base\n")
        self.assertEqual(result["export"]["manifest"]["changes"], [])

    def test_invalid_snapshot_reviewer_policy_has_no_durable_side_effect(self) -> None:
        source = self.create()
        self.bind(source, self.assignment("review-source"))
        self.assertEqual(self.run_workspace(source["id"])["state"], "finished")
        store = Store(self.state)
        with store.db() as db:
            before = {
                table: db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                for table in ("workspace_creations", "task_operations", "workspaces")
            }
        roots_before = sorted(path.name for path in (self.state / "workspaces").iterdir())

        self.assert_error(
            "invalid_workspace",
            workspace.create,
            store,
            self.policy(role="executor", read=["README.md"]),
            "invalid-review-create",
            from_snapshot=source["id"],
        )

        with store.db() as db:
            after = {
                table: db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                for table in ("workspace_creations", "task_operations", "workspaces")
            }
        roots_after = sorted(path.name for path in (self.state / "workspaces").iterdir())
        self.assertEqual(after, before)
        self.assertEqual(roots_after, roots_before)

    def test_stop_finished_preserves_frozen_result_for_acceptance(self) -> None:
        created = self.create()
        bound = self.bind(created, self.assignment("finished-stop"))
        finished = self.run_workspace(created["id"])
        run = finished["task"]["runs"][-1]

        self.assert_error(
            "workspace_finished",
            workspace.stop,
            Store(self.state),
            created["id"],
            "stop-finished",
        )
        accepted = tasks.accept(
            Store(self.state),
            bound["task_id"],
            1,
            run["id"],
            run["result_sha256"],
            {"1": "Codex inspected the unchanged frozen export."},
            "accept-after-stop-rejected",
        )

        self.assertEqual(accepted["kind"], "accept")
        self.assertEqual(workspace.status(Store(self.state), created["id"])["state"], "finished")


if __name__ == "__main__":
    import unittest

    unittest.main()
