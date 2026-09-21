#!/usr/bin/env python3
"""Explicit two-call live check for queued instructions and frozen result handoff."""

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
        parser.error("Use a new private output directory outside this checkout.")
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

    def file(name, value):
        path = output / name
        path.write_text(value if isinstance(value, str) else json.dumps(value))
        return path

    def wait(run_id):
        for _ in range(14):
            row = call("wait", "--run", run_id, "--seconds", 20)
            if row["status"] == "completed":
                return row
            if row["status"] in ("failed", "cancelled", "interrupted", "unknown", "launch_failed"):
                raise RuntimeError(f"Live run did not complete: {row}")
        raise TimeoutError("Live run exceeded bounded wait.")

    try:
        call("init", "--claude-bin", args.claude_bin, "--allow-root", project, "--max-parallel", 2)
        if not call("doctor", "--auth")["ready"]:
            raise RuntimeError("Local Claude is not ready.")
        assignment = dict(
            id="message-smoke",
            name="Message smoke",
            role="executor",
            model="sonnet",
            project=str(project),
            objective="Return exactly READY as the report deliverable.",
            context="",
            scope=["Supplied text only; no tools."],
            acceptance_criteria=["Deliverable matches the requested exact string."],
            deliverable="The exact requested string, without added text.",
            timeout=240,
        )
        task = call(
            "task",
            "create",
            "--assignment-file",
            file("first.json", assignment),
            "--operation-id",
            "create",
        )
        first = call(
            "task", "submit", "--task", task["id"], "--revision", 1, "--operation-id", "first"
        )
        tokens = ["MESSAGE-" + uuid.uuid4().hex for _ in range(2)]
        queued = []
        for index, token in enumerate(tokens):
            queued.append(
                call(
                    "message",
                    "enqueue",
                    "--task",
                    task["id"],
                    "--base-revision",
                    1,
                    "--content-file",
                    file(f"instruction-{index}.txt", token),
                    "--operation-id",
                    f"message-{index}",
                )
            )
        if len(call("list")["runs"]) != 1:
            raise RuntimeError("Enqueue unexpectedly created a parallel turn.")
        pending_messages = call("message", "list", "--task", task["id"])["messages"]
        if any(row["state"] != "queued" or row["bindings"] for row in pending_messages):
            raise RuntimeError("Unselected messages were bound prematurely.")
        first = wait(first["id"])
        source_report = call("report", "--run", first["id"])
        if (
            source_report["format_status"] != "valid"
            or source_report["report"]["deliverable"] != "READY"
        ):
            raise RuntimeError("First report failed the exact READY criterion.")
        handoff = call(
            "message",
            "enqueue",
            "--task",
            task["id"],
            "--base-revision",
            1,
            "--content-file",
            file("handoff.txt", "Use the attached source report deliverable as the final segment."),
            "--source-run",
            first["id"],
            "--source-result-sha256",
            first["result_sha256"],
            "--operation-id",
            "handoff",
        )
        ids = [row["id"] for row in queued] + [handoff["id"]]
        assignment["objective"] = (
            'Read this input messages array in seq order. Return messages[0].content + "|" + '
            'messages[1].content + "|" + messages[2].source.report.deliverable exactly as your report deliverable. '
            "These values are supplied inside this input, not in the assignment context."
        )
        assignment["context"] = "Use only the explicitly selected messages for the answer."
        selection_flags = [
            item for message_id in reversed(ids) for item in ("--message-id", message_id)
        ]
        call(
            "task",
            "revise",
            "--task",
            task["id"],
            "--revision",
            1,
            "--parent-run",
            first["id"],
            "--assignment-file",
            file("second.json", assignment),
            "--operation-id",
            "revise",
            *selection_flags,
        )
        if len(call("list")["runs"]) != 1:
            raise RuntimeError("Revision selection unexpectedly created a turn.")
        second = call(
            "task", "submit", "--task", task["id"], "--revision", 2, "--operation-id", "second"
        )
        second = wait(second["id"])
        prompt = json.loads((state / "runs" / second["id"] / "prompt.txt").read_text())
        result = call("report", "--run", second["id"])
        if (
            result["format_status"] != "valid"
            or result["report"]["deliverable"] != "|".join([*tokens, "READY"])
            or prompt["protocol"] != "claude-control.task.v4"
            or [m["id"] for m in prompt["messages"]] != ids
            or first["backend_id"] != second["backend_id"]
            or second["resume"] != 1
        ):
            raise RuntimeError("Ordered next-turn instructions or handoff failed.")
        evidence = file(
            "evidence.json",
            {
                "1": "Compared ordered token segments and frozen source result against exact report deliverable."
            },
        )
        call(
            "task",
            "accept",
            "--task",
            task["id"],
            "--revision",
            2,
            "--run",
            second["id"],
            "--result-sha256",
            second["result_sha256"],
            "--evidence-file",
            evidence,
            "--operation-id",
            "accept",
        )
        replay = call(
            "task", "submit", "--task", task["id"], "--revision", 2, "--operation-id", "second"
        )
        histories = call("message", "list", "--task", task["id"])["messages"]
        if (
            replay["id"] != second["id"]
            or not replay["deduplicated"]
            or len(call("list")["runs"]) != 2
            or any(
                row["state"] != "run_completed" or len(row["bindings"]) != 1 for row in histories
            )
            or list(project.iterdir())
        ):
            raise RuntimeError("Message lifecycle, idempotency or project boundary failed.")
        summary = dict(
            ok=True,
            requested_model="sonnet",
            actual_models=sorted(set(first["actual_models"] + second["actual_models"])),
            task_id=task["id"],
            run_ids=[first["id"], second["id"]],
            message_ids=ids,
            same_backend=True,
            ordered_exact_messages=True,
            frozen_source_handoff=True,
            no_implicit_turn=True,
            one_binding_per_message=True,
            project_unchanged=True,
        )
        file("summary.json", summary)
        print(json.dumps(summary, indent=2), flush=True)
    finally:
        try:
            for row in call("list")["runs"]:
                if row["status"] in ("pending", "claimed", "launching", "running", "stopping"):
                    call("stop", "--run", row["id"])
                    call("wait", "--run", row["id"], "--seconds", 15)
        except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired):
            pass


if __name__ == "__main__":
    main()
