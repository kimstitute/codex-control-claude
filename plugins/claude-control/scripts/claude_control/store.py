"""Private host-local records and transactional execution reservations."""

import hashlib
import json
import math
import os
import socket
import sqlite3
import subprocess
import time
import uuid
from contextlib import closing, contextmanager
from pathlib import Path

from .execution_settings import binary_identity, probe_effort, validate_effort
from .model_settings import CONTRACT as MODEL_SETTINGS_CONTRACT
from .model_settings import built_in_defaults, normalize_defaults, validate_model
from .platform import host as _host
from .platform.locks import file_lock
from .schema import (
    VERSION,
    add_application_schema,
    add_composition_schema,
    add_execution_schema,
    add_message_schema,
    add_observation_schema,
    add_platform_schema,
    add_queue_schema,
    add_task_schema,
    add_telemetry_schema,
    add_workflow_schema,
    add_workspace_schema,
)

ACTIVE = ("pending", "claimed", "launching", "running", "stopping", "unknown")
TERMINAL = ("completed", "failed", "cancelled", "launch_failed", "interrupted")
CLAIM_SECONDS = 30


class ControlError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _translate(function):
    def wrapper(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except _host.HostError as exc:
            raise ControlError(exc.code, str(exc)) from exc

    return wrapper


host_identity = _translate(_host.host_identity)
boot_id = _translate(_host.boot_id)
pid_namespace = _translate(_host.pid_namespace)
proc_identity = _translate(_host.proc_identity)
alive = _translate(_host.alive)
group_alive = _translate(_host.group_alive)
private_dir = _translate(_host.private_dir)
write_json = _translate(_host.write_json)
principal_identity = _translate(_host.principal_identity)
verify_private_entry = _translate(_host.verify_private_entry)
secure_new_file = _translate(_host.secure_new_file)
reject_reparse = _translate(_host.reject_reparse)
flush_directory = _translate(_host.flush_directory)


def execution_alive(row):
    """Check an owned execution without treating a reused Windows PID as the child."""
    if not row["child_pid"] or row["boot"] != boot_id():
        return False
    keys = row.keys() if hasattr(row, "keys") else ()
    if "process_backend" in keys and row["process_backend"] == "windows-job-object":
        return alive(row["child_pid"], row["child_start"], row["boot"])
    return group_alive(row["child_pid"])


def check_host_and_principal(config):
    if config.get("host_id") != host_identity():
        raise ControlError(
            "host_mismatch", "State belongs to another host or user; no dispatch attempted."
        )
    if "principal_id" in config or "platform" in config:
        if (
            config.get("platform") != _host.HOST.name
            or config.get("principal_id") != principal_identity()
        ):
            raise ControlError(
                "host_mismatch", "State belongs to another host or user; no dispatch attempted."
            )
    elif os.name == "posix":
        if config.get("uid") != os.getuid():
            raise ControlError(
                "host_mismatch", "State belongs to another host or user; no dispatch attempted."
            )
    else:
        raise ControlError(
            "host_mismatch", "Windows state requires platform and principal identity."
        )


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
    def initialize(path, claude_bin, roots, max_parallel=2, max_queued=100):
        os.umask(0o077)
        identity = host_identity()
        principal = principal_identity()
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
        if type(max_queued) is not int or not 1 <= max_queued <= 10000:
            raise ControlError("invalid_limit", "max-queued must be between 1 and 10000.")
        with file_lock(directory / "init.lock", exclusive=True):
            if (directory / "config.json").exists():
                raise ControlError(
                    "already_initialized", "Existing configuration is preserved; use doctor."
                )
            config = dict(
                schema=VERSION,
                installation_id=str(uuid.uuid4()),
                host_id=identity,
                hostname=socket.gethostname(),
                platform=_host.HOST.name,
                principal_id=principal,
                claude_bin=str(binary),
                allowed_roots=allowed,
                max_parallel=max_parallel,
                max_queued=max_queued,
            )
            if os.name == "posix":
                config["uid"] = os.getuid()
            with closing(sqlite3.connect(directory / "state.sqlite3")) as db, db:
                db.executescript(SCHEMA)
                add_task_schema(db)
                add_queue_schema(db)
                add_message_schema(db)
                add_workflow_schema(db)
                add_workspace_schema(db)
                add_execution_schema(db)
                add_composition_schema(db)
                add_telemetry_schema(db)
                add_application_schema(db)
                add_platform_schema(db)
                add_observation_schema(db)
                db.execute("PRAGMA journal_mode=WAL")
            secure_new_file(directory / "state.sqlite3")
            private_dir(directory / "runs")
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
        if self.config.get("schema") not in tuple(range(3, VERSION + 1)):
            raise ControlError("schema_mismatch", "Unsupported state schema.")
        check_host_and_principal(self.config)
        for name in ("config.json", "state.sqlite3", "runs"):
            verify_private_entry(self.path / name)
        with self.db():
            pass

    def _read_role_defaults(self):
        settings = self.path / "role-models.json"
        if not settings.exists():
            return built_in_defaults(), "built_in"
        verify_private_entry(settings)
        try:
            document = json.loads(settings.read_text(encoding="utf-8"))
            return normalize_defaults(document)["roles"], "configured"
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise ControlError("invalid_model_settings", str(exc)) from None

    def role_defaults(self):
        """Read effective role defaults; absence preserves the historical routing."""
        with file_lock(self.path / "role-models.lock", exclusive=False):
            roles, _ = self._read_role_defaults()
            return roles

    def role_defaults_report(self):
        with file_lock(self.path / "role-models.lock", exclusive=False):
            roles, source = self._read_role_defaults()
            return {"contract": MODEL_SETTINGS_CONTRACT, "roles": roles, "source": source}

    def configure_role_defaults(self, document):
        try:
            normalized = normalize_defaults(document)
        except ValueError as exc:
            raise ControlError("invalid_model_settings", str(exc)) from None
        with file_lock(self.path / "role-models.lock", exclusive=True):
            write_json(self.path / "role-models.json", normalized)
        return {**normalized, "source": "configured"}

    def reset_role_defaults(self):
        with file_lock(self.path / "role-models.lock", exclusive=True):
            settings = self.path / "role-models.json"
            if settings.exists():
                verify_private_entry(settings)
                settings.unlink()
                flush_directory(self.path)
        return {
            "contract": MODEL_SETTINGS_CONTRACT,
            "roles": built_in_defaults(),
            "source": "built_in",
        }

    @contextmanager
    def db(self, write=False):
        # New clients take a shared lifecycle lock; migration excludes all DB operations.
        # Schema-3 clients predating this lock must be stopped by the offline operator.
        with file_lock(self.path / "lifecycle.lock", exclusive=False):
            config = json.loads((self.path / "config.json").read_text())
            if config != self.config or config.get("schema") not in tuple(range(3, VERSION + 1)):
                raise ControlError("schema_mismatch", "State changed; reopen or finish migrate.")
            db = sqlite3.connect(self.path / "state.sqlite3", timeout=10, isolation_level=None)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            try:
                version = db.execute("PRAGMA user_version").fetchone()[0]
                if version != (0 if config["schema"] == 3 else config["schema"]):
                    raise ControlError(
                        "schema_mismatch", "DB/config disagree; inspect migrate --status."
                    )
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
        if (
            self.config["schema"] >= 8
            and resolved.parent.parent == self.path / "workspaces"
            and resolved.name == "control"
        ):
            with self.db() as db:
                if db.execute(
                    "SELECT 1 FROM workspaces WHERE id=?", (resolved.parent.name,)
                ).fetchone():
                    return str(resolved)
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
            telemetry = (
                db.execute("SELECT * FROM run_telemetry WHERE run_id=?", (run_id,)).fetchone()
                if self.config["schema"] >= 11
                else None
            )
        if not row:
            raise ControlError("run_not_found", "Unknown managed run ID.")
        row = dict(row)
        if row["status"] == "completed":
            valid = True
            try:
                valid = self.result_valid(row)
            except ControlError as exc:
                if exc.code != "result_unreadable":
                    raise
                row.update(integrity_status="unreadable", integrity_error=str(exc))
            if not valid:
                with self.db(write=True) as db:
                    db.execute(
                        "UPDATE runs SET status='failed',reason='result_integrity' WHERE id=? AND status='completed'",
                        (run_id,),
                    )
                row["status"], row["reason"] = "failed", "result_integrity"
        row["actual_models"] = json.loads(row["actual_models"])
        row["telemetry"] = (
            {
                **dict(telemetry),
                "usage": json.loads(telemetry["usage"]),
                "model_usage": json.loads(telemetry["model_usage"]),
            }
            if telemetry
            else None
        )
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
        except (FileNotFoundError, ValueError, AttributeError):
            return False
        except OSError as exc:
            raise ControlError(
                "result_unreadable", f"Cannot read the result artifact: {exc}"
            ) from exc

    @staticmethod
    def backend_unstarted(db, session_id, backend_id):
        rows = db.execute(
            "SELECT status,child_pid,actual_models FROM runs WHERE session_id=? AND backend_id=?",
            (session_id, backend_id),
        ).fetchall()
        # The exec wrapper commits child identity before exec. Only stopped histories
        # with no exec record or model evidence prove that no conversation was created.
        return bool(rows) and all(
            row["status"] in ("failed", "cancelled", "launch_failed", "interrupted")
            and row["child_pid"] is None
            and json.loads(row["actual_models"]) == []
            for row in rows
        )

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

    def preflight_effort(self, effort, *, fresh=False):
        try:
            validate_effort(effort)
        except ValueError as exc:
            raise ControlError("invalid_argument", str(exc)) from None
        if effort is None:
            return
        if self.config["schema"] < 9:
            raise ControlError("migration_required", "Explicit effort requires migrate --offline.")
        try:
            if not fresh and getattr(self, "_effort_identity", None) == binary_identity(
                self.config["claude_bin"]
            ):
                return
            identity, supported = probe_effort(self.config["claude_bin"], self.path, fresh=fresh)
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            raise ControlError(
                "effort_probe_failed", f"Cannot verify CLI effort support: {exc}"
            ) from None
        if not supported:
            raise ControlError(
                "effort_unsupported", "Configured Claude CLI does not advertise --effort."
            )
        self._effort_identity = identity

    def reserve(self, **options):
        self.refresh()
        # Keep legacy corruption observation outside a reservation transaction.
        if options.get("session_ref"):
            session = self.session(options["session_ref"])
            if options.get("effort") is None:
                options["effort"] = session.get("effort")
            with self.db() as db:
                previous = db.execute(
                    "SELECT id FROM runs WHERE session_id=? ORDER BY rowid DESC LIMIT 1",
                    (options["session_ref"],),
                ).fetchone()
            if previous:
                self.get_run(previous["id"])
        with self.db() as db:
            recorded = db.execute(
                "SELECT 1 FROM runs WHERE request_id=?", (options.get("request_id"),)
            ).fetchone()
        if not recorded:
            self.preflight_effort(options.get("effort"))
        with self.db(write=True) as db:
            return self.reserve_in(db, **options)

    def reserve_in(
        self,
        db,
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
        resume_unstarted=False,
        effort=None,
    ):
        try:
            validate_effort(effort)
        except ValueError as exc:
            raise ControlError("invalid_argument", str(exc)) from None
        if not prompt.strip() or len(prompt.encode()) > 1024 * 1024:
            raise ControlError("invalid_prompt", "Prompt must be nonempty and at most 1 MiB.")
        if not request_id or len(request_id) > 200:
            raise ControlError(
                "invalid_request", "Provide a stable request ID of 1–200 characters."
            )
        if isinstance(timeout, bool) or not math.isfinite(timeout) or not 1 <= timeout <= 3600:
            raise ControlError("invalid_timeout", "Timeout must be 1–3600 seconds.")
        session = None
        if session_ref:
            found = db.execute("SELECT * FROM sessions WHERE id=?", (session_ref,)).fetchone()
            if not found:
                raise ControlError("session_not_found", "Unknown managed session UUID.")
            session = dict(found)
        if resume_unstarted and (
            not session or not self.backend_unstarted(db, session["id"], session["backend_id"])
        ):
            raise ControlError("context_uncertain", "Cannot prove this backend has never executed.")
        if restart and (not session or not acknowledge_context):
            raise ControlError(
                "context_uncertain",
                "Restart creates a fresh backend conversation; --acknowledge-context is required.",
            )
        if session:
            if effort is not None and effort != session.get("effort"):
                raise ControlError(
                    "session_incompatible", "Effort is pinned to the managed session."
                )
            effort = session.get("effort")
            project = self.project(session["project"])
            model, role = session["model"], session["role"]
        else:
            project = self.project(project)
            try:
                model = validate_model(model)
            except ValueError as exc:
                raise ControlError("invalid_session", str(exc)) from None
            if not name or len(name) > 120 or not role:
                raise ControlError(
                    "invalid_session", "A name, role and explicit valid model are required."
                )
        if effort is not None:
            if self.config["schema"] < 9:
                raise ControlError(
                    "migration_required", "Explicit effort requires migrate --offline."
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
        if resume_unstarted:
            intent["resume_unstarted"] = True
        if effort is not None:
            intent["effort"] = effort
        fingerprint = hashlib.sha256(json.dumps(intent, sort_keys=True).encode()).hexdigest()
        now = time.time()
        prior = db.execute("SELECT * FROM runs WHERE request_id=?", (request_id,)).fetchone()
        if prior:
            if prior["fingerprint"] != fingerprint:
                raise ControlError(
                    "request_conflict", "Request ID already belongs to different input."
                )
            return prior["id"], False
        if effort is not None:
            try:
                checked = getattr(self, "_effort_identity", None) == binary_identity(
                    self.config["claude_bin"]
                )
            except OSError as exc:
                raise ControlError("effort_probe_failed", str(exc)) from None
            if not checked:
                raise ControlError(
                    "effort_unchecked", "Check CLI effort support before reservation."
                )
        if session:
            session = dict(
                db.execute("SELECT * FROM sessions WHERE id=?", (session["id"],)).fetchone()
            )
            latest = dict(
                db.execute(
                    "SELECT * FROM runs WHERE session_id=? ORDER BY rowid DESC LIMIT 1",
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
        command_slots = 0
        if self.config["schema"] >= 8:
            from .workspace import command_slots as workspace_slots

            command_slots = workspace_slots(db)
        if (
            command_slots
            + db.execute(
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
                columns = "id,name,backend_id,model,role,project,created"
                if self.config["schema"] >= 9:
                    columns += ",effort"
                    session["effort"] = effort
                db.execute(
                    f"INSERT INTO sessions({columns}) VALUES({','.join(':' + col for col in columns.split(','))})",
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
        columns = "id,session_id,request_id,fingerprint,status,created,resume,timeout,backend_id"
        values = (
            run_id,
            session["id"],
            request_id,
            fingerprint,
            "pending",
            now,
            int(bool(session_ref) and not restart and not resume_unstarted),
            timeout,
            session["backend_id"],
        )
        if self.config["schema"] >= 9:
            columns += ",effort"
            values += (effort,)
        db.execute(
            f"INSERT INTO runs({columns}) VALUES({','.join('?' for _ in values)})",
            values,
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
        lock_context = file_lock(
            self.run_dir(run_id) / "worker.lock",
            exclusive=True,
            blocking=False,
        )
        try:
            lock_context.__enter__()
        except BlockingIOError:
            raise ControlError("worker_active", "Worker still holds its run lock.") from None
        try:
            if alive(row["worker_pid"], row["worker_start"], row["boot"]):
                raise ControlError("worker_active", "Worker identity is still alive.")
            if row["boot"] == boot_id():
                if execution_alive(row):
                    raise ControlError(
                        "unresolved_execution",
                        "Cannot establish a stopped owned execution; retain quarantine until independently resolved or host reboot.",
                    )
            # The exec wrapper records child identity transactionally BEFORE Claude exec.
            # With no child record, the following CAS also prevents a late wrapper launch.
            with self.db(write=True) as db:
                db.execute(
                    "UPDATE runs SET status='interrupted',finished=?,reason='reconciled_dead_execution' WHERE id=? AND status='unknown'",
                    (time.time(), run_id),
                )
        finally:
            lock_context.__exit__(None, None, None)
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
