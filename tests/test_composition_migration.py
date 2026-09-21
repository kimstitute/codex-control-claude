"""Schema 9→10 composition migration preserves all existing orchestration history."""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/claude-control/scripts"))

from claude_control import migration, workflow  # noqa: E402
from claude_control.store import Store  # noqa: E402
from test_migration import legacy_store  # noqa: E402
from test_queue_migration import assignment  # noqa: E402

PRESERVED_TABLES = (
    "sessions",
    "runs",
    "tasks",
    "task_revisions",
    "task_operations",
    "dependency_sets",
    "queue_entries",
    "workflows",
    "workflow_steps",
)


def capture(store: Store) -> dict[str, list[tuple]]:
    with store.db() as db:
        return {
            table: [tuple(row) for row in db.execute(f"SELECT rowid,* FROM {table} ORDER BY rowid")]
            for table in PRESERVED_TABLES
        }


class CompositionMigrationTests(unittest.TestCase):
    def test_schema9_to10_crash_boundaries_preserve_existing_history(self) -> None:
        for phase in ("backup", "marker", "ddl", "db_committed", "config_committed"):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as root:
                state, project = legacy_store(root)
                migration.migrate(state, offline=True, target=9)
                old = Store(state)
                created = workflow.create(old, assignment(project), "legacy-workflow")
                before = capture(old)

                def crash(name: str) -> None:
                    if name == phase:
                        raise RuntimeError("simulated shutdown")

                with (
                    mock.patch.object(migration, "_checkpoint", side_effect=crash),
                    self.assertRaises(RuntimeError),
                ):
                    migration.migrate(state, offline=True, target=10)

                migration.migrate(state, offline=True, target=10)
                current = Store(state)

                self.assertEqual(current.config["schema"], 10)
                self.assertEqual(capture(current), before)
                self.assertEqual(workflow.status(current, created["id"])["state"], "active")
                with closing(sqlite3.connect(state / "schema-9-backup.sqlite3")) as backup:
                    self.assertEqual(backup.execute("PRAGMA user_version").fetchone()[0], 9)
                    tables = {
                        row[0]
                        for row in backup.execute(
                            "SELECT name FROM sqlite_master WHERE type='table'"
                        )
                    }
                self.assertNotIn("compositions", tables)
                self.assertFalse(migration.migrate(state, offline=True, target=10)["migrated"])

    def test_schema10_adds_empty_composition_tables_without_foreign_key_damage(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            state, project = legacy_store(root)
            migration.migrate(state, offline=True, target=9)
            old = Store(state)
            workflow.create(old, assignment(project), "existing-workflow")

            migration.migrate(state, offline=True, target=10)
            current = Store(state)

            with current.db() as db:
                counts = {
                    table: db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                    for table in (
                        "compositions",
                        "composition_members",
                        "composition_results",
                    )
                }
                violations = db.execute("PRAGMA foreign_key_check").fetchall()
            self.assertEqual(counts, {name: 0 for name in counts})
            self.assertEqual(violations, [])


if __name__ == "__main__":
    unittest.main()
