"""Linux-runnable tests for Windows workspace file metadata branches."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/claude-control/scripts"))

from claude_control import windows_wsl_sandbox, workspace_files  # noqa: E402
from claude_control.store import ControlError  # noqa: E402


def policy(*, write=None):
    return {
        "version": 1,
        "role": "executor",
        "read_paths": ["."],
        "write_paths": write or [],
        "checks": {},
    }


class WindowsWorkspaceFileTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="workspace-files-windows-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.windows = mock.patch.object(workspace_files, "_ON_WINDOWS", True)
        self.windows.start()
        self.addCleanup(self.windows.stop)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", "-q")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.invalid")
        (self.repo / "README.md").write_text("base\n")
        (self.repo / "run.sh").write_text("#!/bin/sh\nexit 0\n")
        (self.repo / "run.sh").chmod(0o755)
        self.git("add", ".")
        self.git("commit", "-qm", "base")
        self.commit = self.git("rev-parse", "HEAD")

    def git(self, *arguments):
        completed = subprocess.run(
            ["/usr/bin/git", "-C", str(self.repo), *arguments],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return completed.stdout.strip()

    def snapshot(self, name="snapshot"):
        return workspace_files.create_snapshot(
            self.repo, self.commit, self.root / name, policy()
        )

    def test_snapshot_keeps_modes_outside_model_visible_tree(self):
        result = self.snapshot()
        modes = json.loads((self.root / "snapshot.modes.json").read_text())

        self.assertEqual(modes, {"README.md": "100644", "run.sh": "100755"})
        self.assertEqual(result["files"]["run.sh"]["mode"], "100755")
        self.assertNotIn("snapshot.modes.json", {path.name for path in (self.root / "snapshot").iterdir()})

    def test_inspection_uses_exact_sidecar_and_rejects_drift(self):
        self.snapshot("tree")
        inspected = workspace_files.inspect_tree(self.root / "tree", policy())
        self.assertEqual(inspected["files"]["run.sh"]["mode"], "100755")

        modes_path = self.root / "tree.modes.json"
        modes = json.loads(modes_path.read_text())
        del modes["run.sh"]
        modes_path.write_bytes(workspace_files._canonical(modes))
        with self.assertRaises(ControlError) as raised:
            workspace_files.inspect_tree(self.root / "tree", policy())
        self.assertEqual(raised.exception.code, "workspace_integrity")

    def test_write_preserves_existing_executable_mode_and_adds_new_file_mode(self):
        self.snapshot("edit")
        workspace_files.write_text(
            self.root / "edit", "run.sh", "#!/bin/sh\nexit 1\n", policy(write=["run.sh"])
        )
        modes = json.loads((self.root / "edit.modes.json").read_text())
        self.assertEqual(modes["run.sh"], "100755")

        readable = policy(write=["new.txt"])
        workspace_files.write_text(self.root / "edit", "new.txt", "new\n", readable)
        modes = json.loads((self.root / "edit.modes.json").read_text())
        self.assertEqual(modes["new.txt"], "100644")

    def test_copy_freeze_and_verify_carry_mode_metadata(self):
        self.snapshot("baseline")
        workspace_files.copy_tree(
            self.root / "baseline", self.root / "working", policy()
        )
        workspace_files.write_text(
            self.root / "working", "README.md", "changed\n", policy(write=["README.md"])
        )
        writable = policy(write=["README.md"])
        frozen = workspace_files.freeze(
            self.root / "baseline", self.root / "working", self.root / "frozen", writable
        )
        verified = workspace_files.verify_frozen(
            self.root / "frozen", writable, frozen["manifest_sha256"]
        )
        self.assertEqual(verified["files"]["run.sh"]["mode"], "100755")
        self.assertTrue((self.root / "frozen/tree.modes.json").is_file())

    def test_wsl_archive_uses_controller_mode_metadata(self):
        self.snapshot("scratch")
        packed, manifest = windows_wsl_sandbox._pack_tree(self.root / "scratch")
        self.assertTrue(packed)
        self.assertEqual(manifest["run.sh"]["mode"], 0o755)

    def test_reparse_probe_failure_is_fail_closed(self):
        self.snapshot("unsafe")
        with mock.patch.object(
            workspace_files, "reject_reparse", side_effect=ControlError("unsafe_state", "reparse")
        ):
            with self.assertRaises(ControlError):
                workspace_files.inspect_tree(self.root / "unsafe", policy())


if __name__ == "__main__":
    unittest.main()
