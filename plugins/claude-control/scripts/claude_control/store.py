"""Private host-local records and transactional execution reservations."""

import fcntl
import hashlib
import json
import os
import socket
import sqlite3
import stat
import sys
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

ACTIVE = ("pending", "claimed", "launching", "running", "stopping", "unknown")
TERMINAL = ("completed", "failed", "cancelled", "launch_failed", "interrupted")
CLAIM_SECONDS = 30


class ControlError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def host_identity():
    if sys.platform != "linux":
        raise ControlError("unsupported_platform", "This controller supports Linux only.")
    machine = Path("/etc/machine-id").read_text().strip()
    if not machine:
        raise ControlError("host_identity", "A nonempty /etc/machine-id is required.")
    return hashlib.sha256(machine.encode()).hexdigest()


def boot_id():
    return Path("/proc/sys/kernel/random/boot_id").read_text().strip()


def pid_namespace():
    return os.readlink("/proc/self/ns/pid")


def proc_identity(pid):
    """Return Linux start ticks/state/group without sending any signal."""
    if not pid:
        return None
    try:
        tail = Path(f"/proc/{int(pid)}/stat").read_text().rsplit(")", 1)[1].split()
        return {"start": tail[19], "state": tail[0], "group": int(tail[2])}
    except (FileNotFoundError, ProcessLookupError):
        return None


def alive(pid, start, boot):
    if boot != boot_id():
        return False
    info = proc_identity(pid)
    return bool(info and info["start"] == start and info["state"] not in ("Z", "X"))


def group_alive(group):
    for entry in Path("/proc").iterdir():
        if entry.name.isdigit():
            info = proc_identity(entry.name)
            if info and info["group"] == group and info["state"] not in ("Z", "X"):
                return True
    return False


def private_dir(path):
    path = Path(path).absolute()
    if path.is_symlink():
        raise ControlError("unsafe_state", "State directory cannot be a symlink.")
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    info = path.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise ControlError("unsafe_state", f"Require an owned mode-0700 directory: {path}")
    return path.resolve()


def write_json(path, value):
    """Atomic replace inside a private state directory."""
    path = Path(path)
    tmp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    with tmp.open("x") as handle:
        os.chmod(tmp, 0o600)
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
 id TEXT PRIMARY KEY, name TEXT UNIQUE NOT NULL, backend_id TEXT UNIQUE NOT NULL,
 model TEXT NOT NULL, role TEXT NOT NULL, project TEXT NOT NULL,
 created REAL NOT NULL, blocked INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS runs (
 id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
 request_id TEXT UNIQUE NOT NULL, fingerprint TEXT NOT NULL, status TEXT NOT NULL,
 created REAL NOT NULL, started REAL, finished REAL, heartbeat REAL,
 resume INTEGER NOT NULL, timeout REAL NOT NULL,
 cancel_requested INTEGER NOT NULL DEFAULT 0,
 worker_pid INTEGER, worker_start TEXT, worker_namespace TEXT, boot TEXT,
 child_pid INTEGER, child_start TEXT, exit_code INTEGER, reason TEXT,
 actual_models TEXT NOT NULL DEFAULT '[]', result_sha256 TEXT, backend_id TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_turn ON runs(session_id)
 WHERE status IN ('pending','claimed','launching','running','stopping','unknown');
"""


class Store:
    @staticmethod
    def initialize(path, claude_bin, roots, max_parallel=2):
        os.umask(0o077)
        identity = host_identity()
        directory = private_dir(path)
        binary = Path(claude_bin).expanduser().resolve(strict=True)
        if not binary.is_file() or not os.access(binary, os.X_OK):
            raise ControlError("invalid_binary", "Claude executable must be an executable file.")
        allowed = sorted({str(Path(p).expanduser().resolve(strict=True)) for p in roots})
        if not allowed or not all(Path(p).is_dir() for p in allowed):
            raise ControlError(
                "invalid_roots", "At least one existing project directory is required."
            )
        if not 1 <= max_parallel <= 32:
            raise ControlError("invalid_limit", "max-parallel must be between 1 and 32.")
        with (directory / "init.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if (directory / "config.json").exists():
                raise ControlError(
                    "already_initialized", "Existing configuration is preserved; use doctor."
                )
            config = dict(
                schema=3,
                installation_id=str(uuid.uuid4()),
                host_id=identity,
                hostname=socket.gethostname(),
                uid=os.getuid(),
                claude_bin=str(binary),
                allowed_roots=allowed,
                max_parallel=max_parallel,
            )
            with sqlite3.connect(directory / "state.sqlite3") as db:
                db.executescript(SCHEMA)
                db.execute("PRAGMA journal_mode=WAL")
            os.chmod(directory / "state.sqlite3", 0o600)
            (directory / "runs").mkdir(mode=0o700, exist_ok=True)
            write_json(directory / "config.json", config)
        return {"state_dir": str(directory), **config}

    def __init__(self, path):
        os.umask(0o077)
        self.path = private_dir(path)
        try:
            self.config = json.loads((self.path / "config.json").read_text())
        except FileNotFoundError:
            raise ControlError(
                "not_initialized", "Run init with this state directory first."
            ) from None
        if self.config.get("schema") != 3:
            raise ControlError("schema_mismatch", "Unsupported state schema.")
        if self.config.get("host_id") != host_identity() or self.config.get("uid") != os.getuid():
            raise ControlError(
                "host_mismatch", "State belongs to another host or user; no dispatch attempted."
            )
        for name in ("config.json", "state.sqlite3", "runs"):
            p = self.path / name
            if p.is_symlink() or p.stat().st_uid != os.getuid() or p.stat().st_mode & 0o077:
                raise ControlError("unsafe_state", f"State entry must be private and owned: {name}")

    @contextmanager
    def db(self, write=False):
        db = sqlite3.connect(self.path / "state.sqlite3", timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db
            if write:
                db.commit()
        except BaseException:
            if write:
                db.rollback()
            raise
        finally:
            db.close()

    def run_dir(self, run_id):
        try:
            if str(uuid.UUID(run_id)) != run_id:
                raise ValueError()
        except ValueError:
            raise ControlError("invalid_run", "Expected a full run UUID.") from None
        return self.path / "runs" / run_id

    def project(self, path):
        resolved = Path(path).expanduser().resolve(strict=True)
        if not resolved.is_dir() or not any(
            resolved.is_relative_to(Path(root)) for root in self.config["allowed_roots"]
        ):
            raise ControlError("project_denied", "Project is outside configured canonical roots.")
        return str(resolved)

    def session(self, ref):
        try:
            if str(uuid.UUID(ref)) != ref:
                raise ValueError()
        except ValueError:
            raise ControlError(
                "invalid_session", "Use the full managed session UUID from list/start."
            ) from None
        with self.db() as db:
            row = db.execute("SELECT * FROM sessions WHERE id=?", (ref,)).fetchone()
        if not row:
            raise ControlError("session_not_found", "Unknown managed session UUID.")
        return dict(row)

    def get_run(self, run_id):
        self.run_dir(run_id)
        with self.db() as db:
            row = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if not row:
            raise ControlError("run_not_found", "Unknown managed run ID.")
        row = dict(row)
        if row["status"] == "completed":
            if not self.result_valid(row):
                with self.db(write=True) as db:
                    db.execute(
                        "UPDATE runs SET status='failed',reason='result_integrity' WHERE id=? AND status='completed'",
                        (run_id,),
                    )
                row["status"], row["reason"] = "failed", "result_integrity"
        row["actual_models"] = json.loads(row["actual_models"])
        row["artifacts"] = str(self.run_dir(run_id))
        row["installation_id"] = self.config["installation_id"]
        return row

    def result_valid(self, row):
        try:
            with (self.run_dir(row["id"]) / "result.json").open("rb") as handle:
                result_bytes = handle.read(32 * 1024 * 1024 + 1)
            return (
                len(result_bytes) <= 32 * 1024 * 1024
                and hashlib.sha256(result_bytes).hexdigest() == row["result_sha256"]
                and json.loads(result_bytes).get("validated_success") is True
            )
        except (OSError, ValueError, AttributeError):
            return False

    def refresh(self):
        """Conservative reconciliation: never signal or relaunch from observed state."""
        now = time.time()
        with self.db(write=True) as db:
            rows = db.execute("SELECT * FROM runs WHERE status IN (?,?,?,?,?,?)", ACTIVE).fetchall()
            for row in rows:
                if row["status"] == "pending" and now - row["created"] > CLAIM_SECONDS:
                    db.execute(
                        "UPDATE runs SET status='launch_failed',finished=?,reason='claim_deadline' WHERE id=? AND status='pending'",
                        (now, row["id"]),
                    )
                elif (
                    row["status"] not in ("pending", "unknown")
                    and (row["boot"] != boot_id() or row["worker_namespace"] == pid_namespace())
                    and not alive(row["worker_pid"], row["worker_start"], row["boot"])
                ):
                    status = "launch_failed" if row["status"] == "claimed" else "unknown"
                    db.execute(
                        "UPDATE runs SET status=?,reason='worker_not_alive' WHERE id=?",
                        (status, row["id"]),
                    )

    def reserve(
        self,
        *,
        prompt,
        request_id,
        timeout,
        name=None,
        model=None,
        role=None,
        project=None,
        session_ref=None,
        acknowledge_context=False,
        restart=False,
    ):
        self.refresh()
        if not prompt.strip() or len(prompt.encode()) > 1024 * 1024:
            raise ControlError("invalid_prompt", "Prompt must be nonempty and at most 1 MiB.")
        if not request_id or len(request_id) > 200:
            raise ControlError(
                "invalid_request", "Provide a stable request ID of 1–200 characters."
            )
        if not 1 <= timeout <= 3600:
            raise ControlError("invalid_timeout", "Timeout must be 1–3600 seconds.")
        session = self.session(session_ref) if session_ref else None
        if restart and (not session or not acknowledge_context):
            raise ControlError(
                "context_uncertain",
                "Restart creates a fresh backend conversation; --acknowledge-context is required.",
            )
        if session:
            project = self.project(session["project"])
            model, role = session["model"], session["role"]
        else:
            project = self.project(project)
            if model not in ("sonnet", "fable") or not name or len(name) > 120 or not role:
                raise ControlError(
                    "invalid_session", "A name, role and explicit sonnet/fable model are required."
                )
        intent = dict(
            prompt=prompt,
            timeout=timeout,
            name=name,
            model=model,
            role=role,
            project=project,
            session_id=session["id"] if session else None,
            acknowledge_context=acknowledge_context,
            restart=restart,
        )
        fingerprint = hashlib.sha256(json.dumps(intent, sort_keys=True).encode()).hexdigest()
        now = time.time()
        with self.db(write=True) as db:
            prior = db.execute("SELECT * FROM runs WHERE request_id=?", (request_id,)).fetchone()
            if prior:
                if prior["fingerprint"] != fingerprint:
                    raise ControlError(
                        "request_conflict", "Request ID already belongs to different input."
                    )
                return prior["id"], False
            if session:
                session = dict(
                    db.execute("SELECT * FROM sessions WHERE id=?", (session["id"],)).fetchone()
                )
                latest = dict(
                    db.execute(
                        "SELECT * FROM runs WHERE session_id=? ORDER BY created DESC LIMIT 1",
                        (session["id"],),
                    ).fetchone()
                )
                if latest["status"] == "completed" and not self.result_valid(latest):
                    db.execute(
                        "UPDATE runs SET status='failed',reason='result_integrity' WHERE id=?",
                        (latest["id"],),
                    )
                    latest["status"] = "failed"
                    if not acknowledge_context:
                        # Persist the observed corruption, even though admission is rejected.
                        db.commit()
                        raise ControlError(
                            "context_uncertain",
                            "Previous result failed integrity validation; inspect before acknowledging context.",
                        )
                blocked = db.execute(
                    "SELECT blocked FROM sessions WHERE id=?", (session["id"],)
                ).fetchone()[0]
                if blocked or latest["status"] in ACTIVE:
                    raise ControlError(
                        "session_busy",
                        "Session is active, divergent or ambiguous; inspect/reconcile it.",
                    )
                if latest["status"] != "completed" and not acknowledge_context:
                    raise ControlError(
                        "context_uncertain",
                        "Previous turn did not complete; inspect then pass --acknowledge-context.",
                    )
            if (
                db.execute(
                    "SELECT count(*) FROM runs WHERE status IN (?,?,?,?,?,?)", ACTIVE
                ).fetchone()[0]
                >= self.config["max_parallel"]
            ):
                raise ControlError(
                    "capacity", "Local concurrency limit reached, including ambiguous runs."
                )
            if not session:
                session = dict(
                    id=str(uuid.uuid4()),
                    name=name,
                    backend_id=str(uuid.uuid4()),
                    model=model,
                    role=role,
                    project=project,
                    created=now,
                )
                try:
                    db.execute(
                        "INSERT INTO sessions(id,name,backend_id,model,role,project,created) VALUES(:id,:name,:backend_id,:model,:role,:project,:created)",
                        session,
                    )
                except sqlite3.IntegrityError:
                    raise ControlError(
                        "name_conflict",
                        "Session name already exists; use its explicit ID to follow up.",
                    ) from None
            if restart:
                session["backend_id"] = str(uuid.uuid4())
                db.execute(
                    "UPDATE sessions SET backend_id=? WHERE id=?",
                    (session["backend_id"], session["id"]),
                )
            run_id = str(uuid.uuid4())
            directory = self.run_dir(run_id)
            directory.mkdir(mode=0o700)
            (directory / "prompt.txt").write_text(prompt)
            db.execute(
                "INSERT INTO runs(id,session_id,request_id,fingerprint,status,created,resume,timeout,backend_id) VALUES(?,?,?,?,'pending',?,?,?,?)",
                (
                    run_id,
                    session["id"],
                    request_id,
                    fingerprint,
                    now,
                    int(bool(session_ref) and not restart),
                    timeout,
                    session["backend_id"],
                ),
            )
        return run_id, True

    def stop(self, run_id):
        self.refresh()
        with self.db(write=True) as db:
            row = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if not row:
                raise ControlError("run_not_found", "Unknown managed run ID.")
            if row["status"] == "pending":
                db.execute(
                    "UPDATE runs SET cancel_requested=1,status='cancelled',finished=?,reason='cancelled_before_claim' WHERE id=?",
                    (time.time(), run_id),
                )
            elif row["status"] in ACTIVE:
                db.execute("UPDATE runs SET cancel_requested=1 WHERE id=?", (run_id,))
        return self.get_run(run_id)

    def reconcile(self, run_id):
        self.refresh()
        row = self.get_run(run_id)
        if row["status"] != "unknown":
            return row
        if row["boot"] == boot_id() and row["worker_namespace"] != pid_namespace():
            raise ControlError(
                "namespace_mismatch", "Reconcile from the same PID namespace as the worker."
            )
        with (self.run_dir(run_id) / "worker.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ControlError("worker_active", "Worker still holds its run lock.") from None
            if alive(row["worker_pid"], row["worker_start"], row["boot"]):
                raise ControlError("worker_active", "Worker identity is still alive.")
            if row["boot"] == boot_id():
                if row["child_pid"] and group_alive(row["child_pid"]):
                    raise ControlError(
                        "unresolved_execution",
                        "Cannot establish stopped process group; retain quarantine until independently resolved or host reboot.",
                    )
            # The exec wrapper records child identity transactionally BEFORE Claude exec.
            # With no child record, the following CAS also prevents a late wrapper launch.
            with self.db(write=True) as db:
                db.execute(
                    "UPDATE runs SET status='interrupted',finished=?,reason='reconciled_dead_execution' WHERE id=? AND status='unknown'",
                    (time.time(), run_id),
                )
        return self.get_run(run_id)

    def list_all(self):
        self.refresh()
        with self.db() as db:
            sessions = [dict(row) for row in db.execute("SELECT * FROM sessions ORDER BY created")]
            ids = [row[0] for row in db.execute("SELECT id FROM runs ORDER BY created")]
        return dict(
            installation_id=self.config["installation_id"],
            hostname=socket.gethostname(),
            sessions=sessions,
            runs=[self.get_run(r) for r in ids],
        )
