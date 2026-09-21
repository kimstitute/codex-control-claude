"""P1 behavior: persistent revisions, exact approvals and conservative retry admission."""

import errno
import json
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/claude-control/scripts"))

from claude_control import orchestration, tasks
from claude_control.assignments import ROLE_PRESETS
from claude_control.store import CLAIM_SECONDS, ControlError, Store
from test_controller import ControllerTestCase


class TaskTests(ControllerTestCase):
    def test_missing_result_is_integrity_failure_in_task_view_before_status_read(self):
        task, run = self.completed()
        store = Store(self.state)
        (store.run_dir(run["id"]) / "result.json").unlink()
        self.assertEqual(tasks.show(store, task["id"])["reason"], "result_integrity")
        self.assertEqual(store.get_run(run["id"])["reason"], "result_integrity")

    def test_revision_after_unstarted_first_turn_uses_same_fresh_backend(self):
        task = self.create()
        store = Store(self.state)
        expired, _ = tasks.submit(store, task["id"], 1, "expired")
        with store.db(write=True) as db:
            db.execute(
                "UPDATE runs SET created=? WHERE id=?", (time.time() - CLAIM_SECONDS - 1, expired)
            )
        store.refresh()
        tasks.revise(
            store,
            task["id"],
            1,
            self.assignment(context="Changed input"),
            "revise",
            parent_run_id=expired,
            acknowledge_context=True,
        )
        run = self.submit(task["id"], revision=2, operation="new-turn")
        self.assertEqual(
            (run["resume"], run["backend_id"]), (0, store.get_run(expired)["backend_id"])
        )
        self.assertEqual(self.wait_terminal(run["id"])["status"], "completed")
        self.assertEqual(self.cli("report", "--run", run["id"])["format_status"], "valid")

    def test_transient_read_failure_does_not_permanently_fail_completed_result(self):
        task, run = self.completed()
        store = Store(self.state)
        original = Path.open

        def fail_result(path, *args, **kwargs):
            if path.name == "result.json":
                raise OSError(errno.EMFILE, "temporarily no descriptors")
            return original(path, *args, **kwargs)

        with mock.patch.object(Path, "open", fail_result):
            observed = orchestration.observe(store, [run["id"]], 0)
            self.assertEqual(observed["runs"][0]["status"], "completed")
            self.assertEqual(observed["needs_attention"], [run["id"]])
            self.assertEqual(tasks.show(store, task["id"])["reason"], "result_unreadable")
        self.assertEqual(store.get_run(run["id"])["status"], "completed")
        self.decision(task["id"], run)
        self.assertEqual(tasks.show(store, task["id"])["state"], "accepted")

    def test_model_mismatch_reason_does_not_depend_on_truncated_error_text(self):
        task, run = self.completed(
            context=json.dumps({"__fake_assignment__": {"behavior": "mismatch_model"}})
        )
        store = Store(self.state)
        with store.db(write=True) as db:
            db.execute(
                "UPDATE runs SET reason=? WHERE id=?", ("earlier parse error " * 200, run["id"])
            )
        self.assertEqual(tasks.show(store, task["id"])["reason"], "model_mismatch")

    def test_late_worker_cannot_claim_after_deadline_without_prior_refresh(self):
        task = self.create()
        store = Store(self.state)
        run, _ = tasks.submit(store, task["id"], 1, "pending")
        with store.db(write=True) as db:
            db.execute(
                "UPDATE runs SET created=? WHERE id=?", (time.time() - CLAIM_SECONDS - 1, run)
            )
        self.cli("_worker", "--run", run)
        self.assertFalse((store.run_dir(run) / "invocation.json").exists())
        self.assertEqual(tasks.show(store, task["id"])["reason"], "reserve_expired")

    def test_wrong_revision_report_is_invalid(self):
        task, run = self.completed(
            context=json.dumps({"__fake_assignment__": {"report": {"revision": 2}}})
        )
        self.assertEqual(self.cli("report", "--run", run["id"])["format_status"], "invalid")
        self.assertEqual(self.decision(task["id"], run, expected=2)["error"], "invalid_report")

    def test_accept_racing_revision_never_accepts_the_new_revision(self):
        task, run = self.completed()

        def approve():
            try:
                return tasks.accept(
                    Store(self.state),
                    task["id"],
                    1,
                    run["id"],
                    run["result_sha256"],
                    {"1": "Checked answer", "2": "Checked limits"},
                    "approve",
                )
            except ControlError as exc:
                return exc.code

        def revise():
            return tasks.revise(
                Store(self.state),
                task["id"],
                1,
                self.assignment(context="new input"),
                "revise",
                parent_run_id=run["id"],
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            approval = pool.submit(approve)
            revision = pool.submit(revise)
            result = approval.result()
            self.assertEqual(revision.result()["current_revision"], 2)
        self.assertTrue(isinstance(result, dict) or result == "stale_revision")
        self.assertEqual(tasks.show(Store(self.state), task["id"])["state"], "queued")

    def assignment(self, **changes):
        value = dict(
            id="example",
            name="Example",
            role="executor",
            project=str(self.project),
            objective="Return a supplied-text answer.",
            context="",
            scope=["Supplied text only."],
            acceptance_criteria=["Answer the objective.", "State limitations."],
            deliverable="A concise answer.",
            timeout=10,
        )
        value.update(changes)
        return value

    def create(self, **changes):
        return tasks.create(Store(self.state), self.assignment(**changes), "create")

    def submit(self, task, revision=1, operation="submit", retry=False):
        return self.cli(
            "task",
            "retry" if retry else "submit",
            "--task",
            task,
            "--revision",
            str(revision),
            "--operation-id",
            operation,
        )

    def completed(self, **changes):
        task = self.create(**changes)
        run = self.submit(task["id"])
        row = self.wait_terminal(run["id"])
        return task, row

    def decision(
        self,
        task,
        run,
        *,
        kind="accept",
        revision=1,
        operation="decision",
        expected=0,
        digest=None,
        evidence=None,
    ):
        evidence = (
            evidence
            if evidence is not None
            else {"1": "Answer inspected.", "2": "Limits inspected."}
        )
        args = [
            "task",
            kind,
            "--task",
            task,
            "--revision",
            str(revision),
            "--run",
            run["id"],
            "--result-sha256",
            digest or run["result_sha256"],
            "--evidence-file",
            str(self.prompt(evidence)),
            "--operation-id",
            operation,
        ]
        if kind == "review":
            args += ["--reviewer", "fable-reviewer", "--recommendation", "approve"]
        return self.cli(*args, expected=expected)

    def error(self, code, call, *args, **kwargs):
        with self.assertRaises(ControlError) as raised:
            call(*args, **kwargs)
        self.assertEqual(raised.exception.code, code)

    def test_full_two_turn_lifecycle_separates_review_and_approval(self):
        created = self.cli(
            "task",
            "create",
            "--assignment-file",
            str(self.prompt(self.assignment())),
            "--operation-id",
            "create",
        )
        self.assertEqual(self.cli("task", "show", "--task", created["id"])["state"], "queued")
        first = self.submit(created["id"])
        first = self.wait_terminal(first["id"])
        self.assertEqual(first["status"], "completed")
        reported = self.cli("report", "--run", first["id"])
        self.assertEqual(
            (reported["task_id"], reported["revision"], reported["format_status"]),
            (created["id"], 1, "valid"),
        )
        self.assertEqual(reported["actual_models"], ["claude-sonnet-test"])
        self.decision(created["id"], first, kind="review", operation="review")
        self.assertEqual(
            self.cli("task", "show", "--task", created["id"])["state"], "awaiting_review"
        )
        self.decision(created["id"], first)
        self.assertEqual(self.cli("report", "--run", first["id"])["acceptance"], "accepted")
        revised = self.cli(
            "task",
            "revise",
            "--task",
            created["id"],
            "--revision",
            "1",
            "--assignment-file",
            str(self.prompt(self.assignment(context="Refine the answer."))),
            "--parent-run",
            first["id"],
            "--operation-id",
            "revise",
        )
        self.assertEqual(revised["current_revision"], 2)
        self.assertEqual(self.cli("task", "list")["tasks"][0]["state"], "queued")
        second = self.submit(created["id"], 2, "submit-2")
        second = self.wait_terminal(second["id"])
        self.assertEqual(second["status"], "completed")
        self.assertEqual(second["backend_id"], first["backend_id"])
        self.assertEqual(second["session_id"], first["session_id"])
        self.assertEqual(second["resume"], 1)
        report = self.cli("report", "--run", second["id"])
        self.assertEqual(
            (report["format_status"], report["revision"], report["acceptance"]),
            ("valid", 2, "unreviewed"),
        )
        stale = self.decision(created["id"], first, operation="stale", expected=2)
        self.assertEqual(stale["error"], "stale_revision")
        self.decision(created["id"], first, kind="review", operation="historical-review")
        self.assertEqual(
            self.cli("task", "show", "--task", created["id"])["state"], "awaiting_review"
        )
        self.decision(created["id"], second, revision=2, operation="approve-2")
        self.assertEqual(self.cli("task", "show", "--task", created["id"])["state"], "accepted")

    def test_idempotent_create_submit_revise_and_conflicts(self):
        store = Store(self.state)
        a = self.create()
        self.assertEqual(self.create()["id"], a["id"])
        self.error(
            "request_conflict", tasks.create, store, self.assignment(context="changed"), "create"
        )
        first = self.submit(a["id"])
        first = self.wait_terminal(first["id"])
        self.assertEqual(self.submit(a["id"])["id"], first["id"])
        self.error("already_submitted", tasks.submit, store, a["id"], 1, "different-submit")
        args = (store, a["id"], 1, self.assignment(context="revision 2"), "revise")
        revision = tasks.revise(*args, parent_run_id=first["id"])
        self.assertEqual(tasks.revise(*args, parent_run_id=first["id"])["current_revision"], 2)
        self.assertEqual(revision["current_revision"], 2)
        self.error(
            "request_conflict",
            tasks.revise,
            store,
            a["id"],
            1,
            self.assignment(context="different"),
            "revise",
            parent_run_id=first["id"],
        )
        self.error("stale_revision", tasks.submit, store, a["id"], 1, "stale-submit")

    def test_concurrent_submission_reserves_one_run_and_one_session(self):
        task = self.create()

        def call(_):
            return tasks.submit(Store(self.state), task["id"], 1, "one-op")

        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(call, range(6)))
        self.assertEqual(len({r[0] for r in results}), 1)
        self.assertEqual(sum(r[1] for r in results), 1)
        self.assertEqual(len(Store(self.state).list_all()["runs"]), 1)
        self.assertEqual(len(Store(self.state).list_all()["sessions"]), 1)

    def test_two_different_operations_cannot_submit_same_revision(self):
        task = self.create()

        def call(i):
            try:
                return tasks.submit(Store(self.state), task["id"], 1, f"op-{i}")[1]
            except ControlError as exc:
                return exc.code

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(call, range(2)))
        self.assertCountEqual(results, [True, "already_submitted"])

    def test_reservation_and_task_link_roll_back_together_on_disk_failure(self):
        task = self.create()
        with mock.patch.object(tasks, "_record", side_effect=OSError(errno.ENOSPC, "disk full")):
            with self.assertRaises(OSError):
                tasks.submit(Store(self.state), task["id"], 1, "submit")
        with Store(self.state).db() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM runs").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT count(*) FROM task_runs").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT count(*) FROM sessions").fetchone()[0], 0)
        self.assertTrue(tasks.submit(Store(self.state), task["id"], 1, "submit")[1])

    def test_expired_first_reservation_retry_uses_fresh_backend_and_rejects_late_worker(self):
        store = Store(self.state)
        task = self.create()
        expired, _ = tasks.submit(store, task["id"], 1, "unlaunched")
        with store.db(write=True) as db:
            db.execute(
                "UPDATE runs SET created=? WHERE id=?", (time.time() - CLAIM_SECONDS - 1, expired)
            )
        self.assertEqual(tasks.show(store, task["id"])["reason"], "reserve_expired")
        retry = self.submit(task["id"], operation="retry", retry=True)
        retry = self.wait_terminal(retry["id"])
        self.assertEqual((retry["status"], retry["resume"]), ("completed", 0))
        self.assertEqual(store.get_run(expired)["status"], "launch_failed")
        self.cli("_worker", "--run", expired)
        self.assertFalse((store.run_dir(expired) / "invocation.json").exists())
        self.assertEqual(self.cli("report", "--run", retry["id"])["format_status"], "valid")

    def test_expired_followup_retry_preserves_resume(self):
        task, first = self.completed()
        store = Store(self.state)
        tasks.revise(
            store,
            task["id"],
            1,
            self.assignment(context="revise"),
            "revise",
            parent_run_id=first["id"],
        )
        expired, _ = tasks.submit(store, task["id"], 2, "unlaunched")
        with store.db(write=True) as db:
            db.execute(
                "UPDATE runs SET created=? WHERE id=?", (time.time() - CLAIM_SECONDS - 1, expired)
            )
        retry = self.submit(task["id"], 2, "retry", retry=True)
        retry = self.wait_terminal(retry["id"])
        self.assertEqual(
            (retry["status"], retry["resume"], retry["backend_id"]),
            ("completed", 1, first["backend_id"]),
        )

    def test_unknown_is_not_retried_or_revised(self):
        store = Store(self.state)
        task = self.create()
        run, _ = tasks.submit(store, task["id"], 1, "pending")
        with store.db(write=True) as db:
            db.execute("UPDATE runs SET status='unknown' WHERE id=?", (run,))
        try:
            self.error("retry_denied", tasks.submit, store, task["id"], 1, "retry", retry=True)
            self.error(
                "session_busy",
                tasks.revise,
                store,
                task["id"],
                1,
                self.assignment(),
                "revise",
                parent_run_id=run,
            )
            self.assertEqual(len(store.list_all()["runs"]), 1)
        finally:
            with store.db(write=True) as db:
                db.execute("UPDATE runs SET status='failed' WHERE id=?", (run,))

    def test_legacy_followup_between_revision_and_submit_blocks_context(self):
        task, first = self.completed()
        store = Store(self.state)
        tasks.revise(
            store,
            task["id"],
            1,
            self.assignment(context="revise"),
            "revise",
            parent_run_id=first["id"],
        )
        other = self.followup(first["session_id"], {"text": "Intervening turn"}, "other")
        self.wait_terminal(other["id"])
        self.error("context_changed", tasks.submit, store, task["id"], 2, "submit-2")
        self.assertEqual(tasks.show(store, task["id"])["reason"], "context_changed")

    def test_pinned_role_survives_preset_change(self):
        task, first = self.completed()
        store = Store(self.state)
        before = json.loads(tasks.show(store, task["id"])["revisions"][0]["prompt"])["role"]
        with mock.patch.dict(
            ROLE_PRESETS["executor"], instructions="Changed future preset", version=99
        ):
            tasks.revise(
                store,
                task["id"],
                1,
                self.assignment(context="revise"),
                "revise",
                parent_run_id=first["id"],
            )
        after = json.loads(tasks.show(store, task["id"])["revisions"][1]["prompt"])["role"]
        self.assertEqual(before, after)

    def test_approval_requires_all_criteria_exact_digest_and_is_append_only(self):
        task, run = self.completed()
        self.assertEqual(
            self.decision(task["id"], run, digest="0" * 64, expected=2)["error"],
            "result_not_reviewable",
        )
        self.assertEqual(
            self.decision(task["id"], run, evidence={"1": "One criterion"}, expected=2)["error"],
            "invalid_evidence",
        )
        decision = self.decision(task["id"], run)
        self.assertEqual(self.decision(task["id"], run)["id"], decision["id"])
        with self.assertRaises(sqlite3.IntegrityError), Store(self.state).db(write=True) as db:
            db.execute("UPDATE review_decisions SET reviewer='other'")
        with self.assertRaises(sqlite3.IntegrityError), Store(self.state).db(write=True) as db:
            db.execute("DELETE FROM task_revisions")

    def test_tampered_accepted_result_is_blocked_and_cannot_be_approved_again(self):
        task, run = self.completed()
        self.decision(task["id"], run)
        (Store(self.state).run_dir(run["id"]) / "result.json").write_text("{}")
        self.assertEqual(tasks.show(Store(self.state), task["id"])["state"], "blocked")
        self.assertEqual(self.cli("report", "--run", run["id"])["acceptance"], "unreviewed")
        self.decision(task["id"], run, operation="new-approval", expected=2)

    def test_model_mismatch_cannot_be_accepted_or_retried(self):
        task, run = self.completed(
            context=json.dumps({"__fake_assignment__": {"behavior": "mismatch_model"}})
        )
        self.assertEqual(run["status"], "failed")
        self.assertEqual(tasks.show(Store(self.state), task["id"])["reason"], "model_mismatch")
        self.error(
            "retry_denied", tasks.submit, Store(self.state), task["id"], 1, "retry", retry=True
        )
        self.decision(task["id"], run, expected=2)

    def test_blocked_report_is_reviewable_but_never_acceptable(self):
        task, run = self.completed(
            context=json.dumps({"__fake_assignment__": {"report": {"status": "blocked"}}})
        )
        self.assertEqual(self.cli("report", "--run", run["id"])["agent_status"], "blocked")
        self.decision(task["id"], run, kind="review", operation="review")
        self.assertEqual(
            self.decision(task["id"], run, expected=2)["error"], "result_not_acceptable"
        )

    def test_malformed_evidence_file_is_json_error_not_traceback(self):
        task, run = self.completed()
        path = self.root / "invalid-evidence.json"
        for value in ('{"1":"a","1":"b"}', "{", "[]", '"' + "x" * (1024 * 1024) + '"'):
            path.write_text(value)
            result = self.cli(
                "task",
                "accept",
                "--task",
                task["id"],
                "--revision",
                "1",
                "--run",
                run["id"],
                "--result-sha256",
                run["result_sha256"],
                "--evidence-file",
                str(path),
                "--operation-id",
                "invalid-evidence",
                expected=2,
            )
            self.assertEqual(result["error"], "invalid_evidence")
