"""Schema 8→9 effort migration preserves legacy execution intent."""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/claude-control/scripts"))

from claude_control import migration, scheduler, tasks  # noqa: E402
from claude_control.assignments import normalize_assignment, render_assignment  # noqa: E402
from claude_control.orchestration import report  # noqa: E402
from claude_control.runner import launch_worker  # noqa: E402
from claude_control.store import ControlError, Store  # noqa: E402
from test_controller import TERMINAL  # noqa: E402
from test_migration import legacy_store  # noqa: E402


def assignment(project: Path, label: str) -> dict:
    return {
        "id": label,
        "name": label,
        "role": "executor",
        "model": "sonnet",
        "project": str(project),
        "objective": "Return a legacy supplied-text result.",
        "context": "",
        "scope": ["Use supplied text only."],
        "acceptance_criteria": ["Return the required report."],
        "deliverable": "A concise result.",
        "timeout": 10,
    }


def wait_terminal(store: Store, run_id: str, seconds: float = 8) -> dict:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        row = store.get_run(run_id)
        if row["status"] in TERMINAL or row["status"] == "unknown":
            return row
        time.sleep(0.05)
    raise AssertionError(f"run {run_id} did not become terminal")


class EffortMigrationTests(unittest.TestCase):
    def schema8(self, root: str) -> tuple[Path, Path]:
        state, project = legacy_store(root)
        migration.migrate(state, offline=True, target=8)
        return state, project

    def test_legacy_report_queue_prompt_hash_and_fingerprint_survive(self) -> None:
        with tempfile.TemporaryDirectory(prefix="effort-migration-") as root:
            state, project = self.schema8(root)
            old = Store(state)
            legacy_assignment = normalize_assignment(assignment(project, "legacy-report"))
            prompt = render_assignment(legacy_assignment)
            run_id, created = old.reserve(
                prompt=prompt,
                request_id="legacy-report",
                timeout=legacy_assignment["timeout"],
                name=legacy_assignment["name"],
                model=legacy_assignment["model"],
                role=legacy_assignment["role"],
                project=str(project),
            )
            self.assertTrue(created)
            launch_worker(old, run_id)
            completed = wait_terminal(old, run_id)
            before_report = report(old, run_id)
            queued = tasks.create(old, assignment(project, "legacy-queued"), "legacy-task")
            scheduler.enqueue(old, queued["id"], 1, "legacy-queue")
            with old.db() as db:
                revision_before = tuple(
                    db.execute(
                        "SELECT prompt,prompt_sha256 FROM task_revisions WHERE task_id=?",
                        (queued["id"],),
                    ).fetchone()
                )

            migration.migrate(state, offline=True)
            new = Store(state)

            self.assertEqual(new.config["schema"], 15)
            self.assertEqual(new.get_run(run_id)["fingerprint"], completed["fingerprint"])
            self.assertIsNone(new.get_run(run_id)["effort"])
            self.assertIsNone(new.session(completed["session_id"])["effort"])
            self.assertEqual(report(new, run_id), before_report)
            self.assertEqual((new.run_dir(run_id) / "prompt.txt").read_text(), prompt)
            with new.db() as db:
                revision_after = tuple(
                    db.execute(
                        "SELECT prompt,prompt_sha256 FROM task_revisions WHERE task_id=?",
                        (queued["id"],),
                    ).fetchone()
                )
            self.assertEqual(revision_after, revision_before)
            started = scheduler.tick(new)["started"]
            self.assertEqual(len(started), 1)
            queued_run = wait_terminal(new, started[0])
            self.assertIsNone(queued_run["effort"])
            invocation = json.loads(
                (new.run_dir(started[0]) / "invocation.json").read_text(encoding="utf-8")
            )
            self.assertNotIn("--effort", invocation["argv"])

    def test_schema8_to9_crash_boundaries_resume_with_nullable_backfill(self) -> None:
        for phase in ("backup", "marker", "ddl", "db_committed", "config_committed"):
            with (
                self.subTest(phase=phase),
                tempfile.TemporaryDirectory(prefix="effort-crash-") as root,
            ):
                state, project = self.schema8(root)
                old = Store(state)
                run_id, _ = old.reserve(
                    prompt="legacy",
                    request_id="legacy",
                    timeout=10,
                    name="legacy",
                    model="sonnet",
                    role="executor",
                    project=str(project),
                )
                old.stop(run_id)
                fingerprint = old.get_run(run_id)["fingerprint"]

                def crash(name: str) -> None:
                    if name == phase:
                        raise RuntimeError("power loss")

                with mock.patch.object(migration, "_checkpoint", side_effect=crash):
                    with self.assertRaises(RuntimeError):
                        migration.migrate(state, offline=True)
                migration.migrate(state, offline=True)
                new = Store(state)
                row = new.get_run(run_id)

                self.assertEqual(new.config["schema"], 15)
                self.assertEqual(row["fingerprint"], fingerprint)
                self.assertIsNone(row["effort"])
                self.assertIsNone(new.session(row["session_id"])["effort"])
                self.assertFalse(migration.migrate(state, offline=True)["migrated"])
                with sqlite3.connect(state / "schema-8-backup.sqlite3") as backup:
                    session_columns = {
                        column[1] for column in backup.execute("PRAGMA table_info(sessions)")
                    }
                    run_columns = {
                        column[1] for column in backup.execute("PRAGMA table_info(runs)")
                    }
                    self.assertNotIn("effort", session_columns)
                    self.assertNotIn("effort", run_columns)

    def test_schema8_unknown_run_still_blocks_effort_upgrade(self) -> None:
        with tempfile.TemporaryDirectory(prefix="effort-busy-") as root:
            state, project = self.schema8(root)
            old = Store(state)
            run_id, _ = old.reserve(
                prompt="legacy",
                request_id="unknown",
                timeout=10,
                name="unknown",
                model="sonnet",
                role="executor",
                project=str(project),
            )
            with old.db(write=True) as db:
                db.execute("UPDATE runs SET status='unknown' WHERE id=?", (run_id,))

            with self.assertRaises(ControlError) as raised:
                migration.migrate(state, offline=True)

            self.assertEqual(raised.exception.code, "migration_busy")
            self.assertEqual(Store(state).config["schema"], 8)


if __name__ == "__main__":
    unittest.main()
