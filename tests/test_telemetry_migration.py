"""Schema 10→11 adds append-only run telemetry without rewriting history."""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/claude-control/scripts"))

from claude_control import migration  # noqa: E402
from claude_control.store import Store  # noqa: E402
from test_migration import legacy_store  # noqa: E402


class TelemetryMigrationTests(unittest.TestCase):
    def test_schema10_to11_preserves_runs_and_adds_empty_telemetry(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            state, _ = legacy_store(root)
            migration.migrate(state, offline=True, target=10)
            old = Store(state)
            with old.db() as db:
                before = [tuple(row) for row in db.execute("SELECT rowid,* FROM runs")]

            result = migration.migrate(state, offline=True, target=11)
            current = Store(state)

            with current.db() as db:
                after = [tuple(row) for row in db.execute("SELECT rowid,* FROM runs")]
                telemetry = db.execute("SELECT count(*) FROM run_telemetry").fetchone()[0]
                violations = db.execute("PRAGMA foreign_key_check").fetchall()
            self.assertEqual(result["schema"], 11)
            self.assertEqual(after, before)
            self.assertEqual(telemetry, 0)
            self.assertEqual(violations, [])
            with closing(sqlite3.connect(state / "schema-10-backup.sqlite3")) as backup:
                self.assertEqual(backup.execute("PRAGMA user_version").fetchone()[0], 10)

    def test_schema11_to12_adds_empty_application_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            state, _ = legacy_store(root)
            migration.migrate(state, offline=True, target=11)

            result = migration.migrate(state, offline=True, target=12)
            current = Store(state)

            with current.db() as db:
                count = db.execute("SELECT count(*) FROM workspace_applications").fetchone()[0]
                violations = db.execute("PRAGMA foreign_key_check").fetchall()
            self.assertEqual(result["schema"], 12)
            self.assertEqual(count, 0)
            self.assertEqual(violations, [])


if __name__ == "__main__":
    unittest.main()
