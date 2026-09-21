"""Process-boundary and immutable-input recovery tests for the P2 scheduler."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
SCRIPTS = Path(__file__).resolve().parents[1] / "plugins/claude-control/scripts"
sys.path.insert(0, str(SCRIPTS))

from claude_control import scheduler, task_contracts, tasks  # noqa: E402
from claude_control.store import CLAIM_SECONDS, Store  # noqa: E402
from test_controller import ControllerTestCase  # noqa: E402


class SchedulerRecoveryTests(ControllerTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.sequence = 0

    def assignment(self, label: str, *, context: str = "") -> dict:
        return {
            "id": label,
            "name": label,
            "role": "executor",
            "project": str(self.project),
            "objective": "Return a supplied-text answer.",
            "context": context,
            "scope": ["Supplied text only."],
            "acceptance_criteria": ["Answer the objective.", "State limitations."],
            "deliverable": "A concise answer.",
            "timeout": 10,
        }

    def create(self, label: str, *, context: str = "") -> dict:
        self.sequence += 1
        return tasks.create(
            Store(self.state),
            self.assignment(label, context=context),
            f"create-{label}-{self.sequence}",
        )

    def enqueue(self, task: dict, dependencies: list[dict] | None = None) -> dict:
        self.sequence += 1
        return scheduler.enqueue(
            Store(self.state),
            task["id"],
            task["current_revision"],
            f"enqueue-{self.sequence}",
            dependencies=dependencies,
        )

    def complete(self, task: dict) -> dict:
        self.sequence += 1
        run = self.cli(
            "task",
            "submit",
            "--task",
            task["id"],
            "--revision",
            str(task["current_revision"]),
            "--operation-id",
            f"submit-{self.sequence}",
        )
        return self.wait_terminal(run["id"])

    def accept(self, task: dict, run: dict, operation: str) -> dict:
        return tasks.accept(
            Store(self.state),
            task["id"],
            task["current_revision"],
            run["id"],
            run["result_sha256"],
            {"1": "Answer inspected.", "2": "Limits inspected."},
            operation,
        )

    def coordinator(self, *, crash_phase: str | None = None) -> subprocess.CompletedProcess[str]:
        setup = ""
        if crash_phase is not None:
            setup = (
                "\ndef checkpoint(phase, run_ids):\n"
                f"    if phase == {crash_phase!r} and run_ids:\n"
                "        os._exit(71)\n"
                "scheduler._checkpoint = checkpoint\n"
            )
        program = (
            "import os\n"
            "from claude_control import scheduler\n"
            "from claude_control.store import Store\n"
            f"store = Store({str(self.state)!r})\n"
            f"{setup}"
            "scheduler.tick(store)\n"
        )
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(SCRIPTS)
        return subprocess.run(
            [sys.executable, "-c", program],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
            env=environment,
        )

    def task_runs(self, task_id: str) -> list[dict]:
        with Store(self.state).db() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT r.* FROM runs r JOIN task_runs t ON t.run_id=r.id "
                    "WHERE t.task_id=? ORDER BY r.rowid",
                    (task_id,),
                )
            ]

    def expire(self, run_id: str) -> None:
        store = Store(self.state)
        with store.db(write=True) as db:
            db.execute(
                "UPDATE runs SET created=? WHERE id=?",
                (time.time() - CLAIM_SECONDS - 1, run_id),
            )
        store.refresh()

    def test_crash_after_reservation_requires_explicit_retry(self) -> None:
        task = self.create("reserved-crash")
        queued = self.enqueue(task)

        crashed = self.coordinator(crash_phase="reserved")

        self.assertEqual(crashed.returncode, 71)
        first = self.task_runs(task["id"])[0]
        self.assertEqual(first["status"], "pending")
        self.assertFalse((Store(self.state).run_dir(first["id"]) / "invocation.json").exists())
        self.expire(first["id"])
        self.assertEqual(self.task_runs(task["id"])[0]["status"], "launch_failed")
        self.assertEqual(scheduler.tick(Store(self.state))["started"], [])

        retried, created = tasks.submit(
            Store(self.state), task["id"], 1, "explicit-retry", retry=True
        )

        self.assertTrue(created)
        self.assertNotEqual(retried, first["id"])
        with Store(self.state).db() as db:
            queue = db.execute(
                "SELECT * FROM queue_entries WHERE id=?", (queued["queue_id"],)
            ).fetchone()
        self.assertEqual((queue["state"], queue["run_id"]), ("reserved", retried))
        self.assertFalse((Store(self.state).run_dir(retried) / "invocation.json").exists())

    def test_crash_after_launch_leaves_one_surviving_execution(self) -> None:
        context = json.dumps({"__fake_assignment__": {"behavior": "sleep", "sleep_seconds": 0.4}})
        task = self.create("launched-crash", context=context)
        self.enqueue(task)

        crashed = self.coordinator(crash_phase="launched")
        self.assertEqual(crashed.returncode, 71)
        first = self.task_runs(task["id"])[0]
        result = scheduler.dispatch(Store(self.state), max_seconds=3)
        settled = self.wait_terminal(first["id"])

        self.assertEqual(settled["status"], "completed")
        self.assertEqual(result["started"], [])
        self.assertEqual(len(self.task_runs(task["id"])), 1)

    def test_admitted_child_remains_valid_after_parent_artifact_disappears(self) -> None:
        parent = self.create("immutable-parent")
        parent_run = self.complete(parent)
        self.accept(parent, parent_run, "accept-parent")
        child = self.create("immutable-child")
        self.enqueue(child, [{"task_id": parent["id"], "revision": 1}])

        dispatched = self.coordinator()
        self.assertEqual(dispatched.returncode, 0, dispatched.stderr)
        child_run = self.task_runs(child["id"])[0]
        (Store(self.state).run_dir(parent_run["id"]) / "result.json").unlink()
        settled = self.wait_terminal(child_run["id"])
        report = self.cli("report", "--run", child_run["id"])

        self.assertEqual(settled["status"], "completed")
        self.assertEqual(report["format_status"], "valid")

    def test_parent_revision_change_terminally_blocks_waiting_child(self) -> None:
        parent = self.create("changed-parent")
        child = self.create("changed-child")
        queued = self.enqueue(child, [{"task_id": parent["id"], "revision": 1}])
        tasks.revise(
            Store(self.state),
            parent["id"],
            1,
            self.assignment("changed-parent", context="revision two"),
            "revise-parent",
            parent_run_id=None,
        )

        first = scheduler.tick(Store(self.state))
        second = scheduler.tick(Store(self.state))
        waiting = next(row for row in second["waiting"] if row["id"] == queued["queue_id"])

        self.assertEqual(first["started"], [])
        self.assertEqual(second["started"], [])
        self.assertEqual(waiting["blocked_reason"], "dependency_changed")
        self.assertEqual(waiting["terminal_block"], 1)

    def test_oversized_materialized_input_consumes_no_queue_entry_or_run(self) -> None:
        large_report = "p" * 600_000
        parent_context = json.dumps({"__fake_assignment__": {"report": {"summary": large_report}}})
        parent = self.create("large-parent", context=parent_context)
        parent_run = self.complete(parent)
        self.accept(parent, parent_run, "accept-large-parent")
        child = self.create("large-child", context="c" * 500_000)
        queued = self.enqueue(child, [{"task_id": parent["id"], "revision": 1}])

        snapshot = scheduler.tick(Store(self.state))

        waiting = next(row for row in snapshot["waiting"] if row["id"] == queued["queue_id"])
        self.assertEqual(snapshot["started"], [])
        self.assertEqual(
            (waiting["state"], waiting["blocked_reason"]), ("waiting", "input_too_large")
        )
        self.assertEqual(waiting["terminal_block"], 1)
        self.assertEqual(self.task_runs(child["id"]), [])

    def test_retry_reuses_input_pinned_to_first_acceptance(self) -> None:
        parent = self.create("decision-parent")
        parent_run = self.complete(parent)
        first_decision = self.accept(parent, parent_run, "accept-first")
        child = self.create("decision-child")
        queued = self.enqueue(child, [{"task_id": parent["id"], "revision": 1}])
        self.assertEqual(self.coordinator(crash_phase="reserved").returncode, 71)
        first_run = self.task_runs(child["id"])[0]
        with Store(self.state).db() as db:
            first_input = db.execute(
                "SELECT prompt FROM execution_inputs WHERE run_id=?", (first_run["id"],)
            ).fetchone()[0]
        self.expire(first_run["id"])
        self.accept(parent, parent_run, "accept-redundant")

        retry_run, created = tasks.submit(
            Store(self.state), child["id"], 1, "retry-identical-input", retry=True
        )

        self.assertTrue(created)
        with Store(self.state).db() as db:
            retry_input = db.execute(
                "SELECT prompt FROM execution_inputs WHERE run_id=?", (retry_run,)
            ).fetchone()[0]
            queue = db.execute(
                "SELECT * FROM queue_entries WHERE id=?", (queued["queue_id"],)
            ).fetchone()
        dependency = task_contracts.read(retry_input)["dependencies"][0]
        self.assertEqual(retry_input, first_input)
        self.assertEqual(dependency["decision_id"], first_decision["id"])
        self.assertEqual(queue["run_id"], retry_run)

    def test_dispatch_deadline_does_not_stop_an_existing_worker(self) -> None:
        context = json.dumps({"__fake_assignment__": {"behavior": "sleep", "sleep_seconds": 0.35}})
        task = self.create("deadline-worker", context=context)
        self.enqueue(task)
        launched = self.coordinator()
        self.assertEqual(launched.returncode, 0, launched.stderr)
        run = self.task_runs(task["id"])[0]

        result = scheduler.dispatch(Store(self.state), max_seconds=0.05)
        settled = self.wait_terminal(run["id"])

        self.assertEqual(result["reason"], "deadline")
        self.assertEqual(result["started"], [])
        self.assertEqual(settled["status"], "completed")
        self.assertEqual(len(self.task_runs(task["id"])), 1)
