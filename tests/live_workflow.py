#!/usr/bin/env python3
"""Explicit two-call Sonnet/Fable workflow check; never run by unittest discovery."""

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
        parser.error("Use a new private output directory outside the checkout.")
    os.umask(0o077)
    output.mkdir(parents=True, mode=0o700)
    project = output / "project"
    project.mkdir()
    state = output / "state"
    calls = []

    def call(*argv):
        process = subprocess.run(
            [sys.executable, str(args.cli.resolve()), "--state-dir", str(state), *map(str, argv)],
            capture_output=True,
            text=True,
            timeout=75,
        )
        data = json.loads(process.stdout)
        calls.append(dict(command=argv, exit_code=process.returncode, result=data))
        (output / "commands.json").write_text(json.dumps(calls, indent=2, default=str))
        if process.returncode:
            raise RuntimeError(data)
        return data

    try:
        call("init", "--claude-bin", args.claude_bin, "--allow-root", project, "--max-parallel", 2)
        if not call("doctor", "--auth")["ready"]:
            raise RuntimeError("Local Claude is not ready.")
        token = "P4-" + uuid.uuid4().hex
        assignment = dict(
            id="workflow-smoke",
            name="Workflow smoke",
            role="executor",
            model="sonnet",
            project=str(project),
            objective="Return exactly "
            + token
            + " as the task report deliverable, with no added text in that field.",
            context="This is a supplied-text exact-string test. The reviewer needs only to compare the source report deliverable to the required string.",
            scope=["Supplied text only; no tools or file changes."],
            acceptance_criteria=["The deliverable field is exactly " + token + "."],
            deliverable="The exact requested string.",
            timeout=240,
        )
        file = output / "assignment.json"
        file.write_text(json.dumps(assignment))
        created = call(
            "workflow",
            "create",
            "--assignment-file",
            file,
            "--operation-id",
            "create",
            "--max-calls",
            2,
            "--max-revisions",
            0,
        )
        if call("list")["runs"]:
            raise RuntimeError("Workflow creation unexpectedly made a model call.")
        for _ in range(12):
            status = call(
                "workflow", "run", "--workflow", created["id"], "--until-idle", "--max-seconds", 30
            )
            if status["state"] != "active":
                break
        if (status["state"], status["reason"]) != ("awaiting_codex", "approve_recommended"):
            raise RuntimeError(
                f"Workflow did not recommend approval: {status['state']}/{status['reason']}"
            )
        if status["budget"]["calls_used"] != 2:
            raise RuntimeError("Unexpected reservation count.")
        worker = call("task", "show", "--task", created["worker_task_id"])
        reviewer = call("task", "show", "--task", created["reviewer_task_id"])
        first, second = worker["runs"][0], reviewer["runs"][0]
        first_report = call("report", "--run", first["id"])
        second_report = call("report", "--run", second["id"])
        if (
            first_report["report"]["deliverable"] != token
            or second_report["format_status"] != "valid"
        ):
            raise RuntimeError("Exact deliverable or review contract failed.")
        target = dict(
            task_id=worker["id"],
            revision=1,
            run_id=first["id"],
            result_sha256=first["result_sha256"],
        )
        review = second_report["report"]["review"]
        if (
            review["target"] != target
            or review["recommendation"] != "approve"
            or review["criteria"]["1"]["verdict"] != "pass"
        ):
            raise RuntimeError("Review provenance or criterion verdict mismatch.")
        if (
            first["session_id"] == second["session_id"]
            or first["backend_id"] == second["backend_id"]
        ):
            raise RuntimeError("Worker and reviewer sessions were not independent.")
        if worker["state"] == "accepted" or any(d["kind"] == "accept" for d in worker["decisions"]):
            raise RuntimeError("Workflow accepted without an explicit Codex decision.")
        for run, alias in [(first, "sonnet"), (second, "fable")]:
            if not run["actual_models"] or any(
                not m.startswith("claude-" + alias + "-") for m in run["actual_models"]
            ):
                raise RuntimeError("Unexpected model substitution.")
        evidence = output / "evidence.json"
        evidence.write_text(
            json.dumps(
                {
                    "1": "Codex compared the source deliverable byte-for-byte against the generated exact string and verified independent review target/digest."
                }
            )
        )
        call(
            "task",
            "accept",
            "--task",
            worker["id"],
            "--revision",
            1,
            "--run",
            first["id"],
            "--result-sha256",
            first["result_sha256"],
            "--evidence-file",
            evidence,
            "--operation-id",
            "accept",
        )
        repeated = call("workflow", "run", "--workflow", created["id"], "--once")
        if repeated["state"] != "accepted" or repeated["started"] or len(call("list")["runs"]) != 2:
            raise RuntimeError("Acceptance/repeated execution invariant failed.")
        if list(project.iterdir()):
            raise RuntimeError("The supplied-text project changed.")
        summary = dict(
            ok=True,
            workflow_id=created["id"],
            worker_task_id=worker["id"],
            reviewer_task_id=reviewer["id"],
            run_ids=[first["id"], second["id"]],
            actual_models=first["actual_models"] + second["actual_models"],
            independent_sessions=True,
            exact_review_target=True,
            no_automatic_acceptance=True,
            explicit_acceptance=True,
            calls_used=2,
            project_unchanged=True,
        )
        (output / "summary.json").write_text(json.dumps(summary, indent=2))
        print(json.dumps(summary, indent=2))
    finally:
        if (state / "config.json").exists():
            for row in call("list")["runs"]:
                if row["status"] in ("pending", "claimed", "launching", "running", "stopping"):
                    call("stop", "--run", row["id"])


if __name__ == "__main__":
    main()
