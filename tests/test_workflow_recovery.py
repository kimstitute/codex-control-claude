"""P4 coordinator restart, crash and stop tests."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))
SCRIPTS = Path(__file__).resolve().parents[1] / "plugins/claude-control/scripts"
sys.path.insert(0, str(SCRIPTS))

from claude_control import scheduler, workflow  # noqa: E402
from claude_control.store import CLAIM_SECONDS, Store  # noqa: E402
from test_controller import ControllerTestCase  # noqa: E402


class WorkflowRecoveryTests(ControllerTestCase):
    def assignment(self, label: str, *, fixture: dict | None = None) -> dict:
        return {
            "id": label,
            "name": label,
            "role": "executor",
            "project": str(self.project),
            "objective": "Produce a bounded proposal.",
            "context": json.dumps({"__fake_assignment__": fixture or {}}),
            "scope": ["Supplied text only."],
            "acceptance_criteria": ["Answer the objective.", "State limitations."],
            "deliverable": "A concise proposal.",
            "timeout": 10,
        }

    def create(self, label: str, *, fixture: dict | None = None) -> dict:
        return workflow.create(
            Store(self.state), self.assignment(label, fixture=fixture), f"create-{label}"
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

    def crash_after_reservation(self, workflow_id: str) -> subprocess.CompletedProcess[str]:
        program = (
            "import os\n"
            "from claude_control import scheduler, workflow\n"
            "from claude_control.store import Store\n"
            f"store = Store({str(self.state)!r})\n"
            "def checkpoint(phase, run_ids):\n"
            "    if phase == 'reserved' and run_ids:\n"
            "        os._exit(71)\n"
            "scheduler._checkpoint = checkpoint\n"
            f"workflow.run(store, {workflow_id!r}, once=True)\n"
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

    def crash_after_stop_recorded(self, workflow_id: str) -> subprocess.CompletedProcess[str]:
        program = (
            "import os\n"
            "from claude_control import workflow\n"
            "from claude_control.store import Store\n"
            f"store = Store({str(self.state)!r})\n"
            "def checkpoint(phase, workflow_id):\n"
            "    if phase == 'stop_recorded':\n"
            "        os._exit(72)\n"
            "workflow._checkpoint = checkpoint\n"
            f"workflow.stop(store, {workflow_id!r}, 'crash-stop')\n"
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

    def test_restart_continues_persisted_budget_and_window(self) -> None:
        created = self.create("restart")
        first = workflow.run(Store(self.state), created["id"], once=True)
        worker_run = first["started"][0]
        self.assertEqual(self.wait_terminal(worker_run)["status"], "completed")
        checkpoint = workflow.status(Store(self.state), created["id"])

        resumed = workflow.run(Store(self.state), created["id"], max_seconds=8)

        self.assertEqual(
            (resumed["state"], resumed["reason"]), ("awaiting_codex", "approve_recommended")
        )
        self.assertEqual(resumed["first_reserved_at"], checkpoint["first_reserved_at"])
        self.assertEqual(
            (checkpoint["budget"]["calls_used"], resumed["budget"]["calls_used"]), (1, 2)
        )

    def test_crash_after_reservation_never_relaunches_ambiguous_attempt(self) -> None:
        created = self.create("reserved-crash")

        crashed = self.crash_after_reservation(created["id"])

        self.assertEqual(crashed.returncode, 71)
        runs = self.task_runs(created["worker_task_id"])
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["status"], "pending")
        before = workflow.status(Store(self.state), created["id"])
        self.assertEqual(before["budget"]["calls_used"], 1)
        self.assertIsNotNone(before["first_reserved_at"])
        repeated = workflow.run(Store(self.state), created["id"], once=True)
        self.assertEqual(repeated["started"], [])
        self.assertEqual(len(self.task_runs(created["worker_task_id"])), 1)
        store = Store(self.state)
        with store.db(write=True) as db:
            db.execute(
                "UPDATE runs SET created=? WHERE id=?",
                (time.time() - CLAIM_SECONDS - 1, runs[0]["id"]),
            )
        store.refresh()
        settled = workflow.run(store, created["id"], once=True)
        self.assertEqual(settled["state"], "awaiting_codex")
        self.assertEqual(settled["budget"]["calls_used"], 1)
        self.assertEqual(len(self.task_runs(created["worker_task_id"])), 1)

    def test_two_coordinators_reserve_one_workflow_step(self) -> None:
        created = self.create("coordinator-race")

        with mock.patch.object(scheduler, "launch_worker") as launch:
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(
                    pool.map(
                        lambda _: workflow.run(Store(self.state), created["id"], once=True),
                        range(2),
                    )
                )

        started = [run_id for result in results for run_id in result["started"]]
        self.assertEqual(len(set(started)), 1)
        self.assertEqual(launch.call_count, 1)
        self.assertEqual(
            workflow.status(Store(self.state), created["id"])["budget"]["calls_used"], 1
        )

    def test_stop_active_worker_prevents_every_future_dispatch(self) -> None:
        created = self.create(
            "stop-active",
            fixture={"behavior": "sleep", "sleep_seconds": 0.6},
        )
        started = workflow.run(Store(self.state), created["id"], once=True)
        run_id = started["started"][0]

        workflow.stop(Store(self.state), created["id"], "stop-active-workflow")
        self.wait_terminal(run_id)
        final = workflow.run(Store(self.state), created["id"], once=True)

        self.assertIn(final["state"], ("stopping", "stopped"))
        self.assertEqual(final["started"], [])
        self.assertEqual(len(self.task_runs(created["worker_task_id"])), 1)

    def test_restart_after_stop_recorded_crash_finishes_durable_cancellation(self) -> None:
        created = self.create("stop-recorded-crash")
        started = workflow.run(Store(self.state), created["id"], once=True)
        run_id = started["started"][0]

        crashed = self.crash_after_stop_recorded(created["id"])

        self.assertEqual(crashed.returncode, 72)
        self.assertEqual(workflow.status(Store(self.state), created["id"])["state"], "stopping")
        resumed = workflow.run(Store(self.state), created["id"], once=True)
        self.wait_terminal(run_id)
        final = workflow.run(Store(self.state), created["id"], once=True)
        self.assertEqual(resumed["started"], [])
        self.assertEqual(final["state"], "stopped")

    def test_stop_racing_reserved_admission_never_releases_a_later_step(self) -> None:
        created = self.create("stop-race")
        reserved = threading.Event()
        release = threading.Event()

        def checkpoint(phase, run_ids):
            if phase == "reserved" and run_ids:
                reserved.set()
                self.assertTrue(release.wait(5))

        with mock.patch.object(scheduler, "_checkpoint", side_effect=checkpoint):
            with ThreadPoolExecutor(max_workers=1) as pool:
                running = pool.submit(workflow.run, Store(self.state), created["id"], once=True)
                self.assertTrue(reserved.wait(5))
                stopped = workflow.stop(Store(self.state), created["id"], "stop-racing-reservation")
                release.set()
                result = running.result(timeout=5)

        final = workflow.run(Store(self.state), created["id"], once=True)
        self.assertIn(stopped["state"], ("stopping", "stopped"))
        self.assertEqual(len(result["started"]), 1)
        self.assertEqual(final["started"], [])
        self.assertEqual(len(self.task_runs(created["reviewer_task_id"])), 0)
