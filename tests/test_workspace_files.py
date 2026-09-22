"""Integration tests for bounded Git snapshots and frozen workspace receipts."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/claude-control/scripts"))

from claude_control import workspace_files  # noqa: E402
from claude_control.store import ControlError  # noqa: E402


class WorkspaceFilesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="workspace-files-test-")
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_git(self, repo: Path, *arguments: str) -> str:
        completed = subprocess.run(
            ["/usr/bin/git", "-C", str(repo), *arguments],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(
            completed.returncode,
            0,
            f"git {' '.join(arguments)}: {completed.stdout!r} {completed.stderr!r}",
        )
        return completed.stdout.strip()

    def repository(self, files: dict[str, bytes | str] | None = None) -> tuple[Path, str]:
        repo = self.root / f"repo-{len(list(self.root.glob('repo-*')))}"
        repo.mkdir()
        self.run_git(repo, "init", "-q")
        self.run_git(repo, "config", "user.name", "Test")
        self.run_git(repo, "config", "user.email", "test@example.invalid")
        for relative, content in (files or {"README.md": "base\n"}).items():
            target = repo / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content.encode() if isinstance(content, str) else content)
        self.run_git(repo, "add", ".")
        self.run_git(repo, "commit", "-qm", "base")
        return repo, self.run_git(repo, "rev-parse", "HEAD")

    @staticmethod
    def policy(*, read=None, write=None) -> dict:
        return {
            "version": 1,
            "role": "executor",
            "read_paths": read or ["."],
            "write_paths": write or [],
            "checks": {},
        }

    def assert_error(self, code: str, call, *args, **kwargs) -> None:
        with self.assertRaises(ControlError) as raised:
            call(*args, **kwargs)
        self.assertEqual(raised.exception.code, code)

    def test_snapshot_uses_exact_commit_and_never_mutates_dirty_source(self) -> None:
        repo, commit = self.repository(
            {"README.md": "committed\n", "bin/tool": "#!/bin/sh\nexit 0\n", "raw.bin": b"\x00\xff"}
        )
        (repo / "bin/tool").chmod(0o755)
        self.run_git(repo, "add", "bin/tool")
        self.run_git(repo, "commit", "--amend", "--no-edit", "-q")
        commit = self.run_git(repo, "rev-parse", "HEAD")
        (repo / "README.md").write_text("dirty\n")
        (repo / "untracked.txt").write_text("untracked\n")
        before = self.run_git(repo, "status", "--porcelain=v1", "--untracked-files=all")

        result = workspace_files.create_snapshot(
            repo, commit, self.root / "snapshot", self.policy()
        )

        self.assertEqual(result["base_commit"], commit)
        self.assertEqual((self.root / "snapshot/README.md").read_text(), "committed\n")
        self.assertFalse((self.root / "snapshot/untracked.txt").exists())
        self.assertEqual(result["files"]["bin/tool"]["mode"], "100755")
        self.assertEqual((self.root / "snapshot/raw.bin").read_bytes(), b"\x00\xff")
        self.assertEqual(
            self.run_git(repo, "status", "--porcelain=v1", "--untracked-files=all"), before
        )
        self.assertEqual(
            workspace_files.inspect_tree(self.root / "snapshot", self.policy())["sha256"],
            result["sha256"],
        )
        self.assert_error(
            "workspace_integrity",
            workspace_files.create_snapshot,
            repo,
            commit,
            repo / "nested-output",
            self.policy(),
        )

    def test_snapshot_scope_excludes_unselected_and_protected_files(self) -> None:
        repo, commit = self.repository(
            {
                "src/a.py": "a\n",
                "docs/note.md": "docs\n",
                ".env": "secret\n",
                "nested/credentials.json": "{}\n",
            }
        )

        scoped = workspace_files.create_snapshot(
            repo, commit, self.root / "scoped", self.policy(read=["src/"])
        )
        read_all = workspace_files.create_snapshot(
            repo, commit, self.root / "all", self.policy(read=["."])
        )

        self.assertEqual(set(scoped["files"]), {"src/a.py"})
        self.assertEqual(set(read_all["files"]), {"docs/note.md", "src/a.py"})

    def test_snapshot_rejects_selected_symlink_and_submodule_entries(self) -> None:
        symlink_repo, _ = self.repository()
        os.symlink("README.md", symlink_repo / "link")
        self.run_git(symlink_repo, "add", "link")
        self.run_git(symlink_repo, "commit", "-qm", "symlink")
        symlink_commit = self.run_git(symlink_repo, "rev-parse", "HEAD")

        module_repo, module_commit = self.repository()
        self.run_git(
            module_repo,
            "update-index",
            "--add",
            "--cacheinfo",
            f"160000,{module_commit},vendor",
        )
        self.run_git(module_repo, "commit", "-qm", "gitlink")
        gitlink_commit = self.run_git(module_repo, "rev-parse", "HEAD")

        for name, repo, commit in (
            ("symlink", symlink_repo, symlink_commit),
            ("submodule", module_repo, gitlink_commit),
        ):
            with self.subTest(name=name):
                self.assert_error(
                    "workspace_source",
                    workspace_files.create_snapshot,
                    repo,
                    commit,
                    self.root / name,
                    self.policy(),
                )

    def test_snapshot_enforces_file_count_size_and_safe_destination(self) -> None:
        large_repo, large_commit = self.repository({"large.bin": b"x" * (256 * 1024 + 1)})
        occupied = self.root / "occupied"
        occupied.mkdir()
        (occupied / "keep.txt").write_text("keep")

        self.assert_error(
            "workspace_source",
            workspace_files.create_snapshot,
            large_repo,
            large_commit,
            self.root / "large-output",
            self.policy(),
        )
        count_repo, count_commit = self.repository({f"many/f{i:04}.txt": "" for i in range(1025)})
        self.assert_error(
            "workspace_source",
            workspace_files.create_snapshot,
            count_repo,
            count_commit,
            self.root / "count-output",
            self.policy(),
        )
        total_tree = self.root / "total-tree"
        total_tree.mkdir()
        block = b"x" * (256 * 1024)
        for index in range(65):
            path = total_tree / f"f{index:02}.bin"
            path.write_bytes(block)
            path.chmod(0o644)
        self.assert_error(
            "workspace_integrity",
            workspace_files.inspect_tree,
            total_tree,
            self.policy(),
        )
        repo, commit = self.repository()
        self.assert_error(
            "workspace_integrity",
            workspace_files.create_snapshot,
            repo,
            commit,
            occupied,
            self.policy(),
        )
        self.assertEqual((occupied / "keep.txt").read_text(), "keep")
        self.assert_error(
            "workspace_source",
            workspace_files.create_snapshot,
            repo,
            "--help",
            self.root / "bad-ref",
            self.policy(),
        )

    def test_inspect_and_copy_reject_unauthorized_symlink_and_hardlink_files(self) -> None:
        source = self.root / "source"
        source.mkdir()
        (source / "allowed.txt").write_text("ok\n")
        (source / "allowed.txt").chmod(0o644)
        policy = self.policy(read=["allowed.txt"])
        (source / "extra.txt").write_text("extra\n")
        (source / "extra.txt").chmod(0o644)
        self.assert_error("workspace_path_denied", workspace_files.inspect_tree, source, policy)
        (source / "extra.txt").unlink()
        copied = workspace_files.copy_tree(source, self.root / "copy", policy)
        self.assertEqual(set(copied["files"]), {"allowed.txt"})

        for name, prepare in (
            ("symlink", lambda root: os.symlink("allowed.txt", root / "other.txt")),
            ("hardlink", lambda root: os.link(root / "allowed.txt", root / "other.txt")),
        ):
            bad = self.root / name
            bad.mkdir()
            (bad / "allowed.txt").write_text("ok\n")
            (bad / "allowed.txt").chmod(0o644)
            prepare(bad)
            with self.subTest(name=name):
                self.assert_error(
                    "workspace_integrity", workspace_files.inspect_tree, bad, self.policy()
                )

    def test_text_reads_and_atomic_writes_enforce_policy_utf8_and_symlink_boundaries(self) -> None:
        tree = self.root / "text-tree"
        (tree / "src").mkdir(parents=True)
        target = tree / "src/file.txt"
        target.write_text("before\n")
        target.chmod(0o644)
        policy = self.policy(read=["src/"], write=["src/file.txt", "src/new.txt"])

        read = workspace_files.read_text(tree, "src/file.txt", policy)
        changed = workspace_files.write_text(tree, "src/file.txt", "after\n", policy)
        created = workspace_files.write_text(tree, "src/new.txt", "new\n", policy)

        self.assertEqual(read["content"], "before\n")
        self.assertEqual(changed["before_sha256"], read["sha256"])
        self.assertEqual(target.read_text(), "after\n")
        self.assertIsNone(created["before_sha256"])
        self.assert_error(
            "workspace_path_denied",
            workspace_files.write_text,
            tree,
            "src/other.txt",
            "denied",
            policy,
        )
        target.write_bytes(b"\xff")
        self.assert_error(
            "workspace_integrity",
            workspace_files.write_text,
            tree,
            "src/file.txt",
            "replacement",
            policy,
        )
        target.unlink()
        (tree / "outside").mkdir()
        os.symlink(tree / "outside", tree / "src/link")
        escape_policy = self.policy(read=["src/"], write=["src/link/escape.txt"])
        self.assert_error(
            "workspace_integrity",
            workspace_files.write_text,
            tree,
            "src/link/escape.txt",
            "escape",
            escape_policy,
        )

    def test_hunk_patch_requires_exact_base_and_context(self) -> None:
        tree = self.root / "patch-tree"
        tree.mkdir()
        target = tree / "file.txt"
        target.write_text("one\ntwo\nthree\n", encoding="utf-8")
        target.chmod(0o644)
        policy = self.policy(read=["file.txt"], write=["file.txt"])
        base = workspace_files.read_text(tree, "file.txt", policy)["sha256"]
        hunks = [
            {
                "old_start": 2,
                "old_count": 1,
                "new_start": 2,
                "new_count": 2,
                "lines": ["-two\n", "+second\n", "+2.5\n"],
            }
        ]

        receipt = workspace_files.apply_patch(tree, "file.txt", base, hunks, policy)

        self.assertEqual(target.read_text(encoding="utf-8"), "one\nsecond\n2.5\nthree\n")
        self.assertEqual(receipt["before_sha256"], base)
        self.assertEqual(receipt["hunks"], 1)
        self.assert_error(
            "workspace_conflict",
            workspace_files.apply_patch,
            tree,
            "file.txt",
            base,
            hunks,
            policy,
        )

    def test_freeze_records_only_authorized_text_changes_and_verifies_receipts(self) -> None:
        baseline = self.root / "baseline"
        working = self.root / "working"
        baseline.mkdir()
        (baseline / "keep.txt").write_text("same\n")
        (baseline / "edit.txt").write_text("old\n")
        (baseline / "delete.txt").write_text("gone\n")
        for path in baseline.iterdir():
            path.chmod(0o644)
        workspace_files.copy_tree(baseline, working, self.policy())
        (working / "edit.txt").write_text("new\n")
        (working / "delete.txt").unlink()
        (working / "add.txt").write_text("added\n")
        (working / "add.txt").chmod(0o644)
        policy = self.policy(
            write=["edit.txt", "delete.txt", "add.txt"],
        )

        frozen = workspace_files.freeze(baseline, working, self.root / "frozen", policy)
        verified = workspace_files.verify_frozen(
            self.root / "frozen", policy, frozen["manifest_sha256"]
        )

        self.assertEqual(verified, frozen)
        self.assertEqual(
            [change["path"] for change in frozen["changes"]],
            ["add.txt", "delete.txt", "edit.txt"],
        )
        patch = (self.root / "frozen/patch.diff").read_text()
        self.assertIn("--- /dev/null\n+++ b/add.txt", patch)
        self.assertIn("--- a/delete.txt\n+++ /dev/null", patch)
        self.assertEqual((self.root / "frozen/tree/edit.txt").read_text(), "new\n")

    def test_freeze_marks_text_without_a_final_newline(self) -> None:
        baseline = self.root / "nonl-baseline"
        working = self.root / "nonl-working"
        baseline.mkdir()
        (baseline / "edit.txt").write_text("before")
        (baseline / "edit.txt").chmod(0o644)
        workspace_files.copy_tree(baseline, working, self.policy())
        (working / "edit.txt").write_text("after")

        workspace_files.freeze(
            baseline,
            working,
            self.root / "nonl-frozen",
            self.policy(write=["edit.txt"]),
        )

        patch = (self.root / "nonl-frozen/patch.diff").read_text()
        self.assertIn("-before\n\\ No newline at end of file\n", patch)
        self.assertIn("+after\n\\ No newline at end of file\n", patch)

    def test_freeze_rejects_unauthorized_binary_and_mode_changes(self) -> None:
        for name, mutate in (
            ("unauthorized", lambda tree: (tree / "fixed.txt").write_text("changed\n")),
            ("binary", lambda tree: (tree / "edit.txt").write_bytes(b"\xff\x00")),
            ("mode", lambda tree: (tree / "edit.txt").chmod(0o755)),
        ):
            baseline = self.root / f"baseline-{name}"
            working = self.root / f"working-{name}"
            baseline.mkdir()
            for relative in ("fixed.txt", "edit.txt"):
                (baseline / relative).write_text("base\n")
                (baseline / relative).chmod(0o644)
            workspace_files.copy_tree(baseline, working, self.policy())
            mutate(working)
            with self.subTest(name=name):
                code = "workspace_path_denied" if name == "unauthorized" else "workspace_integrity"
                self.assert_error(
                    code,
                    workspace_files.freeze,
                    baseline,
                    working,
                    self.root / f"frozen-{name}",
                    self.policy(write=["edit.txt"]),
                )

    def test_verify_frozen_detects_manifest_patch_and_tree_tampering(self) -> None:
        for name, tamper in (
            ("manifest", lambda root: (root / "manifest.json").write_text("{}")),
            ("patch", lambda root: (root / "patch.diff").write_text("tampered")),
            ("tree", lambda root: (root / "tree/edit.txt").write_text("tampered")),
            ("extra", lambda root: (root / "extra.txt").write_text("unexpected")),
        ):
            baseline = self.root / f"verify-base-{name}"
            working = self.root / f"verify-work-{name}"
            frozen_dir = self.root / f"verify-frozen-{name}"
            baseline.mkdir()
            (baseline / "edit.txt").write_text("old\n")
            (baseline / "edit.txt").chmod(0o644)
            workspace_files.copy_tree(baseline, working, self.policy())
            (working / "edit.txt").write_text("new\n")
            policy = self.policy(write=["edit.txt"])
            receipt = workspace_files.freeze(baseline, working, frozen_dir, policy)
            tamper(frozen_dir)
            with self.subTest(name=name):
                self.assert_error(
                    "workspace_integrity",
                    workspace_files.verify_frozen,
                    frozen_dir,
                    policy,
                    receipt["manifest_sha256"],
                )


if __name__ == "__main__":
    unittest.main()
