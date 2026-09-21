#!/usr/bin/env python3
"""Explicit two-call task/revision smoke test. Uses a real local Claude account."""

import argparse
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--claude-bin", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model", choices=("sonnet", "fable"), default="sonnet")
    parser.add_argument(
        "--cli", type=Path, default=REPO / "plugins/claude-control/scripts/claude_control_cli.py"
    )
    args = parser.parse_args()
    os.umask(0o077)
    output = args.output.resolve()
    if output.is_relative_to(REPO) or output.exists():
        parser.error("Use a new private output directory outside the checkout.")
    output.mkdir(parents=True, mode=0o700)
    project = output / "project"
    project.mkdir()
    state = output / "state"
    owned = []
    transcript = []

    def call(*arguments):
        process = subprocess.run(
            [
                sys.executable,
                str(args.cli.resolve()),
                "--state-dir",
                str(state),
                *map(str, arguments),
            ],
            capture_output=True,
            text=True,
            timeout=70,
        )
        result = json.loads(process.stdout)
        transcript.append(dict(command=arguments, exit_code=process.returncode, result=result))
        (output / "commands.json").write_text(json.dumps(transcript, indent=2, default=str))
        if process.returncode:
            raise RuntimeError(f"{arguments[0]} failed: {result}")
        return result

    def wait(run_id):
        deadline = time.monotonic() + 260
        while time.monotonic() < deadline:
            row = call("wait", "--run", run_id, "--seconds", "20")
            if row["status"] in (
                "completed",
                "failed",
                "cancelled",
                "launch_failed",
                "interrupted",
                "unknown",
            ):
                if row["status"] != "completed":
                    raise RuntimeError(f"Live run did not complete: {row}")
                return row
        raise TimeoutError("Live task exceeded bounded wait")

    def submit(task, revision, operation):
        row = call(
            "task", "submit", "--task", task, "--revision", revision, "--operation-id", operation
        )
        owned.append(row["id"])
        return wait(row["id"])

    def accept(task, revision, row):
        evidence = output / f"evidence-{revision}.json"
        evidence.write_text(
            json.dumps(
                {"1": "Parsed report deliverable equals the exact expected synthetic token."}
            )
        )
        return call(
            "task",
            "accept",
            "--task",
            task,
            "--revision",
            revision,
            "--run",
            row["id"],
            "--result-sha256",
            row["result_sha256"],
            "--evidence-file",
            evidence,
            "--operation-id",
            f"accept-{revision}",
        )

    try:
        call(
            "init", "--claude-bin", args.claude_bin, "--allow-root", project, "--max-parallel", "1"
        )
        ready = call("doctor", "--auth")
        if not ready["ready"]:
            raise RuntimeError("Claude capability/authentication check failed")
        token = "TASK-RECALL-" + uuid.uuid4().hex
        assignment = dict(
            id="task-smoke",
            name="Task smoke",
            role="executor",
            model=args.model,
            project=str(project),
            objective="Remember the supplied token. Return it exactly in deliverable.",
            context=token,
            scope=["Supplied text only, no tools."],
            acceptance_criteria=[
                "deliverable equals the exact synthetic token, with no added text."
            ],
            deliverable="The exact token as the report deliverable string.",
            timeout=240,
        )
        first_file = output / "first.json"
        first_file.write_text(json.dumps(assignment))
        task = call("task", "create", "--assignment-file", first_file, "--operation-id", "create")
        first = submit(task["id"], "1", "submit-1")
        first_report = call("report", "--run", first["id"])
        if (
            first_report["format_status"] != "valid"
            or first_report["report"]["deliverable"] != token
        ):
            raise RuntimeError("First structured report did not satisfy the token criterion")
        accept(task["id"], "1", first)
        assignment.update(
            objective="Recall the token from the preceding turn and return it exactly in deliverable.",
            context="The token is deliberately omitted from this new input.",
        )
        second_file = output / "second.json"
        second_file.write_text(json.dumps(assignment))
        call(
            "task",
            "revise",
            "--task",
            task["id"],
            "--revision",
            "1",
            "--assignment-file",
            second_file,
            "--parent-run",
            first["id"],
            "--operation-id",
            "revise",
        )
        second = submit(task["id"], "2", "submit-2")
        second_prompt = (state / "runs" / second["id"] / "prompt.txt").read_text()
        second_report = call("report", "--run", second["id"])
        if (
            token in second_prompt
            or second_report["format_status"] != "valid"
            or second_report["report"]["deliverable"] != token
            or second["backend_id"] != first["backend_id"]
            or second["resume"] != 1
        ):
            raise RuntimeError("Structured follow-up did not preserve exact conversation recall")
        accept(task["id"], "2", second)
        task_state = call("task", "show", "--task", task["id"])
        if task_state["state"] != "accepted" or list(project.iterdir()):
            raise RuntimeError("Approval state or text-only project boundary failed")
        summary = dict(
            ok=True,
            requested_model=args.model,
            actual_models=sorted(set(first["actual_models"] + second["actual_models"])),
            task_id=task["id"],
            session_id=first["session_id"],
            run_ids=owned,
            revisions=2,
            exact_recall=True,
            token_in_followup=False,
            final_state=task_state["state"],
            project_unchanged=True,
        )
        (output / "summary.json").write_text(json.dumps(summary, indent=2))
        print(json.dumps(summary, indent=2), flush=True)
    finally:
        for run_id in owned:
            try:
                row = call("status", "--run", run_id)
                if row["status"] in ("pending", "claimed", "launching", "running", "stopping"):
                    call("stop", "--run", run_id)
                    call("wait", "--run", run_id, "--seconds", "15")
            except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired):
                # Retain the original failure and artifacts; never force an uncertain execution.
                pass


if __name__ == "__main__":
    main()
