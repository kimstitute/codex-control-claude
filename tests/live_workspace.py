#!/usr/bin/env python3
"""Explicit local Sonnet edit/check and independent Fable frozen-snapshot review."""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
FIXED = "def add(a, b):\n    return a + b\n"
ORIGINAL = "def add(a, b):\n    return a - b\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--claude-bin", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--cli", type=Path, default=REPO / "plugins/claude-control/scripts/claude_control_cli.py"
    )
    args = parser.parse_args()
    output = args.output.resolve()
    if output.exists() or output.is_relative_to(REPO):
        parser.error("Use a new private output directory outside this checkout.")
    os.umask(0o077)
    output.mkdir(parents=True, mode=0o700)
    project = output / "project"
    project.mkdir()
    state = output / "state"
    calls = []
    ids = []

    def call(*argv):
        result = subprocess.run(
            [sys.executable, str(args.cli.resolve()), "--state-dir", str(state), *map(str, argv)],
            capture_output=True,
            text=True,
            timeout=75,
        )
        data = json.loads(result.stdout)
        calls.append(dict(command=argv, exit_code=result.returncode, result=data))
        (output / "commands.json").write_text(json.dumps(calls, indent=2, default=str))
        if result.returncode:
            raise RuntimeError(data)
        return data

    def save(name, value):
        path = output / name
        path.write_text(json.dumps(value, indent=2))
        return path

    def drive(workspace_id):
        for _ in range(20):
            value = call(
                "workspace", "run", "--workspace", workspace_id, "--until-idle", "--max-seconds", 30
            )
            if value["state"] != "active":
                break
        if value["state"] != "finished":
            task = value.get("task")
            if task and task.get("active_run_id"):
                call("report", "--run", task["active_run_id"])
            raise RuntimeError(f"Workspace stopped: {value['state']}/{value['reason']}")
        return value

    for argv in (
        ["init", "-q"],
        ["config", "user.name", "Test"],
        ["config", "user.email", "test@example.invalid"],
    ):
        subprocess.run(["/usr/bin/git", "-C", str(project), *argv], check=True, capture_output=True)
    (project / "calc.py").write_text(ORIGINAL)
    (project / "test_calc.py").write_text(
        "import unittest\nfrom calc import add\nclass TestAdd(unittest.TestCase):\n    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n"
    )
    subprocess.run(
        ["/usr/bin/git", "-C", str(project), "add", "."], check=True, capture_output=True
    )
    subprocess.run(
        [
            "/usr/bin/git",
            "-C",
            str(project),
            "-c",
            "core.hooksPath=/dev/null",
            "commit",
            "-qm",
            "Synthetic baseline",
        ],
        check=True,
        capture_output=True,
    )
    try:
        call("init", "--claude-bin", args.claude_bin, "--allow-root", project, "--max-parallel", 2)
        assert call("doctor", "--auth")["ready"]
        assert call("workspace", "doctor")["ready"]
        policy = dict(
            version=1,
            role="executor",
            read_paths=["."],
            write_paths=["calc.py"],
            checks={"unit": {"argv": ["/usr/bin/python3", "-m", "unittest", "-v"], "timeout": 10}},
            max_actions=4,
            max_calls=3,
        )
        created = call(
            "workspace",
            "create",
            "--repo",
            project,
            "--policy-file",
            save("policy.json", policy),
            "--operation-id",
            "editor-create",
        )
        ids.append(created["id"])
        assignment = dict(
            id="workspace-smoke",
            name="Workspace smoke",
            role="executor",
            model="sonnet",
            project=str(project),
            objective="Repair calc.add using the controller operation protocol. On the first turn request write(calc.py, supplied exact fixed content) then run_check(unit). After both receipts succeed, return status complete with operations []. Do not request native tools.",
            context="Exact required calc.py text: "
            + json.dumps(FIXED)
            + ". Intermediate report MUST have status blocked, nonempty limitations and top-level operations. Do not claim operations executed until their controller receipts arrive. On EVERY turn, including after successful receipts, return ONLY one JSON object: first character {, last character }. No introductory explanation, no code fence, no extra keys. Put all explanations inside summary, deliverable or evidence. Use only the exact report_contract fields.",
            scope=["Only calc.py in the independent workspace copy."],
            acceptance_criteria=[
                "calc.py equals the supplied addition implementation and the isolated unit check passes."
            ],
            deliverable="Brief result and operations list required by report_contract.",
            timeout=240,
        )
        binding = call(
            "workspace",
            "task",
            "--workspace",
            created["id"],
            "--assignment-file",
            save("editor.json", assignment),
            "--operation-id",
            "editor-bind",
        )
        assert call("list")["runs"] == []
        edited = drive(created["id"])
        frozen = call("workspace", "export", "--workspace", created["id"])
        assert (Path(frozen["directory"]) / "tree/calc.py").read_text() == FIXED
        assert (project / "calc.py").read_text() == ORIGINAL
        assert any(
            r["result"].get("exit_code") == 0 and r["result"]["outcome"] == "ok"
            for r in edited["receipts"]
        )
        task = call("task", "show", "--task", binding["task_id"])
        assert task["state"] == "awaiting_review"
        assert not any(d["kind"] == "accept" for d in task["decisions"])
        review_policy = dict(
            policy, role="verifier", write_paths=[], checks={}, max_actions=4, max_calls=3
        )
        reviewer = call(
            "workspace",
            "create",
            "--from-snapshot",
            created["id"],
            "--policy-file",
            save("review-policy.json", review_policy),
            "--operation-id",
            "review-create",
        )
        ids.append(reviewer["id"])
        review_assignment = dict(
            assignment,
            id="workspace-review",
            name="Frozen workspace review",
            role="critic",
            model="fable",
            objective="Independently inspect the frozen result. First request read operations for calc.py and test_calc.py using the top-level operations array, status blocked and nonempty limitations. After receiving both files, verify that add implements addition and its test checks 2+3=5; if true return status complete and operations [], with your review in deliverable. Return only the report JSON, without any introductory prose or code fence.",
            context="Use only controller read requests. Return exactly the required task report JSON. This is a read-only independent snapshot; no native tools.",
            acceptance_criteria=[
                "Read the frozen implementation and test, then verify addition correctness."
            ],
            scope=["Read-only frozen snapshot."],
        )
        reviewer_binding = call(
            "workspace",
            "task",
            "--workspace",
            reviewer["id"],
            "--assignment-file",
            save("reviewer.json", review_assignment),
            "--operation-id",
            "review-bind",
        )
        reviewed = drive(reviewer["id"])
        assert edited["task"]["session_id"] != reviewed["task"]["session_id"]
        assert frozen["manifest"]["tree_sha256"] == reviewed["export"]["manifest"]["tree_sha256"]
        final = task["runs"][-1]
        call(
            "task",
            "accept",
            "--task",
            task["id"],
            "--revision",
            task["current_revision"],
            "--run",
            final["id"],
            "--result-sha256",
            final["result_sha256"],
            "--evidence-file",
            save(
                "evidence.json",
                {
                    "1": "Exact frozen calc.py matches expected addition; isolated unit check exited zero; independent Fable read both files."
                },
            ),
            "--operation-id",
            "codex-accept",
        )
        assert call("task", "show", "--task", task["id"])["state"] == "accepted"
        total = len(call("list")["runs"])
        assert call("workspace", "run", "--workspace", created["id"], "--once")["started"] == []
        assert len(call("list")["runs"]) == total
        all_runs = call("list")["runs"]
        assert all(
            call("result", "--run", r["id"])["result"]["tool_use_count"] == 0 for r in all_runs
        )
        assert all(call("report", "--run", r["id"])["format_status"] == "valid" for r in all_runs)
        summary = dict(
            ok=True,
            workspace_id=created["id"],
            reviewer_workspace_id=reviewer["id"],
            editor_task_id=task["id"],
            reviewer_task_id=reviewer_binding["task_id"],
            calls=total,
            actual_models=sorted({m for r in all_runs for m in r["actual_models"]}),
            source_unchanged=(project / "calc.py").read_text() == ORIGINAL,
            isolated_check_passed=True,
            independent_frozen_review=True,
            native_tool_calls=0,
            explicit_acceptance=True,
            no_duplicate_calls=True,
        )
        save("summary.json", summary)
        print(json.dumps(summary, indent=2))
    finally:
        if (state / "config.json").exists():
            for workspace_id in ids:
                # Preserve finished exports and approval; only stop unfinished live work.
                current = call("workspace", "status", "--workspace", workspace_id)
                if current["state"] not in ("finished", "stopped"):
                    call(
                        "workspace",
                        "stop",
                        "--workspace",
                        workspace_id,
                        "--operation-id",
                        "cleanup-" + workspace_id,
                    )


if __name__ == "__main__":
    main()
