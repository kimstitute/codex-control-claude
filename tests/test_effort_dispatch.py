"""Effort capability probes remain outside atomic queue admission."""

import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/claude-control/scripts"))

from claude_control import scheduler, tasks
from claude_control.store import ControlError, Store
from test_controller import ControllerTestCase


class EffortDispatchTests(ControllerTestCase):
    def assignment(self, label):
        return {
            "id": label,
            "name": label,
            "role": "executor",
            "project": str(self.project),
            "objective": "Return the supplied answer.",
            "context": "bounded input",
            "scope": ["Supplied text only."],
            "acceptance_criteria": ["Return a structured answer."],
            "deliverable": "A concise answer.",
            "timeout": 10,
            "effort": "medium",
        }

    def queue(self, label):
        task = tasks.create(Store(self.state), self.assignment(label), "create-" + label)
        queued = scheduler.enqueue(Store(self.state), task["id"], 1, "enqueue-" + label)
        return task, queued

    def test_entry_added_during_preflight_waits_for_a_later_tick(self):
        first, _ = self.queue("race-first")
        original = tasks.preflight
        inserted = {}

        def preflight_then_enqueue(store, task_id, revision, operation_id=None):
            if not inserted:
                inserted["task"], inserted["queue"] = self.queue("race-second")
            return original(store, task_id, revision, operation_id)

        with (
            mock.patch.object(tasks, "preflight", side_effect=preflight_then_enqueue),
            mock.patch.object(scheduler, "launch_worker"),
        ):
            result = scheduler.tick(Store(self.state))

        self.assertEqual(len(result["started"]), 1)
        with Store(self.state).db() as db:
            reserved_task = db.execute(
                "SELECT task_id FROM task_runs WHERE run_id=?", (result["started"][0],)
            ).fetchone()[0]
            second = db.execute(
                "SELECT state,run_id FROM queue_entries WHERE id=?",
                (inserted["queue"]["queue_id"],),
            ).fetchone()
        self.assertEqual(reserved_task, first["id"])
        self.assertEqual(tuple(second), ("waiting", None))

    def test_transient_probe_failure_stays_waiting_and_is_retriable(self):
        task, queued = self.queue("probe-transient")
        with mock.patch.object(
            tasks,
            "preflight",
            side_effect=ControlError("effort_probe_failed", "temporary probe failure"),
        ):
            failed = scheduler.tick(Store(self.state))

        waiting = next(row for row in failed["waiting"] if row["id"] == queued["queue_id"])
        self.assertEqual(failed["started"], [])
        self.assertEqual(waiting["blocked_reason"], "effort_probe_failed")
        self.assertEqual(waiting["terminal_block"], 0)
        self.assertEqual(Store(self.state).list_all()["runs"], [])
        self.assertEqual(Store(self.state).list_all()["sessions"], [])

        with mock.patch.object(scheduler, "launch_worker"):
            retried = scheduler.tick(Store(self.state))
        self.assertEqual(len(retried["started"]), 1)
        with Store(self.state).db() as db:
            linked = db.execute(
                "SELECT task_id FROM task_runs WHERE run_id=?", (retried["started"][0],)
            ).fetchone()[0]
        self.assertEqual(linked, task["id"])

    def test_recorded_dispatch_operation_skips_replay_probe(self):
        task, queued = self.queue("probe-replay")
        store = Store(self.state)
        operation = f"dispatch:{store.config['installation_id']}:{queued['queue_id']}"
        with mock.patch.object(scheduler, "launch_worker"):
            result = scheduler.tick(store)
        self.assertEqual(len(result["started"]), 1)

        reopened = Store(self.state)
        with mock.patch.object(
            Store,
            "preflight_effort",
            side_effect=AssertionError("replay must not probe"),
        ) as probe:
            self.assertIsNone(tasks.preflight(reopened, task["id"], 1, operation))
        probe.assert_not_called()


if __name__ == "__main__":
    import unittest

    unittest.main()
