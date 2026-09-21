"""Offline schema upgrade preserves legacy history across crashes and WAL backups."""

import errno
import json
import os
import sqlite3
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/claude-control/scripts"))

from claude_control import migration, tasks
from claude_control.assignments import normalize_assignment, render_assignment
from claude_control.orchestration import report
from claude_control.runner import launch_worker
from claude_control.store import SCHEMA, ControlError, Store, write_json
from test_controller import FAKE, ControllerTestCase


def legacy_store(root):
    """Freeze the v0.2 layout, not a new schema with its version label changed."""
    root = Path(root)
    project = root / "project"
    project.mkdir()
    state = root / "state"
    config = Store.initialize(state, FAKE, [project])
    config.pop("state_dir")
    # No handles or workers exist: replace only this synthetic new fixture with a v0.2 DB.
    (state / "state.sqlite3").unlink()
    for suffix in ("-wal", "-shm"):
        (state / ("state.sqlite3" + suffix)).unlink(missing_ok=True)
    with sqlite3.connect(state / "state.sqlite3") as db:
        db.executescript(SCHEMA)
        db.execute("PRAGMA journal_mode=WAL")
    os.chmod(state / "state.sqlite3", 0o600)
    config["schema"] = 3
    write_json(state / "config.json", config)
    return state, project


class MigrationTests(unittest.TestCase):
    def test_pre_ddl_old_client_race_aborts_without_quarantining_diagnostics(self):
        with tempfile.TemporaryDirectory() as root:
            state, project = legacy_store(root)
            session_id, run_id, backend = (str(uuid.uuid4()) for _ in range(3))

            def old_client(phase):
                if phase == "marker":
                    with sqlite3.connect(state / "state.sqlite3") as db:
                        db.execute(
                            "INSERT INTO sessions(id,name,backend_id,model,role,project,created) VALUES(?,?,?,?,?,?,0)",
                            (session_id, "late", backend, "sonnet", "executor", str(project)),
                        )
                        db.execute(
                            "INSERT INTO runs(id,session_id,request_id,fingerprint,status,created,resume,timeout,backend_id) VALUES(?,?,?,?,'pending',0,0,10,?)",
                            (run_id, session_id, "late", "legacy-fingerprint", backend),
                        )

            with mock.patch.object(migration, "_checkpoint", side_effect=old_client):
                with self.assertRaises(ControlError) as raised:
                    migration.migrate(state, offline=True, target=4)
            self.assertEqual(raised.exception.code, "migration_busy")
            self.assertEqual(Store(state).config["schema"], 3)
            # The old-client row was retained and normal diagnostics can now expire it.
            Store(state).refresh()
            self.assertEqual(Store(state).get_run(run_id)["status"], "launch_failed")
            migration.migrate(state, offline=True, target=4)
            self.assertEqual(Store(state).config["schema"], 4)
            self.assertEqual(Store(state).get_run(run_id)["fingerprint"], "legacy-fingerprint")

    def test_crash_at_each_boundary_recovers_without_history_loss(self):
        for phase in ("backup", "marker", "ddl", "db_committed", "config_committed"):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as root:
                state, project = legacy_store(root)
                store = Store(state)
                run, _ = store.reserve(
                    prompt="legacy",
                    request_id="legacy",
                    timeout=10,
                    name="legacy",
                    model="sonnet",
                    role="executor",
                    project=project,
                )
                store.stop(run)
                original = store.get_run(run)

                def crash(name):
                    if name == phase:
                        raise OSError(errno.ENOSPC, "simulated interruption")

                with mock.patch.object(migration, "_checkpoint", side_effect=crash):
                    with self.assertRaises(OSError):
                        migration.migrate(state, offline=True, target=4)
                status = migration.migration_status(state)
                self.assertIn(status["schema"], (3, "migrating", 4))
                if status["schema"] == "migrating":
                    with self.assertRaises(ControlError) as raised:
                        Store(state)
                    self.assertEqual(raised.exception.code, "schema_mismatch")
                migration.migrate(state, offline=True, target=4)
                migrated = Store(state)
                self.assertEqual(migrated.get_run(run), original)
                self.assertEqual(migration.migration_status(state)["db_version"], 4)
                self.assertEqual(migration.migration_status(state)["journal"]["phase"], "complete")
                self.assertFalse(migration.migrate(state, offline=True, target=4)["migrated"])
                with sqlite3.connect(state / "schema-3-backup.sqlite3") as backup:
                    self.assertEqual(
                        backup.execute(
                            "SELECT fingerprint FROM runs WHERE id=?", (run,)
                        ).fetchone()[0],
                        original["fingerprint"],
                    )
                    self.assertEqual(backup.execute("PRAGMA user_version").fetchone()[0], 0)
                # A pre-migration Store object cannot write after the schema switch.
                with self.assertRaises(ControlError):
                    store.stop(run)

    def test_requires_offline_and_rejects_every_active_state(self):
        for status in ("pending", "claimed", "launching", "running", "stopping", "unknown"):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as root:
                state, project = legacy_store(root)
                store = Store(state)
                run, _ = store.reserve(
                    prompt="text",
                    request_id="run",
                    timeout=10,
                    name="work",
                    model="sonnet",
                    role="executor",
                    project=project,
                )
                with store.db(write=True) as db:
                    db.execute("UPDATE runs SET status=? WHERE id=?", (status, run))
                for offline, code in ((False, "offline_required"), (True, "migration_busy")):
                    with self.assertRaises(ControlError) as raised:
                        migration.migrate(state, offline=offline, target=4)
                    self.assertEqual(raised.exception.code, code)
                self.assertEqual(json.loads((state / "config.json").read_text())["schema"], 3)
                self.assertFalse((state / "migration-3-4.json").exists())

    def test_backup_includes_uncheckpointed_wal_content(self):
        with tempfile.TemporaryDirectory() as root:
            state, _ = legacy_store(root)
            db = sqlite3.connect(state / "state.sqlite3")
            try:
                db.execute("PRAGMA wal_autocheckpoint=0")
                db.execute("CREATE TABLE legacy_evidence (text TEXT)")
                db.execute("INSERT INTO legacy_evidence VALUES('persist this WAL row')")
                db.commit()
                self.assertGreater((state / "state.sqlite3-wal").stat().st_size, 0)
                migration.migrate(state, offline=True, target=4)
                with sqlite3.connect(state / "schema-3-backup.sqlite3") as backup:
                    self.assertEqual(
                        backup.execute("SELECT text FROM legacy_evidence").fetchone()[0],
                        "persist this WAL row",
                    )
            finally:
                db.close()

    def test_config_db_mismatch_blocks_normal_commands(self):
        with tempfile.TemporaryDirectory() as root:
            state, _ = legacy_store(root)
            config = json.loads((state / "config.json").read_text())
            write_json(state / "config.json", {**config, "schema": 4})
            with self.assertRaises(ControlError) as raised:
                Store(state)
            self.assertEqual(raised.exception.code, "schema_mismatch")
            self.assertEqual(migration.migration_status(state)["db_version"], 0)

    def test_missing_backup_keeps_interrupted_migration_blocked(self):
        with tempfile.TemporaryDirectory() as root:
            state, _ = legacy_store(root)

            def crash(phase):
                if phase == "marker":
                    raise RuntimeError("power loss")

            with mock.patch.object(migration, "_checkpoint", side_effect=crash):
                with self.assertRaises(RuntimeError):
                    migration.migrate(state, offline=True, target=4)
            (state / "schema-3-backup.sqlite3").unlink()
            with self.assertRaises(ControlError) as raised:
                migration.migrate(state, offline=True, target=4)
            self.assertEqual(raised.exception.code, "migration_backup")
            self.assertEqual(migration.migration_status(state)["schema"], "migrating")


class LegacyReportMigrationTests(ControllerTestCase):
    def test_real_v1_prompt_fingerprint_and_report_survive_migration(self):
        # Use a separate legacy fixture so ordinary cleanup never mutates schema-3 records.
        legacy_root = self.root / "legacy"
        legacy_root.mkdir()
        state, project = legacy_store(legacy_root)
        store = Store(state)
        assignment = normalize_assignment(
            dict(
                id="legacy",
                name="legacy",
                role="executor",
                project=str(project),
                objective="Explain.",
                context="Given context.",
                scope=["text"],
                acceptance_criteria=["Answer"],
                deliverable="Text",
            )
        )
        prompt = render_assignment(assignment)
        run, _ = store.reserve(
            prompt=prompt,
            request_id="legacy",
            timeout=assignment["timeout"],
            name="legacy",
            model="sonnet",
            role="executor",
            project=project,
        )
        launch_worker(store, run)
        original_state = self.state
        self.state = state
        try:
            row = self.wait_terminal(run)
            self.assertEqual(row["status"], "completed")
            before = report(store, run)
            self.assertEqual(before["format_status"], "valid")
            with self.assertRaises(ControlError) as raised:
                tasks.create(store, assignment, "new-task")
            self.assertEqual(raised.exception.code, "migration_required")
            migration.migrate(state, offline=True, target=4)
            self.assertEqual(report(Store(state), run), before)
            self.assertEqual(Store(state).get_run(run)["fingerprint"], row["fingerprint"])
            self.assertEqual((Store(state).run_dir(run) / "prompt.txt").read_text(), prompt)
            task = tasks.create(
                Store(state),
                assignment,
                "new-task",
                session_ref=row["session_id"],
                parent_run_id=run,
            )
            new_run, _ = tasks.submit(Store(state), task["id"], 1, "new-run")
            launch_worker(Store(state), new_run)
            self.assertEqual(self.wait_terminal(new_run)["status"], "completed")
            self.assertEqual(report(Store(state), new_run)["format_status"], "valid")
        finally:
            self.state = original_state
