"""One detached, single-threaded supervisor per tools-disabled Claude turn."""

import ctypes
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from .execution_settings import binary_identity, validate_effort
from .platform.locks import file_lock
from .protocol import build_argv, parse_stream
from .store import (
    CLAIM_SECONDS,
    ControlError,
    Store,
    alive,
    boot_id,
    group_alive,
    pid_namespace,
    proc_identity,
    write_json,
)

OUTPUT_LIMIT = 16 * 1024 * 1024
EFFORT_IDENTITY_ENV = "CLAUDE_CONTROL_EFFORT_BINARY"


def child_environment():
    names = (
        "HOME",
        "PATH",
        "USER",
        "LOGNAME",
        "SHELL",
        "LANG",
        "LC_ALL",
        "TERM",
        "TMPDIR",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_CACHE_HOME",
        "XDG_STATE_HOME",
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "NO_PROXY",
        "https_proxy",
        "http_proxy",
        "no_proxy",
    )
    return {name: os.environ[name] for name in names if name in os.environ}


def launch_worker(store, run_id):
    script = Path(__file__).resolve().parents[1] / "claude_control_cli.py"
    with (store.run_dir(run_id) / "worker.log").open("ab") as log:
        try:
            subprocess.Popen(
                [
                    sys.executable,
                    str(script),
                    "--state-dir",
                    str(store.path),
                    "_worker",
                    "--run",
                    run_id,
                ],
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                start_new_session=True,
                close_fds=True,
                env=child_environment(),
            )
        except OSError as exc:
            with store.db(write=True) as db:
                db.execute(
                    "UPDATE runs SET status='launch_failed',finished=?,reason=? WHERE id=? AND status='pending'",
                    (time.time(), str(exc), run_id),
                )


def bind_parent(parent):
    """Single-threaded Linux child pre-exec: die if the owning worker disappears."""
    signal.signal(signal.SIGHUP, signal.SIG_DFL)
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, signal.SIGTERM, 0, 0, 0) != 0 or os.getppid() != parent:
        os._exit(125)


def child_exited(proc):
    # WNOWAIT retains the group leader identity until all killpg calls have finished.
    return os.waitid(os.P_PID, proc.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None


def checked_effort(store, row, session, *, probe=True):
    effort = row.get("effort")
    validate_effort(effort)
    if effort != session.get("effort"):
        raise ControlError("session_incompatible", "Run effort does not match its session.")
    if store.config["schema"] >= 4:
        from . import task_contracts

        with store.db() as db:
            version = db.execute(
                "SELECT v.prompt FROM task_runs t JOIN task_revisions v "
                "ON v.task_id=t.task_id AND v.revision=t.revision WHERE t.run_id=?",
                (row["id"],),
            ).fetchone()
        if version and task_contracts.read(version["prompt"])["assignment"].get("effort") != effort:
            raise ControlError("session_incompatible", "Run effort differs from frozen assignment.")
    if probe:
        store.preflight_effort(effort, fresh=True)
    elif effort is not None:
        try:
            expected = json.loads(os.environ.get(EFFORT_IDENTITY_ENV, "null"))
            if expected != list(binary_identity(store.config["claude_bin"])):
                raise ValueError("Verified Claude binary identity changed before exec.")
        except (OSError, ValueError) as exc:
            raise ControlError("effort_unsupported", str(exc)) from None
    return effort


def structured_report_schema(store, row):
    """Load a task run's frozen prompt and derive its CLI output schema."""
    if store.config["schema"] < 4:
        return None
    from . import task_contracts

    with store.db() as db:
        linked = db.execute("SELECT 1 FROM task_runs WHERE run_id=?", (row["id"],)).fetchone()
    if not linked:
        return None
    prompt = (store.run_dir(row["id"]) / "prompt.txt").read_text(encoding="utf-8")
    return task_contracts.report_schema(task_contracts.read(prompt))


def exec_claude(state_dir, run_id):
    """Persist exec identity before model launch; a late wrapper cannot escape quarantine."""
    store = Store(state_dir)
    row = store.get_run(run_id)
    session = store.session(row["session_id"])
    project = store.project(session["project"])
    try:
        effort = checked_effort(store, row, session, probe=False)
    except ControlError as exc:
        with store.db(write=True) as db:
            db.execute(
                "UPDATE runs SET reason=? WHERE id=? AND status='launching' AND child_pid IS NULL",
                (exc.code, run_id),
            )
        raise
    json_schema = structured_report_schema(store, row)
    argv = build_argv(
        store.config["claude_bin"],
        session["model"],
        row["backend_id"],
        bool(row["resume"]),
        effort=effort,
        json_schema=json_schema,
    )
    argv[1:1] = ["--setting-sources", ""]
    identity = proc_identity(os.getpid())
    with store.db(write=True) as db:
        row = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if (
            row["status"] != "launching"
            or row["cancel_requested"]
            or row["worker_namespace"] != pid_namespace()
            or not alive(row["worker_pid"], row["worker_start"], row["boot"])
            or os.getppid() != row["worker_pid"]
        ):
            return
        changed = db.execute(
            "UPDATE runs SET status='running',child_pid=?,child_start=?,heartbeat=? WHERE id=? AND status='launching' AND child_pid IS NULL",
            (os.getpid(), identity["start"], time.time(), run_id),
        ).rowcount
        if not changed:
            return
    os.chdir(project)
    environment = child_environment()
    if json_schema is not None:
        environment["MAX_STRUCTURED_OUTPUT_RETRIES"] = "0"
    os.execve(argv[0], argv, environment)


def kill_group(proc, graceful=True):
    if graceful:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not child_exited(proc):
            time.sleep(0.05)
    # Never call poll()/wait() before the last killpg: a reaped PGID could be reused.
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    deadline = time.monotonic() + 5
    while group_alive(proc.pid):
        if time.monotonic() >= deadline:
            raise ControlError(
                "group_still_alive", "Owned process group has not stopped; quarantine execution."
            )
        time.sleep(0.05)
    return proc.wait()


def run_worker(state_dir, run_id):
    os.umask(0o077)
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    stopping = [False]
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stopping.__setitem__(0, True))
    store = Store(state_dir)
    directory = store.run_dir(run_id)
    proc = None
    lock_context = file_lock(directory / "worker.lock", exclusive=True, blocking=False)
    try:
        lock_context.__enter__()
    except BlockingIOError:
        return
    try:
        identity = proc_identity(os.getpid())
        with store.db(write=True) as db:
            claimed = db.execute(
                "UPDATE runs SET status='claimed',worker_pid=?,worker_start=?,worker_namespace=?,boot=?,heartbeat=? WHERE id=? AND status='pending' AND cancel_requested=0 AND created>=?",
                (
                    os.getpid(),
                    identity["start"],
                    pid_namespace(),
                    boot_id(),
                    time.time(),
                    run_id,
                    time.time() - CLAIM_SECONDS,
                ),
            ).rowcount
        if not claimed:
            return
        try:
            row = store.get_run(run_id)
            session = store.session(row["session_id"])
            project = store.project(session["project"])
            effort = checked_effort(store, row, session)
            json_schema = structured_report_schema(store, row)
            argv = build_argv(
                store.config["claude_bin"],
                session["model"],
                row["backend_id"],
                bool(row["resume"]),
                effort=effort,
                json_schema=json_schema,
            )
            # Safe mode disables hooks/plugins; empty sources further excludes project settings.
            argv[1:1] = ["--setting-sources", ""]
            with store.db(write=True) as db:
                current = db.execute(
                    "SELECT cancel_requested FROM runs WHERE id=?", (run_id,)
                ).fetchone()
                if current[0] or stopping[0]:
                    db.execute(
                        "UPDATE runs SET status='cancelled',finished=?,reason='cancelled_before_launch' WHERE id=?",
                        (time.time(), run_id),
                    )
                    return
                db.execute(
                    "UPDATE runs SET status='launching',started=?,heartbeat=? WHERE id=?",
                    (time.time(), time.time(), run_id),
                )
            write_json(
                directory / "invocation.json",
                dict(
                    argv=argv,
                    cwd=project,
                    requested_model=session["model"],
                    backend_session_id=row["backend_id"],
                    **({"requested_effort": effort} if effort is not None else {}),
                ),
            )
            parent = os.getpid()
            exec_environment = child_environment()
            if effort is not None:
                exec_environment[EFFORT_IDENTITY_ENV] = json.dumps(store._effort_identity)
            script = Path(__file__).resolve().parents[1] / "claude_control_cli.py"
            with (
                (directory / "prompt.txt").open("rb") as source,
                (directory / "events.jsonl").open("wb") as out,
                (directory / "stderr.txt").open("wb") as err,
            ):
                proc = subprocess.Popen(
                    [
                        sys.executable,
                        str(script),
                        "--state-dir",
                        str(store.path),
                        "_exec",
                        "--run",
                        run_id,
                    ],
                    cwd=project,
                    stdin=source,
                    stdout=out,
                    stderr=err,
                    start_new_session=True,
                    close_fds=True,
                    env=exec_environment,
                    preexec_fn=lambda: bind_parent(parent),
                )
                start = time.monotonic()
                reason = None
                while True:
                    current = store.get_run(run_id)
                    if stopping[0] or current["cancel_requested"]:
                        reason = "cancel_requested"
                    elif child_exited(proc):
                        break
                    elif time.monotonic() - start >= row["timeout"]:
                        reason = "timeout"
                    elif any(
                        (directory / name).stat().st_size > OUTPUT_LIMIT
                        for name in ("events.jsonl", "stderr.txt")
                    ):
                        reason = "output_limit"
                    if reason:
                        with store.db(write=True) as db:
                            db.execute(
                                "UPDATE runs SET status='stopping',reason=? WHERE id=?",
                                (reason, run_id),
                            )
                        break
                    with store.db(write=True) as db:
                        db.execute("UPDATE runs SET heartbeat=? WHERE id=?", (time.time(), run_id))
                    time.sleep(0.15)
                exit_code = kill_group(proc, graceful=bool(reason))
                proc = None
            final_state = store.get_run(run_id)
            if reason is None and final_state["child_pid"] is None and final_state["reason"]:
                reason = final_state["reason"]
            if (
                reason is None
                and final_state["cancel_requested"]
                and final_state["child_pid"] is None
            ):
                reason = "cancel_requested"
            try:
                parsed = parse_stream(
                    directory / "events.jsonl",
                    session["model"],
                    row["backend_id"],
                    expect_structured_output=json_schema is not None,
                )
            except (ValueError, OSError) as exc:
                parsed = dict(
                    response="", errors=[str(exc)], actual_models=[], observed_session_ids=[]
                )
            parsed["exit_code"] = exit_code
            parsed["reason"] = reason
            parsed["validated_success"] = bool(
                parsed.get("validated_success") and exit_code == 0 and reason is None
            )
            write_json(directory / "result.json", parsed)
            result_sha256 = hashlib.sha256((directory / "result.json").read_bytes()).hexdigest()
            (directory / "response.txt").write_text(parsed["response"])
            status = "cancelled" if reason == "cancel_requested" else "failed"
            if parsed["validated_success"]:
                status = "completed"
            divergent = any(s != row["backend_id"] for s in parsed["observed_session_ids"])
            finished_at = time.time()
            duration_ms = max(
                0.0,
                (finished_at - (final_state["started"] or finished_at)) * 1000,
            )
            with store.db(write=True) as db:
                db.execute(
                    "UPDATE runs SET status=?,finished=?,heartbeat=?,exit_code=?,reason=?,actual_models=?,result_sha256=? WHERE id=?",
                    (
                        status,
                        finished_at,
                        finished_at,
                        exit_code,
                        reason or ("; ".join(parsed["errors"])[:2000] or None),
                        json.dumps(parsed["actual_models"]),
                        result_sha256,
                        run_id,
                    ),
                )
                if store.config["schema"] >= 11:
                    db.execute(
                        "INSERT INTO run_telemetry VALUES(?,?,?,?,?,?,?)",
                        (
                            run_id,
                            json.dumps(
                                parsed.get("usage", {}),
                                sort_keys=True,
                                separators=(",", ":"),
                            ),
                            json.dumps(
                                parsed.get("model_usage", {}),
                                sort_keys=True,
                                separators=(",", ":"),
                            ),
                            parsed.get("provider_cost_usd"),
                            parsed.get("duration_api_ms"),
                            duration_ms,
                            finished_at,
                        ),
                    )
                if divergent:
                    db.execute("UPDATE sessions SET blocked=1 WHERE id=?", (session["id"],))
        except BaseException as exc:
            cleaned = False
            if proc is not None:
                try:
                    kill_group(proc)
                    cleaned = True
                except (OSError, ChildProcessError, ControlError):
                    pass
            # If Popen raised, it did not return a live owned child; otherwise record uncertainty.
            status = "failed" if proc is None or cleaned else "unknown"
            with store.db(write=True) as db:
                db.execute(
                    "UPDATE runs SET status=?,finished=?,reason=? WHERE id=?",
                    (
                        status,
                        time.time(),
                        exc.code
                        if isinstance(exc, ControlError)
                        and exc.code
                        in ("effort_unsupported", "effort_probe_failed", "session_incompatible")
                        and proc is None
                        else f"worker_error:{type(exc).__name__}:{exc}"[:2000],
                        run_id,
                    ),
                )
            raise
    finally:
        lock_context.__exit__(None, None, None)
