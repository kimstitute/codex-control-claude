"""Bounded, networkless checks in a disposable Bubblewrap PID/filesystem namespace."""

import hashlib
import json
import os
import selectors
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

try:
    import resource
except ImportError:  # pragma: no cover - native Windows
    resource = None

from . import windows_wsl_sandbox
from .runner import bind_parent
from .store import ControlError, Store, alive, boot_id, pid_namespace, proc_identity

OUTPUT_BYTES = 64 * 1024
ENVIRONMENT = {
    "PATH": "/usr/bin:/bin",
    "LANG": "C.UTF-8",
    "TMPDIR": "/tmp",
    "PYTHONDONTWRITEBYTECODE": "1",
}


def _require_posix():
    if os.name != "posix" or resource is None:
        raise ControlError(
            "sandbox_unavailable",
            "Bubblewrap checks require Linux process and resource primitives.",
        )


def _process_limit():
    _require_posix()
    # Account for visible existing UID threads before namespace bootstrap. Kernel
    # accounting is namespace-hierarchical; this is not an aggregate cgroup quota.
    threads = 0
    for entry in Path("/proc").iterdir():
        if entry.name.isdigit():
            try:
                fields = dict(
                    line.split(":", 1) for line in (entry / "status").read_text().splitlines()
                )
                if int(fields["Uid"].split()[0]) == os.getuid():
                    threads += int(fields["Threads"])
            except (OSError, KeyError, ValueError):
                continue
    _, hard = resource.getrlimit(resource.RLIMIT_NPROC)
    return threads + 32 if hard == resource.RLIM_INFINITY else min(threads + 32, hard)


def argv_for(directory, argv):
    _require_posix()
    binary = shutil.which("bwrap")
    if not binary:
        raise ControlError("sandbox_unavailable", "Bubblewrap is required; no unconfined fallback.")
    result = [
        binary,
        "--unshare-all",
        "--unshare-user",
        "--die-with-parent",
        "--new-session",
        "--cap-drop",
        "ALL",
    ]
    for name in ("/usr", "/lib", "/lib64", "/bin"):
        if Path(name).exists():
            result.extend(["--ro-bind", name, name])
    return result + [
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--tmpfs",
        "/tmp",
        "--bind",
        str(Path(directory).resolve()),
        "/workspace",
        "--chdir",
        "/workspace",
        "--",
        *argv,
    ]


def _limits(timeout, process_limit):
    resource.setrlimit(resource.RLIMIT_CPU, (timeout + 1, timeout + 1))
    resource.setrlimit(resource.RLIMIT_AS, (1024**3, 1024**3))
    resource.setrlimit(resource.RLIMIT_FSIZE, (1024**2, 1024**2))
    resource.setrlimit(resource.RLIMIT_NOFILE, (128, 128))
    resource.setrlimit(resource.RLIMIT_NPROC, (process_limit, process_limit))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def exec_check(state_dir, run_id, seq, scratch):
    """Record this launcher's identity before exec; late launchers cannot escape reconciliation."""
    from .workspace import _checkpoint, directory

    store = Store(state_dir)
    identity = proc_identity(os.getpid())
    with store.db(write=True) as db:
        command = db.execute(
            "SELECT c.*,q.action,a.workspace_id FROM workspace_commands c "
            "JOIN workspace_requests q USING(run_id,seq) JOIN workspace_calls a USING(run_id) "
            "WHERE c.run_id=? AND c.seq=?",
            (run_id, seq),
        ).fetchone()
        if not command:
            raise ControlError("workspace_command_closed", "No reserved check exists.")
        row = db.execute(
            "SELECT * FROM workspaces WHERE id=?", (command["workspace_id"],)
        ).fetchone()
        receipt = db.execute(
            "SELECT 1 FROM workspace_receipts WHERE run_id=? AND seq=?", (run_id, seq)
        ).fetchone()
        if (
            receipt
            or row["state"] != "operating"
            or command["child_pid"] is not None
            or command["namespace"] != pid_namespace()
            or command["boot"] != boot_id()
            or not alive(command["owner_pid"], command["owner_start"], command["boot"])
            or os.getppid() != command["owner_pid"]
        ):
            raise ControlError(
                "workspace_command_closed", "Check owner changed or execution was reconciled."
            )
        scratch = Path(scratch).resolve(strict=True)
        root = directory(store, row["id"])
        if (
            scratch.name != "tree"
            or not scratch.parent.name.startswith("check-")
            or scratch.parent.parent != root
        ):
            raise ControlError(
                "workspace_command_closed", "Check needs its private disposable tree."
            )
        action = json.loads(command["action"])
        if action["op"] != "run_check":
            raise ControlError("workspace_command_closed", "Only named checks may launch.")
        check = json.loads(row["policy"])["checks"][action["name"]]
        argv = argv_for(scratch, check["argv"])
        db.execute(
            "UPDATE workspace_commands SET child_pid=?,child_start=? WHERE run_id=? AND seq=?",
            (os.getpid(), str(identity["start"]), run_id, seq),
        )
    _checkpoint("command_identity_recorded")
    bind_parent(command["owner_pid"])
    _limits(check["timeout"], _process_limit())
    os.execve(argv[0], argv, ENVIRONMENT)


def execute(
    directory, argv, timeout, *, cancelled=lambda: False, started=lambda _: None, managed=None
):
    """The caller supplies a disposable copy, never the editor or source tree."""
    if os.name == "nt":
        return windows_wsl_sandbox.execute(
            directory,
            argv,
            timeout,
            cancelled=cancelled,
            started=started,
            managed=managed,
        )
    parent = os.getpid()
    process_limit = _process_limit()

    def limits():
        bind_parent(parent)
        if managed is None:
            _limits(timeout, process_limit)

    command = (
        [
            sys.executable,
            str(Path(__file__).resolve().parents[1] / "claude_control_cli.py"),
            "--state-dir",
            str(managed["state_dir"]),
            "_workspace_exec",
            "--run",
            managed["run_id"],
            "--seq",
            str(managed["seq"]),
            "--scratch",
            str(directory),
        ]
        if managed is not None
        else argv_for(directory, argv)
    )
    begin = time.monotonic()
    proc = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=ENVIRONMENT,
        cwd="/",
        close_fds=True,
        start_new_session=True,
        preexec_fn=limits,
    )
    outputs = {"stdout": bytearray(), "stderr": bytearray()}
    outcome = None
    try:
        if managed is None:
            started(dict(pid=proc.pid, start=proc_identity(proc.pid)["start"]))
        with selectors.DefaultSelector() as selector:
            for name in outputs:
                pipe = getattr(proc, name)
                os.set_blocking(pipe.fileno(), False)
                selector.register(pipe, selectors.EVENT_READ, name)
            while selector.get_map():
                if cancelled():
                    outcome = "cancelled"
                    break
                if time.monotonic() - begin >= timeout:
                    outcome = "timeout"
                    break
                for key, _ in selector.select(0.1):
                    data = os.read(key.fileobj.fileno(), 8192)
                    if not data:
                        selector.unregister(key.fileobj)
                        continue
                    remaining = OUTPUT_BYTES - sum(map(len, outputs.values()))
                    outputs[key.data].extend(data[:remaining])
                    if len(data) > remaining:
                        outcome = "output_limit"
                        break
                if outcome:
                    break
        if outcome:
            proc.kill()
        try:
            code = proc.wait(timeout=max(0.1, timeout - (time.monotonic() - begin)))
        except subprocess.TimeoutExpired:
            outcome = "timeout"
            proc.kill()
            code = proc.wait(timeout=5)
        outcome = outcome or ("ok" if code == 0 else "failed")
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
        for name in outputs:
            getattr(proc, name).close()
    return {
        "outcome": outcome,
        "exit_code": code,
        "duration": time.monotonic() - begin,
        "truncated": outcome == "output_limit",
        **{key: bytes(value).decode("utf-8", errors="replace") for key, value in outputs.items()},
        **{key + "_sha256": hashlib.sha256(value).hexdigest() for key, value in outputs.items()},
    }


def probe():
    """Exercise production namespace flags without reading login data or making a model call."""
    if os.name == "nt":
        return windows_wsl_sandbox.probe()
    with tempfile.TemporaryDirectory(prefix="claude-workspace-probe-") as directory:
        try:
            result = execute(directory, ["/usr/bin/true"], 5)
        except (ControlError, OSError, subprocess.SubprocessError) as exc:
            return {"ready": False, "reason": str(exc)}
    return {
        "ready": result["outcome"] == "ok",
        "backend": "bubblewrap",
        "reason": None if result["outcome"] == "ok" else result["stderr"][:1024],
        "network": "isolated",
        "processes": "private PID namespace",
        "scope": "disposable checks; Claude remains supplied-text",
    }


def require():
    if os.name == "nt":
        return windows_wsl_sandbox.require()
    result = probe()
    if not result["ready"]:
        raise ControlError("sandbox_unavailable", result["reason"] or "Sandbox probe failed.")
    return result
