"""Installation rollback must retain the previous package even on a second failure."""

import importlib.util
import hashlib
import json
import multiprocessing
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    "claude_control_installer", Path(__file__).resolve().parents[1] / "install.py"
)
INSTALLER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(INSTALLER)


def _locked_activation(
    lock_path,
    staged,
    target,
    label,
    attempted,
    holding,
    release,
    exited,
    observations,
):
    """Activate one staged tree while exposing deterministic lock-boundary events."""
    attempted.set()
    success = subprocess.CompletedProcess([], 0, '{"enabled":true}', "")
    try:
        with INSTALLER.installation_lock(Path(lock_path)):
            with patch.object(INSTALLER.subprocess, "run", return_value=success):
                result = INSTALLER.activate(Path(staged), Path(target), "test-marketplace")
            observed = (Path(target) / "version.txt").read_text()
            observations.put((label, observed, result))
            holding.set()
            if release is not None and not release.wait(timeout=5):
                raise RuntimeError(f"timed out holding installer lock for {label}")
    finally:
        exited.set()


class InstallRollbackTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="claude-control-install-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.target = self.root / "claude-control"
        self.stage = self.root / "stage" / "claude-control"
        self.stage.mkdir(parents=True)
        (self.stage / "version.txt").write_text("new")

    def previous_install(self):
        self.target.mkdir()
        (self.target / "version.txt").write_text("previous")

    def test_codex_failure_restores_previous_tree(self):
        self.previous_install()
        failure = subprocess.CompletedProcess([], 1, "", "test installation failure")
        with patch.object(INSTALLER.subprocess, "run", return_value=failure):
            with self.assertRaisesRegex(RuntimeError, "test installation failure"):
                INSTALLER.activate(self.stage, self.target, "test-marketplace")
        self.assertEqual((self.target / "version.txt").read_text(), "previous")
        self.assertEqual(list(self.root.glob(".claude-control-backup-*")), [])

    def test_rollback_failure_preserves_backup_outside_staging(self):
        self.previous_install()
        real_rename = Path.rename

        def fail_restoration(path, destination):
            if path.name.startswith(".claude-control-backup-"):
                raise OSError("test restoration failure")
            return real_rename(path, destination)

        failure = subprocess.CompletedProcess([], 1, "", "test installation failure")
        with patch.object(INSTALLER.subprocess, "run", return_value=failure):
            with patch.object(Path, "rename", fail_restoration):
                with self.assertRaisesRegex(OSError, "test restoration failure"):
                    INSTALLER.activate(self.stage, self.target, "test-marketplace")
        backups = list(self.root.glob(".claude-control-backup-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual((backups[0] / "version.txt").read_text(), "previous")
        self.assertEqual(backups[0].parent, self.root)

    def test_first_install_without_existing_target(self):
        success = subprocess.CompletedProcess([], 0, '{"enabled":true}', "")
        with patch.object(INSTALLER.subprocess, "run", return_value=success):
            result = INSTALLER.activate(self.stage, self.target, "test-marketplace")
        self.assertEqual(result, {"enabled": True})
        self.assertEqual((self.target / "version.txt").read_text(), "new")


class ConcurrentInstallTests(unittest.TestCase):
    def test_second_activation_waits_for_first_installation_lock(self):
        temporary = tempfile.TemporaryDirectory(prefix="claude-control-install-race-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        lock_path = root / "install.lock"
        target = root / "claude-control"
        stage_a = root / "stage-a" / "claude-control"
        stage_b = root / "stage-b" / "claude-control"
        stage_a.mkdir(parents=True)
        stage_b.mkdir(parents=True)
        (stage_a / "version.txt").write_text("A")
        (stage_b / "version.txt").write_text("B")

        context = multiprocessing.get_context("fork")
        observations = context.Queue()
        a_attempted = context.Event()
        a_holding = context.Event()
        release_a = context.Event()
        a_exited = context.Event()
        b_attempted = context.Event()
        b_holding = context.Event()
        b_exited = context.Event()
        process_a = context.Process(
            target=_locked_activation,
            args=(
                lock_path,
                stage_a,
                target,
                "A",
                a_attempted,
                a_holding,
                release_a,
                a_exited,
                observations,
            ),
        )
        process_b = context.Process(
            target=_locked_activation,
            args=(
                lock_path,
                stage_b,
                target,
                "B",
                b_attempted,
                b_holding,
                None,
                b_exited,
                observations,
            ),
        )

        def clean_processes():
            release_a.set()
            for process in (process_a, process_b):
                if process.pid is not None:
                    process.join(timeout=2)
                if process.is_alive():
                    process.terminate()
                    process.join(timeout=2)
            observations.close()

        self.addCleanup(clean_processes)
        process_a.start()
        self.assertTrue(a_attempted.wait(timeout=2))
        self.assertTrue(a_holding.wait(timeout=3))

        process_b.start()
        self.assertTrue(b_attempted.wait(timeout=2))
        self.assertFalse(
            b_holding.wait(timeout=0.3),
            "second installer entered activation while the first held the lock",
        )

        release_a.set()
        self.assertTrue(a_exited.wait(timeout=3))
        self.assertTrue(b_holding.wait(timeout=3))
        self.assertTrue(b_exited.wait(timeout=3))
        process_a.join(timeout=2)
        process_b.join(timeout=2)

        self.assertEqual(process_a.exitcode, 0)
        self.assertEqual(process_b.exitcode, 0)
        observed = dict(
            (label, (version, result))
            for label, version, result in (
                observations.get(timeout=2),
                observations.get(timeout=2),
            )
        )
        self.assertEqual(observed["A"], ("A", {"enabled": True}))
        self.assertEqual(observed["B"], ("B", {"enabled": True}))
        self.assertEqual((target / "version.txt").read_text(), "B")
        self.assertEqual(list(root.glob(".claude-control-backup-*")), [])


class WindowsDistributionTests(unittest.TestCase):
    def test_windows_launchers_are_shipped_with_the_plugin(self):
        scripts = Path(__file__).resolve().parents[1] / "plugins/claude-control/scripts"

        self.assertIn("claude_control_cli.py", (scripts / "claude_control.cmd").read_text())
        self.assertIn("claude_control_cli.py", (scripts / "claude_control.ps1").read_text())

    def test_installer_stages_a_sha256_pinned_helper(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            helper = root / "helper.exe"
            helper.write_bytes(b"verified-helper")
            staged = root / "plugin"
            staged.mkdir()
            digest = hashlib.sha256(helper.read_bytes()).hexdigest()

            INSTALLER.stage_windows_helper(staged, helper, digest, machine="AMD64")

            manifest = json.loads((staged / "bin/windows-helper-manifest.json").read_text())
            artifact = manifest["artifacts"][0]
            self.assertEqual(artifact["sha256"], digest)
            self.assertEqual(artifact["size"], len(b"verified-helper"))
            self.assertEqual(
                (staged / "bin" / artifact["name"]).read_bytes(), b"verified-helper"
            )

    def test_installer_rejects_a_helper_hash_mismatch(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            helper = root / "helper.exe"
            helper.write_bytes(b"untrusted")
            staged = root / "plugin"
            staged.mkdir()

            with self.assertRaisesRegex(ValueError, "does not match"):
                INSTALLER.stage_windows_helper(staged, helper, "0" * 64, machine="AMD64")

    def test_update_preserves_only_a_verified_existing_helper(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            existing = root / "existing"
            staged = root / "staged"
            binary_dir = existing / "bin"
            binary_dir.mkdir(parents=True)
            staged.mkdir()
            name = "ccc-win-supervisor-x86_64-pc-windows-msvc.exe"
            data = b"old-verified-helper"
            (binary_dir / name).write_bytes(data)
            (binary_dir / "windows-helper-manifest.json").write_text(
                json.dumps(
                    {
                        "protocol": 1,
                        "artifacts": [
                            {
                                "name": name,
                                "sha256": hashlib.sha256(data).hexdigest(),
                                "size": len(data),
                            }
                        ],
                    }
                )
            )

            self.assertTrue(INSTALLER.preserve_windows_helper(existing, staged))
            self.assertEqual((staged / "bin" / name).read_bytes(), data)

            (binary_dir / name).write_bytes(b"tampered")
            clean = root / "clean"
            clean.mkdir()
            self.assertFalse(INSTALLER.preserve_windows_helper(existing, clean))
            self.assertFalse((clean / "bin").exists())


if __name__ == "__main__":
    unittest.main()
