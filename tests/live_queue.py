#!/usr/bin/env python3
"""Explicit two-call live queue check. Uses a real local Claude account."""

import argparse
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


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
        parser.error("Use a new private directory outside this checkout.")
    os.umask(0o077)
    output.mkdir(parents=True, mode=0o700)
    project = output / "project"
    project.mkdir()
    state = output / "state"
    transcript = []

    def call(*argv):
        process = subprocess.run(
            [sys.executable, str(args.cli.resolve()), "--state-dir", str(state), *map(str, argv)],
            capture_output=True,
            text=True,
            timeout=75,
        )
        result = json.loads(process.stdout)
        transcript.append(dict(command=argv, exit_code=process.returncode, result=result))
        (output / "commands.json").write_text(json.dumps(transcript, indent=2, default=str))
        if process.returncode:
            raise RuntimeError(f"{argv[0]} failed: {result}")
        return result

    def write(name, value):
        path = output / name
        path.write_text(json.dumps(value))
        return path

    def assignment(label, objective, context):
        return dict(
            id=label,
            name=label,
            role="executor",
            model="sonnet",
            project=str(project),
            objective=objective,
            context=context,
            scope=["Supplied text only; no tools."],
            acceptance_criteria=["deliverable equals the exact supplied synthetic token."],
            deliverable="The exact token string; no added text.",
            timeout=240,
        )

    def wait(run_id):
        for _ in range(14):
            row = call("wait", "--run", run_id, "--seconds", 20)
            if row["status"] == "completed":
                return row
            if row["status"] in ("failed", "cancelled", "interrupted", "unknown", "launch_failed"):
                raise RuntimeError(f"Live run failed: {row}")
        raise TimeoutError("Live queue run exceeded bounded wait.")

    token = "QUEUE-EVIDENCE-" + uuid.uuid4().hex
    owned = []
    try:
        call("init", "--claude-bin", args.claude_bin, "--allow-root", project, "--max-parallel", 2)
        if not call("doctor", "--auth")["ready"]:
            raise RuntimeError("Local Claude is not ready.")
        parent = call(
            "task",
            "create",
            "--assignment-file",
            write(
                "parent.json",
                assignment("parent", "Return the context token exactly in deliverable.", token),
            ),
            "--operation-id",
            "parent",
        )
        child = call(
            "task",
            "create",
            "--assignment-file",
            write(
                "child.json",
                assignment(
                    "child",
                    "Read dependencies[0].report.deliverable in this input and return that token exactly as your deliverable.",
                    "No token appears in this base definition.",
                ),
            ),
            "--operation-id",
            "child",
        )
        deps = write("dependencies.json", [dict(task_id=parent["id"], revision=1)])
        call(
            "task",
            "enqueue",
            "--task",
            child["id"],
            "--revision",
            1,
            "--dependencies-file",
            deps,
            "--operation-id",
            "queue-child",
        )
        call(
            "task",
            "enqueue",
            "--task",
            parent["id"],
            "--revision",
            1,
            "--operation-id",
            "queue-parent",
        )
        first = call("dispatch", "--once")
        owned.extend(first["started"])
        if len(owned) != 1:
            raise RuntimeError("Child was admitted before parent acceptance.")
        parent_run = wait(owned[0])
        parent_report = call("report", "--run", parent_run["id"])
        if (
            parent_report["format_status"] != "valid"
            or parent_report["report"]["deliverable"] != token
        ):
            raise RuntimeError("Parent report does not meet the exact-token criterion.")
        blocked = call("dispatch", "--once")
        if blocked["started"] or call("task", "show", "--task", child["id"])["runs"]:
            raise RuntimeError("Completion alone incorrectly opened the approval gate.")
        evidence = write(
            "evidence.json",
            {"1": "Codex test harness compared deliverable with exact synthetic token."},
        )
        approval = call(
            "task",
            "accept",
            "--task",
            parent["id"],
            "--revision",
            1,
            "--run",
            parent_run["id"],
            "--result-sha256",
            parent_run["result_sha256"],
            "--evidence-file",
            evidence,
            "--operation-id",
            "accept-parent",
        )
        second = call("dispatch", "--once")
        owned.extend(second["started"])
        if len(second["started"]) != 1:
            raise RuntimeError("Accepted parent did not release the child.")
        child_run = wait(second["started"][0])
        child_report = call("report", "--run", child_run["id"])
        prompt = json.loads((state / "runs" / child_run["id"] / "prompt.txt").read_text())
        child_state = call("task", "show", "--task", child["id"])
        if (
            child_report["format_status"] != "valid"
            or child_report["report"]["deliverable"] != token
            or prompt["dependencies"][0]["decision_id"] != approval["id"]
            or token in child_state["revisions"][0]["prompt"]
            or child_run["session_id"] == parent_run["session_id"]
        ):
            raise RuntimeError("Frozen dependency evidence failed independent-session transfer.")
        call(
            "task",
            "accept",
            "--task",
            child["id"],
            "--revision",
            1,
            "--run",
            child_run["id"],
            "--result-sha256",
            child_run["result_sha256"],
            "--evidence-file",
            evidence,
            "--operation-id",
            "accept-child",
        )
        if call("dispatch", "--until-idle", "--max-seconds", 1)["started"] or list(
            project.iterdir()
        ):
            raise RuntimeError("Duplicate dispatch or project mutation detected.")
        history = call("task", "events", "--task", child["id"])
        summary = dict(
            ok=True,
            requested_model="sonnet",
            actual_models=sorted(set(parent_run["actual_models"] + child_run["actual_models"])),
            run_ids=owned,
            parent_task_id=parent["id"],
            child_task_id=child["id"],
            approval_gate=True,
            exact_dependency_transfer=True,
            independent_sessions=True,
            event_count=len(history["events"]),
            project_unchanged=True,
        )
        write("summary.json", summary)
        print(json.dumps(summary, indent=2), flush=True)
    finally:
        # Inspect all jobs in this dedicated store, including a lost dispatch response.
        try:
            for row in call("list")["runs"]:
                if row["status"] in ("pending", "claimed", "launching", "running", "stopping"):
                    call("stop", "--run", row["id"])
                    call("wait", "--run", row["id"], "--seconds", 15)
        except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired):
            pass


if __name__ == "__main__":
    main()
