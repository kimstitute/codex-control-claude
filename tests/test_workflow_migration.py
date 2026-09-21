"""Offline schema 6→7 preserves message history and exact v4 reports."""

import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/claude-control/scripts"))

from claude_control import messages, migration, tasks, workflow
from claude_control.orchestration import report
from claude_control.runner import launch_worker
from claude_control.store import ControlError, Store
from test_controller import ControllerTestCase
from test_message_migration import P2_TABLES
from test_migration import legacy_store
from test_queue_migration import assignment

TABLES = (*P2_TABLES, "messages", "message_cancellations", "revision_messages", "message_bindings")


def capture(store):
    with store.db() as db:
        return {
            table: [tuple(row) for row in db.execute(f"SELECT rowid,* FROM {table} ORDER BY rowid")]
            for table in TABLES
        }


class WorkflowMigrationTests(unittest.TestCase):
    def test_each_boundary_preserves_p3_rows_and_backup(self):
        for phase in ("backup", "marker", "ddl", "db_committed", "config_committed"):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as root:
                state, project = legacy_store(root)
                migration.migrate(state, offline=True, target=6)
                old = Store(state)
                task = tasks.create(old, assignment(project), "task")
                message = messages.enqueue(old, task["id"], 1, "Keep this history", "message")
                tasks.revise(
                    old, task["id"], 1, assignment(project), "revision", message_ids=[message["id"]]
                )
                before = capture(old)
                with self.assertRaises(ControlError) as raised:
                    workflow.create(old, assignment(project), "too-early")
                self.assertEqual(raised.exception.code, "migration_required")

                def fail(name):
                    if name == phase:
                        raise RuntimeError("simulated shutdown")

                with mock.patch.object(migration, "_checkpoint", side_effect=fail):
                    with self.assertRaises(RuntimeError):
                        migration.migrate(state, offline=True, target=7)
                migration.migrate(state, offline=True, target=7)
                current = Store(state)
                self.assertEqual(current.config["schema"], 7)
                self.assertEqual(capture(current), before)
                with closing(sqlite3.connect(state / "schema-6-backup.sqlite3")) as db:
                    self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 6)
                    self.assertEqual(
                        db.execute("SELECT id FROM messages").fetchone()[0], message["id"]
                    )
                with self.assertRaises(ControlError):
                    messages.cancel(old, message["id"], "old-client")
                self.assertFalse(migration.migrate(state, offline=True, target=7)["migrated"])
                self.assertTrue(workflow.create(current, assignment(project), "new-workflow")["id"])

    def test_latest_chain_and_unknown_block(self):
        with tempfile.TemporaryDirectory() as root:
            state, _ = legacy_store(root)
            result = migration.migrate(state, offline=True, target=7)
            self.assertEqual((result["schema"], len(result["backups"])), (7, 4))
            with self.assertRaises(ControlError):
                migration.migrate(state, offline=True, target=6)
        with tempfile.TemporaryDirectory() as root:
            state, project = legacy_store(root)
            migration.migrate(state, offline=True, target=6)
            old = Store(state)
            task = tasks.create(old, assignment(project), "task")
            run, _ = tasks.submit(old, task["id"], 1, "reserve")
            with old.db(write=True) as db:
                db.execute("UPDATE runs SET status='unknown' WHERE id=?", (run,))
            with self.assertRaises(ControlError) as raised:
                migration.migrate(state, offline=True, target=7)
            self.assertEqual(raised.exception.code, "migration_busy")
            self.assertEqual(Store(state).config["schema"], 6)

    def test_missing_backup_keeps_migration_closed(self):
        with tempfile.TemporaryDirectory() as root:
            state, _ = legacy_store(root)
            migration.migrate(state, offline=True, target=6)

            def fail(phase):
                if phase == "marker":
                    raise RuntimeError("shutdown")

            with mock.patch.object(migration, "_checkpoint", side_effect=fail):
                with self.assertRaises(RuntimeError):
                    migration.migrate(state, offline=True, target=7)
            (state / "schema-6-backup.sqlite3").unlink()
            with self.assertRaises(ControlError) as raised:
                migration.migrate(state, offline=True, target=7)
            self.assertEqual(raised.exception.code, "migration_backup")
            self.assertEqual(migration.migration_status(state)["schema"], "migrating")


class WorkflowContractMigrationTests(ControllerTestCase):
    def test_accepted_v4_and_message_receipts_survive(self):
        root = self.root / "p3"
        root.mkdir()
        state, project = legacy_store(root)
        migration.migrate(state, offline=True, target=6)
        store = Store(state)
        task = tasks.create(store, assignment(project), "task")
        message = messages.enqueue(store, task["id"], 1, "Exact queued instruction", "message")
        tasks.revise(
            store, task["id"], 1, assignment(project), "revision", message_ids=[message["id"]]
        )
        run, _ = tasks.submit(store, task["id"], 2, "submit")
        launch_worker(store, run)
        previous_state = self.state
        self.state = state
        try:
            completed = self.wait_terminal(run)
            self.assertEqual(completed["status"], "completed")
            tasks.accept(
                store,
                task["id"],
                2,
                run,
                completed["result_sha256"],
                {"1": "Answer inspected."},
                "accept",
            )
            before = capture(store)
            checked = report(store, run)
            delivered = messages.list_messages(store)
            migration.migrate(state, offline=True, target=7)
            current = Store(state)
            self.assertEqual(capture(current), before)
            self.assertEqual(report(current, run), checked)
            self.assertEqual(messages.list_messages(current), delivered)
            self.assertEqual(tasks.show(current, task["id"])["state"], "accepted")
            flow = workflow.create(current, assignment(project), "migrated-workflow")
            outcome = workflow.run(current, flow["id"], max_seconds=8)
            self.assertEqual(outcome["reason"], "approve_recommended")
            stopped = workflow.stop(current, flow["id"], "stop-migrated")
            self.assertIn(stopped["state"], ("stopping", "stopped"))
            self.assertEqual(workflow.run(current, flow["id"], once=True)["started"], [])
        finally:
            self.state = previous_state
