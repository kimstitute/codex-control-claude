"""Workflow command boundaries: explicit durations, policy errors and top-level overview."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from test_controller import ControllerTestCase
from test_queue_migration import assignment


class WorkflowCliTests(ControllerTestCase):
    def create_workflow(self):
        path = self.root / "assignment.json"
        path.write_text(json.dumps(assignment(self.project)))
        return self.cli(
            "workflow", "create", "--assignment-file", str(path), "--operation-id", "create"
        )

    def test_zero_second_loop_and_overview_make_no_call(self):
        created = self.create_workflow()
        result = self.cli(
            "workflow", "run", "--workflow", created["id"], "--until-idle", "--max-seconds", "0"
        )
        self.assertEqual((result["started"], result["loop_reason"]), ([], "deadline"))
        self.assertEqual(self.cli("list")["runs"], [])
        self.assertEqual(self.cli("overview")["workflows"][0]["id"], created["id"])
        self.assertEqual(self.cli("overview", "--attention")["workflows"], [])

    def test_loop_requires_explicit_finite_duration(self):
        created = self.create_workflow()
        for extra in [
            [],
            ["--max-seconds", "nan"],
            ["--max-seconds", "inf"],
            ["--max-seconds", "-1"],
            ["--max-seconds", "3601"],
        ]:
            with self.subTest(extra=extra):
                result = self.cli(
                    "workflow",
                    "run",
                    "--workflow",
                    created["id"],
                    "--until-idle",
                    *extra,
                    expected=2,
                )
                self.assertIn("error", result)
        self.assertEqual(self.cli("list")["runs"], [])

    def test_once_rejects_duration_and_create_rejects_invalid_policy(self):
        created = self.create_workflow()
        result = self.cli(
            "workflow",
            "run",
            "--workflow",
            created["id"],
            "--once",
            "--max-seconds",
            "1",
            expected=2,
        )
        self.assertIn("error", result)
        path = self.root / "assignment.json"
        for option, value in [
            ("--max-calls", "1"),
            ("--max-revisions", "11"),
            ("--dispatch-window-seconds", "nan"),
        ]:
            with self.subTest(option=option):
                self.cli(
                    "workflow",
                    "create",
                    "--assignment-file",
                    str(path),
                    "--operation-id",
                    "invalid",
                    option,
                    value,
                    expected=2,
                )
        self.assertEqual(len(self.cli("overview")["workflows"]), 1)
