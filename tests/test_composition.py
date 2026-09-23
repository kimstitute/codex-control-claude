"""Regression tests for durable P4 planning → P5 editing and review composition."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/claude-control/scripts"))

from claude_control import composition, tasks, workspace  # noqa: E402
from claude_control.store import ControlError, Store  # noqa: E402
from test_controller import ControllerTestCase  # noqa: E402


class CompositionTests(ControllerTestCase):
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

    def assignment(
        self,
        label: str,
        *,
        role: str,
        model: str,
        fixture: dict | None = None,
    ) -> dict:
        return {
            "id": label,
            "name": label,
            "role": role,
            "model": model,
            "project": str(self.repo),
            "objective": f"Complete the bounded {label} phase.",
            "context": json.dumps({"__fake_assignment__": fixture or {}}),
            "scope": ["Use only controller-provided evidence and authority."],
            "acceptance_criteria": ["Return a complete, bounded report."],
            "deliverable": f"The {label} report.",
            "timeout": 10,
        }

    @staticmethod
    def editor_policy() -> dict:
        return {
            "version": 1,
            "role": "executor",
            "read_paths": ["."],
            "write_paths": ["README.md"],
            "checks": {},
            "max_actions": 4,
            "max_calls": 4,
        }

    def create(
        self,
        label: str,
        *,
        plan_fixture: dict | None = None,
        reviewer_fixture: dict | None = None,
    ) -> dict:
        self.sequence += 1
        return composition.create(
            Store(self.state),
            self.assignment(label + "-plan", role="planner", model="fable", fixture=plan_fixture),
            self.assignment(label + "-edit", role="executor", model="sonnet"),
            self.editor_policy(),
            self.assignment(
                label + "-review", role="verifier", model="fable", fixture=reviewer_fixture
            ),
            f"composition-create-{self.sequence}",
            repo=str(self.repo),
            reviewer_effort="high",
        )

    def run_once(self, composition_id: str) -> dict:
        with mock.patch.object(workspace.sandbox, "require"):
            result = composition.run(Store(self.state), composition_id, once=True)
        for run_id in result["started"]:
            self.wait_terminal(run_id)
        return result

    def advance_until(self, composition_id: str, predicate, limit: int = 24) -> dict:
        for _ in range(limit):
            current = composition.status(Store(self.state), composition_id)
            if predicate(current):
                return current
            self.run_once(composition_id)
        self.fail(f"composition did not reach expected state: {current}")

    def accept_task(self, task_id: str, operation_id: str) -> dict:
        store = Store(self.state)
        shown = tasks.show(store, task_id)
        run = shown["runs"][-1]
        with store.db() as db:
            prompt = json.loads(
                db.execute(
                    "SELECT prompt FROM task_revisions WHERE task_id=? AND revision=?",
                    (task_id, shown["current_revision"]),
                ).fetchone()[0]
            )
        criteria = prompt["assignment"]["acceptance_criteria"]
        evidence = {
            str(index + 1): "Codex inspected this exact result." for index in range(len(criteria))
        }
        return tasks.accept(
            store,
            task_id,
            shown["current_revision"],
            run["id"],
            run["result_sha256"],
            evidence,
            operation_id,
        )

    def reach_plan_gate(self, composition_id: str) -> dict:
        return self.advance_until(
            composition_id,
            lambda value: (
                value["state"] == "awaiting_codex" and value["reason"] == "plan_acceptance_required"
            ),
        )

    def accept_plan(self, composition_id: str) -> dict:
        gated = self.reach_plan_gate(composition_id)
        self.accept_task(gated["workflow"]["worker_task_id"], "accept-plan-" + composition_id)
        return gated

    def reach_reviewer_member(self, composition_id: str) -> dict:
        return self.advance_until(composition_id, lambda value: "reviewer" in value["members"])

    def reach_final_gate(self, composition_id: str) -> dict:
        return self.advance_until(
            composition_id,
            lambda value: (
                value["state"] == "awaiting_codex" and value["reason"] == "final_review_ready"
            ),
        )

    def assert_error(self, expected: str, call, *args, **kwargs) -> None:
        with self.assertRaises(ControlError) as raised:
            call(*args, **kwargs)
        self.assertEqual(raised.exception.code, expected)

    def test_create_is_idempotent_and_does_not_create_workspaces(self) -> None:
        plan = self.assignment("same-plan", role="planner", model="fable")
        editor = self.assignment("same-edit", role="executor", model="sonnet")
        reviewer = self.assignment("same-review", role="verifier", model="fable")
        arguments = (Store(self.state), plan, editor, self.editor_policy(), reviewer, "same-create")

        first = composition.create(*arguments, repo=str(self.repo), reviewer_effort="high")
        (self.repo / "README.md").write_text("advanced\n", encoding="utf-8")
        self.git("add", "README.md")
        self.git("commit", "-qm", "advance after composition create")
        repeated = composition.create(*arguments, repo=str(self.repo), reviewer_effort="high")

        self.assertEqual(repeated["id"], first["id"])
        self.assertTrue(repeated["deduplicated"])
        with Store(self.state).db() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM compositions").fetchone()[0], 1)
            self.assertEqual(
                db.execute("SELECT count(*) FROM composition_members").fetchone()[0], 0
            )

    def test_leader_spec_is_critiqued_before_editor_and_test_gate_passes(self) -> None:
        (self.repo / "TEST.txt").write_text("frozen\n", encoding="utf-8")
        self.git("add", "TEST.txt")
        self.git("commit", "-qm", "add frozen test input")
        editor = self.assignment("leader-edit", role="executor", model="sonnet")
        reviewer = self.assignment("leader-review", role="verifier", model="fable")
        policy = self.editor_policy()
        policy["checks"] = {"unit": {"argv": ["/usr/bin/true"], "timeout": 5}}
        spec = {
            "version": 1,
            "id": "leader-feature",
            "name": "Leader feature",
            "objective": "Implement the leader-authored feature.",
            "context": "The leader owns this specification.",
            "scope": ["Only the selected files."],
            "acceptance_criteria": editor["acceptance_criteria"],
            "implementation_plan": ["Run the frozen check after the final edit."],
        }
        created = composition.create(
            Store(self.state),
            None,
            editor,
            policy,
            reviewer,
            "leader-composition-create",
            repo=str(self.repo),
            reviewer_effort="high",
            leader_spec=spec,
            critic_assignment=self.assignment("leader-critic", role="critic", model="fable"),
            test_contract={
                "version": 1,
                "frozen_paths": ["TEST.txt"],
                "checks": {"unit": {"baseline": "pass", "post": "pass"}},
            },
        )

        initial = composition.status(Store(self.state), created["id"])
        self.assertEqual(initial["policy"]["planning_mode"], "leader_spec")
        self.assertEqual(initial["workflow"]["budget"]["max_revisions"], 0)
        self.assertEqual(initial["members"], {})

        self.accept_plan(created["id"])
        baseline_receipt = {
            "outcome": "ok",
            "exit_code": 0,
            "duration": 0.01,
            "truncated": False,
            "stdout": "",
            "stderr": "",
            "stdout_sha256": "0" * 64,
            "stderr_sha256": "0" * 64,
        }
        with mock.patch.object(workspace.sandbox, "execute", return_value=baseline_receipt):
            editor_state = self.run_once(created["id"])
        editor_workspace = editor_state["members"]["editor"]["workspace_id"]
        while True:
            current = self.run_once(created["id"])
            if current["members"]["editor"]["workspace"]["state"] == "finished":
                break
        exported = workspace.export(Store(self.state), editor_workspace)
        final_run = current["members"]["editor"]["workspace"]["export"]["run_id"]
        check_result = {
            "outcome": "ok",
            "tree_sha256": exported["manifest"]["tree_sha256"],
        }
        with Store(self.state).db(write=True) as db:
            db.execute(
                "INSERT INTO workspace_requests VALUES(?,?,?,?)",
                (final_run, 0, json.dumps({"op": "run_check", "name": "unit"}), 1.0),
            )
            db.execute(
                "INSERT INTO workspace_receipts VALUES(?,?,?,?)",
                (final_run, 0, json.dumps(check_result), 1.0),
            )
        reviewing = self.reach_reviewer_member(created["id"])

        self.assertEqual(reviewing["test_gates"]["baseline"]["status"], "passed")
        self.assertEqual(reviewing["test_gates"]["post"]["status"], "passed")
        with Store(self.state).db() as db:
            assignment = json.loads(
                db.execute(
                    "SELECT assignment FROM workspace_tasks WHERE workspace_id=?",
                    (reviewing["members"]["editor"]["workspace_id"],),
                ).fetchone()[0]
            )
        self.assertIn("claude-control.leader-spec.v1", assignment["context"])
        self.assertIn("claude-control.test-contract.v1", assignment["context"])

    def test_test_contract_rejects_editor_write_overlap(self) -> None:
        policy = self.editor_policy()
        policy["checks"] = {"unit": {"argv": ["/usr/bin/true"], "timeout": 5}}

        self.assert_error(
            "invalid_composition",
            composition.create,
            Store(self.state),
            self.assignment("overlap-plan", role="planner", model="fable"),
            self.assignment("overlap-edit", role="executor", model="sonnet"),
            policy,
            self.assignment("overlap-review", role="verifier", model="fable"),
            "overlap-composition-create",
            repo=str(self.repo),
            test_contract={
                "version": 1,
                "frozen_paths": ["README.md"],
                "checks": {"unit": {"baseline": "pass", "post": "pass"}},
            },
        )

    def test_missing_final_tree_check_blocks_reviewer_creation(self) -> None:
        (self.repo / "TEST.txt").write_text("frozen\n", encoding="utf-8")
        self.git("add", "TEST.txt")
        self.git("commit", "-qm", "add frozen test input")
        policy = self.editor_policy()
        policy["checks"] = {"unit": {"argv": ["/usr/bin/true"], "timeout": 5}}
        created = composition.create(
            Store(self.state),
            self.assignment("missing-check-plan", role="planner", model="fable"),
            self.assignment("missing-check-edit", role="executor", model="sonnet"),
            policy,
            self.assignment("missing-check-review", role="verifier", model="fable"),
            "missing-check-composition-create",
            repo=str(self.repo),
            reviewer_effort="high",
            test_contract={
                "version": 1,
                "frozen_paths": ["TEST.txt"],
                "checks": {"unit": {"baseline": "pass", "post": "pass"}},
            },
        )
        self.accept_plan(created["id"])
        baseline_receipt = {
            "outcome": "ok",
            "exit_code": 0,
            "duration": 0.01,
            "truncated": False,
            "stdout": "",
            "stderr": "",
            "stdout_sha256": "0" * 64,
            "stderr_sha256": "0" * 64,
        }
        with mock.patch.object(workspace.sandbox, "execute", return_value=baseline_receipt):
            self.run_once(created["id"])

        blocked = self.advance_until(
            created["id"], lambda value: value["reason"] == "post_check_failed"
        )

        self.assertEqual(blocked["state"], "awaiting_codex")
        self.assertEqual(set(blocked["members"]), {"editor"})
        self.assertEqual(blocked["test_gates"]["post"]["status"], "failed")

    def test_finished_sonnet_scout_is_pinned_into_planning_context(self) -> None:
        scout_policy = {
            "version": 1,
            "role": "scout",
            "read_paths": ["."],
            "write_paths": [],
            "checks": {},
            "max_actions": 4,
            "max_calls": 4,
        }
        with mock.patch.object(workspace.sandbox, "require"):
            scout = workspace.create(
                Store(self.state),
                scout_policy,
                "composition-scout-create",
                repo=str(self.repo),
                ref="HEAD",
            )
            workspace.bind(
                Store(self.state),
                scout["id"],
                self.assignment(
                    "composition-scout",
                    role="researcher",
                    model="sonnet",
                    fixture={"workspace_operations": [[{"op": "read", "path": "README.md"}], []]},
                ),
                "composition-scout-bind",
            )
            finished = workspace.run(Store(self.state), scout["id"], max_seconds=8)
        self.assertEqual(finished["state"], "finished")

        created = composition.create(
            Store(self.state),
            self.assignment("scouted-plan", role="planner", model="fable"),
            self.assignment("scouted-edit", role="executor", model="sonnet"),
            self.editor_policy(),
            self.assignment("scouted-review", role="verifier", model="fable"),
            "scouted-composition-create",
            repo=str(self.repo),
            reviewer_effort="high",
            scout_workspace=scout["id"],
        )

        with Store(self.state).db() as db:
            row = db.execute(
                "SELECT policy FROM compositions WHERE id=?", (created["id"],)
            ).fetchone()
            policy = json.loads(row["policy"])
            workflow_row = db.execute(
                "SELECT worker_task_id FROM workflows WHERE id=?", (created["workflow_id"],)
            ).fetchone()
            prompt = json.loads(
                db.execute(
                    "SELECT prompt FROM task_revisions WHERE task_id=? AND revision=1",
                    (workflow_row["worker_task_id"],),
                ).fetchone()["prompt"]
            )
        self.assertEqual(policy["scout"]["workspace_id"], scout["id"])
        self.assertEqual(policy["scout"]["report"]["status"], "complete")
        self.assertIn("claude-control.scout.v1", prompt["assignment"]["context"])
        self.assertIn("Fixture report for researcher", prompt["assignment"]["context"])

    def test_create_recovers_child_workflow_after_outer_transaction_crash(self) -> None:
        planning = self.assignment("crash-plan", role="planner", model="fable")
        editor = self.assignment("crash-edit", role="executor", model="sonnet")
        reviewer = self.assignment("crash-review", role="verifier", model="fable")
        pinned_commit = self.git("rev-parse", "HEAD")
        original_record = tasks._record

        class SimulatedCrash(BaseException):
            pass

        def crash_outer_record(db, operation_id, fingerprint, response):
            if operation_id == "crash-create":
                raise SimulatedCrash()
            return original_record(db, operation_id, fingerprint, response)

        with (
            mock.patch.object(tasks, "_record", side_effect=crash_outer_record),
            self.assertRaises(SimulatedCrash),
        ):
            composition.create(
                Store(self.state),
                planning,
                editor,
                self.editor_policy(),
                reviewer,
                "crash-create",
                repo=str(self.repo),
                reviewer_effort="high",
            )

        (self.repo / "README.md").write_text("advanced after crash\n", encoding="utf-8")
        self.git("add", "README.md")
        self.git("commit", "-qm", "advance after outer crash")

        recovered = composition.create(
            Store(self.state),
            planning,
            editor,
            self.editor_policy(),
            reviewer,
            "crash-create",
            repo=str(self.repo),
            reviewer_effort="high",
        )
        with Store(self.state).db() as db:
            workflow_count = db.execute("SELECT count(*) FROM workflows").fetchone()[0]
            composition_count = db.execute("SELECT count(*) FROM compositions").fetchone()[0]

        self.assertEqual((workflow_count, composition_count), (1, 1))
        self.assertEqual(
            composition.status(Store(self.state), recovered["id"])["policy"]["ref"],
            pinned_commit,
        )
        self.assertEqual(
            recovered["workflow_id"],
            composition.status(Store(self.state), recovered["id"])["workflow_id"],
        )

    def test_stop_discovers_and_stops_editor_orphaned_before_member_insert(self) -> None:
        created = self.create("orphan-editor")
        self.accept_plan(created["id"])

        class SimulatedCrash(BaseException):
            pass

        with (
            mock.patch.object(composition, "_record_result", side_effect=SimulatedCrash),
            self.assertRaises(SimulatedCrash),
        ):
            self.run_once(created["id"])

        with Store(self.state).db() as db:
            self.assertEqual(
                db.execute(
                    "SELECT count(*) FROM composition_members WHERE composition_id=?",
                    (created["id"],),
                ).fetchone()[0],
                0,
            )
            orphan = db.execute("SELECT id,state FROM workspaces").fetchone()
        self.assertIsNotNone(orphan)

        stopped = composition.stop(Store(self.state), created["id"], "stop-orphan-editor")
        with Store(self.state).db() as db:
            orphan_state = db.execute(
                "SELECT state FROM workspaces WHERE id=?", (orphan["id"],)
            ).fetchone()[0]

        self.assertEqual((stopped["state"], orphan_state), ("stopped", "stopped"))

    def test_plan_gate_does_not_dispatch_or_create_editor_while_waiting(self) -> None:
        created = self.create("plan-gate")
        gated = self.reach_plan_gate(created["id"])
        calls_before = len(gated["workflow"]["runs"])

        repeated = self.run_once(created["id"])

        self.assertEqual(repeated["started"], [])
        self.assertEqual(len(repeated["workflow"]["runs"]), calls_before)
        self.assertEqual(repeated["members"], {})

    def test_only_exact_worker_acceptance_opens_editor_phase(self) -> None:
        created = self.create("exact-plan")
        gated = self.reach_plan_gate(created["id"])
        reviewer_task = gated["workflow"]["reviewer_task_id"]

        self.accept_task(reviewer_task, "accept-wrong-plan-result")
        still_gated = self.run_once(created["id"])

        self.assertEqual((still_gated["phase"], still_gated["members"]), ("plan", {}))
        self.accept_task(gated["workflow"]["worker_task_id"], "accept-exact-plan-result")
        opened = self.run_once(created["id"])
        self.assertEqual(opened["phase"], "editor")
        self.assertEqual(set(opened["members"]), {"editor"})

    def test_worker_acceptance_before_plan_review_is_rejected_without_editor(self) -> None:
        created = self.create("early-plan-accept")
        first = self.run_once(created["id"])
        worker_task = first["workflow"]["worker_task_id"]

        self.assert_error(
            "composition_plan_review_required",
            self.accept_task,
            worker_task,
            "accept-before-plan-review",
        )
        current = composition.status(Store(self.state), created["id"])
        with Store(self.state).db() as db:
            decisions = db.execute(
                "SELECT count(*) FROM review_decisions WHERE task_id=? AND kind='accept'",
                (worker_task,),
            ).fetchone()[0]

        self.assertEqual(decisions, 0)
        self.assertEqual((current["phase"], current["members"]), ("plan", {}))

    def test_editor_provenance_contains_exact_accepted_plan(self) -> None:
        created = self.create("plan-provenance")
        gated = self.accept_plan(created["id"])
        accepted_worker = tasks.show(Store(self.state), gated["workflow"]["worker_task_id"])
        accepted_run = accepted_worker["runs"][-1]
        editor = self.run_once(created["id"])["members"]["editor"]

        with Store(self.state).db() as db:
            assignment = json.loads(
                db.execute(
                    "SELECT assignment FROM workspace_tasks WHERE workspace_id=?",
                    (editor["workspace_id"],),
                ).fetchone()[0]
            )

        self.assertIn(accepted_run["id"], assignment["context"])
        self.assertIn(accepted_run["result_sha256"], assignment["context"])
        self.assertIn("acceptance_decision_id", assignment["context"])

    def test_editor_uses_commit_pinned_when_composition_was_created(self) -> None:
        commit_a = self.git("rev-parse", "HEAD")
        created = self.create("pinned-ref")
        (self.repo / "README.md").write_text("commit-b\n", encoding="utf-8")
        self.git("add", "README.md")
        self.git("commit", "-qm", "commit b")
        self.assertNotEqual(self.git("rev-parse", "HEAD"), commit_a)

        self.accept_plan(created["id"])
        editor = self.run_once(created["id"])["members"]["editor"]
        store = Store(self.state)
        with store.db() as db:
            base_commit = db.execute(
                "SELECT base_commit FROM workspaces WHERE id=?",
                (editor["workspace_id"],),
            ).fetchone()[0]
        baseline = workspace.directory(store, editor["workspace_id"]) / "baseline/README.md"

        self.assertEqual(base_commit, commit_a)
        self.assertEqual(baseline.read_text(encoding="utf-8"), "base\n")

    def test_reviewer_is_automatically_derived_from_frozen_editor_snapshot(self) -> None:
        created = self.create("automatic-review")
        self.accept_plan(created["id"])
        reviewing = self.reach_reviewer_member(created["id"])
        editor = reviewing["members"]["editor"]
        reviewer = reviewing["members"]["reviewer"]

        with Store(self.state).db() as db:
            reviewer_row = db.execute(
                "SELECT * FROM workspaces WHERE id=?", (reviewer["workspace_id"],)
            ).fetchone()
            reviewer_policy = json.loads(reviewer_row["policy"])
            editor_policy = json.loads(
                db.execute(
                    "SELECT policy FROM workspaces WHERE id=?", (editor["workspace_id"],)
                ).fetchone()[0]
            )
            reviewer_assignment = json.loads(
                db.execute(
                    "SELECT assignment FROM workspace_tasks WHERE workspace_id=?",
                    (reviewer["workspace_id"],),
                ).fetchone()[0]
            )
            accepts = db.execute(
                "SELECT count(*) FROM review_decisions WHERE task_id=? AND kind='accept'",
                (editor["task_id"],),
            ).fetchone()[0]
            editor_assignment = json.loads(
                db.execute(
                    "SELECT assignment FROM workspace_tasks WHERE workspace_id=?",
                    (editor["workspace_id"],),
                ).fetchone()[0]
            )

        self.assertEqual(reviewer_row["source_workspace"], editor["workspace_id"])
        self.assertEqual(reviewer_policy["role"], "verifier")
        self.assertEqual(reviewer_policy["read_paths"], editor_policy["read_paths"])
        self.assertEqual((reviewer_policy["write_paths"], reviewer_policy["checks"]), ([], {}))
        self.assertEqual(reviewer_assignment["model"], "fable")
        self.assertEqual(
            reviewer_assignment["acceptance_criteria"],
            editor_assignment["acceptance_criteria"],
        )
        self.assertEqual(accepts, 0)

    def test_editor_acceptance_is_blocked_until_reviewer_result_is_frozen(self) -> None:
        created = self.create("review-gate")
        self.accept_plan(created["id"])
        reviewing = self.reach_reviewer_member(created["id"])
        editor_task = reviewing["members"]["editor"]["task_id"]

        self.assert_error(
            "composition_final_review_not_ready",
            self.accept_task,
            editor_task,
            "premature-editor-accept",
        )

    def test_exact_editor_acceptance_derives_accepted_status(self) -> None:
        created = self.create("final-accept")
        self.accept_plan(created["id"])
        final = self.reach_final_gate(created["id"])
        editor_task = final["members"]["editor"]["task_id"]

        decision = self.accept_task(editor_task, "accept-final-editor")
        accepted = composition.status(Store(self.state), created["id"])

        self.assertEqual((accepted["state"], accepted["reason"]), ("accepted", "codex_accepted"))
        self.assertEqual(accepted["acceptance_decision_id"], decision["id"])

    def test_final_reviewer_revise_verdict_vetoes_editor_acceptance(self) -> None:
        created = self.create("final-veto", reviewer_fixture={"workspace_recommendation": "revise"})
        self.accept_plan(created["id"])
        vetoed = self.advance_until(
            created["id"],
            lambda value: value["reason"] == "final_review_revise",
        )
        editor_task = vetoed["members"]["editor"]["task_id"]

        self.assertEqual(vetoed["state"], "awaiting_codex")
        self.assert_error(
            "composition_review_veto",
            self.accept_task,
            editor_task,
            "accept-vetoed-editor",
        )

    def test_stop_after_final_review_revokes_editor_acceptance_gate(self) -> None:
        created = self.create("stop-final-review")
        self.accept_plan(created["id"])
        final = self.reach_final_gate(created["id"])
        editor_task = final["members"]["editor"]["task_id"]

        stopped = composition.stop(Store(self.state), created["id"], "stop-final-review")

        self.assertEqual(stopped["state"], "stopped")
        self.assert_error(
            "composition_final_review_not_ready",
            self.accept_task,
            editor_task,
            "accept-stopped-final-review",
        )
        with Store(self.state).db() as db:
            decisions = db.execute(
                "SELECT count(*) FROM review_decisions WHERE task_id=? AND kind='accept'",
                (editor_task,),
            ).fetchone()[0]
        self.assertEqual(decisions, 0)

    def test_plan_failure_halts_without_retry_or_editor_creation(self) -> None:
        created = self.create("failed-plan", plan_fixture={"behavior": "mismatch_model"})
        failed = self.advance_until(
            created["id"],
            lambda value: value["state"] == "awaiting_codex",
        )
        calls = len(failed["workflow"]["runs"])

        repeated = self.run_once(created["id"])

        self.assertEqual(repeated["reason"], "model_mismatch")
        self.assertEqual(len(repeated["workflow"]["runs"]), calls)
        self.assertEqual(repeated["members"], {})

    def test_stop_before_dispatch_is_idempotent_and_prevents_calls(self) -> None:
        created = self.create("stop-before-run")

        stopped = composition.stop(Store(self.state), created["id"], "stop-composition")
        repeated = composition.stop(Store(self.state), created["id"], "stop-composition")
        after = self.run_once(created["id"])

        self.assertEqual((stopped["state"], repeated["state"]), ("stopped", "stopped"))
        self.assertTrue(repeated["deduplicated"])
        self.assertEqual(after["started"], [])


if __name__ == "__main__":
    import unittest

    unittest.main()
