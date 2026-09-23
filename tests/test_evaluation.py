"""Tests for read-only composition evaluation."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/claude-control/scripts"))

from claude_control import composition, evaluation  # noqa: E402
from claude_control.store import ControlError, Store  # noqa: E402
from test_controller import ControllerTestCase  # noqa: E402


class EvaluationTests(ControllerTestCase):
    def setUp(self) -> None:
        super().setUp()
        subprocess.run(["git", "-C", str(self.project), "init", "-q"], check=True)
        subprocess.run(["git", "-C", str(self.project), "config", "user.name", "Test"], check=True)
        subprocess.run(
            ["git", "-C", str(self.project), "config", "user.email", "test@example.invalid"],
            check=True,
        )
        (self.project / "README.md").write_text("base\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.project), "add", "README.md"], check=True)
        subprocess.run(["git", "-C", str(self.project), "commit", "-qm", "base"], check=True)
        self.sequence = 0

    def assignment(self, label, role, model):
        return {
            "id": label,
            "name": label,
            "role": role,
            "model": model,
            "project": str(self.project),
            "objective": "Evaluate the bounded fixture.",
            "context": "Fixture context.",
            "scope": ["Fixture scope."],
            "acceptance_criteria": ["Fixture criterion."],
            "deliverable": "Fixture deliverable.",
            "timeout": 10,
        }

    def create(self, label, routing_metadata=None):
        self.sequence += 1
        return composition.create(
            Store(self.state),
            self.assignment(label + "-plan", "planner", "fable"),
            self.assignment(label + "-edit", "executor", "sonnet"),
            {
                "version": 1,
                "role": "executor",
                "read_paths": ["."],
                "write_paths": ["README.md"],
                "checks": {},
            },
            self.assignment(label + "-review", "verifier", "fable"),
            f"evaluate-create-{self.sequence}",
            repo=str(self.project),
            reviewer_effort="high",
            routing_metadata=routing_metadata,
        )

    def assert_control_error(self, code, call, *args):
        with self.assertRaises(ControlError) as raised:
            call(*args)
        self.assertEqual(raised.exception.code, code)

    def test_evaluate_uses_terminal_denominator_and_does_not_mutate(self) -> None:
        active = self.create("active")["id"]
        ready = self.create("ready")["id"]
        failed = self.create("failed")["id"]
        accepted = self.create("accepted")["id"]
        with Store(self.state).db(write=True) as db:
            db.execute(
                "UPDATE compositions SET state='awaiting_codex',reason='final_review_ready' "
                "WHERE id=?",
                (ready,),
            )
            db.execute(
                "UPDATE compositions SET state='awaiting_codex',reason='final_review_blocked' "
                "WHERE id=?",
                (failed,),
            )
            db.execute(
                "UPDATE compositions SET state='awaiting_codex',reason='final_review_ready' "
                "WHERE id=?",
                (accepted,),
            )
        store = Store(self.state)
        with store.db() as db:
            before = {
                table: db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                for table in ("compositions", "task_operations", "review_decisions")
            }

        with mock.patch.object(
            evaluation,
            "_accepted",
            side_effect=lambda _db, composition_id: composition_id == accepted,
        ):
            report = evaluation.evaluate(store)

        with store.db() as db:
            after = {
                table: db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                for table in ("compositions", "task_operations", "review_decisions")
            }
        self.assertEqual(before, after)
        self.assertEqual(
            report["summary"],
            {
                "selected": 4,
                "terminal": 3,
                "in_progress": 1,
                "failed": 1,
                "ready": 1,
                "accepted": 1,
                "successful": 2,
                "unassisted_success": 2,
                "readiness_rate": evaluation._rate(2, 3),
                "acceptance_rate": evaluation._rate(1, 3),
                "unassisted_success_rate": evaluation._rate(2, 3),
            },
        )
        self.assertEqual(report["telemetry"]["missing"], 4)
        self.assertIsNone(report["telemetry"]["measured_provider_cost_usd"])
        self.assertIsNone(report["telemetry"]["measured_tokens"])
        self.assertIsNone(report["telemetry"]["cost_per_success_usd"])
        self.assertEqual(
            {entry["id"]: entry["outcome"] for entry in report["compositions"]},
            {active: "in_progress", ready: "ready", failed: "failed", accepted: "accepted"},
        )

    def test_zero_terminal_and_selection_errors(self) -> None:
        created = self.create("only-active")["id"]
        report = evaluation.evaluate(Store(self.state), [created])
        self.assertIsNone(report["summary"]["readiness_rate"]["value"])
        self.assert_control_error(
            "invalid_arguments", evaluation.evaluate, Store(self.state), [created, created]
        )
        self.assert_control_error(
            "composition_not_found", evaluation.evaluate, Store(self.state), ["missing"]
        )

    def test_stratification_preserves_recorded_and_unrecorded_routes(self) -> None:
        routed = self.create(
            "routed",
            routing_metadata={
                "version": 1,
                "task_type": "bugfix",
                "risk_class": "R1",
                "routing_policy_version": "manual.v1",
                "model_selection_reason": "A bounded routine implementation.",
                "parent": None,
            },
        )["id"]
        legacy = self.create("legacy")["id"]

        report = evaluation.evaluate(Store(self.state), stratify=["risk_class", "task_type"])

        groups = report["stratification"]["groups"]
        self.assertEqual(report["stratification"]["fields"], ["risk_class", "task_type"])
        self.assertEqual(
            [group["values"] for group in groups],
            [
                {"risk_class": "R1", "task_type": "bugfix"},
                {"risk_class": "unrecorded", "task_type": "unrecorded"},
            ],
        )
        self.assertEqual(groups[0]["composition_ids"], [routed])
        self.assertEqual(groups[1]["composition_ids"], [legacy])
        self.assertEqual(sum(group["summary"]["selected"] for group in groups), 2)
        entries = {entry["id"]: entry for entry in report["compositions"]}
        self.assertEqual(entries[routed]["routing"]["routing_policy_version"], "manual.v1")
        self.assertIsNone(entries[legacy]["routing"])
        self.assert_control_error(
            "invalid_arguments",
            evaluation.evaluate,
            Store(self.state),
            None,
            ["risk_class", "risk_class"],
        )

    def test_telemetry_requires_cost_and_all_exact_token_fields(self) -> None:
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        db.execute(
            "CREATE TABLE run_telemetry(run_id TEXT PRIMARY KEY,usage TEXT,provider_cost_usd REAL)"
        )
        complete = {key: index + 1 for index, key in enumerate(evaluation.TOKEN_KEYS)}
        db.execute(
            "INSERT INTO run_telemetry VALUES(?,?,?)",
            ("complete", json.dumps(complete), 0.25),
        )
        db.execute(
            "INSERT INTO run_telemetry VALUES(?,?,?)",
            ("partial", json.dumps({"input_tokens": 1}), 0.1),
        )

        self.assertEqual(evaluation._telemetry(db, ["complete"]), ("measured", 0.25, complete))
        self.assertEqual(evaluation._telemetry(db, ["complete", "partial"])[0], "partial")
        self.assertEqual(evaluation._telemetry(db, ["partial"]), ("partial", None, None))
        self.assertEqual(evaluation._telemetry(db, ["missing"]), ("missing", None, None))


if __name__ == "__main__":
    import unittest

    unittest.main()
