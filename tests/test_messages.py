"""P3 regression tests for durable, explicitly selected task messages."""

from __future__ import annotations

import json
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/claude-control/scripts"))

from claude_control import messages, scheduler, task_contracts, tasks  # noqa: E402
from claude_control.store import ControlError, Store  # noqa: E402
from test_controller import ControllerTestCase  # noqa: E402


class MessageTests(ControllerTestCase):
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

    def enqueue_message(
        self,
        task: dict,
        content: str,
        *,
        base_revision: int | None = None,
        operation: str | None = None,
        **source,
    ) -> dict:
        self.sequence += 1
        return messages.enqueue(
            Store(self.state),
            task["id"],
            base_revision or task["current_revision"],
            content,
            operation or f"message-{self.sequence}",
            **source,
        )

    def assert_error(self, expected: str, call, *args, **kwargs) -> None:
        with self.assertRaises(ControlError) as raised:
            call(*args, **kwargs)
        self.assertEqual(raised.exception.code, expected)

    def complete(self, task: dict, *, revision: int | None = None) -> dict:
        revision = revision or task["current_revision"]
        self.sequence += 1
        run = self.cli(
            "task",
            "submit",
            "--task",
            task["id"],
            "--revision",
            str(revision),
            "--operation-id",
            f"submit-{self.sequence}",
        )
        return self.wait_terminal(run["id"])

    def test_messages_enqueue_while_active_without_starting_parallel_turns(self) -> None:
        context = json.dumps({"__fake_assignment__": {"behavior": "sleep", "sleep_seconds": 0.35}})
        task = self.create("active-messages", context=context)
        submitted = self.cli(
            "task",
            "submit",
            "--task",
            task["id"],
            "--revision",
            "1",
            "--operation-id",
            "active-submit",
        )

        first = self.enqueue_message(task, "First correction")
        second = self.enqueue_message(task, "Second correction")
        with Store(self.state).db() as db:
            run_count = db.execute("SELECT count(*) FROM task_runs").fetchone()[0]

        self.assertLess(first["seq"], second["seq"])
        self.assertEqual(run_count, 1)
        completed = self.wait_terminal(submitted["id"])
        self.assertEqual(completed["status"], "completed")
        tasks.revise(
            Store(self.state),
            task["id"],
            1,
            self.assignment("active-messages", context="next turn"),
            "revise-active-messages",
            parent_run_id=completed["id"],
            message_ids=[second["id"], first["id"]],
        )
        next_run, _ = tasks.submit(Store(self.state), task["id"], 2, "submit-active-messages")
        prompt = task_contracts.read(
            (Store(self.state).run_dir(next_run) / "prompt.txt").read_text()
        )
        self.assertEqual([item["id"] for item in prompt["messages"]], [first["id"], second["id"]])
        self.assertEqual(Store(self.state).get_run(next_run)["resume"], 1)

    def test_selected_messages_are_materialized_in_sequence_order(self) -> None:
        task = self.create("ordered-messages")
        first = self.enqueue_message(task, "First")
        second = self.enqueue_message(task, "Second")
        revised = tasks.revise(
            Store(self.state),
            task["id"],
            1,
            self.assignment("ordered-messages", context="revision two"),
            "revise-ordered",
            parent_run_id=None,
            message_ids=[second["id"], first["id"]],
        )

        run_id, created = tasks.submit(Store(self.state), task["id"], 2, "submit-ordered-messages")
        prompt = json.loads((Store(self.state).run_dir(run_id) / "prompt.txt").read_text())

        self.assertTrue(created)
        self.assertEqual(revised["current_revision"], 2)
        self.assertEqual(prompt["protocol"], "claude-control.task.v4")
        self.assertEqual(prompt["dependencies"], [])
        self.assertEqual([item["id"] for item in prompt["messages"]], [first["id"], second["id"]])
        self.assertEqual([item["content"] for item in prompt["messages"]], ["First", "Second"])

    def test_new_revision_does_not_inherit_earlier_selected_messages(self) -> None:
        task = self.create("no-inheritance")
        message = self.enqueue_message(task, "Only for revision two")
        tasks.revise(
            Store(self.state),
            task["id"],
            1,
            self.assignment("no-inheritance", context="revision two"),
            "revise-two",
            parent_run_id=None,
            message_ids=[message["id"]],
        )
        tasks.revise(
            Store(self.state),
            task["id"],
            2,
            self.assignment("no-inheritance", context="revision three"),
            "revise-three",
            parent_run_id=None,
        )

        run_id, _ = tasks.submit(Store(self.state), task["id"], 3, "submit-three")
        prompt = task_contracts.read((Store(self.state).run_dir(run_id) / "prompt.txt").read_text())

        self.assertEqual(prompt.get("messages", []), [])

    def test_selected_cancelled_message_blocks_submit_without_creating_run(self) -> None:
        task = self.create("cancel-rules")
        selected = self.enqueue_message(task, "Selected")
        tasks.revise(
            Store(self.state),
            task["id"],
            1,
            self.assignment("cancel-rules", context="revision two"),
            "select-message",
            parent_run_id=None,
            message_ids=[selected["id"]],
        )
        before = messages.list_messages(Store(self.state), task_id=task["id"])["messages"][0]
        cancelled = messages.cancel(Store(self.state), selected["id"], "cancel-selected")

        self.assertEqual((before["state"], before["reason"]), ("queued", "selected_for_revision"))
        self.assertEqual(cancelled["state"], "cancelled")
        self.assert_error(
            "message_cancelled",
            tasks.submit,
            Store(self.state),
            task["id"],
            2,
            "submit-cancelled-selection",
        )
        with Store(self.state).db() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM task_runs").fetchone()[0], 0)

    def test_bound_message_cannot_be_cancelled(self) -> None:
        task = self.create("bound-cancel")
        bound = self.enqueue_message(task, "Bound")
        tasks.revise(
            Store(self.state),
            task["id"],
            1,
            self.assignment("bound-cancel", context="revision two"),
            "select-bound",
            parent_run_id=None,
            message_ids=[bound["id"]],
        )
        tasks.submit(Store(self.state), task["id"], 2, "bind-message")

        self.assert_error(
            "message_bound",
            messages.cancel,
            Store(self.state),
            bound["id"],
            "cancel-bound",
        )

    def test_message_target_and_revision_are_enforced_at_selection(self) -> None:
        first = self.create("message-target-first")
        second = self.create("message-target-second")
        message = self.enqueue_message(first, "Targeted message")

        self.assert_error(
            "message_target_changed",
            tasks.revise,
            Store(self.state),
            second["id"],
            1,
            self.assignment("message-target-second", context="revision two"),
            "cross-target",
            parent_run_id=None,
            message_ids=[message["id"]],
        )
        tasks.revise(
            Store(self.state),
            first["id"],
            1,
            self.assignment("message-target-first", context="revision two"),
            "advance-target",
            parent_run_id=None,
        )
        self.assert_error(
            "stale_message",
            tasks.revise,
            Store(self.state),
            first["id"],
            2,
            self.assignment("message-target-first", context="revision three"),
            "stale-target",
            parent_run_id=None,
            message_ids=[message["id"]],
        )

    def test_message_operation_deduplicates_and_cancel_race_is_idempotent(self) -> None:
        task = self.create("message-idempotency")
        first = self.enqueue_message(task, "Stable", operation="same-message")
        duplicate = self.enqueue_message(task, "Stable", operation="same-message")
        self.assertEqual(first["id"], duplicate["id"])
        self.assertTrue(duplicate["deduplicated"])
        self.assert_error(
            "request_conflict",
            self.enqueue_message,
            task,
            "Changed",
            operation="same-message",
        )

        def cancel(_):
            return messages.cancel(Store(self.state), first["id"], "same-cancel")

        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(cancel, range(4)))
        self.assertEqual({result["id"] for result in results}, {first["id"]})
        self.assertEqual(sum(not result["deduplicated"] for result in results), 1)

    def test_cancel_racing_selection_never_creates_a_cancelled_binding(self) -> None:
        task = self.create("cancel-select-race")
        message = self.enqueue_message(task, "Race safely")

        def cancel():
            return messages.cancel(Store(self.state), message["id"], "race-cancel")

        def revise():
            try:
                return tasks.revise(
                    Store(self.state),
                    task["id"],
                    1,
                    self.assignment("cancel-select-race", context="revision two"),
                    "race-select",
                    parent_run_id=None,
                    message_ids=[message["id"]],
                )
            except ControlError as exc:
                return exc.code

        with ThreadPoolExecutor(max_workers=2) as pool:
            cancelled = pool.submit(cancel)
            revised = pool.submit(revise)
            cancel_result = cancelled.result()
            revise_result = revised.result()

        self.assertEqual(cancel_result["state"], "cancelled")
        if isinstance(revise_result, dict):
            self.assert_error(
                "message_cancelled",
                tasks.submit,
                Store(self.state),
                task["id"],
                2,
                "race-submit",
            )
        else:
            self.assertEqual(revise_result, "message_cancelled")
        with Store(self.state).db() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM message_bindings").fetchone()[0], 0)

    def test_database_rejects_cross_task_message_selection(self) -> None:
        source = self.create("fk-selection-source")
        target = self.create("fk-selection-target")
        message = self.enqueue_message(source, "Stay with source task")

        with self.assertRaises(sqlite3.IntegrityError), Store(self.state).db(write=True) as db:
            db.execute(
                "INSERT INTO revision_messages VALUES(?,?,?,NULL)",
                (target["id"], 1, message["id"]),
            )

    def test_database_rejects_cross_task_message_binding(self) -> None:
        source = self.create("fk-binding-source")
        message = self.enqueue_message(source, "Stay with source run")
        tasks.revise(
            Store(self.state),
            source["id"],
            1,
            self.assignment("fk-binding-source", context="revision two"),
            "fk-select",
            parent_run_id=None,
            message_ids=[message["id"]],
        )
        source_run, _ = tasks.submit(Store(self.state), source["id"], 2, "fk-source-run")
        target = self.create("fk-binding-target")
        target_run, _ = tasks.submit(Store(self.state), target["id"], 1, "fk-target-run")

        with self.assertRaises(sqlite3.IntegrityError), Store(self.state).db(write=True) as db:
            db.execute(
                "INSERT INTO message_bindings(message_id,run_id,task_id,revision,created) "
                "VALUES(?,?,?,?,?)",
                (message["id"], target_run, source["id"], 2, 0.0),
            )
        self.assertNotEqual(source_run, target_run)

    def test_selection_rejects_more_than_sixty_four_messages(self) -> None:
        task = self.create("message-selection-limit")
        selected = [self.enqueue_message(task, f"Message {index}")["id"] for index in range(65)]

        self.assert_error(
            "invalid_messages",
            tasks.revise,
            Store(self.state),
            task["id"],
            1,
            self.assignment("message-selection-limit", context="revision two"),
            "select-too-many",
            parent_run_id=None,
            message_ids=selected,
        )

    def test_one_hundred_message_capacity_is_independent_and_cancel_frees_one(self) -> None:
        task = self.create("message-capacity")
        queued = scheduler.enqueue(Store(self.state), task["id"], 1, "capacity-task-queue")
        stored = [
            self.enqueue_message(task, f"Message {index}", operation=f"capacity-message-{index}")
            for index in range(100)
        ]

        self.assert_error(
            "message_full",
            self.enqueue_message,
            task,
            "Overflow",
            operation="capacity-overflow",
        )
        messages.cancel(Store(self.state), stored[0]["id"], "capacity-cancel")
        replacement = self.enqueue_message(task, "Replacement", operation="capacity-replacement")

        self.assertGreater(replacement["seq"], stored[-1]["seq"])
        with Store(self.state).db() as db:
            queue = db.execute(
                "SELECT state FROM queue_entries WHERE id=?", (queued["queue_id"],)
            ).fetchone()[0]
            unbound = db.execute(
                "SELECT count(*) FROM messages m WHERE m.task_id=? "
                "AND NOT EXISTS(SELECT 1 FROM message_bindings b WHERE b.message_id=m.id) "
                "AND NOT EXISTS(SELECT 1 FROM message_cancellations c WHERE c.message_id=m.id)",
                (task["id"],),
            ).fetchone()[0]
        self.assertEqual((queue, unbound), ("waiting", 100))

    def test_message_events_preserve_task_identity_across_lifecycle(self) -> None:
        bound_task = self.create("event-bound")
        bound = self.enqueue_message(bound_task, "Bind this")
        tasks.revise(
            Store(self.state),
            bound_task["id"],
            1,
            self.assignment("event-bound", context="revision two"),
            "event-select",
            parent_run_id=None,
            message_ids=[bound["id"]],
        )
        tasks.submit(Store(self.state), bound_task["id"], 2, "event-bind")
        cancelled_task = self.create("event-cancelled")
        cancelled = self.enqueue_message(cancelled_task, "Cancel this")
        messages.cancel(Store(self.state), cancelled["id"], "event-cancel")

        bound_events = scheduler.events(Store(self.state), task_id=bound_task["id"], limit=100)[
            "events"
        ]
        cancelled_events = scheduler.events(
            Store(self.state), task_id=cancelled_task["id"], limit=100
        )["events"]
        bound_states = [
            row["status"]
            for row in bound_events
            if row["kind"] == "message" and row["reference"] == bound["id"]
        ]
        cancelled_states = [
            row["status"]
            for row in cancelled_events
            if row["kind"] == "message" and row["reference"] == cancelled["id"]
        ]

        self.assertEqual(bound_states, ["queued", "selected", "bound_to_run"])
        self.assertEqual(cancelled_states, ["queued", "cancelled"])
        self.assertTrue(all(row["task_id"] == bound_task["id"] for row in bound_events))
        self.assertTrue(all(row["task_id"] == cancelled_task["id"] for row in cancelled_events))

    def test_message_cli_rejects_invalid_utf8_and_oversized_content_as_json(self) -> None:
        task = self.create("cli-content")
        invalid = self.root / "invalid-message.bin"
        invalid.write_bytes(b"\xff")
        oversized = self.root / "oversized-message.txt"
        oversized.write_bytes(b"x" * (1024 * 1024 + 1))

        for operation, path in (("invalid-utf8", invalid), ("oversized", oversized)):
            with self.subTest(operation=operation):
                result = self.cli(
                    "message",
                    "enqueue",
                    "--task",
                    task["id"],
                    "--base-revision",
                    "1",
                    "--content-file",
                    str(path),
                    "--operation-id",
                    operation,
                    expected=2,
                )
                self.assertEqual(result["error"], "invalid_message")

    def test_message_cli_rejects_each_unpaired_source_flag_as_json(self) -> None:
        task = self.create("cli-source")
        content = self.root / "source-message.txt"
        content.write_text("handoff")
        cases = (
            ("--source-run", "00000000-0000-0000-0000-000000000001"),
            ("--source-result-sha256", "0" * 64),
        )

        for index, extra in enumerate(cases):
            with self.subTest(extra=extra[0]):
                result = self.cli(
                    "message",
                    "enqueue",
                    "--task",
                    task["id"],
                    "--base-revision",
                    "1",
                    "--content-file",
                    str(content),
                    "--operation-id",
                    f"unpaired-source-{index}",
                    *extra,
                    expected=2,
                )
                self.assertEqual(result["error"], "message_source_invalid")

    def test_backend_restart_after_enqueue_marks_message_target_changed(self) -> None:
        task = self.create("restart-target")
        completed = self.complete(task)
        message = self.enqueue_message(task, "Only for the original backend")
        restart = self.cli(
            "restart",
            "--session",
            completed["session_id"],
            "--acknowledge-context",
            "--prompt-file",
            str(self.prompt({"text": "new backend"})),
            "--request-id",
            "restart-message-target",
            "--timeout",
            "10",
        )
        restarted = self.wait_terminal(restart["id"])

        self.assert_error(
            "message_target_changed",
            tasks.revise,
            Store(self.state),
            task["id"],
            1,
            self.assignment("restart-target", context="revision two"),
            "select-after-restart",
            parent_run_id=restarted["id"],
            message_ids=[message["id"]],
        )
        listed = messages.list_messages(Store(self.state), task_id=task["id"])["messages"][0]
        self.assertEqual((listed["state"], listed["reason"]), ("queued", "target_changed"))

    def test_handoff_copies_valid_blocked_report_without_acceptance(self) -> None:
        source_context = json.dumps({"__fake_assignment__": {"report": {"status": "blocked"}}})
        source = self.create("handoff-source", context=source_context)
        source_run = self.complete(source)
        target = self.create("handoff-target")

        handoff = self.enqueue_message(
            target,
            "Inspect the source handoff",
            source_run_id=source_run["id"],
            source_result_sha256=source_run["result_sha256"],
        )
        tasks.revise(
            Store(self.state),
            target["id"],
            1,
            self.assignment("handoff-target", context="revision two"),
            "select-handoff",
            parent_run_id=None,
            message_ids=[handoff["id"]],
        )
        (Store(self.state).run_dir(source_run["id"]) / "result.json").unlink()
        run_id, _ = tasks.submit(Store(self.state), target["id"], 2, "submit-handoff")
        prompt = task_contracts.read((Store(self.state).run_dir(run_id) / "prompt.txt").read_text())

        supplied = prompt["messages"][0]["source"]
        self.assertEqual(supplied["run_id"], source_run["id"])
        self.assertEqual(supplied["result_sha256"], source_run["result_sha256"])
        self.assertEqual(supplied["report"]["status"], "blocked")

    def test_handoff_requires_paired_and_exact_source_evidence(self) -> None:
        target = self.create("invalid-handoff-target")
        self.assert_error(
            "message_source_invalid",
            self.enqueue_message,
            target,
            "Missing digest",
            source_run_id="00000000-0000-0000-0000-000000000001",
        )
        source = self.create("invalid-handoff-source")
        source_run = self.complete(source)
        self.assert_error(
            "message_source_invalid",
            self.enqueue_message,
            target,
            "Wrong digest",
            source_run_id=source_run["id"],
            source_result_sha256="0" * 64,
        )

    def test_failed_binding_requires_explicit_redelivery(self) -> None:
        task = self.create("redelivery")
        message = self.enqueue_message(task, "Try again if execution fails")
        failure = json.dumps({"__fake_assignment__": {"behavior": "exit_fail"}})
        tasks.revise(
            Store(self.state),
            task["id"],
            1,
            self.assignment("redelivery", context=failure),
            "select-first-delivery",
            parent_run_id=None,
            message_ids=[message["id"]],
        )
        failed = self.complete({**task, "current_revision": 2}, revision=2)
        self.assertEqual(failed["status"], "failed")

        self.assert_error(
            "message_already_bound",
            tasks.revise,
            Store(self.state),
            task["id"],
            2,
            self.assignment("redelivery", context=failure),
            "redelivery-implicit",
            parent_run_id=failed["id"],
            message_ids=[message["id"]],
        )
        revised = tasks.revise(
            Store(self.state),
            task["id"],
            2,
            self.assignment("redelivery", context=failure),
            "redelivery-explicit",
            parent_run_id=failed["id"],
            message_ids=[message["id"]],
            redeliver_messages=True,
            acknowledge_context=True,
        )
        retry_run, _ = tasks.submit(Store(self.state), task["id"], 3, "redelivery-submit")
        prompt = task_contracts.read(
            (Store(self.state).run_dir(retry_run) / "prompt.txt").read_text()
        )

        self.assertEqual(revised["current_revision"], 3)
        self.assertEqual(prompt["messages"][0]["previous_run_id"], failed["id"])
        listing = messages.list_messages(Store(self.state), task_id=task["id"])
        row = listing["messages"][0]
        self.assertEqual(len(row["selections"]), 2)
        self.assertEqual(len(row["bindings"]), 2)

    def test_completed_binding_cannot_be_redelivered(self) -> None:
        task = self.create("completed-redelivery")
        message = self.enqueue_message(task, "Do not replay successful work")
        tasks.revise(
            Store(self.state),
            task["id"],
            1,
            self.assignment("completed-redelivery", context="revision two"),
            "select-completed-delivery",
            parent_run_id=None,
            message_ids=[message["id"]],
        )
        completed = self.complete({**task, "current_revision": 2}, revision=2)

        self.assert_error(
            "redelivery_denied",
            tasks.revise,
            Store(self.state),
            task["id"],
            2,
            self.assignment("completed-redelivery", context="revision three"),
            "replay-completed-delivery",
            parent_run_id=completed["id"],
            message_ids=[message["id"]],
            redeliver_messages=True,
        )
