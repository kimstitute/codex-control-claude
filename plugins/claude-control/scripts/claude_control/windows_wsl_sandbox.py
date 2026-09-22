"""Windows checks: a deterministic scratch tar streamed to a static WSL2 Bubblewrap helper.

The controller never inspects or executes anything under /mnt/*; the only
material that crosses into WSL is the already-authorized disposable scratch
tree (as a tar) and the exact named check argv. The helper source below is
static and transmitted as a command-line argument to ``python3 -c`` through
``wsl.exe --exec``, launched via the SHA-256-pinned ccc-win-supervisor so its
PID and creation FILETIME can be durably recorded before resume.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import subprocess
import tarfile
import tempfile
import time
import uuid
from pathlib import Path

from . import windows_process, workspace_files
from .store import ControlError
from .workspace_files import MAX_TOTAL_BYTES as _MAX_TREE_BYTES

OUTPUT_BYTES = 64 * 1024
MAX_RESULT_BYTES = 256 * 1024
MAX_TAR_BYTES = 20 * 1024 * 1024

HELPER_SOURCE = """
import hashlib, io, json, os, pwd, selectors, shutil, stat, subprocess, sys, tarfile, tempfile, time


def _fail(reason):
    empty = hashlib.sha256(b"").hexdigest()
    sys.stdout.write(json.dumps({
        "outcome": "rejected",
        "exit_code": None,
        "duration": 0,
        "truncated": False,
        "stdout": "",
        "stderr": reason,
        "stdout_sha256": empty,
        "stderr_sha256": hashlib.sha256(reason.encode()).hexdigest(),
    }, separators=(",", ":")) + "\\n")
    sys.stdout.flush()
    sys.exit(0)


def _limits(timeout):
    import resource
    cpu = max(1, int(timeout) + 1)
    resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
    resource.setrlimit(resource.RLIMIT_AS, (1024 ** 3, 1024 ** 3))
    resource.setrlimit(resource.RLIMIT_FSIZE, (1024 ** 2, 1024 ** 2))
    resource.setrlimit(resource.RLIMIT_NOFILE, (128, 128))
    resource.setrlimit(resource.RLIMIT_NPROC, (256, 256))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def main():
    raw = sys.stdin.buffer
    header = raw.readline(1 << 20)
    if not header:
        _fail("no_manifest")
    try:
        manifest = json.loads(header.decode("utf-8"))
    except Exception:
        _fail("bad_manifest")
    if not isinstance(manifest, dict) or set(manifest) != {
        "argv", "timeout", "tar_sha256", "tar_size", "files", "output_limit",
    }:
        _fail("bad_manifest_shape")
    argv = manifest["argv"]
    timeout = manifest["timeout"]
    tar_sha256 = manifest["tar_sha256"]
    tar_size = manifest["tar_size"]
    files = manifest["files"]
    output_limit = manifest["output_limit"]
    if not (
        isinstance(argv, list)
        and 1 <= len(argv) <= 32
        and all(isinstance(a, str) and a and len(a) <= 1024 and "\\x00" not in a for a in argv)
        and argv[0].startswith("/usr/bin/")
        and "/" not in argv[0][len("/usr/bin/"):]
    ):
        _fail("bad_argv")
    if not (isinstance(timeout, (int, float)) and not isinstance(timeout, bool) and 0 < timeout <= 60):
        _fail("bad_timeout")
    if not (isinstance(tar_size, int) and not isinstance(tar_size, bool) and 0 <= tar_size <= 20 * 1024 * 1024):
        _fail("bad_size")
    if not (isinstance(output_limit, int) and 0 < output_limit <= 16 * 1024 * 1024):
        _fail("bad_output_limit")
    if not isinstance(files, dict) or len(files) > 1024:
        _fail("bad_files")
    data = raw.read(tar_size)
    if len(data) != tar_size:
        _fail("truncated_input")
    if raw.read(1):
        _fail("trailing_input")
    if hashlib.sha256(data).hexdigest() != tar_sha256:
        _fail("digest_mismatch")

    home = os.path.realpath(pwd.getpwuid(os.getuid()).pw_dir)
    if not home or home == "/mnt" or home.startswith("/mnt/"):
        _fail("unsafe_home")
    state_root = os.path.join(home, ".local", "state", "codex-control-claude", "checks")
    os.makedirs(state_root, exist_ok=True, mode=0o700)
    real_state_root = os.path.realpath(state_root)
    if (
        real_state_root == "/mnt"
        or real_state_root.startswith("/mnt/")
        or os.path.commonpath((home, real_state_root)) != home
    ):
        _fail("unsafe_state_root")
    root_info = os.lstat(state_root)
    if not stat.S_ISDIR(root_info.st_mode) or root_info.st_uid != os.getuid():
        _fail("unsafe_state_root")
    os.chmod(state_root, 0o700)
    private_dir = tempfile.mkdtemp(prefix="check-", dir=state_root)
    try:
        os.chmod(private_dir, 0o700)
        tree = os.path.join(private_dir, "tree")
        os.mkdir(tree, 0o700)
        try:
            archive = tarfile.open(fileobj=io.BytesIO(data), mode="r:")
        except tarfile.TarError:
            _fail("bad_tar")
        with archive:
            seen = set()
            extracted = {}
            for member in archive.getmembers():
                if not member.isfile():
                    _fail("unsafe_member")
                name = member.name
                parts = name.split("/")
                if (
                    not name
                    or name.startswith("/")
                    or ".." in parts
                    or "" in parts
                    or "\\\\" in name
                    or any(any(ord(char) < 32 for char in part) for part in parts)
                    or name in seen
                ):
                    _fail("unsafe_path")
                seen.add(name)
                if member.mode not in (0o644, 0o755):
                    _fail("unsafe_mode")
                if member.size < 0 or member.size > 16 * 1024 * 1024:
                    _fail("unsafe_size")
                target = os.path.join(tree, name)
                parent = os.path.dirname(target)
                os.makedirs(parent, exist_ok=True, mode=0o700)
                source = archive.extractfile(member)
                if source is None:
                    _fail("unsafe_member")
                digest = hashlib.sha256()
                written = 0
                with open(target, "xb") as handle:
                    while True:
                        block = source.read(1024 * 1024)
                        if not block:
                            break
                        written += len(block)
                        digest.update(block)
                        handle.write(block)
                os.chmod(target, member.mode)
                extracted[name] = {
                    "sha256": digest.hexdigest(),
                    "size": written,
                    "mode": member.mode,
                }
            if extracted != files:
                _fail("tree_manifest_mismatch")

        bwrap = shutil.which("bwrap")
        if not bwrap:
            _fail("bwrap_missing")
        command = [bwrap, "--unshare-all", "--unshare-user", "--die-with-parent",
                   "--new-session", "--cap-drop", "ALL"]
        for name in ("/usr", "/lib", "/lib64", "/bin"):
            if os.path.isdir(name):
                command += ["--ro-bind", name, name]
        command += ["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp",
                    "--bind", tree, "/workspace", "--chdir", "/workspace", "--", *argv]
        environment = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "TMPDIR": "/tmp",
                        "PYTHONDONTWRITEBYTECODE": "1"}
        begin = time.monotonic()
        proc = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, env=environment, cwd="/",
                                 start_new_session=True, preexec_fn=lambda: _limits(timeout))
        outputs = {"stdout": bytearray(), "stderr": bytearray()}
        outcome = None
        with selectors.DefaultSelector() as selector:
            for name in outputs:
                pipe = getattr(proc, name)
                os.set_blocking(pipe.fileno(), False)
                selector.register(pipe, selectors.EVENT_READ, name)
            while selector.get_map():
                if time.monotonic() - begin >= timeout:
                    outcome = "timeout"
                    break
                for key, _ in selector.select(0.1):
                    block = os.read(key.fileobj.fileno(), 8192)
                    if not block:
                        selector.unregister(key.fileobj)
                        continue
                    remaining = output_limit - sum(map(len, outputs.values()))
                    outputs[key.data].extend(block[:remaining])
                    if len(block) > remaining:
                        outcome = "output_limit"
                        break
                if outcome:
                    break
        if outcome:
            try:
                os.killpg(proc.pid, 9)
            except OSError:
                proc.kill()
        try:
            code = proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, 9)
            code = proc.wait(timeout=5)
            outcome = outcome or "timeout"
        outcome = outcome or ("ok" if code == 0 else "failed")
        out, err = bytes(outputs["stdout"]), bytes(outputs["stderr"])
        truncated = outcome == "output_limit"
        out_text = out.decode("utf-8", errors="replace")
        err_text = err.decode("utf-8", errors="replace")
        result = {
            "outcome": outcome,
            "exit_code": code,
            "duration": time.monotonic() - begin,
            "truncated": truncated,
            "stdout": out_text,
            "stderr": err_text,
            "stdout_sha256": hashlib.sha256(out_text.encode("utf-8")).hexdigest(),
            "stderr_sha256": hashlib.sha256(err_text.encode("utf-8")).hexdigest(),
        }
        sys.stdout.write(json.dumps(result, separators=(",", ":")) + "\\n")
        sys.stdout.flush()
    finally:
        shutil.rmtree(private_dir, ignore_errors=True)


main()
"""


def _pack_tree(directory):
    """Sorted names, uid/gid/mtime normalized, regular files only, 0644/0755, 16 MiB bound."""
    root = Path(directory)
    modes = workspace_files._load_modes(root) if workspace_files._ON_WINDOWS else {}
    relatives = []
    for current, dirs, filenames in os.walk(root):
        dirs.sort()
        for dirname in dirs:
            entry = Path(current) / dirname
            info = entry.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise ControlError(
                    "workspace_integrity", "Scratch tree contains an unsafe directory."
                )
        for filename in filenames:
            path = Path(current) / filename
            relatives.append(path.relative_to(root).as_posix())
    relatives.sort()
    buffer = io.BytesIO()
    total = 0
    manifest = {}
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for relative in relatives:
            path = root / relative
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ControlError(
                    "workspace_integrity", "Scratch tree contains a non-regular file."
                )
            if workspace_files._ON_WINDOWS:
                recorded = modes.get(relative)
                if recorded not in ("100644", "100755"):
                    raise ControlError(
                        "workspace_integrity", "Scratch tree mode metadata is missing."
                    )
                mode = 0o755 if recorded == "100755" else 0o644
            else:
                mode = stat.S_IMODE(info.st_mode)
                if mode not in (0o644, 0o755):
                    raise ControlError(
                        "workspace_integrity", "Scratch tree file mode is unsupported."
                    )
            data = path.read_bytes()
            total += len(data)
            if total > _MAX_TREE_BYTES:
                raise ControlError(
                    "workspace_integrity", "Scratch tree exceeds the 16 MiB bound."
                )
            member = tarfile.TarInfo(relative)
            member.size = len(data)
            member.mode = mode
            member.uid = 0
            member.gid = 0
            member.uname = ""
            member.gname = ""
            member.mtime = 0
            member.type = tarfile.REGTYPE
            archive.addfile(member, io.BytesIO(data))
            manifest[relative] = {
                "sha256": hashlib.sha256(data).hexdigest(),
                "size": len(data),
                "mode": mode,
            }
    if workspace_files._ON_WINDOWS and set(modes) != set(manifest):
        raise ControlError(
            "workspace_integrity", "Scratch tree mode metadata differs from its files."
        )
    packed = buffer.getvalue()
    if len(packed) > MAX_TAR_BYTES:
        raise ControlError("workspace_integrity", "Scratch archive exceeds the transport bound.")
    return packed, manifest


def _pack_tar(directory):
    """Compatibility helper used by deterministic-archive tests."""
    return _pack_tree(directory)[0]


def _locate_wsl(environ):
    override = environ.get("CLAUDE_CONTROL_WSL_EXE")
    path = (
        Path(override)
        if override
        else Path(environ.get("SystemRoot", r"C:\Windows")) / "System32" / "wsl.exe"
    )
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise ControlError("sandbox_unavailable", f"wsl.exe was not found: {path}")
    return str(path)


def _distro_args(environ):
    distro = environ.get("CLAUDE_CONTROL_WSL_DISTRO")
    return ["-d", distro] if distro else []


def _result(outcome, exit_code, duration, reason):
    empty = hashlib.sha256(b"").hexdigest()
    reason = reason or ""
    return {
        "outcome": outcome,
        "exit_code": exit_code,
        "duration": duration,
        "truncated": False,
        "stdout": "",
        "stderr": reason,
        "stdout_sha256": empty,
        "stderr_sha256": hashlib.sha256(reason.encode()).hexdigest(),
    }


_RESULT_KEYS = {
    "outcome", "exit_code", "duration", "truncated",
    "stdout", "stderr", "stdout_sha256", "stderr_sha256",
}


def _windows_environment(environ):
    """Give wsl.exe only the Windows runtime variables it needs, never provider secrets."""
    names = ("SystemRoot", "SYSTEMROOT", "WINDIR", "PATH", "PATHEXT", "TEMP", "TMP")
    return {name: environ[name] for name in names if name in environ}


def _validated_result(raw, exit_code, duration):
    if exit_code != 0:
        return _result("unknown", exit_code, duration, "wsl_transport_failed")
    if not raw or len(raw) > MAX_RESULT_BYTES or raw.count(b"\n") != 1:
        return _result("unknown", exit_code, duration, "invalid_result_frame")
    try:
        result = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return _result("unknown", exit_code, duration, "invalid_result")
    if (
        not isinstance(result, dict)
        or set(result) != _RESULT_KEYS
        or result["outcome"] not in ("ok", "failed", "timeout", "output_limit", "rejected")
        or (result["exit_code"] is not None and type(result["exit_code"]) is not int)
        or not isinstance(result["duration"], (int, float))
        or isinstance(result["duration"], bool)
        or result["duration"] < 0
        or type(result["truncated"]) is not bool
        or not isinstance(result["stdout"], str)
        or not isinstance(result["stderr"], str)
    ):
        return _result("unknown", exit_code, duration, "invalid_result_shape")
    for name in ("stdout", "stderr"):
        digest = result[name + "_sha256"]
        encoded = result[name].encode("utf-8")
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or hashlib.sha256(encoded).hexdigest() != digest
            or len(encoded) > OUTPUT_BYTES
        ):
            return _result("unknown", exit_code, duration, "invalid_result_digest")
    if result["outcome"] == "rejected":
        result["outcome"] = "unknown"
    result["duration"] = duration
    return result


def execute(directory, argv, timeout, *, cancelled=lambda: False, started=lambda _: None, managed=None):
    environ = os.environ
    begin = time.monotonic()
    try:
        helper = windows_process.resolve_helper()
    except windows_process.HelperError as exc:
        raise ControlError("sandbox_unavailable", str(exc)) from None
    wsl_exe = _locate_wsl(environ)
    tar_bytes, tree_manifest = _pack_tree(directory)
    manifest = {
        "argv": list(argv),
        "timeout": timeout,
        "tar_sha256": hashlib.sha256(tar_bytes).hexdigest(),
        "tar_size": len(tar_bytes),
        "files": tree_manifest,
        "output_limit": OUTPUT_BYTES,
    }
    with tempfile.TemporaryDirectory(prefix="ccc-wsl-") as staging_name:
        staging = Path(staging_name)
        stdin_path, stdout_path, stderr_path = (
            staging / "input.bin", staging / "output.jsonl", staging / "stderr.txt"
        )
        stdin_path.write_bytes(
            json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode() + b"\n" + tar_bytes
        )
        stdout_path.write_bytes(b"")
        stderr_path.write_bytes(b"")
        wsl_argv = [
            wsl_exe,
            *_distro_args(environ),
            "--exec",
            "/usr/bin/python3",
            "-c",
            HELPER_SOURCE,
        ]
        process = subprocess.Popen(
            [str(helper.path)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            cwd=str(staging),
            close_fds=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200),
            env=_windows_environment(environ),
        )
        client = windows_process.HelperClient(process)
        created, stop_sent, stop_reason, exit_code = False, False, None, None
        try:
            client.hello()
            run_uuid = str(uuid.uuid4())
            try:
                created_reply = client.create(
                    run_id=run_uuid,
                    job_name=f"Local\\ccc-wsl-{run_uuid[:8]}",
                    argv=wsl_argv,
                    cwd=str(staging),
                    env=_windows_environment(environ),
                    stdin_path=str(stdin_path),
                    stdout_path=str(stdout_path),
                    stderr_path=str(stderr_path),
                )
            except windows_process.HelperError as exc:
                if exc.uncertain:
                    return _result("unknown", None, time.monotonic() - begin, str(exc))
                raise ControlError("sandbox_unavailable", str(exc)) from None
            created = True

            if managed is not None:
                from .store import Store

                store = Store(Path(managed["state_dir"]))
                with store.db(write=True) as db:
                    changed = db.execute(
                        "UPDATE workspace_commands SET child_pid=?,child_start=? "
                        "WHERE run_id=? AND seq=? AND child_pid IS NULL AND NOT EXISTS "
                        "(SELECT 1 FROM workspace_receipts WHERE run_id=workspace_commands.run_id "
                        "AND seq=workspace_commands.seq)",
                        (
                            created_reply["pid"],
                            created_reply["creation_filetime"],
                            managed["run_id"],
                            managed["seq"],
                        ),
                    ).rowcount
                if not changed:
                    client.abort()
                    return _result("unknown", None, time.monotonic() - begin, "command_closed")
            else:
                started(dict(pid=created_reply["pid"], start=created_reply["creation_filetime"]))

            try:
                client.resume()
            except windows_process.HelperError as exc:
                return _result("unknown", None, time.monotonic() - begin, str(exc))

            while True:
                is_cancelled = cancelled()
                if not stop_sent and (is_cancelled or time.monotonic() - begin >= timeout):
                    stop_reason = "cancelled" if is_cancelled else "timeout"
                    try:
                        client.stop(3000)
                    except windows_process.HelperError:
                        pass
                    stop_sent = True
                try:
                    event = client.next_event(0.2)
                    exit_code = event["exit_code"]
                    break
                except windows_process.HelperError as exc:
                    if exc.code == "helper_timeout":
                        continue
                    return _result("unknown", exit_code, time.monotonic() - begin, str(exc))
        except windows_process.HelperError as exc:
            if created or exc.uncertain:
                return _result("unknown", exit_code, time.monotonic() - begin, str(exc))
            raise ControlError("sandbox_unavailable", str(exc)) from None
        finally:
            client.close()

        duration = time.monotonic() - begin
        try:
            raw = stdout_path.read_bytes()
        except OSError:
            raw = b""
        if not raw:
            return _result(
                stop_reason or "unknown", exit_code, duration, "no_result"
            )
        result = _validated_result(raw, exit_code, duration)
        if stop_reason and result["outcome"] == "ok":
            result["outcome"] = stop_reason
        return result


def probe():
    distro = os.environ.get("CLAUDE_CONTROL_WSL_DISTRO")
    with tempfile.TemporaryDirectory(prefix="ccc-wsl-probe-") as directory:
        try:
            result = execute(directory, ["/usr/bin/true"], 5)
        except (ControlError, OSError) as exc:
            return {"ready": False, "backend": None, "reason": str(exc), "distro": distro}
    return {
        "ready": result["outcome"] == "ok",
        "backend": "wsl2-bubblewrap",
        "reason": None if result["outcome"] == "ok" else (result.get("stderr") or result["outcome"])[:500],
        "distro": distro,
    }


def require():
    result = probe()
    if not result["ready"]:
        raise ControlError("sandbox_unavailable", result["reason"] or "WSL sandbox probe failed.")
    return result
