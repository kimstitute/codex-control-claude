import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins" / "claude-control" / "scripts"))

from claude_control.execution_settings import binary_identity  # noqa: E402
from claude_control.store import ControlError, Store  # noqa: E402


def executable(path: Path, version: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "#!/bin/sh\n"
        f'if [ "$1" = "--version" ]; then echo "fake-claude {version}"; exit 0; fi\n'
        "exit 0\n",
        encoding="utf-8",
    )
    path.chmod(0o700)


@unittest.skipIf(os.name == "nt", "POSIX symlink fixture")
class BinaryRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.versions = self.root / ".local" / "share" / "claude" / "versions"
        self.launcher = self.root / ".local" / "bin" / "claude"
        self.old = self.versions / "1.0.0"
        executable(self.old, "1.0.0")
        self.launcher.parent.mkdir(parents=True)
        self.launcher.symlink_to(self.old)
        self.state = self.root / "state"

    def tearDown(self):
        self.temporary.cleanup()

    def rotate(self):
        new = self.versions / "1.1.0"
        executable(new, "1.1.0")
        self.launcher.unlink()
        self.launcher.symlink_to(new)
        self.old.unlink()
        return new

    def test_initialize_preserves_launcher_and_missing_target_recovers_atomically(self):
        initialized = Store.initialize(self.state, self.launcher, [self.project])
        self.assertEqual(initialized["claude_launcher"], str(self.launcher.absolute()))
        self.assertEqual(initialized["claude_binary_dir"], str(self.versions.resolve()))
        new = self.rotate()

        reopened = Store(self.state)

        self.assertEqual(reopened.config["claude_bin"], str(new.resolve()))
        recovery = reopened.config["claude_binary_recovery"]
        self.assertEqual(recovery["previous"], str(self.old.resolve(strict=False)))
        self.assertEqual(recovery["launcher"], str(self.launcher.absolute()))
        self.assertEqual(json.loads((self.state / "config.json").read_text()), reopened.config)

    def test_recovery_refuses_candidate_outside_pinned_binary_directory(self):
        Store.initialize(self.state, self.launcher, [self.project])
        outside = self.root / "outside" / "claude"
        executable(outside, "hostile")
        self.launcher.unlink()
        self.launcher.symlink_to(outside)
        self.old.unlink()

        with self.assertRaises(ControlError) as raised:
            Store(self.state)

        self.assertEqual(raised.exception.code, "binary_recovery_failed")

    def test_recovery_waits_until_controller_runs_are_terminal(self):
        Store.initialize(self.state, self.launcher, [self.project])
        with sqlite3.connect(self.state / "state.sqlite3") as database:
            database.execute(
                "INSERT INTO sessions(id,name,backend_id,model,role,project,created,effort) "
                "VALUES('session','session','backend','sonnet','executor',?,1.0,NULL)",
                (str(self.project.resolve()),),
            )
            database.execute(
                "INSERT INTO runs(id,session_id,request_id,fingerprint,status,created,resume,timeout,backend_id,effort) "
                "VALUES('run','session','request','fingerprint','pending',1.0,0,30.0,'backend',NULL)"
            )
        self.rotate()

        with self.assertRaises(ControlError) as raised:
            Store(self.state)

        self.assertEqual(raised.exception.code, "binary_recovery_busy")

    def test_legacy_version_layout_uses_only_the_canonical_sibling_launcher(self):
        Store.initialize(self.state, self.old, [self.project])
        config_path = self.state / "config.json"
        config = json.loads(config_path.read_text())
        config.pop("claude_launcher")
        config.pop("claude_binary_dir")
        config_path.write_text(json.dumps(config), encoding="utf-8")
        config_path.chmod(0o600)
        seeded = Store(self.state)
        self.assertEqual(seeded.config["claude_launcher"], str(self.launcher.absolute()))
        new = self.rotate()

        reopened = Store(self.state)

        self.assertEqual(reopened.config["claude_bin"], str(new.resolve()))
        self.assertEqual(reopened.config["claude_launcher"], str(self.launcher.absolute()))

    def test_recovered_binary_cannot_silently_resume_an_existing_headless_session(self):
        Store.initialize(self.state, self.launcher, [self.project])
        previous_identity = list(binary_identity(self.old))
        run_id = "11111111-1111-4111-8111-111111111111"
        run_dir = self.state / "runs" / run_id
        run_dir.mkdir(mode=0o700)
        (run_dir / "invocation.json").write_text(
            json.dumps(
                {"argv": [str(self.old.resolve()), "-p"], "binary_identity": previous_identity}
            ),
            encoding="utf-8",
        )
        (run_dir / "invocation.json").chmod(0o600)
        with sqlite3.connect(self.state / "state.sqlite3") as database:
            database.execute(
                "INSERT INTO sessions(id,name,backend_id,model,role,project,created,effort) "
                "VALUES('session','session','backend','sonnet','executor',?,1.0,NULL)",
                (str(self.project.resolve()),),
            )
            database.execute(
                "INSERT INTO runs(id,session_id,request_id,fingerprint,status,created,resume,timeout,backend_id,effort) "
                "VALUES(?,'session','request','fingerprint','completed',1.0,0,30.0,'backend',NULL)",
                (run_id,),
            )
        self.rotate()
        reopened = Store(self.state)

        with self.assertRaises(ControlError) as raised:
            reopened._assert_resume_binary({"id": run_id})

        self.assertEqual(raised.exception.code, "session_binary_changed")


if __name__ == "__main__":
    unittest.main()
