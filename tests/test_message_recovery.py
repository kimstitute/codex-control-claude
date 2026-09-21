"""P3 process-boundary and atomicity tests for message delivery receipts."""

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

from claude_control import messages, scheduler, tasks  # noqa: E402
from claude_control.store import CLAIM_SECONDS, ControlError, Store  # noqa: E402
from test_controller import ControllerTestCase  # noqa: E402


class MessageRecoveryTests(ControllerTestCase):
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

    def subprocess_call(self, statement: str) -> subprocess.CompletedProcess[str]:
        program = (
            "import os\n"
            "from claude_control import messages, tasks\n"
            "from claude_control.store import Store\n"
            f"store = Store({str(self.state)!r})\n"
            f"{statement}\n"
            "os._exit(73)\n"
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

    def test_committed_enqueue_selection_and_reservation_survive_process_exit(self) -> None:
        task = self.create("message-crash")
        enqueued = self.subprocess_call(
            f"messages.enqueue(store, {task['id']!r}, 1, 'Persist me', 'crash-enqueue')"
        )
        self.assertEqual(enqueued.returncode, 73)
        message = messages.list_messages(Store(self.state), task_id=task["id"])["messages"][0]

        assignment = json.dumps(self.assignment("message-crash", context="revision two"))
        selected = self.subprocess_call(
            "tasks.revise(store, "
            f"{task['id']!r}, 1, {assignment}, 'crash-select', parent_run_id=None, "
            f"message_ids=[{message['id']!r}])"
        )
        self.assertEqual(selected.returncode, 73)
        selected_row = messages.list_messages(Store(self.state), task_id=task["id"])["messages"][0]
        self.assertEqual(
            (selected_row["state"], selected_row["reason"]),
            ("queued", "selected_for_revision"),
        )

        reserved = self.subprocess_call(f"tasks.submit(store, {task['id']!r}, 2, 'crash-reserve')")
        self.assertEqual(reserved.returncode, 73)
        run_id, created = tasks.submit(Store(self.state), task["id"], 2, "crash-reserve")
        bound = messages.list_messages(Store(self.state), task_id=task["id"])["messages"][0]

        self.assertFalse(created)
        self.assertEqual(bound["state"], "bound_to_run")
        self.assertEqual(bound["bindings"][0]["run_id"], run_id)
        with Store(self.state).db() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM task_runs").fetchone()[0], 1)

    def test_oversized_message_input_creates_no_run_or_binding(self) -> None:
        task = self.create("oversized-message")
        message = messages.enqueue(
            Store(self.state),
            task["id"],
            1,
            "m" * 600_000,
            "large-message",
        )
        with self.assertRaises(ControlError) as raised:
            tasks.revise(
                Store(self.state),
                task["id"],
                1,
                self.assignment("oversized-message", context="c" * 500_000),
                "select-large-message",
                parent_run_id=None,
                message_ids=[message["id"]],
            )
        listing = messages.list_messages(Store(self.state), task_id=task["id"])["messages"][0]

        self.assertEqual(raised.exception.code, "input_too_large")
        self.assertEqual((listing["state"], listing["reason"]), ("queued", "awaiting_selection"))
        self.assertEqual(listing["selections"], [])
        self.assertEqual(listing["bindings"], [])
        with Store(self.state).db() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM task_runs").fetchone()[0], 0)

    def test_never_started_retry_preserves_v4_input_and_appends_receipt(self) -> None:
        task = self.create("message-retry")
        message = messages.enqueue(
            Store(self.state), task["id"], 1, "Deliver exactly once per attempt", "retry-message"
        )
        tasks.revise(
            Store(self.state),
            task["id"],
            1,
            self.assignment("message-retry", context="revision two"),
            "select-retry-message",
            parent_run_id=None,
            message_ids=[message["id"]],
        )
        store = Store(self.state)
        first_run, _ = tasks.submit(store, task["id"], 2, "first-reservation")
        first_prompt = (store.run_dir(first_run) / "prompt.txt").read_text()
        with store.db(write=True) as db:
            db.execute(
                "UPDATE runs SET created=? WHERE id=?",
                (time.time() - CLAIM_SECONDS - 1, first_run),
            )
        store.refresh()

        retry_run, created = tasks.submit(store, task["id"], 2, "retry-reservation", retry=True)
        retry_prompt = (store.run_dir(retry_run) / "prompt.txt").read_text()
        listing = messages.list_messages(store, task_id=task["id"])["messages"][0]

        self.assertTrue(created)
        self.assertEqual(retry_prompt, first_prompt)
        self.assertEqual([row["run_id"] for row in listing["bindings"]], [first_run, retry_run])

    def test_completed_binding_becomes_attention_when_result_integrity_is_lost(self) -> None:
        task = self.create("message-integrity")
        message = messages.enqueue(
            Store(self.state), task["id"], 1, "Track delivery integrity", "integrity-message"
        )
        tasks.revise(
            Store(self.state),
            task["id"],
            1,
            self.assignment("message-integrity", context="revision two"),
            "select-integrity-message",
            parent_run_id=None,
            message_ids=[message["id"]],
        )
        submitted = self.cli(
            "task",
            "submit",
            "--task",
            task["id"],
            "--revision",
            "2",
            "--operation-id",
            "submit-integrity-message",
        )
        run = self.wait_terminal(submitted["id"])
        completed = messages.list_messages(Store(self.state), task_id=task["id"])["messages"][0]
        self.assertEqual(completed["state"], "run_completed")

        (Store(self.state).run_dir(run["id"]) / "result.json").unlink()
        damaged = messages.list_messages(Store(self.state), task_id=task["id"])["messages"][0]

        self.assertEqual(damaged["state"], "needs_attention")
        self.assertEqual(damaged["reason"], "result_integrity")

    def test_combined_dependencies_and_messages_overflow_at_dispatch_atomically(self) -> None:
        parent_context = json.dumps({"__fake_assignment__": {"report": {"summary": "p" * 600_000}}})
        parent = self.create("combined-parent", context=parent_context)
        parent_run = self.cli(
            "task",
            "submit",
            "--task",
            parent["id"],
            "--revision",
            "1",
            "--operation-id",
            "combined-parent-submit",
        )
        parent_run = self.wait_terminal(parent_run["id"])
        tasks.accept(
            Store(self.state),
            parent["id"],
            1,
            parent_run["id"],
            parent_run["result_sha256"],
            {"1": "Answer inspected.", "2": "Limits inspected."},
            "combined-parent-accept",
        )
        child = self.create("combined-child")
        message = messages.enqueue(
            Store(self.state), child["id"], 1, "m" * 500_000, "combined-message"
        )
        tasks.revise(
            Store(self.state),
            child["id"],
            1,
            self.assignment("combined-child", context="revision two"),
            "combined-revise",
            parent_run_id=None,
            message_ids=[message["id"]],
        )
        queued = scheduler.enqueue(
            Store(self.state),
            child["id"],
            2,
            "combined-enqueue",
            dependencies=[{"task_id": parent["id"], "revision": 1}],
        )

        snapshot = scheduler.tick(Store(self.state))
        waiting = next(row for row in snapshot["waiting"] if row["id"] == queued["queue_id"])
        listed = messages.list_messages(Store(self.state), task_id=child["id"])["messages"][0]

        self.assertEqual(snapshot["started"], [])
        self.assertEqual(
            (waiting["state"], waiting["blocked_reason"]), ("waiting", "input_too_large")
        )
        self.assertEqual(listed["bindings"], [])
        with Store(self.state).db() as db:
            self.assertEqual(
                db.execute(
                    "SELECT count(*) FROM task_runs WHERE task_id=?", (child["id"],)
                ).fetchone()[0],
                0,
            )

    def test_unknown_binding_blocks_redelivery_without_extra_receipt(self) -> None:
        task = self.create("unknown-redelivery")
        message = messages.enqueue(
            Store(self.state), task["id"], 1, "Never replay ambiguity", "unknown-message"
        )
        tasks.revise(
            Store(self.state),
            task["id"],
            1,
            self.assignment("unknown-redelivery", context="revision two"),
            "unknown-select",
            parent_run_id=None,
            message_ids=[message["id"]],
        )
        run_id, _ = tasks.submit(Store(self.state), task["id"], 2, "unknown-submit")
        with Store(self.state).db(write=True) as db:
            db.execute("UPDATE runs SET status='unknown' WHERE id=?", (run_id,))

        with self.assertRaises(ControlError) as raised:
            tasks.revise(
                Store(self.state),
                task["id"],
                2,
                self.assignment("unknown-redelivery", context="revision three"),
                "unknown-redeliver",
                parent_run_id=run_id,
                acknowledge_context=True,
                message_ids=[message["id"]],
                redeliver_messages=True,
            )
        listed = messages.list_messages(Store(self.state), task_id=task["id"])["messages"][0]

        self.assertEqual(raised.exception.code, "session_busy")
        self.assertEqual(len(listed["selections"]), 1)
        self.assertEqual(len(listed["bindings"]), 1)
        with Store(self.state).db(write=True) as db:
            db.execute(
                "UPDATE runs SET status='failed',reason='test_cleanup' WHERE id=?", (run_id,)
            )

    def test_superseded_redelivery_draft_cannot_create_an_extra_receipt(self) -> None:
        failure = json.dumps({"__fake_assignment__": {"behavior": "exit_fail"}})
        task = self.create("redelivery-drafts")
        message = messages.enqueue(
            Store(self.state), task["id"], 1, "Replay only the current draft", "draft-message"
        )
        tasks.revise(
            Store(self.state),
            task["id"],
            1,
            self.assignment("redelivery-drafts", context=failure),
            "draft-initial-selection",
            parent_run_id=None,
            message_ids=[message["id"]],
        )
        submitted = self.cli(
            "task",
            "submit",
            "--task",
            task["id"],
            "--revision",
            "2",
            "--operation-id",
            "draft-initial-submit",
        )
        failed = self.wait_terminal(submitted["id"])
        self.assertEqual(failed["status"], "failed")
        tasks.revise(
            Store(self.state),
            task["id"],
            2,
            self.assignment("redelivery-drafts", context=failure),
            "draft-revision-three",
            parent_run_id=failed["id"],
            acknowledge_context=True,
            message_ids=[message["id"]],
            redeliver_messages=True,
        )
        old_queue = scheduler.enqueue(Store(self.state), task["id"], 3, "draft-queue-three")
        tasks.revise(
            Store(self.state),
            task["id"],
            3,
            self.assignment("redelivery-drafts", context=failure),
            "draft-revision-four",
            parent_run_id=failed["id"],
            acknowledge_context=True,
            message_ids=[message["id"]],
            redeliver_messages=True,
        )

        with self.assertRaises(ControlError) as stale:
            tasks.submit(Store(self.state), task["id"], 3, "stale-draft-submit")
        current_run, created = tasks.submit(
            Store(self.state), task["id"], 4, "current-draft-submit"
        )
        duplicate_run, duplicate_created = tasks.submit(
            Store(self.state), task["id"], 4, "current-draft-submit"
        )
        listed = messages.list_messages(Store(self.state), task_id=task["id"])["messages"][0]
        with Store(self.state).db() as db:
            queue_state = db.execute(
                "SELECT state FROM queue_entries WHERE id=?", (old_queue["queue_id"],)
            ).fetchone()[0]

        self.assertEqual(stale.exception.code, "stale_revision")
        self.assertTrue(created)
        self.assertEqual((duplicate_run, duplicate_created), (current_run, False))
        self.assertEqual(queue_state, "superseded")
        self.assertEqual([row["revision"] for row in listed["selections"]], [2, 3, 4])
        self.assertEqual([row["run_id"] for row in listed["bindings"]], [failed["id"], current_run])
