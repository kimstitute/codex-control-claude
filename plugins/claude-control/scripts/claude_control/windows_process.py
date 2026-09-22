"""Verified ccc-win-supervisor discovery and protocol-v1 client."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import queue
import subprocess
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass
from pathlib import Path

PROTOCOL_VERSION = 1
MAX_FRAME_BYTES = 1024 * 1024
REQUEST_TIMEOUT = 15.0
HELPER_PATH_ENV = "CLAUDE_CONTROL_WINDOWS_HELPER"
HELPER_SHA256_ENV = "CLAUDE_CONTROL_WINDOWS_HELPER_SHA256"


class HelperError(Exception):
    def __init__(self, code, message, *, uncertain=False):
        super().__init__(message)
        self.code = code
        self.uncertain = uncertain


@dataclass(frozen=True)
class HelperInfo:
    path: Path
    sha256: str
    target: str
    source: str


def _target(machine):
    normalized = machine.lower()
    if normalized in ("amd64", "x86_64"):
        return "x86_64-pc-windows-msvc"
    if normalized in ("arm64", "aarch64"):
        return "aarch64-pc-windows-msvc"
    raise HelperError("helper_arch", f"Unsupported Windows architecture: {machine}")


def _digest(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def _expected_digest(value):
    value = (value or "").strip().lower()
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise HelperError("helper_hash", "Expected helper SHA-256 must be 64 hexadecimal digits.")
    return value


def _valid_creation_filetime(value):
    return (
        isinstance(value, str)
        and len(value) == 16
        and all(character in "0123456789abcdef" for character in value)
    )


def _verified(path, expected, target, source):
    path = Path(path).expanduser().absolute()
    if path.is_symlink() or not path.is_file():
        raise HelperError("helper_missing", f"Verified Windows helper is unavailable: {path}")
    actual = _digest(path)
    if actual != _expected_digest(expected):
        raise HelperError("helper_hash", "Windows helper SHA-256 does not match its pin.")
    return HelperInfo(path=path, sha256=actual, target=target, source=source)


def resolve_helper(environ=None, *, plugin_root=None, machine=None):
    """Resolve only an explicitly or manifest-pinned helper executable."""
    environ = os.environ if environ is None else environ
    machine = platform.machine() if machine is None else machine
    target = _target(machine)
    explicit_path = environ.get(HELPER_PATH_ENV)
    explicit_hash = environ.get(HELPER_SHA256_ENV)
    if explicit_path or explicit_hash:
        if not explicit_path or not explicit_hash:
            raise HelperError(
                "helper_hash",
                f"{HELPER_PATH_ENV} and {HELPER_SHA256_ENV} must be supplied together.",
            )
        return _verified(explicit_path, explicit_hash, target, "environment")

    root = (
        Path(__file__).resolve().parents[2]
        if plugin_root is None
        else Path(plugin_root).expanduser().absolute()
    )
    binary_dir = root / "bin"
    manifest_path = binary_dir / "windows-helper-manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise HelperError("helper_missing", "No pinned Windows helper manifest is installed.") from None
    except (OSError, ValueError) as exc:
        raise HelperError("helper_manifest", f"Cannot read Windows helper manifest: {exc}") from None
    if set(manifest) != {"protocol", "artifacts"} or manifest["protocol"] != PROTOCOL_VERSION:
        raise HelperError("helper_manifest", "Windows helper manifest has an unsupported shape.")
    name = f"ccc-win-supervisor-{target}.exe"
    matches = [item for item in manifest["artifacts"] if item.get("name") == name]
    if len(matches) != 1 or set(matches[0]) != {"name", "sha256", "size"}:
        raise HelperError("helper_manifest", f"Manifest does not pin exactly one {target} helper.")
    info = _verified(binary_dir / name, matches[0]["sha256"], target, "bundled")
    if info.path.stat().st_size != matches[0]["size"]:
        raise HelperError("helper_hash", "Windows helper size does not match its manifest.")
    return info


def helper_capability(environ=None, *, plugin_root=None, machine=None):
    try:
        info = resolve_helper(environ, plugin_root=plugin_root, machine=machine)
        return True, "windows-job-object", None, info
    except HelperError as exc:
        return False, None, str(exc), None


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate key: {key}")
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError(f"invalid JSON constant: {value}")


def _read_frame(stream):
    line = stream.readline(MAX_FRAME_BYTES + 2)
    if not line:
        return None
    if len(line) > MAX_FRAME_BYTES + 1 or (
        len(line) == MAX_FRAME_BYTES + 2 and not line.endswith(b"\n")
    ):
        while line and not line.endswith(b"\n"):
            line = stream.readline(MAX_FRAME_BYTES + 2)
        raise HelperError("helper_protocol", "Windows helper frame exceeds 1 MiB.")
    line = line.rstrip(b"\r\n")
    if len(line) > MAX_FRAME_BYTES:
        raise HelperError("helper_protocol", "Windows helper frame exceeds 1 MiB.")
    try:
        value = json.loads(
            line.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, ValueError) as exc:
        raise HelperError("helper_protocol", f"Invalid Windows helper JSON: {exc}") from None
    if not isinstance(value, dict):
        raise HelperError("helper_protocol", "Windows helper frame must be a JSON object.")
    return value


class HelperClient:
    def __init__(self, process):
        if process.stdin is None or process.stdout is None:
            raise ValueError("Helper process requires stdin/stdout pipes.")
        self.process = process
        self._queue = queue.Queue()
        self._pending = deque()
        self._next_id = 1
        self._closed = False
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def _read_loop(self):
        try:
            while True:
                value = _read_frame(self.process.stdout)
                if value is None:
                    self._queue.put(HelperError("helper_eof", "Windows helper closed stdout."))
                    return
                self._queue.put(value)
        except BaseException as exc:
            self._queue.put(exc)

    def _next(self, timeout):
        if self._pending:
            return self._pending.popleft()
        try:
            value = self._queue.get(timeout=timeout)
        except queue.Empty:
            raise HelperError("helper_timeout", "Windows helper response timed out.") from None
        if isinstance(value, HelperError):
            raise value
        if isinstance(value, BaseException):
            raise HelperError("helper_protocol", str(value)) from value
        return value

    def _request(self, op, *, timeout=REQUEST_TIMEOUT, **payload):
        request_id = self._next_id
        self._next_id += 1
        request = {"id": request_id, "op": op, **payload}
        try:
            data = json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode() + b"\n"
            self.process.stdin.write(data)
            self.process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise HelperError("helper_eof", f"Cannot write to Windows helper: {exc}") from None
        deadline = time.monotonic() + timeout
        deferred = []
        try:
            while True:
                try:
                    value = self._queue.get(timeout=max(0.0, deadline - time.monotonic()))
                except queue.Empty:
                    raise HelperError(
                        "helper_timeout", "Windows helper response timed out."
                    ) from None
                if isinstance(value, HelperError):
                    raise value
                if isinstance(value, BaseException):
                    raise HelperError("helper_protocol", str(value)) from value
                if "id" not in value:
                    deferred.append(value)
                    continue
                if value.get("id") != request_id:
                    raise HelperError(
                        "helper_protocol", "Windows helper reply ID is out of sequence."
                    )
                if value.get("ok") is False:
                    if set(value) != {"id", "ok", "error"} or not isinstance(
                        value.get("error"), str
                    ):
                        raise HelperError(
                            "helper_protocol", "Malformed Windows helper error reply."
                        )
                    raise HelperError("helper_rejected", value["error"])
                if value.get("ok") is not True:
                    raise HelperError(
                        "helper_protocol", "Windows helper reply omits a boolean result."
                    )
                return value
        finally:
            self._pending.extend(deferred)

    def hello(self):
        value = self._request("hello", protocol=PROTOCOL_VERSION)
        if set(value) != {"id", "ok", "event", "protocol", "version"} or (
            value["event"] != "hello"
            or value["protocol"] != PROTOCOL_VERSION
            or not isinstance(value["version"], str)
        ):
            raise HelperError("helper_protocol", "Invalid Windows helper hello reply.")
        return value

    def create(self, **payload):
        try:
            value = self._request("create", **payload)
        except HelperError as exc:
            if exc.code != "helper_rejected":
                exc.uncertain = True
            raise
        if set(value) != {
            "id",
            "ok",
            "event",
            "pid",
            "creation_filetime",
            "broke_away",
        } or (
            value["event"] != "created"
            or type(value["pid"]) is not int
            or value["pid"] <= 0
            or not _valid_creation_filetime(value["creation_filetime"])
            or type(value["broke_away"]) is not bool
        ):
            raise HelperError("helper_protocol", "Invalid Windows helper create reply.", uncertain=True)
        return value

    def resume(self):
        value = self._request("resume")
        if set(value) != {"id", "ok", "event"} or value["event"] != "running":
            raise HelperError("helper_protocol", "Invalid Windows helper resume reply.")
        return value

    def stop(self, grace_ms=3000):
        value = self._request("stop", grace_ms=grace_ms)
        if set(value) != {"id", "ok", "event"} or value["event"] != "stopping":
            raise HelperError("helper_protocol", "Invalid Windows helper stop reply.")
        return value

    def abort(self):
        value = self._request("abort")
        if set(value) != {"id", "ok", "event"} or value["event"] != "aborted":
            raise HelperError("helper_protocol", "Invalid Windows helper abort reply.")
        return value

    def next_event(self, timeout):
        value = self._next(timeout)
        if set(value) != {"event", "exit_code", "reason"} or (
            value["event"] != "exited"
            or type(value["exit_code"]) is not int
            or not 0 <= value["exit_code"] <= 0xFFFFFFFF
            or value["reason"] is not None
        ):
            raise HelperError("helper_protocol", "Invalid asynchronous Windows helper event.")
        return value

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            self.process.stdin.close()
        except OSError:
            pass
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)


def run_windows_child(
    store,
    row,
    project,
    argv,
    directory,
    environment,
    stopping,
    output_limit,
):
    """Run one Claude turn through the native helper and return terminal evidence."""
    if store.config["schema"] < 13:
        from .store import ControlError

        raise ControlError("migration_required", "Windows execution requires schema 13.")
    try:
        helper = resolve_helper()
    except HelperError as exc:
        from .store import ControlError

        raise ControlError(exc.code, str(exc)) from None

    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) | getattr(
        subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200
    )
    helper_log = (directory / "worker.log").open("ab")
    process = None
    client = None
    created = False
    try:
        launched = time.monotonic()
        process = subprocess.Popen(
            [str(helper.path)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=helper_log,
            cwd=project,
            close_fds=True,
            creationflags=creationflags,
            env=environment,
        )
        client = HelperClient(process)
        client.hello()
        nonce = uuid.uuid4().hex
        job_name = f"Local\\ccc-{row['id']}-{nonce[:16]}"
        created_reply = client.create(
            run_id=row["id"],
            job_name=job_name,
            argv=argv,
            cwd=project,
            env=environment,
            stdin_path=str(directory / "prompt.txt"),
            stdout_path=str(directory / "events.jsonl"),
            stderr_path=str(directory / "stderr.txt"),
        )
        created = True
        child_identity = {
            "pid": created_reply["pid"],
            "creation_filetime": created_reply["creation_filetime"],
            "job_name": job_name,
            "nonce": nonce,
            "helper_sha256": helper.sha256,
            "helper_target": helper.target,
            "broke_away": created_reply["broke_away"],
        }
        with store.db(write=True) as db:
            current = db.execute(
                "SELECT status,cancel_requested FROM runs WHERE id=?", (row["id"],)
            ).fetchone()
            if current["status"] != "launching" or current["cancel_requested"] or stopping[0]:
                client.abort()
                return {"exit_code": 1, "reason": "cancel_requested", "uncertain": False}
            changed = db.execute(
                "UPDATE runs SET child_pid=?,child_start=?,child_identity_json=?,heartbeat=? "
                "WHERE id=? AND status='launching' AND child_pid IS NULL",
                (
                    child_identity["pid"],
                    child_identity["creation_filetime"],
                    json.dumps(child_identity, sort_keys=True, separators=(",", ":")),
                    time.time(),
                    row["id"],
                ),
            ).rowcount
            if not changed:
                client.abort()
                return {"exit_code": 1, "reason": "identity_commit_failed", "uncertain": False}
        client.resume()
        with store.db(write=True) as db:
            changed = db.execute(
                "UPDATE runs SET status='running',heartbeat=? WHERE id=? AND status='launching'",
                (time.time(), row["id"]),
            ).rowcount
        if not changed:
            raise HelperError(
                "state_race", "Run state changed after Windows child resume.", uncertain=True
            )

        reason = None
        stop_sent = False
        while True:
            current = store.get_run(row["id"])
            if stopping[0] or current["cancel_requested"]:
                reason = "cancel_requested"
            elif time.monotonic() - launched >= row["timeout"]:
                reason = "timeout"
            elif any(
                (directory / name).stat().st_size > output_limit
                for name in ("events.jsonl", "stderr.txt")
            ):
                reason = "output_limit"
            if reason and not stop_sent:
                with store.db(write=True) as db:
                    db.execute(
                        "UPDATE runs SET status='stopping',reason=? WHERE id=?",
                        (reason, row["id"]),
                    )
                client.stop(3000)
                stop_sent = True
            try:
                event = client.next_event(0.15)
                return {
                    "exit_code": event["exit_code"],
                    "reason": reason,
                    "uncertain": False,
                }
            except HelperError as exc:
                if exc.code != "helper_timeout":
                    raise
            with store.db(write=True) as db:
                db.execute("UPDATE runs SET heartbeat=? WHERE id=?", (time.time(), row["id"]))
    except HelperError as exc:
        # A create request can reach the helper even when its reply is lost.
        # Treat that boundary as uncertain so the controller never relaunches.
        if created or exc.uncertain:
            if client is not None:
                try:
                    client.abort()
                except HelperError:
                    pass
            return {"exit_code": None, "reason": exc.code, "uncertain": True}
        from .store import ControlError

        raise ControlError(exc.code, str(exc)) from None
    finally:
        if client is not None:
            client.close()
        elif process is not None:
            process.kill()
            process.wait(timeout=5)
        helper_log.close()
