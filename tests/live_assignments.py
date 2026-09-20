"""Opt-in two-model smoke check for structured delegation; never auto-discovered."""

import argparse
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "plugins/claude-control/scripts/claude_control_cli.py"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--claude-bin", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    if output.is_relative_to(ROOT):
        parser.error("Use a new private output directory outside the checkout.")
    os.umask(0o077)
    output.mkdir(parents=True, exist_ok=False)
    project = output / "project"
    project.mkdir()
    state = output / "state"
    receipt = {"ok": False, "runs": [], "overlap_observed": False}
    owned = []

    def call(*arguments):
        process = subprocess.run(
            [sys.executable, str(CLI), "--state-dir", str(state), *map(str, arguments)],
            text=True,
            capture_output=True,
            timeout=40,
        )
        if process.returncode:
            raise RuntimeError(process.stdout or process.stderr)
        return json.loads(process.stdout)

    try:
        call("init", "--claude-bin", args.claude_bin, "--allow-root", project)
        expected = {}
        for role, model in (("executor", "sonnet"), ("critic", "fable")):
            marker = "ASSIGNMENT-" + uuid.uuid4().hex
            task = dict(
                id=role + "-smoke",
                name=role + "-smoke",
                role=role,
                project=str(project),
                objective="Assess the supplied identity assertion and return the requested text.",
                context=f"The expected marker and the observed marker are both {marker}.",
                scope=["Only the supplied assertion"],
                acceptance_criteria=["Confirm that the two supplied markers match."],
                deliverable="Return exactly the marker as the report's deliverable string.",
                timeout=240,
            )
            path = output / (role + ".json")
            path.write_text(json.dumps(task))
            row = call("delegate", "--assignment-file", path, "--request-id", role + "-smoke")
            owned.append(row["id"])
            expected[row["id"]] = (role, model, marker)
        watch = [argument for run_id in owned for argument in ("--run", run_id)]
        deadline = time.monotonic() + 250
        while True:
            snapshot = call("observe", *watch, "--seconds", "0")
            if all(row["status"] == "running" for row in snapshot["runs"]):
                receipt["overlap_observed"] = True
            if snapshot["execution_done"] or snapshot["needs_attention"]:
                receipt["observation"] = snapshot
                break
            if time.monotonic() >= deadline:
                raise RuntimeError("Structured delegation smoke deadline exceeded.")
            time.sleep(0.3)
        for run_id in owned:
            role, model, marker = expected[run_id]
            result = call("report", "--run", run_id)
            raw = call("result", "--run", run_id)
            receipt["runs"].append(result)
            assert result["execution_status"] == "completed", result
            assert result["contract_status"] == "supported", result
            assert result["format_status"] == "valid", result
            assert result["agent_status"] == "complete", result
            assert result["acceptance"] == "unreviewed", result
            assert result["requested_model"] == model, result
            assert result["role"] == role, result
            assert result["report"]["deliverable"] == marker, result
            assert raw["result"]["tool_use_count"] == 0, raw
            assert all(name.startswith("claude-" + model + "-") for name in result["actual_models"])
        assert receipt["overlap_observed"], "No overlapping running states observed."
        receipt["ok"] = True
    except Exception as exc:
        receipt["error"] = str(exc)
    finally:
        for run_id in owned:
            try:
                call("stop", "--run", run_id)
                row = call("wait", "--run", run_id, "--seconds", "10")
                if row["status"] not in {
                    "completed",
                    "failed",
                    "cancelled",
                    "launch_failed",
                    "interrupted",
                }:
                    raise RuntimeError(f"Unresolved owned run {run_id}: {row['status']}")
            except Exception as exc:
                receipt["ok"] = False
                receipt.setdefault("cleanup_errors", []).append(str(exc))
        path = output / "report.json"
        path.write_text(json.dumps(receipt, indent=2))
        print(json.dumps({"ok": receipt["ok"], "report": str(path)}))
    return 0 if receipt["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
