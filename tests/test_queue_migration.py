"""Schema-5 upgrade preserves P1 tasks, approvals, and recovery checkpoints."""

import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/claude-control/scripts"))

from claude_control import migration, scheduler, tasks
from claude_control.orchestration import report
from claude_control.runner import launch_worker
from claude_control.store import ControlError, Store
from test_controller import ControllerTestCase
from test_migration import legacy_store

TABLES = (
    "sessions",
    "runs",
    "tasks",
    "task_revisions",
    "task_runs",
    "task_operations",
    "review_decisions",
)


def rows(store):
    with store.db() as db:
        return {
            table: [tuple(row) for row in db.execute(f"SELECT rowid,* FROM {table} ORDER BY rowid")]
            for table in TABLES
        }


def assignment(project):
    return dict(
        id="preserved",
        name="Preserved",
        role="executor",
        project=str(project),
        objective="Return an answer.",
        context="",
        scope=["Text only."],
        acceptance_criteria=["Answer."],
        deliverable="Text",
        timeout=10,
    )


class QueueMigrationTests(unittest.TestCase):
    def test_schema4_crash_boundaries_preserve_revisions_and_receipts(self):
        for phase in ("backup", "marker", "ddl", "db_committed", "config_committed"):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as root:
                state, project = legacy_store(root)
                migration.migrate(state, offline=True, target=4)
                old = Store(state)
                task = tasks.create(old, assignment(project), "create-preserved")
                before = rows(old)

                def fail(name):
                    if name == phase:
                        raise RuntimeError("simulated power loss")

                with mock.patch.object(migration, "_checkpoint", side_effect=fail):
                    with self.assertRaises(RuntimeError):
                        migration.migrate(state, offline=True, target=5)
                migration.migrate(state, offline=True, target=5)
                current = Store(state)
                self.assertEqual(current.config["schema"], 5)
                self.assertEqual(current.config["max_queued"], 100)
                self.assertEqual(rows(current), before)
                with self.assertRaises(ControlError):
                    tasks.create(old, assignment(project), "stale-client")
                with closing(sqlite3.connect(state / "schema-4-backup.sqlite3")) as db:
                    self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 4)
                    self.assertEqual(db.execute("SELECT id FROM tasks").fetchone()[0], task["id"])
                self.assertEqual(scheduler.events(current)["events"], [])
                self.assertFalse(migration.migrate(state, offline=True, target=5)["migrated"])
                self.assertEqual(migration.migration_status(state)["journal"]["phase"], "complete")
                self.assertTrue(scheduler.enqueue(current, task["id"], 1, "enqueue")["queue_id"])

    def test_latest_target_upgrades_both_steps_and_rejects_downgrade(self):
        with tempfile.TemporaryDirectory() as root:
            state, _ = legacy_store(root)
            result = migration.migrate(state, offline=True, target=5)
            self.assertEqual(result["schema"], 5)
            self.assertEqual(len(result["backups"]), 2)
            with self.assertRaises(ControlError):
                migration.migrate(state, offline=True, target=4)

    def test_schema4_unknown_blocks_upgrade(self):
        with tempfile.TemporaryDirectory() as root:
            state, project = legacy_store(root)
            migration.migrate(state, offline=True, target=4)
            store = Store(state)
            task = tasks.create(store, assignment(project), "create")
            run, _ = tasks.submit(store, task["id"], 1, "submit")
            with store.db(write=True) as db:
                db.execute("UPDATE runs SET status='unknown' WHERE id=?", (run,))
            with self.assertRaises(ControlError) as raised:
                migration.migrate(state, offline=True, target=5)
            self.assertEqual(raised.exception.code, "migration_busy")
            self.assertEqual(Store(state).config["schema"], 4)

    def test_schema4_missing_backup_keeps_marker_closed(self):
        with tempfile.TemporaryDirectory() as root:
            state, _ = legacy_store(root)
            migration.migrate(state, offline=True, target=4)

            def fail(phase):
                if phase == "marker":
                    raise RuntimeError("power loss")

            with mock.patch.object(migration, "_checkpoint", side_effect=fail):
                with self.assertRaises(RuntimeError):
                    migration.migrate(state, offline=True, target=5)
            (state / "schema-4-backup.sqlite3").unlink()
            with self.assertRaises(ControlError) as raised:
                migration.migrate(state, offline=True, target=5)
            self.assertEqual(raised.exception.code, "migration_backup")
            self.assertEqual(json.loads((state / "config.json").read_text())["schema"], "migrating")


class QueueApprovalMigrationTests(ControllerTestCase):
    def test_accepted_v2_history_remains_reviewable_after_upgrade(self):
        root = self.root / "legacy"
        root.mkdir()
        state, project = legacy_store(root)
        migration.migrate(state, offline=True, target=4)
        store = Store(state)
        task = tasks.create(store, assignment(project), "create")
        run, _ = tasks.submit(store, task["id"], 1, "submit")
        launch_worker(store, run)
        current_state = self.state
        self.state = state
        try:
            completed = self.wait_terminal(run)
            self.assertEqual(completed["status"], "completed")
            tasks.accept(
                store,
                task["id"],
                1,
                run,
                completed["result_sha256"],
                {"1": "Answer inspected."},
                "accept",
            )
            before = rows(store)
            original_report = report(store, run)
            migration.migrate(state, offline=True, target=5)
            store = Store(state)
            self.assertEqual(rows(store), before)
            self.assertEqual(report(store, run), original_report)
            self.assertEqual(tasks.show(store, task["id"])["state"], "accepted")
            self.assertEqual(scheduler.events(store)["events"], [])
        finally:
            self.state = current_state
