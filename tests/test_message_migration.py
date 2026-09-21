"""Schema-6 message migration preserves P2 queue state and execution contracts."""

import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/claude-control/scripts"))

from claude_control import messages, migration, scheduler, tasks
from claude_control.orchestration import report
from claude_control.store import ControlError, Store
from test_controller import ControllerTestCase
from test_migration import legacy_store
from test_queue_migration import TABLES, assignment

P2_TABLES = (
    *TABLES,
    "dependency_sets",
    "task_dependencies",
    "queue_entries",
    "execution_inputs",
    "task_events",
)


def capture(store):
    with store.db() as db:
        return {
            table: [tuple(row) for row in db.execute(f"SELECT rowid,* FROM {table} ORDER BY rowid")]
            for table in P2_TABLES
        }


class MessageMigrationTests(unittest.TestCase):
    def test_each_boundary_preserves_p2_queue_and_history(self):
        for phase in ("backup", "marker", "ddl", "db_committed", "config_committed"):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as root:
                state, project = legacy_store(root)
                migration.migrate(state, offline=True, target=5)
                old = Store(state)
                task = tasks.create(old, assignment(project), "create")
                queued = scheduler.enqueue(old, task["id"], 1, "queue")
                before = capture(old)
                with self.assertRaises(ControlError) as raised:
                    messages.enqueue(old, task["id"], 1, "instruction", "pre-upgrade")
                self.assertEqual(raised.exception.code, "migration_required")

                def fail(name):
                    if name == phase:
                        raise RuntimeError("power loss")

                with mock.patch.object(migration, "_checkpoint", side_effect=fail):
                    with self.assertRaises(RuntimeError):
                        migration.migrate(state, offline=True, target=6)
                migration.migrate(state, offline=True, target=6)
                current = Store(state)
                self.assertEqual(current.config["schema"], 6)
                self.assertEqual(capture(current), before)
                self.assertEqual(messages.list_messages(current)["messages"], [])
                with self.assertRaises(ControlError):
                    scheduler.dequeue(old, queued["queue_id"], "old-client")
                with closing(sqlite3.connect(state / "schema-5-backup.sqlite3")) as db:
                    self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 5)
                    self.assertEqual(
                        db.execute("SELECT id FROM queue_entries").fetchone()[0], queued["queue_id"]
                    )
                self.assertEqual(migration.migration_status(state)["journal"]["phase"], "complete")
                self.assertFalse(migration.migrate(state, offline=True, target=6)["migrated"])
                self.assertTrue(
                    messages.enqueue(
                        current, task["id"], 1, "Instruction after upgrade.", "message"
                    )["id"]
                )

    def test_three_step_chain_and_unknown_guard(self):
        with tempfile.TemporaryDirectory() as root:
            state, project = legacy_store(root)
            result = migration.migrate(state, offline=True, target=6)
            self.assertEqual((result["schema"], len(result["backups"])), (6, 3))
            with self.assertRaises(ControlError):
                migration.migrate(state, offline=True, target=5)
        with tempfile.TemporaryDirectory() as root:
            state, project = legacy_store(root)
            migration.migrate(state, offline=True, target=5)
            old = Store(state)
            task = tasks.create(old, assignment(project), "create")
            run, _ = tasks.submit(old, task["id"], 1, "submit")
            with old.db(write=True) as db:
                db.execute("UPDATE runs SET status='unknown' WHERE id=?", (run,))
            with self.assertRaises(ControlError) as raised:
                migration.migrate(state, offline=True, target=6)
            self.assertEqual(raised.exception.code, "migration_busy")
            self.assertEqual(Store(state).config["schema"], 5)

    def test_missing_schema5_backup_keeps_marker_closed(self):
        with tempfile.TemporaryDirectory() as root:
            state, _ = legacy_store(root)
            migration.migrate(state, offline=True, target=5)

            def fail(phase):
                if phase == "marker":
                    raise RuntimeError("power loss")

            with mock.patch.object(migration, "_checkpoint", side_effect=fail):
                with self.assertRaises(RuntimeError):
                    migration.migrate(state, offline=True, target=6)
            (state / "schema-5-backup.sqlite3").unlink()
            with self.assertRaises(ControlError) as raised:
                migration.migrate(state, offline=True, target=6)
            self.assertEqual(raised.exception.code, "migration_backup")
            self.assertEqual(migration.migration_status(state)["schema"], "migrating")


class MessageContractMigrationTests(ControllerTestCase):
    def test_accepted_v3_and_waiting_dependencies_survive(self):
        root = self.root / "p2"
        root.mkdir()
        state, project = legacy_store(root)
        migration.migrate(state, offline=True, target=5)
        store = Store(state)
        parent = tasks.create(store, assignment(project), "parent")
        child = tasks.create(store, assignment(project), "child")
        scheduler.enqueue(store, parent["id"], 1, "queue-parent")
        scheduler.enqueue(
            store, child["id"], 1, "queue-child", [dict(task_id=parent["id"], revision=1)]
        )
        run = scheduler.tick(store)["started"][0]
        original_state = self.state
        self.state = state
        try:
            completed = self.wait_terminal(run)
            self.assertEqual(completed["status"], "completed")
            tasks.accept(
                store,
                parent["id"],
                1,
                run,
                completed["result_sha256"],
                {"1": "Answer inspected."},
                "accept",
            )
            before = capture(store)
            original_report = report(store, run)
            migration.migrate(state, offline=True, target=6)
            store = Store(state)
            self.assertEqual(capture(store), before)
            self.assertEqual(report(store, run), original_report)
            next_run = scheduler.tick(store)["started"][0]
            self.assertEqual(self.wait_terminal(next_run)["status"], "completed")
            self.assertEqual(report(store, next_run)["format_status"], "valid")
        finally:
            self.state = original_state
