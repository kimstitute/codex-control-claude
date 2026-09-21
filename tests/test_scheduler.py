"""P2 regression tests for durable dependency-aware task scheduling."""

from __future__ import annotations

import json
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/claude-control/scripts"))

from claude_control import scheduler, tasks
from claude_control.store import ControlError, Store
from test_controller import ControllerTestCase


class SchedulerTests(ControllerTestCase):
    """Exercise scheduler behavior through public Python APIs and persisted state."""

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
        submitted = self.cli(
            "task",
            "submit",
            "--task",
            task["id"],
            "--revision",
            str(task["current_revision"]),
            "--operation-id",
            f"submit-{self.sequence}",
        )
        return self.wait_terminal(submitted["id"])

    def accept(self, task: dict, run: dict) -> dict:
        self.sequence += 1
        return tasks.accept(
            Store(self.state),
            task["id"],
            task["current_revision"],
            run["id"],
            run["result_sha256"],
            {"1": "Answer inspected.", "2": "Limits inspected."},
            f"accept-{self.sequence}",
        )

    def assert_error(self, expected: str, call, *args, **kwargs) -> None:
        with self.assertRaises(ControlError) as raised:
            call(*args, **kwargs)
        self.assertEqual(raised.exception.code, expected)

    @staticmethod
    def dependency(task: dict, revision: int | None = None) -> dict:
        return {"task_id": task["id"], "revision": revision or task["current_revision"]}

    def test_enqueue_rejects_a_self_dependency(self) -> None:
        task = self.create("self")

        self.assert_error(
            "dependency_self",
            scheduler.enqueue,
            Store(self.state),
            task["id"],
            1,
            "enqueue-self",
            dependencies=[self.dependency(task)],
        )

    def test_enqueue_rejects_a_missing_dependency(self) -> None:
        task = self.create("missing")

        self.assert_error(
            "dependency_not_found",
            scheduler.enqueue,
            Store(self.state),
            task["id"],
            1,
            "enqueue-missing",
            dependencies=[{"task_id": str(uuid.uuid4()), "revision": 1}],
        )

    def test_enqueue_rejects_a_stale_dependency_revision(self) -> None:
        parent = self.create("parent-stale")
        tasks.revise(
            Store(self.state),
            parent["id"],
            1,
            self.assignment("parent-stale", context="revision two"),
            "revise-parent-stale",
            parent_run_id=None,
        )
        child = self.create("child-stale")

        self.assert_error(
            "stale_dependency",
            scheduler.enqueue,
            Store(self.state),
            child["id"],
            1,
            "enqueue-child-stale",
            dependencies=[self.dependency(parent, 1)],
        )

    def test_enqueue_rejects_a_dependency_cycle(self) -> None:
        first = self.create("cycle-first")
        second = self.create("cycle-second")
        self.enqueue(first, [self.dependency(second)])

        self.assert_error(
            "dependency_cycle",
            scheduler.enqueue,
            Store(self.state),
            second["id"],
            1,
            "enqueue-cycle-second",
            dependencies=[self.dependency(first)],
        )

    def test_manual_submit_cannot_bypass_dependency_acceptance(self) -> None:
        parent = self.create("manual-parent")
        parent_run = self.complete(parent)
        self.assertEqual(parent_run["status"], "completed")
        child = self.create("manual-child")
        self.enqueue(child, [self.dependency(parent)])

        self.assert_error(
            "dependency_not_accepted",
            tasks.submit,
            Store(self.state),
            child["id"],
            1,
            "manual-child-submit",
        )

    def test_tick_does_not_start_child_until_parent_is_accepted(self) -> None:
        parent = self.create("gate-parent")
        parent_run = self.complete(parent)
        child = self.create("gate-child")
        queued = self.enqueue(child, [self.dependency(parent)])

        blocked = scheduler.tick(Store(self.state))
        self.assertEqual(blocked["started"], [])
        waiting = next(row for row in blocked["waiting"] if row["id"] == queued["queue_id"])
        self.assertEqual(waiting["blocked_reason"], "dependency_not_accepted")

        self.accept(parent, parent_run)
        with mock.patch.object(scheduler, "launch_worker"):
            admitted = scheduler.tick(Store(self.state))
        self.assertEqual(len(admitted["started"]), 1)

    def test_blocked_dependency_does_not_prevent_independent_work(self) -> None:
        parent = self.create("blocked-parent")
        self.complete(parent)
        child = self.create("blocked-child")
        independent = self.create("independent")
        self.enqueue(child, [self.dependency(parent)])
        self.enqueue(independent)

        with mock.patch.object(scheduler, "launch_worker"):
            snapshot = scheduler.tick(Store(self.state))

        self.assertEqual(len(snapshot["started"]), 1)
        run = Store(self.state).get_run(snapshot["started"][0])
        with Store(self.state).db() as db:
            linked = db.execute(
                "SELECT task_id FROM task_runs WHERE run_id=?", (run["id"],)
            ).fetchone()[0]
        self.assertEqual(linked, independent["id"])

    def test_queue_capacity_is_independent_from_execution_capacity(self) -> None:
        for index in range(100):
            self.enqueue(self.create(f"capacity-{index}"))
        overflow = self.create("capacity-overflow")

        self.assert_error(
            "queue_full",
            scheduler.enqueue,
            Store(self.state),
            overflow["id"],
            1,
            "enqueue-overflow",
        )

    def test_six_real_workers_obey_two_slots_and_fifo_admission(self) -> None:
        queued = []
        for index in range(6):
            context = json.dumps(
                {"__fake_assignment__": {"behavior": "sleep", "sleep_seconds": 0.3}}
            )
            queued.append(self.enqueue(self.create(f"real-{index}", context=context)))

        result = self.cli("dispatch", "--until-idle", "--max-seconds", "5")

        self.assertEqual(len(set(result["started"])), 6)
        with Store(self.state).db() as db:
            rows = [
                dict(row)
                for row in db.execute(
                    "SELECT q.id AS queue_id,r.rowid AS run_order,r.started,r.finished,r.status "
                    "FROM queue_entries q JOIN runs r ON r.id=q.run_id ORDER BY q.id"
                )
            ]
        self.assertEqual([row["queue_id"] for row in rows], [row["queue_id"] for row in queued])
        self.assertTrue(all(row["status"] == "completed" for row in rows))
        self.assertEqual(
            [row["run_order"] for row in rows], sorted(row["run_order"] for row in rows)
        )
        maximum_overlap = max(
            sum(row["started"] <= point < row["finished"] for row in rows)
            for point in (row["started"] for row in rows)
        )
        self.assertEqual(maximum_overlap, self.max_parallel)

    def test_dequeue_preserves_dependencies_for_submit_and_reenqueue(self) -> None:
        parent = self.create("dequeue-parent")
        other = self.create("dequeue-other")
        child = self.create("dequeue-child")
        dependencies = [self.dependency(parent)]
        first = self.enqueue(child, dependencies)
        scheduler.dequeue(Store(self.state), first["queue_id"], "dequeue-child")

        self.assert_error(
            "dependency_not_accepted",
            tasks.submit,
            Store(self.state),
            child["id"],
            1,
            "submit-dequeued-child",
        )
        second = scheduler.enqueue(
            Store(self.state), child["id"], 1, "reenqueue-child", dependencies=dependencies
        )
        self.assertGreater(second["queue_id"], first["queue_id"])
        self.assert_error(
            "dependency_conflict",
            scheduler.enqueue,
            Store(self.state),
            child["id"],
            1,
            "reenqueue-changed",
            dependencies=[self.dependency(other)],
        )

    def test_cli_rejects_a_nonarray_dependencies_file(self) -> None:
        task = self.create("malformed-dependencies")

        result = self.cli(
            "task",
            "enqueue",
            "--task",
            task["id"],
            "--revision",
            "1",
            "--operation-id",
            "malformed-dependencies",
            "--dependencies-file",
            str(self.prompt({"task_id": task["id"], "revision": 1})),
            expected=2,
        )

        self.assertEqual(result["error"], "invalid_dependencies")

    def test_cli_rejects_max_seconds_with_once_dispatch(self) -> None:
        result = self.cli("dispatch", "--once", "--max-seconds", "1", expected=2)

        self.assertEqual(result["error"], "invalid_dispatch")

    def test_revising_an_unsubmitted_task_supersedes_its_waiting_entry(self) -> None:
        task = self.create("revise-waiting")
        queued = self.enqueue(task)

        revised = tasks.revise(
            Store(self.state),
            task["id"],
            1,
            self.assignment("revise-waiting", context="revision two"),
            "revise-waiting",
            parent_run_id=None,
        )

        self.assertEqual(revised["current_revision"], 2)
        with Store(self.state).db() as db:
            row = db.execute(
                "SELECT * FROM queue_entries WHERE id=?", (queued["queue_id"],)
            ).fetchone()
        self.assertEqual(row["state"], "superseded")

    def test_events_use_stable_exclusive_cursor_pagination(self) -> None:
        task = self.create("events")
        queued = self.enqueue(task)
        scheduler.dequeue(Store(self.state), queued["queue_id"], "dequeue-events")

        first = scheduler.events(Store(self.state), task_id=task["id"], limit=1)
        second = scheduler.events(
            Store(self.state), task_id=task["id"], after=first["next_cursor"], limit=1
        )

        self.assertEqual(len(first["events"]), 1)
        self.assertEqual(len(second["events"]), 1)
        self.assertLess(first["events"][0]["id"], second["events"][0]["id"])
        self.assertEqual(first["next_cursor"], first["events"][0]["id"])
        self.assertEqual(second["next_cursor"], second["events"][0]["id"])

    def test_two_coordinators_reserve_each_revision_once(self) -> None:
        task = self.create("coordinator-race")
        self.enqueue(task)

        with mock.patch.object(scheduler, "launch_worker") as launch:
            with ThreadPoolExecutor(max_workers=2) as pool:
                snapshots = list(pool.map(lambda _: scheduler.tick(Store(self.state)), range(2)))

        started = [run for snapshot in snapshots for run in snapshot["started"]]
        self.assertEqual(len(set(started)), 1)
        self.assertEqual(launch.call_count, 1)
        with Store(self.state).db() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM task_runs").fetchone()[0], 1)

    def test_unknown_execution_retains_a_parallel_slot(self) -> None:
        ambiguous = self.create("unknown")
        unknown_run, _ = tasks.submit(Store(self.state), ambiguous["id"], 1, "submit-unknown")
        with Store(self.state).db(write=True) as db:
            db.execute("UPDATE runs SET status='unknown' WHERE id=?", (unknown_run,))
        first = self.create("after-unknown-first")
        second = self.create("after-unknown-second")
        self.enqueue(first)
        self.enqueue(second)

        with mock.patch.object(scheduler, "launch_worker"):
            snapshot = scheduler.tick(Store(self.state))

        self.assertEqual(len(snapshot["started"]), 1)
        self.assertTrue(any(row["id"] == unknown_run for row in snapshot["active"]))
        with Store(self.state).db(write=True) as db:
            db.execute(
                "UPDATE runs SET status='failed',reason='test_cleanup' WHERE id=?", (unknown_run,)
            )

    def test_twenty_jobs_finish_admission_in_at_most_twenty_one_ticks(self) -> None:
        for index in range(20):
            self.enqueue(self.create(f"batch-{index}"))
        started: list[str] = []

        with mock.patch.object(scheduler, "launch_worker"):
            for tick_count in range(1, 22):
                snapshot = scheduler.tick(Store(self.state))
                self.assertLessEqual(len(snapshot["started"]), self.max_parallel)
                started.extend(snapshot["started"])
                if snapshot["started"]:
                    with Store(self.state).db(write=True) as db:
                        db.executemany(
                            "UPDATE runs SET status='completed' WHERE id=?",
                            [(run_id,) for run_id in snapshot["started"]],
                        )
                if len(started) == 20:
                    break

        self.assertEqual(len(set(started)), 20)
        self.assertLessEqual(tick_count, 21)
        final = scheduler.tick(Store(self.state))
        self.assertEqual((final["started"], final["waiting"]), ([], []))
        with Store(self.state).db() as db:
            self.assertEqual(
                db.execute("SELECT count(*) FROM runs WHERE status='completed'").fetchone()[0],
                20,
            )

    def test_expired_dispatch_deadline_starts_no_new_work(self) -> None:
        self.enqueue(self.create("expired-deadline"))

        with mock.patch.object(scheduler, "launch_worker") as launch:
            snapshot = scheduler.tick(Store(self.state), deadline=time.monotonic() - 1)

        self.assertEqual(snapshot["started"], [])
        self.assertEqual(launch.call_count, 0)
        self.assertEqual(len(snapshot["waiting"]), 1)

    def test_dispatch_returns_when_only_ambiguous_capacity_remains(self) -> None:
        ambiguous = [self.create(f"finite-unknown-{index}") for index in range(2)]
        unknown_runs = []
        for index, task in enumerate(ambiguous):
            run_id, _ = tasks.submit(Store(self.state), task["id"], 1, f"unknown-{index}")
            unknown_runs.append(run_id)
        with Store(self.state).db(write=True) as db:
            db.executemany(
                "UPDATE runs SET status='unknown' WHERE id=?",
                [(run_id,) for run_id in unknown_runs],
            )
        self.enqueue(self.create("finite-waiting"))

        started_at = time.monotonic()
        result = scheduler.dispatch(Store(self.state), max_seconds=0.2)
        elapsed = time.monotonic() - started_at

        self.assertLess(elapsed, 1.0)
        self.assertEqual(result["started"], [])
        self.assertEqual(result["reason"], "needs_attention")
        self.assertTrue(result["attention"])
        with Store(self.state).db(write=True) as db:
            db.executemany(
                "UPDATE runs SET status='failed',reason='test_cleanup' WHERE id=?",
                [(run_id,) for run_id in unknown_runs],
            )

    def test_historical_failed_revision_is_not_reported_as_current_attention(self) -> None:
        task = self.create("historical-attention")
        queued = self.enqueue(task)
        with mock.patch.object(scheduler, "launch_worker"):
            admitted = scheduler.tick(Store(self.state))
        run_id = admitted["started"][0]
        with Store(self.state).db(write=True) as db:
            db.execute(
                "UPDATE runs SET status='launch_failed',reason='test_failure' WHERE id=?",
                (run_id,),
            )
        revised = tasks.revise(
            Store(self.state),
            task["id"],
            1,
            self.assignment("historical-attention", context="revision two"),
            "revise-after-failure",
            parent_run_id=run_id,
            acknowledge_context=True,
        )

        snapshot = scheduler.tick(Store(self.state))

        self.assertEqual(revised["current_revision"], 2)
        self.assertEqual(snapshot["attention"], [])
        with Store(self.state).db() as db:
            old_queue = db.execute(
                "SELECT * FROM queue_entries WHERE id=?", (queued["queue_id"],)
            ).fetchone()
        self.assertEqual((old_queue["revision"], old_queue["state"]), (1, "reserved"))

    def test_dependency_failure_statuses_have_specific_block_reasons(self) -> None:
        statuses = ("failed", "cancelled", "launch_failed", "interrupted", "unknown")
        unknown_run = None
        for status in statuses:
            with self.subTest(status=status):
                parent = self.create(f"status-parent-{status}")
                run_id, _ = tasks.submit(
                    Store(self.state), parent["id"], 1, f"submit-parent-{status}"
                )
                with Store(self.state).db(write=True) as db:
                    db.execute("UPDATE runs SET status=? WHERE id=?", (status, run_id))
                child = self.create(f"status-child-{status}")
                queued = self.enqueue(child, [self.dependency(parent)])

                snapshot = scheduler.tick(Store(self.state))
                waiting = next(
                    row for row in snapshot["waiting"] if row["id"] == queued["queue_id"]
                )

                self.assertEqual(waiting["blocked_reason"], f"dependency_{status}")
                if status == "unknown":
                    unknown_run = run_id
        with Store(self.state).db(write=True) as db:
            db.execute(
                "UPDATE runs SET status='failed',reason='test_cleanup' WHERE id=?",
                (unknown_run,),
            )

    def test_dependency_execution_prompt_copies_accepted_parent_evidence(self) -> None:
        parent = self.create("prompt-parent")
        parent_run = self.complete(parent)
        decision = self.accept(parent, parent_run)
        child = self.create("prompt-child")
        self.enqueue(child, [self.dependency(parent)])

        with mock.patch.object(scheduler, "launch_worker"):
            snapshot = scheduler.tick(Store(self.state))
        self.assertEqual(len(snapshot["started"]), 1)
        prompt = json.loads(
            (Store(self.state).run_dir(snapshot["started"][0]) / "prompt.txt").read_text()
        )

        self.assertEqual(prompt["protocol"], "claude-control.task.v3")
        self.assertEqual(len(prompt["dependencies"]), 1)
        supplied = prompt["dependencies"][0]
        self.assertEqual(supplied["task_id"], parent["id"])
        self.assertEqual(supplied["revision"], 1)
        self.assertEqual(supplied["run_id"], parent_run["id"])
        self.assertEqual(supplied["result_sha256"], parent_run["result_sha256"])
        self.assertEqual(supplied["decision_id"], decision["id"])
        self.assertEqual(supplied["report"]["status"], "complete")
