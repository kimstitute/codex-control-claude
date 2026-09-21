"""Integration contracts for immutable Claude effort settings."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/claude-control/scripts"))

from claude_control import tasks, workflow, workspace  # noqa: E402
from claude_control.assignments import normalize_assignment  # noqa: E402
from claude_control.orchestration import report  # noqa: E402
from claude_control.store import ControlError, Store  # noqa: E402
from test_controller import ControllerTestCase  # noqa: E402


class EffortIntegrationTests(ControllerTestCase):
    def assignment(self, label: str, *, effort="absent", fixture=None, **changes) -> dict:
        value = {
            "id": label,
            "name": label,
            "role": "executor",
            "model": "sonnet",
            "project": str(self.project),
            "objective": "Return a bounded supplied-text result.",
            "context": json.dumps({"__fake_assignment__": fixture or {}}),
            "scope": ["Use supplied text only."],
            "acceptance_criteria": ["Return the required report."],
            "deliverable": "A concise result.",
            "timeout": 10,
        }
        if effort != "absent":
            value["effort"] = effort
        value.update(changes)
        return value

    def raw_start(self, label: str, request: str, effort="absent") -> dict:
        arguments = [
            "start",
            "--name",
            label,
            "--model",
            "sonnet",
            "--role",
            "executor",
            "--project",
            str(self.project),
            "--prompt-file",
            str(self.prompt({"text": label})),
            "--request-id",
            request,
            "--timeout",
            "10",
        ]
        if effort != "absent":
            arguments.extend(("--effort", effort))
        return self.cli(*arguments)

    def invocation_argv(self, run_id: str) -> list[str]:
        return json.loads(
            (Store(self.state).run_dir(run_id) / "invocation.json").read_text(encoding="utf-8")
        )["argv"]

    def assert_error(self, code: str, call, *args, **kwargs) -> None:
        with self.assertRaises(ControlError) as raised:
            call(*args, **kwargs)
        self.assertEqual(raised.exception.code, code)

    def test_raw_continuations_and_restart_inherit_pinned_effort(self) -> None:
        first = self.raw_start("effort-session", "effort-start", "xhigh")
        self.assertEqual(self.wait_terminal(first["id"])["status"], "completed")
        runs = [first]
        for command in ("followup", "resume", "restart"):
            arguments = [
                command,
                "--session",
                first["session_id"],
                "--prompt-file",
                str(self.prompt({"text": command})),
                "--request-id",
                "effort-" + command,
                "--timeout",
                "10",
            ]
            if command == "restart":
                arguments.append("--acknowledge-context")
            current = self.cli(*arguments)
            self.assertEqual(self.wait_terminal(current["id"])["status"], "completed")
            runs.append(current)

        store = Store(self.state)
        self.assertEqual(store.session(first["session_id"])["effort"], "xhigh")
        for run in runs:
            self.assertEqual(store.get_run(run["id"])["effort"], "xhigh")
            self.assertEqual(report(store, run["id"])["requested_effort"], "xhigh")
            argv = self.invocation_argv(run["id"])
            self.assertEqual(argv[argv.index("--effort") + 1], "xhigh")

    def test_explicit_continuation_mismatch_and_request_replay_change_are_rejected(self) -> None:
        first = self.raw_start("effort-conflict", "same-effort-request", "low")
        self.assertEqual(self.wait_terminal(first["id"])["status"], "completed")
        mismatch = self.cli(
            "followup",
            "--session",
            first["session_id"],
            "--prompt-file",
            str(self.prompt({"text": "changed"})),
            "--request-id",
            "mismatched-followup",
            "--timeout",
            "10",
            "--effort",
            "max",
            expected=2,
        )
        replay = self.cli(
            "start",
            "--name",
            "effort-conflict",
            "--model",
            "sonnet",
            "--role",
            "executor",
            "--project",
            str(self.project),
            "--prompt-file",
            str(self.prompt({"text": "effort-conflict"})),
            "--request-id",
            "same-effort-request",
            "--timeout",
            "10",
            "--effort",
            "max",
            expected=2,
        )

        self.assertEqual(mismatch["error"], "session_incompatible")
        self.assertEqual(replay["error"], "request_conflict")

    def test_legacy_none_omits_flag_and_poison_environment_is_not_forwarded(self) -> None:
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_EFFORT_LEVEL": "max"}):
            run = self.raw_start("legacy-effort", "legacy-effort")
            self.assertEqual(self.wait_terminal(run["id"])["status"], "completed")

        self.assertIsNone(Store(self.state).session(run["session_id"])["effort"])
        self.assertIsNone(Store(self.state).get_run(run["id"])["effort"])
        self.assertNotIn("--effort", self.invocation_argv(run["id"]))
        self.assertNotIn("requested_effort", report(Store(self.state), run["id"]))
        result = json.loads((Store(self.state).run_dir(run["id"]) / "result.json").read_text())
        self.assertNotIn("CLAUDE_CODE_EFFORT_LEVEL", json.dumps(result))
        mismatch = self.cli(
            "resume",
            "--session",
            run["session_id"],
            "--prompt-file",
            str(self.prompt({"text": "legacy mismatch"})),
            "--request-id",
            "legacy-effort-mismatch",
            "--timeout",
            "10",
            "--effort",
            "low",
            expected=2,
        )
        self.assertEqual(mismatch["error"], "session_incompatible")

    def test_assignment_effort_values_are_strict_and_null_is_invalid(self) -> None:
        for value in ("low", "medium", "high", "xhigh", "max"):
            with self.subTest(value=value):
                self.assertEqual(
                    normalize_assignment(self.assignment(value, effort=value))["effort"], value
                )
        for value in (None, True, "", "ultra", 3):
            with self.subTest(invalid=value):
                self.assert_error(
                    "invalid_assignment", normalize_assignment, self.assignment("bad", effort=value)
                )
        self.assertNotIn("effort", normalize_assignment(self.assignment("omitted")))

    def test_task_attach_inherits_after_operation_fingerprint_and_revision_is_fixed(self) -> None:
        first = self.raw_start("attach-session", "attach-session", "high")
        parent = self.wait_terminal(first["id"])
        store = Store(self.state)
        supplied = self.assignment("attached")
        task = tasks.create(
            store,
            supplied,
            "attach-task",
            session_ref=first["session_id"],
            parent_run_id=first["id"],
        )
        with store.db() as db:
            prompt = json.loads(
                db.execute(
                    "SELECT prompt FROM task_revisions WHERE task_id=? AND revision=1",
                    (task["id"],),
                ).fetchone()[0]
            )
        self.assertEqual(prompt["assignment"]["effort"], "high")
        self.assert_error(
            "request_conflict",
            tasks.create,
            store,
            self.assignment("attached", effort="high"),
            "attach-task",
            session_ref=first["session_id"],
            parent_run_id=first["id"],
        )
        revision = tasks.revise(
            store,
            task["id"],
            1,
            supplied,
            "inherit-revision",
            parent_run_id=parent["id"],
        )
        self.assertEqual(revision["current_revision"], 2)
        self.assert_error(
            "revision_conflict",
            tasks.revise,
            store,
            task["id"],
            2,
            self.assignment("attached", effort="max"),
            "change-effort",
            parent_run_id=parent["id"],
        )

    def test_workflow_worker_and_reviewer_efforts_are_independent(self) -> None:
        created = workflow.create(
            Store(self.state),
            self.assignment("effort-flow", effort="low"),
            "effort-flow",
            reviewer_effort="max",
        )
        with Store(self.state).db() as db:
            prompts = {
                task_id: json.loads(
                    db.execute(
                        "SELECT prompt FROM task_revisions WHERE task_id=? AND revision=1",
                        (task_id,),
                    ).fetchone()[0]
                )["assignment"]
                for task_id in (created["worker_task_id"], created["reviewer_task_id"])
            }

        self.assertEqual(prompts[created["worker_task_id"]]["effort"], "low")
        self.assertEqual(prompts[created["reviewer_task_id"]]["effort"], "max")
        without = workflow.create(
            Store(self.state),
            self.assignment("no-review-effort", effort="high"),
            "no-review-effort",
        )
        with Store(self.state).db() as db:
            reviewer = json.loads(
                db.execute(
                    "SELECT prompt FROM task_revisions WHERE task_id=? AND revision=1",
                    (without["reviewer_task_id"],),
                ).fetchone()[0]
            )["assignment"]
        self.assertNotIn("effort", reviewer)

    def test_workspace_operation_revisions_keep_bound_effort(self) -> None:
        subprocess.run(["/usr/bin/git", "-C", str(self.project), "init", "-q"], check=True)
        subprocess.run(
            ["/usr/bin/git", "-C", str(self.project), "config", "user.name", "Test"],
            check=True,
        )
        subprocess.run(
            [
                "/usr/bin/git",
                "-C",
                str(self.project),
                "config",
                "user.email",
                "test@example.invalid",
            ],
            check=True,
        )
        (self.project / "README.md").write_text("base\n")
        subprocess.run(["/usr/bin/git", "-C", str(self.project), "add", "README.md"], check=True)
        subprocess.run(
            ["/usr/bin/git", "-C", str(self.project), "commit", "-qm", "base"], check=True
        )
        policy = {
            "version": 1,
            "role": "executor",
            "read_paths": ["."],
            "write_paths": ["README.md"],
            "checks": {},
        }
        fixture = {
            "workspace_operations": [
                [{"op": "write", "path": "README.md", "content": "changed\n"}],
                [],
            ]
        }
        with mock.patch.object(workspace.sandbox, "require"):
            created = workspace.create(
                Store(self.state), policy, "effort-workspace-create", repo=str(self.project)
            )
            bound = workspace.bind(
                Store(self.state),
                created["id"],
                self.assignment("effort-workspace", effort="max", fixture=fixture),
                "effort-workspace-bind",
            )
            finished = workspace.run(Store(self.state), created["id"], max_seconds=8)
        self.assertEqual(finished["state"], "finished")
        with Store(self.state).db() as db:
            efforts = [
                json.loads(row[0])["assignment"].get("effort")
                for row in db.execute(
                    "SELECT prompt FROM task_revisions WHERE task_id=? ORDER BY revision",
                    (bound["task_id"],),
                )
            ]
        self.assertEqual(efforts, ["max", "max"])


if __name__ == "__main__":
    import unittest

    unittest.main()
