"""Schema 7→8 backup/recovery preserves P4 and legacy task history."""

import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/claude-control/scripts"))
from claude_control import migration, tasks, workflow, workspace
from claude_control.store import ControlError, Store
from test_migration import legacy_store
from test_queue_migration import assignment
from test_workflow_migration import TABLES


def capture(store):
    with store.db() as db:
        return {
            table: [tuple(row) for row in db.execute(f"SELECT rowid,* FROM {table} ORDER BY rowid")]
            for table in (*TABLES, "workflows", "workflow_steps", "workflow_runs")
        }


class WorkspaceMigrationTests(unittest.TestCase):
    def test_each_checkpoint_preserves_all_p4_rows(self):
        for phase in ("backup", "marker", "ddl", "db_committed", "config_committed"):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as root:
                state, project = legacy_store(root)
                migration.migrate(state, offline=True, target=7)
                old = Store(state)
                flow = workflow.create(old, assignment(project), "old-workflow")
                before = capture(old)
                with self.assertRaises(ControlError):
                    workspace.status(old, "missing")

                def crash(name):
                    if name == phase:
                        raise RuntimeError("power loss")

                with (
                    mock.patch.object(migration, "_checkpoint", side_effect=crash),
                    self.assertRaises(RuntimeError),
                ):
                    migration.migrate(state, offline=True, target=8)
                migration.migrate(state, offline=True, target=8)
                new = Store(state)
                self.assertEqual(new.config["schema"], 8)
                self.assertEqual(capture(new), before)
                self.assertEqual(workflow.status(new, flow["id"])["state"], "active")
                with closing(sqlite3.connect(state / "schema-7-backup.sqlite3")) as db:
                    self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 7)
                with self.assertRaises(ControlError):
                    workflow.status(old, flow["id"])
                self.assertFalse(migration.migrate(state, offline=True, target=8)["migrated"])

    def test_full_chain_has_seven_verified_backups(self):
        with tempfile.TemporaryDirectory() as root:
            state, _ = legacy_store(root)
            result = migration.migrate(state, offline=True)
            self.assertEqual((result["schema"], len(result["backups"])), (13, 10))
            with self.assertRaises(ControlError):
                migration.migrate(state, offline=True, target=7)

    def test_unknown_run_prevents_upgrade(self):
        with tempfile.TemporaryDirectory() as root:
            state, project = legacy_store(root)
            migration.migrate(state, offline=True, target=7)
            old = Store(state)
            task = tasks.create(old, assignment(project), "task")
            run, _ = tasks.submit(old, task["id"], 1, "reserved")
            with old.db(write=True) as db:
                db.execute("UPDATE runs SET status='unknown' WHERE id=?", (run,))
            with self.assertRaises(ControlError) as raised:
                migration.migrate(state, offline=True, target=8)
            self.assertEqual(raised.exception.code, "migration_busy")
            self.assertEqual(Store(state).config["schema"], 7)

    def test_missing_backup_keeps_store_closed(self):
        with tempfile.TemporaryDirectory() as root:
            state, _ = legacy_store(root)
            migration.migrate(state, offline=True, target=7)

            def crash(name):
                if name == "marker":
                    raise RuntimeError("power loss")

            with (
                mock.patch.object(migration, "_checkpoint", side_effect=crash),
                self.assertRaises(RuntimeError),
            ):
                migration.migrate(state, offline=True, target=8)
            (state / "schema-7-backup.sqlite3").unlink()
            with self.assertRaises(ControlError) as raised:
                migration.migrate(state, offline=True, target=8)
            self.assertEqual(raised.exception.code, "migration_backup")
            self.assertEqual(migration.migration_status(state)["schema"], "migrating")


if __name__ == "__main__":
    unittest.main()
