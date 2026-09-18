"""Explicit opt-in live Claude tests. Never run by unittest discovery."""

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "plugins/claude-control/scripts/claude_control_cli.py"
p = argparse.ArgumentParser()
p.add_argument("--output", type=Path, required=True)
p.add_argument("--claude-bin", required=True)
a = p.parse_args()
os.umask(0o077)
a.output.mkdir(parents=True, exist_ok=True)
state = a.output / "state"
project = a.output / "project"
project.mkdir()
hooks = project / ".claude"
hooks.mkdir()
sentinel = project / "HOOK_EXECUTED"
(hooks / "settings.json").write_text(
    json.dumps(
        {
            "hooks": {
                "SessionStart": [
                    {
                        "hooks": [
                            {"type": "command", "command": "touch " + shlex.quote(str(sentinel))}
                        ]
                    }
                ]
            }
        }
    )
)
report = {"started": time.time(), "checks": {}, "runs": [], "ok": False}
owned = []


def call(*args):
    result = subprocess.run(
        [sys.executable, str(CLI), "--state-dir", str(state), *map(str, args)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode:
        raise RuntimeError(result.stdout or result.stderr)
    return json.loads(result.stdout)


def save():
    (a.output / "report.json").write_text(json.dumps(report, indent=2))


def submit(prompt, name=None, model=None, session=None, acknowledge=False):
    request = str(uuid.uuid4())
    file = a.output / (request + ".prompt.txt")
    file.write_text(prompt)
    args = [
        "followup" if session else "start",
        "--prompt-file",
        file,
        "--request-id",
        request,
        "--timeout",
        "240",
    ]
    args += (
        ["--session", session]
        if session
        else [
            "--name",
            name,
            "--model",
            model,
            "--role",
            "smoke-verifier" if model == "fable" else "smoke-routine",
            "--project",
            project,
        ]
    )
    if acknowledge:
        args.append("--acknowledge-context")
    row = call(*args)
    owned.append(row["id"])
    return row


def wait_many(rows, require_overlap=False):
    deadline = time.monotonic() + 250
    overlap = False
    while True:
        snapshots = [call("status", "--run", row["id"]) for row in rows]
        if len(snapshots) > 1 and all(row["status"] == "running" for row in snapshots):
            overlap = True
            report["overlap_snapshot"] = snapshots
        if all(
            row["status"] not in ("pending", "claimed", "launching", "running", "stopping")
            for row in snapshots
        ):
            break
        if time.monotonic() >= deadline:
            raise RuntimeError("Live smoke deadline exceeded")
        time.sleep(0.15)
    for row in snapshots:
        result = call("result", "--run", row["id"])
        report["runs"].append(result)
        assert row["status"] == "completed", result
        assert result["result"]["tool_use_count"] == 0
    if require_overlap:
        assert overlap, "No actual overlapping running states observed"
    save()
    return [call("result", "--run", row["id"])["result"]["response"].strip() for row in rows]


try:
    report["init"] = call("init", "--claude-bin", a.claude_bin, "--allow-root", project)
    report["doctor"] = call("doctor", "--auth")
    assert report["doctor"]["ready"], report["doctor"]
    sonnet_marker, fable_marker = "SONNET-" + uuid.uuid4().hex, "FABLE-" + uuid.uuid4().hex
    first = submit(
        "Remember exactly "
        + sonnet_marker
        + ". Reply with the marker, then number 40 short words. No tools.",
        name="sonnet-smoke",
        model="sonnet",
    )
    second = submit(
        "Remember exactly "
        + fable_marker
        + ". Give 6 concise invariants for safe local persistent-session management, then repeat the marker. No tools.",
        name="fable-smoke",
        model="fable",
    )
    wait_many([first, second], require_overlap=True)
    report["checks"]["parallel_models"] = True
    resumed = [
        submit("Reply only with the exact marker from the first turn.", session=row["session_id"])
        for row in (first, second)
    ]
    answers = wait_many(resumed)
    assert answers == [sonnet_marker, fable_marker], answers
    report["checks"]["separate_session_recall"] = True
    long = submit(
        "Write all integers from 1 to 50000, one per line, without skipping any. This is a bounded cancellation test.",
        session=first["session_id"],
    )
    peer = submit(
        "Briefly state why stopping one managed session should preserve another independent session. One sentence.",
        session=second["session_id"],
    )
    deadline = time.monotonic() + 45
    while True:
        current = call("status", "--run", long["id"])
        stream = Path(current["artifacts"]) / "events.jsonl"
        if current["status"] == "running" and stream.exists() and stream.stat().st_size:
            break
        if (
            current["status"] in ("completed", "failed", "cancelled", "unknown")
            or time.monotonic() > deadline
        ):
            raise RuntimeError("Cancellation target did not reach an initialized running state")
        time.sleep(0.15)
    report["stop_ack"] = call("stop", "--run", long["id"])
    deadline = time.monotonic() + 15
    while True:
        stopped = call("status", "--run", long["id"])
        if stopped["status"] == "cancelled":
            break
        if time.monotonic() > deadline:
            raise RuntimeError("Cancellation did not complete")
        time.sleep(0.15)
    report["runs"].append(call("result", "--run", long["id"]))
    wait_many([peer])
    report["checks"]["targeted_cancel_peer_completed"] = True
    after = submit(
        "Reply only with the exact marker you were asked to remember in the first turn.",
        session=first["session_id"],
        acknowledge=True,
    )
    assert wait_many([after]) == [sonnet_marker]
    report["checks"]["resume_after_cancel"] = True
    assert not sentinel.exists(), "Project hook ran despite safe configuration"
    report["checks"]["project_hook_disabled"] = True
    report["ok"] = True
except BaseException as exc:
    report["error"] = f"{type(exc).__name__}: {exc}"
    raise
finally:
    for run in owned:
        try:
            row = call("status", "--run", run)
            if row["status"] in ("pending", "claimed", "launching", "running", "stopping"):
                call("stop", "--run", run)
                call("wait", "--run", run, "--seconds", "10")
        except Exception as exc:
            report.setdefault("cleanup_errors", []).append(str(exc))
    report["finished"] = time.time()
    save()
    print(
        json.dumps(
            {
                "ok": report["ok"],
                "checks": report["checks"],
                "report": str(a.output / "report.json"),
            },
            indent=2,
        )
    )
