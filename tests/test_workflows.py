"""P4 regression tests for bounded worker-review workflows."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/claude-control/scripts"))

from claude_control import messages, scheduler, tasks, workflow, workflow_contracts  # noqa: E402
from claude_control.store import ControlError, Store  # noqa: E402
from test_controller import ControllerTestCase  # noqa: E402


class WorkflowTests(ControllerTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.sequence = 0

    def assignment(
        self,
        label: str,
        *,
        fixture: dict | None = None,
        role: str = "executor",
        model: str | None = None,
    ) -> dict:
        value = {
            "id": label,
            "name": label,
            "role": role,
            "project": str(self.project),
            "objective": "Produce a bounded supplied-text proposal.",
            "context": json.dumps({"__fake_assignment__": fixture or {}}),
            "scope": ["Supplied text only."],
            "acceptance_criteria": ["Answer the objective.", "State limitations."],
            "deliverable": "A concise proposal.",
            "timeout": 10,
        }
        if model is not None:
            value["model"] = model
        return value

    def create(self, label: str, *, fixture: dict | None = None, **limits) -> dict:
        self.sequence += 1
        return workflow.create(
            Store(self.state),
            self.assignment(label, fixture=fixture),
            f"workflow-create-{self.sequence}",
            **limits,
        )

    def run_workflow(self, workflow_id: str, *, once: bool = False, max_seconds: float = 8) -> dict:
        return workflow.run(Store(self.state), workflow_id, once=once, max_seconds=max_seconds)

    def assert_error(self, expected: str, call, *args, **kwargs) -> None:
        with self.assertRaises(ControlError) as raised:
            call(*args, **kwargs)
        self.assertEqual(raised.exception.code, expected)

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

    @staticmethod
    def review_context() -> dict:
        return {
            "source": {
                "task_id": "worker",
                "revision": 1,
                "run_id": "run",
                "result_sha256": "a" * 64,
            },
            "criteria": {"1": "Answer the objective.", "2": "State limitations."},
        }

    @classmethod
    def valid_review(cls) -> dict:
        context = cls.review_context()
        return {
            "target": dict(context["source"]),
            "recommendation": "approve",
            "criteria": {
                key: {"verdict": "pass", "evidence": "Verified from supplied report."}
                for key in context["criteria"]
            },
            "unverified": [],
            "revision_instructions": "",
        }

    def test_create_is_call_free_and_queues_only_initial_worker(self) -> None:
        created = self.create("create-only")
        status = workflow.status(Store(self.state), created["id"])

        self.assertEqual((created["state"], status["state"]), ("active", "active"))
        self.assertEqual((status["phase"], status["round"]), ("worker", 0))
        self.assertIsNone(status["first_reserved_at"])
        self.assertEqual(status["budget"]["calls_used"], 0)
        self.assertEqual(status["runs"], [])
        with Store(self.state).db() as db:
            queued = db.execute(
                "SELECT task_id FROM queue_entries WHERE state='waiting' ORDER BY id"
            ).fetchall()
        self.assertEqual([row[0] for row in queued], [created["worker_task_id"]])

    def test_create_operation_deduplicates_same_policy_and_conflicts_on_change(self) -> None:
        assignment = self.assignment("create-idempotency")
        first = workflow.create(Store(self.state), assignment, "same-create")
        repeated = workflow.create(Store(self.state), assignment, "same-create")
        changed = dict(assignment, name="Changed workflow")

        self.assertEqual(repeated["id"], first["id"])
        self.assertTrue(repeated["deduplicated"])
        self.assert_error(
            "request_conflict", workflow.create, Store(self.state), changed, "same-create"
        )

    def test_create_queue_full_rolls_back_every_workflow_row(self) -> None:
        store = Store(self.state)
        for index in range(100):
            assignment = self.assignment(f"fill-{index}")
            task = tasks.create(store, assignment, f"fill-task-{index}")
            scheduler.enqueue(store, task["id"], task["current_revision"], f"fill-queue-{index}")
        tables = (
            "tasks",
            "task_revisions",
            "workflows",
            "workflow_steps",
            "queue_entries",
            "task_operations",
        )
        with store.db() as db:
            before = {
                table: db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in tables
            }

        self.assert_error(
            "queue_full",
            workflow.create,
            store,
            self.assignment("queue-full-workflow"),
            "queue-full-create",
        )

        with store.db() as db:
            after = {
                table: db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in tables
            }
        self.assertEqual(after, before)

    def test_reservation_overflow_rolls_back_run_receipt_and_window_start(self) -> None:
        assignment = self.assignment("large-policy")
        assignment["context"] = "x" * 540_000
        created = workflow.create(Store(self.state), assignment, "create-large-policy")

        result = self.run_workflow(created["id"], once=True)

        self.assertEqual((result["state"], result["reason"]), ("awaiting_codex", "input_too_large"))
        self.assertIsNone(result["first_reserved_at"])
        self.assertEqual(result["budget"]["calls_used"], 0)
        self.assertEqual(result["runs"], [])
        self.assertEqual(self.task_runs(created["worker_task_id"]), [])

    def test_review_rejects_nonexact_criterion_keys(self) -> None:
        review = self.valid_review()
        review["criteria"].pop("2")

        self.assert_error(
            "invalid_report", workflow_contracts.validate_review, review, self.review_context()
        )

    def test_review_rejects_approve_with_failed_or_unverified_evidence(self) -> None:
        for mutation in ("failed", "unverified"):
            with self.subTest(mutation=mutation):
                review = self.valid_review()
                if mutation == "failed":
                    review["criteria"]["1"]["verdict"] = "fail"
                else:
                    review["unverified"] = ["Source does not establish criterion 1."]
                self.assert_error(
                    "invalid_report",
                    workflow_contracts.validate_review,
                    review,
                    self.review_context(),
                )

    def test_review_rejects_changed_target_digest(self) -> None:
        review = self.valid_review()
        review["target"]["result_sha256"] = "b" * 64

        self.assert_error(
            "invalid_report", workflow_contracts.validate_review, review, self.review_context()
        )

    def test_approve_path_uses_two_calls_and_waits_for_codex(self) -> None:
        created = self.create("approve-path")

        result = self.run_workflow(created["id"])

        self.assertEqual(
            (result["state"], result["reason"]), ("awaiting_codex", "approve_recommended")
        )
        self.assertEqual(result["budget"]["calls_used"], 2)
        self.assertEqual(result["budget"]["revisions_used"], 0)
        self.assertEqual(len(result["runs"]), 2)
        self.assertIsNotNone(result["first_reserved_at"])
        self.assertEqual(
            tasks.show(Store(self.state), created["reviewer_task_id"])["current_revision"],
            2,
        )

    def test_awaiting_codex_workflow_never_resumes_automation(self) -> None:
        created = self.create("automation-terminal")
        completed = self.run_workflow(created["id"])

        repeated = self.run_workflow(created["id"], once=True)

        self.assertEqual(completed["state"], "awaiting_codex")
        self.assertEqual(repeated["started"], [])
        self.assertEqual(repeated["budget"]["calls_used"], 2)

    def test_approve_recommendation_never_creates_codex_acceptance(self) -> None:
        created = self.create("no-auto-accept")
        self.run_workflow(created["id"])

        with Store(self.state).db() as db:
            accepted = db.execute(
                "SELECT count(*) FROM review_decisions WHERE kind='accept' AND task_id IN (?,?)",
                (created["worker_task_id"], created["reviewer_task_id"]),
            ).fetchone()[0]
        self.assertEqual(accepted, 0)
        self.assertNotEqual(
            tasks.show(Store(self.state), created["worker_task_id"])["state"], "accepted"
        )

    def test_status_becomes_accepted_only_after_explicit_codex_acceptance(self) -> None:
        created = self.create("explicit-accept")
        recommended = self.run_workflow(created["id"])
        worker = tasks.show(Store(self.state), created["worker_task_id"])
        run = worker["runs"][-1]

        self.assertEqual(recommended["state"], "awaiting_codex")
        tasks.accept(
            Store(self.state),
            created["worker_task_id"],
            worker["current_revision"],
            run["id"],
            run["result_sha256"],
            {"1": "Answer inspected.", "2": "Limitations inspected."},
            "explicit-codex-accept",
        )
        accepted = workflow.status(Store(self.state), created["id"])
        self.assertEqual((accepted["state"], accepted["reason"]), ("accepted", "codex_accepted"))
        self.assertEqual(accepted["accepted_revision"], 1)

    def test_result_tamper_after_acceptance_revokes_accepted_status(self) -> None:
        created = self.create("tampered-accept")
        self.run_workflow(created["id"])
        worker = tasks.show(Store(self.state), created["worker_task_id"])
        run = worker["runs"][-1]
        tasks.accept(
            Store(self.state),
            created["worker_task_id"],
            1,
            run["id"],
            run["result_sha256"],
            {"1": "Answer inspected.", "2": "Limitations inspected."},
            "accept-before-tamper",
        )

        (Store(self.state).run_dir(run["id"]) / "result.json").write_text("{}")
        status = workflow.status(Store(self.state), created["id"])

        self.assertNotEqual(status["state"], "accepted")
        self.assertIsNone(status["accepted_revision"])

    def test_corrupted_source_between_stage_and_admission_blocks_review_edge(self) -> None:
        created = self.create("corrupt-staged-source")
        started = self.run_workflow(created["id"], once=True)
        worker_run = started["started"][0]
        self.assertEqual(self.wait_terminal(worker_run)["status"], "completed")
        workflow.advance(Store(self.state), created["id"])
        staged = workflow.status(Store(self.state), created["id"])
        self.assertEqual((staged["phase"], staged["budget"]["calls_used"]), ("review", 1))
        (Store(self.state).run_dir(worker_run) / "result.json").write_text("{}")

        blocked = self.run_workflow(created["id"], once=True)

        self.assertEqual(
            (blocked["state"], blocked["reason"]), ("awaiting_codex", "result_integrity")
        )
        self.assertEqual(blocked["budget"]["calls_used"], 1)
        self.assertEqual(self.task_runs(created["reviewer_task_id"]), [])

    def test_worker_and_reviewer_use_independent_sonnet_and_fable_sessions(self) -> None:
        created = self.create("independent-sessions")
        result = self.run_workflow(created["id"])

        worker_run = self.task_runs(created["worker_task_id"])[0]
        reviewer_run = self.task_runs(created["reviewer_task_id"])[0]
        with Store(self.state).db() as db:
            worker_session = db.execute(
                "SELECT * FROM sessions WHERE id=?", (worker_run["session_id"],)
            ).fetchone()
            reviewer_session = db.execute(
                "SELECT * FROM sessions WHERE id=?", (reviewer_run["session_id"],)
            ).fetchone()

        self.assertEqual(result["state"], "awaiting_codex")
        self.assertNotEqual(worker_run["session_id"], reviewer_run["session_id"])
        self.assertNotEqual(worker_run["backend_id"], reviewer_run["backend_id"])
        self.assertEqual((worker_session["model"], reviewer_session["model"]), ("sonnet", "fable"))

    def test_explicit_fable_worker_is_allowed_but_still_separate_from_reviewer(self) -> None:
        assignment = self.assignment("fable-worker", role="planner", model="fable")
        created = workflow.create(Store(self.state), assignment, "create-fable-worker")
        self.run_workflow(created["id"])
        worker_run = self.task_runs(created["worker_task_id"])[0]
        reviewer_run = self.task_runs(created["reviewer_task_id"])[0]
        with Store(self.state).db() as db:
            models = [
                db.execute(
                    "SELECT model FROM sessions WHERE id=?", (run["session_id"],)
                ).fetchone()[0]
                for run in (worker_run, reviewer_run)
            ]

        self.assertEqual(models, ["fable", "fable"])
        self.assertNotEqual(worker_run["session_id"], reviewer_run["session_id"])

    def test_revise_then_approve_copies_exact_review_as_message(self) -> None:
        created = self.create(
            "revise-once", fixture={"workflow_recommendations": ["revise", "approve"]}
        )

        result = self.run_workflow(created["id"])

        self.assertEqual(
            (result["state"], result["reason"]), ("awaiting_codex", "approve_recommended")
        )
        self.assertEqual((result["round"], result["budget"]["revisions_used"]), (1, 1))
        self.assertEqual(result["budget"]["calls_used"], 4)
        worker = tasks.show(Store(self.state), created["worker_task_id"])
        reviewer = tasks.show(Store(self.state), created["reviewer_task_id"])
        self.assertEqual((worker["current_revision"], reviewer["current_revision"]), (2, 3))
        deliveries = messages.list_messages(Store(self.state), task_id=created["worker_task_id"])[
            "messages"
        ]
        self.assertEqual(len(deliveries), 1)
        self.assertEqual(deliveries[0]["kind"], "handoff")
        self.assertEqual(deliveries[0]["source"]["run_id"], reviewer["runs"][0]["id"])
        self.assertEqual(
            deliveries[0]["source"]["result_sha256"],
            reviewer["runs"][0]["result_sha256"],
        )
        prompt = json.loads(
            (Store(self.state).run_dir(worker["runs"][1]["id"]) / "prompt.txt").read_text()
        )
        self.assertEqual(prompt["workflow"]["round"], 1)
        self.assertEqual(prompt["messages"][0]["source"]["run_id"], reviewer["runs"][0]["id"])

    def test_perpetual_revision_stops_at_revision_limit(self) -> None:
        created = self.create(
            "revision-limit",
            fixture={"workflow_recommendations": ["revise", "revise", "revise"]},
            max_revisions=1,
            max_calls=6,
        )

        result = self.run_workflow(created["id"])

        self.assertEqual((result["state"], result["reason"]), ("awaiting_codex", "revise_limit"))
        self.assertEqual(result["budget"]["revisions_used"], 1)
        self.assertEqual(result["budget"]["calls_used"], 4)

    def test_call_budget_requires_worker_revision_and_review_to_fit(self) -> None:
        created = self.create(
            "call-limit",
            fixture={"workflow_recommendations": ["revise"]},
            max_calls=3,
        )

        result = self.run_workflow(created["id"])

        self.assertEqual((result["state"], result["reason"]), ("awaiting_codex", "call_limit"))
        self.assertEqual(result["budget"]["calls_used"], 2)
        self.assertEqual(result["budget"]["revisions_used"], 0)
        self.assertEqual(len(self.task_runs(created["worker_task_id"])), 1)

    def test_window_opens_on_worker_reservation_then_blocks_later_review(self) -> None:
        created = self.create(
            "window-limit",
            fixture={"behavior": "sleep", "sleep_seconds": 1.1},
            dispatch_window_seconds=1,
        )
        self.assertIsNone(workflow.status(Store(self.state), created["id"])["first_reserved_at"])

        result = self.run_workflow(created["id"])

        self.assertEqual((result["state"], result["reason"]), ("awaiting_codex", "window_expired"))
        self.assertIsNotNone(result["first_reserved_at"])
        self.assertEqual(result["budget"]["calls_used"], 1)

    def test_completed_reviewer_after_window_still_records_recommendation(self) -> None:
        created = self.create(
            "review-completes-after-window",
            dispatch_window_seconds=60,
        )
        worker = self.run_workflow(created["id"], once=True)["started"][0]
        self.assertEqual(self.wait_terminal(worker)["status"], "completed")
        workflow.advance(Store(self.state), created["id"])
        review = scheduler.tick(Store(self.state), workflow_id=created["id"])["started"][0]
        self.assertEqual(self.wait_terminal(review)["status"], "completed")
        checkpoint = workflow.status(Store(self.state), created["id"])

        after_deadline = checkpoint["first_reserved_at"] + 61
        with mock.patch.object(workflow.time, "time", return_value=after_deadline):
            result = self.run_workflow(created["id"], once=True)

        self.assertEqual(
            (result["state"], result["reason"]),
            ("awaiting_codex", "approve_recommended"),
        )
        self.assertEqual(result["budget"]["calls_used"], 2)
        with Store(self.state).db() as db:
            decisions = db.execute(
                "SELECT recommendation FROM review_decisions WHERE reviewer=?",
                ("workflow:" + review,),
            ).fetchall()
        self.assertEqual([row[0] for row in decisions], ["approve"])

    def test_invalid_worker_report_stops_before_review(self) -> None:
        created = self.create("invalid-worker", fixture={"response": "{}"})

        result = self.run_workflow(created["id"])

        self.assertEqual((result["state"], result["reason"]), ("awaiting_codex", "invalid_report"))
        self.assertEqual(result["budget"]["calls_used"], 1)

    def test_blocked_worker_report_stops_before_review(self) -> None:
        created = self.create("blocked-worker", fixture={"report": {"status": "blocked"}})

        result = self.run_workflow(created["id"])

        self.assertEqual((result["state"], result["reason"]), ("awaiting_codex", "agent_blocked"))
        self.assertEqual(result["budget"]["calls_used"], 1)

    def test_model_mismatch_stops_without_retry(self) -> None:
        created = self.create("model-mismatch", fixture={"behavior": "mismatch_model"})

        result = self.run_workflow(created["id"])

        self.assertEqual((result["state"], result["reason"]), ("awaiting_codex", "model_mismatch"))
        self.assertEqual(result["budget"]["calls_used"], 1)

    def test_completed_run_with_spoofed_actual_model_family_is_rejected(self) -> None:
        created = self.create("spoofed-model-family")
        started = self.run_workflow(created["id"], once=True)
        run_id = started["started"][0]
        self.assertEqual(self.wait_terminal(run_id)["status"], "completed")
        with Store(self.state).db(write=True) as db:
            db.execute(
                "UPDATE runs SET actual_models=? WHERE id=?",
                (json.dumps(["claude-fable-test"]), run_id),
            )

        result = self.run_workflow(created["id"], once=True)

        self.assertEqual(
            (result["state"], result["reason"]),
            ("awaiting_codex", "result_integrity"),
        )
        self.assertEqual(result["budget"]["calls_used"], 1)

    def test_workflow_receipt_with_missing_member_session_fails_closed(self) -> None:
        created = self.create("missing-member-session")
        with mock.patch.object(scheduler, "launch_worker"):
            started = self.run_workflow(created["id"], once=True)
        run_id = started["started"][0]
        with Store(self.state).db(write=True) as db:
            db.execute("UPDATE tasks SET session_id=NULL WHERE id=?", (created["worker_task_id"],))

        result = self.run_workflow(created["id"], once=True)

        self.assertEqual((result["state"], result["reason"]), ("awaiting_codex", "invalid_state"))
        self.assertEqual(result["budget"]["calls_used"], 1)
        with Store(self.state).db(write=True) as db:
            db.execute(
                "UPDATE runs SET status='failed',reason='test_cleanup' WHERE id=?", (run_id,)
            )

    def test_public_message_mutations_reject_workflow_owned_task(self) -> None:
        created = self.create("owned-messages")
        started = self.run_workflow(created["id"], once=True)
        worker_run = started["started"][0]
        self.assertEqual(self.wait_terminal(worker_run)["status"], "completed")
        workflow.advance(Store(self.state), created["id"])
        reviewer = tasks.show(Store(self.state), created["reviewer_task_id"])
        with Store(self.state).db() as db:
            selected_message = db.execute(
                "SELECT message_id FROM revision_messages WHERE task_id=? AND revision=?",
                (created["reviewer_task_id"], reviewer["current_revision"]),
            ).fetchone()[0]

        self.assert_error(
            "workflow_owned",
            messages.enqueue,
            Store(self.state),
            created["reviewer_task_id"],
            reviewer["current_revision"],
            "Public mutation",
            "public-workflow-message",
        )
        self.assert_error(
            "workflow_owned",
            messages.cancel,
            Store(self.state),
            selected_message,
            "public-workflow-cancel",
        )

    def test_scoped_run_waits_for_unrelated_capacity_then_admits_workflow(self) -> None:
        config_path = self.state / "config.json"
        config = json.loads(config_path.read_text())
        config["max_parallel"] = 1
        config_path.write_text(json.dumps(config))
        unrelated = self.start(
            "capacity-holder",
            {"behavior": "sleep", "sleep_seconds": 0.4},
            "capacity-holder-start",
        )
        created = self.create("waits-for-capacity")

        result = self.run_workflow(created["id"], once=False, max_seconds=4)

        self.assertEqual(self.wait_terminal(unrelated["id"])["status"], "completed")
        self.assertEqual(
            (result["state"], result["reason"]), ("awaiting_codex", "approve_recommended")
        )
        self.assertEqual(result["budget"]["calls_used"], 2)

    def test_blocked_review_never_creates_revision(self) -> None:
        created = self.create("blocked-review", fixture={"workflow_recommendations": ["blocked"]})

        result = self.run_workflow(created["id"])

        self.assertEqual((result["state"], result["reason"]), ("awaiting_codex", "review_blocked"))
        self.assertEqual(result["budget"]["calls_used"], 2)
        self.assertEqual(result["budget"]["revisions_used"], 0)

    def test_malformed_review_never_advances_worker(self) -> None:
        created = self.create("malformed-review", fixture={"workflow_reviews": [{"target": None}]})

        result = self.run_workflow(created["id"])

        self.assertEqual((result["state"], result["reason"]), ("awaiting_codex", "invalid_report"))
        self.assertEqual(result["budget"]["calls_used"], 2)
        self.assertEqual(result["budget"]["revisions_used"], 0)

    def test_unknown_reservation_consumes_budget_and_is_not_retried(self) -> None:
        created = self.create("unknown-worker")
        with mock.patch.object(scheduler, "launch_worker"):
            first = self.run_workflow(created["id"], once=True)
        run_id = first["started"][0]
        with Store(self.state).db(write=True) as db:
            db.execute("UPDATE runs SET status='unknown' WHERE id=?", (run_id,))

        second = self.run_workflow(created["id"], once=True)

        self.assertEqual(
            (second["state"], second["reason"]), ("awaiting_codex", "unknown_execution")
        )
        self.assertEqual(second["budget"]["calls_used"], 1)
        self.assertEqual(len(self.task_runs(created["worker_task_id"])), 1)
        with Store(self.state).db(write=True) as db:
            db.execute(
                "UPDATE runs SET status='failed',reason='test_cleanup' WHERE id=?", (run_id,)
            )

    def test_owned_task_rejects_public_submit(self) -> None:
        created = self.create("owned-members")

        self.assert_error(
            "workflow_owned",
            tasks.submit,
            Store(self.state),
            created["worker_task_id"],
            1,
            "public-submit",
        )

    def test_owned_queue_rejects_public_dequeue(self) -> None:
        created = self.create("owned-queue")
        with Store(self.state).db() as db:
            queue_id = db.execute(
                "SELECT id FROM queue_entries WHERE task_id=? AND state='waiting'",
                (created["worker_task_id"],),
            ).fetchone()[0]

        self.assert_error(
            "workflow_owned",
            scheduler.dequeue,
            Store(self.state),
            queue_id,
            "public-dequeue",
        )

    def test_owned_task_rejects_direct_retry(self) -> None:
        created = self.create("owned-retry")
        completed = self.run_workflow(created["id"])

        self.assertEqual(completed["state"], "awaiting_codex")
        self.assert_error(
            "workflow_owned",
            tasks.submit,
            Store(self.state),
            created["worker_task_id"],
            1,
            "public-retry",
            retry=True,
        )

    def test_stop_before_dispatch_is_terminal_and_starts_no_run(self) -> None:
        created = self.create("stop-queued")

        stopped = workflow.stop(Store(self.state), created["id"], "stop-workflow")
        rerun = self.run_workflow(created["id"], once=True)

        self.assertEqual(stopped["state"], "stopped")
        self.assertEqual(rerun["state"], "stopped")
        self.assertEqual(rerun["started"], [])
        self.assertEqual(rerun["budget"]["calls_used"], 0)

    def test_attention_overview_contains_terminal_review_and_unknown_workflows(self) -> None:
        blocked = self.create("overview-blocked", fixture={"workflow_recommendations": ["blocked"]})
        self.run_workflow(blocked["id"])
        unknown = self.create("overview-unknown")
        with mock.patch.object(scheduler, "launch_worker"):
            started = self.run_workflow(unknown["id"], once=True)
        unknown_run = started["started"][0]
        with Store(self.state).db(write=True) as db:
            db.execute("UPDATE runs SET status='unknown' WHERE id=?", (unknown_run,))
        self.run_workflow(unknown["id"], once=True)

        overview = workflow.overview(Store(self.state), attention=True)
        ids = {row["id"] for row in overview["workflows"]}

        self.assertTrue({blocked["id"], unknown["id"]}.issubset(ids))
        with Store(self.state).db(write=True) as db:
            db.execute(
                "UPDATE runs SET status='failed',reason='test_cleanup' WHERE id=?", (unknown_run,)
            )
