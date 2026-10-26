"""Subscription gate and isolated autonomous task contract, without model calls."""

import argparse
import json
import os
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins/claude-control/scripts"))

from claude_control import harness_cli
from claude_control import harness_status
from claude_control.store import ControlError


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class HarnessTests(unittest.TestCase):
    def test_agents_snapshot_tracks_mission_model_tokens_and_elapsed(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state"
            root = state / "harness"
            root.mkdir(parents=True, mode=0o700)
            task_id = str(uuid.uuid4())
            path = root / "tasks" / task_id
            path.mkdir(parents=True)
            harness_cli._write(path / "task.json", {
                "contract": harness_cli.CONTRACT, "id": task_id,
                "status": "running", "model": "sonnet", "mission": "Original mission",
                "turns": [{"number": 1, "started": 100.0, "finished": 120.0,
                           "status": "completed", "mission": "First mission"},
                          {"number": 2, "started": 200.0, "status": "running",
                           "mission": "Fix the parser", "process": {"pid": 123, "start": 5}}],
            })
            (path / "turn-1.jsonl").write_text(json.dumps({
                "type": "result", "usage": {"input_tokens": 2,
                "cache_creation_input_tokens": 3, "cache_read_input_tokens": 5,
                "output_tokens": 7}}) + "\n")
            message = {"type": "assistant", "message": {"id": "msg-1",
                "model": "claude-sonnet-4-5-test", "usage": {"input_tokens": 11,
                "cache_creation_input_tokens": 13, "cache_read_input_tokens": 17,
                "output_tokens": 19}}}
            (path / "turn-2.jsonl").write_text(json.dumps(message) + "\n" +
                                                 json.dumps(message) + "\n{unfinished")
            with patch.object(harness_cli, "_process_alive", return_value=True):
                result = harness_status.snapshot(root, now=250.0)
            self.assertEqual(result["active_count"], 1)
            worker = result["workers"][0]
            self.assertEqual(worker["mission"], "Fix the parser")
            self.assertEqual(worker["actual_models"], ["claude-sonnet-4-5-test"])
            self.assertEqual(worker["tokens"]["total"], 77)
            self.assertEqual(worker["elapsed_seconds"], 50)
            self.assertEqual(worker["token_scope"], "provisional")
            (path / "turn-2.jsonl").write_text(json.dumps({
                "type": "result", "usage": {"input_tokens": 20,
                "cache_creation_input_tokens": 30, "cache_read_input_tokens": 40,
                "output_tokens": 50}}) + "\n")
            record = harness_cli._read(path / "task.json")
            record["status"] = "ready"
            record["turns"][-1].update(status="completed", finished=260.0)
            harness_cli._write(path / "task.json", record)
            self.assertEqual(harness_status.snapshot(root, now=300.0)["workers"], [])
            all_workers = harness_status.snapshot(root, include_all=True, now=300.0)["workers"]
            self.assertEqual(all_workers[0]["tokens"]["total"], 157)
            self.assertEqual(all_workers[0]["elapsed_seconds"], 60)
            self.assertEqual(all_workers[0]["token_scope"], "final")

    def test_auth_rejects_api_and_other_team_org(self):
        team = {"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "firstParty",
                "subscriptionType": "team", "orgId": "lab-org"}
        with patch.object(harness_cli.subprocess, "run") as run:
            run.return_value = argparse.Namespace(returncode=0, stdout=json.dumps(team))
            pinned = harness_cli._auth("claude")
            self.assertEqual(len(pinned), 64)
            self.assertEqual(harness_cli._auth("claude", pinned), pinned)
            with self.assertRaises(ControlError) as changed:
                harness_cli._auth("claude", "0" * 64)
            self.assertEqual(changed.exception.code, "lab_org_changed")
            team["authMethod"] = "api_key"
            run.return_value.stdout = json.dumps(team)
            with self.assertRaises(ControlError) as api:
                harness_cli._auth("claude")
            self.assertEqual(api.exception.code, "lab_subscription_required")

    def test_new_task_uses_independent_clone_and_exact_sha_approval(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "source"
            repo.mkdir()
            git(repo, "init", "-q")
            git(repo, "config", "user.email", "fixture@example.test")
            git(repo, "config", "user.name", "Fixture")
            (repo / "README.md").write_text("base\n")
            git(repo, "add", "README.md")
            git(repo, "commit", "-qm", "base")
            base = git(repo, "rev-parse", "HEAD")
            fake = root / "fake-claude"
            fake.write_text(
                "#!/usr/bin/env python3\n"
                "import json, subprocess, sys\n"
                "from pathlib import Path\n"
                "a=sys.argv[1:]\n"
                "if '--help' in a:\n"
                " print('--restricted --settings --tools --session-id --resume'); sys.exit(0)\n"
                "if 'auth' in a and 'status' in a:\n"
                " print(json.dumps(dict(loggedIn=True,authMethod='claude.ai',apiProvider='firstParty',"
                "subscriptionType='team',orgId='lab'))); sys.exit(0)\n"
                "sid=a[a.index('--session-id')+1] if '--session-id' in a else a[a.index('--resume')+1]\n"
                "Path('README.md').write_text('changed\\n')\n"
                "print(json.dumps(dict(type='assistant',session_id=sid,message=dict("
                "model='claude-sonnet-test',content=[dict(type='tool_use',name='Bash'),"
                "dict(type='text',text='done')]))))\n"
                "print(json.dumps(dict(type='result',subtype='success',session_id=sid,"
                "is_error=False,result='done')))\n"
            )
            fake.chmod(0o700)
            state = root / "state"
            init = argparse.Namespace(state_dir=state, harness_command="init", claude_bin=str(fake),
                                      socat_bin=None, rebind=False)
            self.assertTrue(harness_cli.execute(init)["org_pinned"])
            with self.assertRaises(ControlError) as already:
                harness_cli.execute(init)
            self.assertEqual(already.exception.code, "already_initialized")
            spec = root / "spec.md"
            spec.write_text("Change README.")
            start = argparse.Namespace(state_dir=state, harness_command="start", repo=repo,
                                       spec_file=spec, model="sonnet", base="HEAD",
                                       timeout=30, request_id="same-request")
            with patch.object(harness_cli, "_execution_preflight"):
                result = harness_cli.execute(start)
            self.assertEqual(result["status"], "ready")
            self.assertEqual(result["evidence"]["base_sha"], base)
            self.assertEqual(result["last_turn"]["tool_use_count"], 1)
            task = result["id"]
            work = Path(result["clone"])
            self.assertEqual(git(work, "remote"), "")
            self.assertEqual(git(work, "rev-parse", "--git-common-dir"), ".git")
            self.assertEqual(git(repo, "rev-parse", "HEAD"), base)
            with patch.object(harness_cli, "_execution_preflight"):
                self.assertTrue(harness_cli.execute(start)["deduplicated"])
            spec.write_text("A different task.")
            with patch.object(harness_cli, "_execution_preflight"):
                with self.assertRaises(ControlError) as conflict:
                    harness_cli.execute(start)
            self.assertEqual(conflict.exception.code, "request_conflict")
            approve = argparse.Namespace(state_dir=state, harness_command="approve", task=task,
                                         sha=result["evidence"]["head_sha"], accept_unchecked=False)
            with self.assertRaises(ControlError) as missing:
                harness_cli.execute(approve)
            self.assertEqual(missing.exception.code, "check_required")
            check_file = root / "check.json"
            check_file.write_text(json.dumps(["/usr/bin/true"]))
            check = argparse.Namespace(state_dir=state, harness_command="check", task=task,
                                       argv_json=check_file, timeout=5)
            with patch("claude_control.workspace_sandbox.execute", return_value={"exit_code": 0, "outcome": "ok"}):
                self.assertTrue(harness_cli.execute(check)["check"]["passed"])
            approve.sha = "0" * 40
            with self.assertRaises(ControlError):
                harness_cli.execute(approve)
            approve.sha = result["evidence"]["head_sha"]
            self.assertEqual(harness_cli.execute(approve)["status"], "approved")

    def test_cli_flags_require_restricted_sandbox_and_no_mcp(self):
        args = harness_cli._argv("claude", "sonnet", str(uuid.uuid4()), False)
        self.assertIn("--restricted", args)
        self.assertEqual(args[args.index("--tools") + 1], "Bash,Read,Edit")
        self.assertEqual(args[args.index("--disallowedTools") + 1], "mcp__*")
        settings = json.loads(args[args.index("--settings") + 1])
        self.assertTrue(settings["sandbox"]["failIfUnavailable"])
        self.assertFalse(settings["sandbox"]["allowUnsandboxedCommands"])

    def test_custom_socat_binary_is_pinned(self):
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "socat"
            executable.write_text("#!/bin/sh\necho socat fixture\n")
            executable.chmod(0o700)
            config = {"socat_bin": str(executable),
                      "socat_identity": list(harness_cli.binary_identity(executable))}
            with patch("claude_control.workspace_sandbox.probe", return_value={"ready": True}):
                harness_cli._execution_preflight(config)
                executable.write_text("#!/bin/sh\necho changed\n")
                with self.assertRaises(ControlError) as changed:
                    harness_cli._execution_preflight(config)
            self.assertEqual(changed.exception.code, "socat_binary_changed")

    def test_dead_running_turn_requires_explicit_reconcile(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state"
            task_id = str(uuid.uuid4())
            root = state / "harness"
            task_path = root / "tasks" / task_id
            task_path.mkdir(parents=True, mode=0o700)
            root.chmod(0o700)
            harness_cli._write(task_path / "task.json", {
                "contract": harness_cli.CONTRACT, "id": task_id,
                "status": "running", "model": "sonnet", "base_sha": "0" * 40,
                "work": str(task_path / "work"),
                "turns": [{"number": 1, "status": "running", "process": {"pid": 999999}}],
            })
            args = argparse.Namespace(state_dir=state, harness_command="reconcile", task=task_id)
            with patch.object(harness_cli, "_config", return_value={}):
                result = harness_cli.execute(args)
            self.assertEqual(result["status"], "unknown")
            self.assertEqual(harness_cli._read(task_path / "task.json")["turns"][0]["status"],
                             "unknown")

    def test_clone_rejects_links_to_files_outside_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            work = root / "work"
            work.mkdir()
            (root / "outside.txt").write_text("private")
            (work / "internal.txt").write_text("safe")
            (work / "inside-link").symlink_to("internal.txt")
            (work / "outside-link").symlink_to("../outside.txt")
            self.assertEqual(harness_cli._outside_symlinks(work), ["outside-link"])


if __name__ == "__main__":
    unittest.main()
