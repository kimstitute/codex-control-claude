"""Relocation test for the self-contained plugin distribution."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_SOURCE = PROJECT_ROOT / "plugins" / "claude-control"
FAKE_CLAUDE = Path(__file__).with_name("fake_claude.py")
ACTIVE = {"pending", "claimed", "launching", "running", "stopping", "unknown"}


class RelocatedDistributionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="claude-control-distribution-")
        self.root = Path(self.temporary.name)
        self.plugin = self.root / "Installed Plugin With Spaces"
        shutil.copytree(
            PLUGIN_SOURCE,
            self.plugin,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
        self.cli_path = self.plugin / "scripts" / "claude_control_cli.py"
        self.state = self.root / "Private State With Spaces"
        self.project = self.root / "Authorized Project With Spaces"
        self.cwd = self.root / "Unrelated Working Directory"
        for directory in (self.project, self.cwd):
            directory.mkdir()
        self.environment = dict(os.environ)
        self.environment.pop("PYTHONPATH", None)
        # All writes use explicit temporary project/state paths; preserve the host's HOME.
        self.environment["PYTHONDONTWRITEBYTECODE"] = "1"
        self.known_runs: list[str] = []

    def tearDown(self) -> None:
        if (self.state / "config.json").exists():
            listed = self.cli("list", expected=None)
            for run in listed.get("runs", []):
                if run["id"] in self.known_runs and run["status"] in ACTIVE:
                    self.cli("stop", "--run", run["id"], expected=None)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                listed = self.cli("list", expected=None)
                owned = [run for run in listed.get("runs", []) if run["id"] in self.known_runs]
                if all(run["status"] not in ACTIVE or run["status"] == "unknown" for run in owned):
                    break
                time.sleep(0.1)
        self.temporary.cleanup()

    def cli(self, *arguments: str, expected: int | None = 0) -> dict:
        completed = subprocess.run(
            [
                sys.executable,
                str(self.cli_path),
                "--state-dir",
                str(self.state),
                *arguments,
            ],
            cwd=self.cwd,
            env=self.environment,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            self.fail(
                f"relocated CLI emitted invalid JSON (exit {completed.returncode}): "
                f"stdout={completed.stdout!r} stderr={completed.stderr!r}: {exc}"
            )
        if expected is not None:
            self.assertEqual(
                completed.returncode,
                expected,
                f"stdout={completed.stdout!r} stderr={completed.stderr!r}",
            )
        return payload

    def wait_terminal(self, run_id: str) -> dict:
        deadline = time.monotonic() + 8
        last = None
        while time.monotonic() < deadline:
            last = self.cli("wait", "--run", run_id, "--seconds", "0.4")
            if last["status"] not in ACTIVE or last["status"] == "unknown":
                return last
        self.fail(f"relocated run {run_id} did not settle: {last}")

    def test_relocated_plugin_runs_supplied_text_and_continues_session(self) -> None:
        initialized = self.cli(
            "init",
            "--claude-bin",
            str(FAKE_CLAUDE),
            "--allow-root",
            str(self.project),
            "--max-parallel",
            "2",
        )
        doctor = self.cli("doctor", "--auth")
        first_prompt = self.root / "First Prompt With Spaces.json"
        first_prompt.write_text(
            json.dumps({"remember": "supplied-text implementation answer"}),
            encoding="utf-8",
        )
        first = self.cli(
            "start",
            "--name",
            "relocated-sonnet",
            "--model",
            "sonnet",
            "--role",
            "small implementation",
            "--project",
            str(self.project),
            "--prompt-file",
            str(first_prompt),
            "--request-id",
            "distribution-first-turn",
            "--timeout",
            "10",
        )
        self.known_runs.append(first["id"])
        first_done = self.wait_terminal(first["id"])
        first_result = self.cli("result", "--run", first["id"])

        continuation_prompt = self.root / "Continuation Prompt.json"
        continuation_prompt.write_text(json.dumps({"recall": True}), encoding="utf-8")
        continuation = self.cli(
            "followup",
            "--session",
            first["session_id"],
            "--prompt-file",
            str(continuation_prompt),
            "--request-id",
            "distribution-second-turn",
            "--timeout",
            "10",
        )
        self.known_runs.append(continuation["id"])
        continuation_done = self.wait_terminal(continuation["id"])
        continuation_result = self.cli("result", "--run", continuation["id"])

        self.assertEqual(initialized["schema"], 8)
        self.assertIs(doctor["ready"], True)
        self.assertEqual(doctor["missing_options"], [])
        self.assertEqual(first_done["status"], "completed")
        self.assertEqual(first_result["result"]["response"], "supplied-text implementation answer")
        self.assertEqual(first_result["run"]["actual_models"], ["claude-sonnet-test"])
        self.assertEqual(continuation_done["status"], "completed")
        self.assertEqual(
            continuation_result["result"]["response"],
            "supplied-text implementation answer",
        )
        self.assertEqual(continuation["session_id"], first["session_id"])
        self.assertTrue(str(self.cli_path).startswith(str(self.plugin)))
        self.assertEqual(list(self.cwd.iterdir()), [])

        assignment = self.root / "Task Assignment.json"
        assignment.write_text(
            json.dumps(
                dict(
                    id="relocated-task",
                    name="Relocated task",
                    role="executor",
                    project=str(self.project),
                    objective="Return a bounded answer.",
                    context="Supplied facts",
                    scope=["Text only"],
                    acceptance_criteria=["State limitations"],
                    deliverable="A short answer",
                    timeout=10,
                )
            )
        )
        task = self.cli(
            "task",
            "create",
            "--assignment-file",
            str(assignment),
            "--operation-id",
            "relocated-create",
        )
        turn = self.cli(
            "task",
            "submit",
            "--task",
            task["id"],
            "--revision",
            "1",
            "--operation-id",
            "relocated-submit",
        )
        self.known_runs.append(turn["id"])
        self.assertEqual(self.wait_terminal(turn["id"])["status"], "completed")
        self.assertEqual(self.cli("report", "--run", turn["id"])["format_status"], "valid")
        self.assertEqual(self.cli("task", "show", "--task", task["id"])["state"], "awaiting_review")


if __name__ == "__main__":
    unittest.main()
