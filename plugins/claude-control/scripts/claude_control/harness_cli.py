"""Opt-in autonomous Claude Code work in an independent local clone.

This path deliberately uses the signed-in Claude Code Team subscription.  It
does not use the Agent SDK or an Anthropic API key.  Existing 0.27 commands
and their tools-disabled contract remain separate.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import time
import uuid
from pathlib import Path

from .execution_settings import binary_identity
from .model_settings import validate_model
from .platform.locks import file_lock
from .protocol import parse_stream
from .runner import child_environment
from .store import ControlError, alive, boot_id, proc_identity

CONTRACT = "claude-control.harness.v1"
MAX_SPEC_BYTES = 64 * 1024
MAX_EVENTS_BYTES = 16 * 1024 * 1024
TOOLS = frozenset({"Bash", "Read", "Edit"})
AUTH_VARIABLES = (
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
    "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY",
    "CLAUDE_CODE_API_KEY", "ANTHROPIC_AWS_API_KEY", "CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST",
)
SETTINGS = {
    "sandbox": {
        "enabled": True,
        "failIfUnavailable": True,
        "allowUnsandboxedCommands": False,
        "filesystem": {"disabled": False},
        "network": {"allowedDomains": []},
    },
    "permissions": {"blockReadsOutsideWorkingDirectories": True},
}


def register(commands):
    p = commands.add_parser("harness", help="Opt-in autonomous local Claude Code work.")
    sub = p.add_subparsers(dest="harness_command", required=True)
    init = sub.add_parser("init", help="Pin the signed-in Team organization and Claude binary.")
    init.add_argument("--claude-bin", default="claude")
    init.add_argument("--socat-bin", help="Explicit local socat executable when it is not on PATH.")
    init.add_argument("--rebind", action="store_true",
                      help="Explicitly replace the pinned runtime after reviewing existing tasks.")
    sub.add_parser("doctor")
    start = sub.add_parser("start")
    start.add_argument("--repo", type=Path, required=True)
    start.add_argument("--spec-file", type=Path, required=True)
    start.add_argument("--model", required=True)
    start.add_argument("--base", default="HEAD")
    start.add_argument("--timeout", type=int, default=1800)
    start.add_argument("--request-id", required=True)
    followup = sub.add_parser("followup")
    followup.add_argument("--task", required=True)
    followup.add_argument("--spec-file", type=Path, required=True)
    followup.add_argument("--timeout", type=int, default=1800)
    followup.add_argument("--acknowledge-unknown", action="store_true")
    followup.add_argument("--restart-session", action="store_true",
                          help="Start a new Claude conversation in the same isolated clone.")
    show = sub.add_parser("show")
    show.add_argument("--task", required=True)
    agents = sub.add_parser("agents", help="Read-only snapshot of managed Claude workers and token usage.")
    agents.add_argument("--all", action="store_true", help="Include completed and attention-needed tasks.")
    reconcile = sub.add_parser("reconcile")
    reconcile.add_argument("--task", required=True)
    check = sub.add_parser("check")
    check.add_argument("--task", required=True)
    check.add_argument("--argv-json", type=Path, required=True,
                       help="JSON array containing one executable and its literal arguments.")
    check.add_argument("--timeout", type=int, default=300)
    approve = sub.add_parser("approve")
    approve.add_argument("--task", required=True)
    approve.add_argument("--sha", required=True)
    approve.add_argument("--accept-unchecked", action="store_true",
                         help="Explicitly approve a commit without an independent check.")


def _root_readonly(args):
    if not sys.platform.startswith("linux"):
        raise ControlError("harness_platform", "Autonomous harness currently requires Linux.")
    root = args.state_dir / "harness"
    if root.is_symlink():
        raise ControlError("unsafe_state_dir", "Harness state directory may not be a symlink.")
    if not root.is_dir():
        return None
    info = root.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise ControlError("unsafe_state_dir", "Harness state directory must be private to its owner.")
    return root


def _root(args):
    if not sys.platform.startswith("linux"):
        raise ControlError("harness_platform", "Autonomous harness currently requires Linux.")
    root = args.state_dir / "harness"
    if root.is_symlink():
        raise ControlError("unsafe_state_dir", "Harness state directory may not be a symlink.")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = root.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise ControlError("unsafe_state_dir", "Harness state directory must be private to its owner.")
    return root


def _write(path: Path, value: dict):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
    try:
        with tmp.open("x", encoding="utf-8") as handle:
            os.chmod(tmp, 0o600)
            json.dump(value, handle, sort_keys=True, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _read(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ControlError("harness_missing", f"Cannot read {path.name}: {exc}") from None


def _settings_arg():
    return json.dumps(SETTINGS, sort_keys=True, separators=(",", ":"))


def _environment():
    # Keep the minimal environment used by the existing runner.  Do not pass
    # an API key or a provider override even if the caller exported one.
    environment = child_environment()
    for name in AUTH_VARIABLES:
        environment.pop(name, None)
    return environment


def _execution_environment(config):
    environment = _environment()
    socat = config.get("socat_bin")
    if socat:
        environment["PATH"] = str(Path(socat).parent) + os.pathsep + environment.get("PATH", "")
    return environment


def _auth(binary, expected_org=None):
    result = subprocess.run(
        [binary, "--restricted", "--settings", _settings_arg(), "auth", "status"],
        capture_output=True, text=True, timeout=15, env=_environment(),
    )
    try:
        data = json.loads(result.stdout)
    except ValueError:
        data = {}
    if (result.returncode != 0 or data.get("loggedIn") is not True
            or data.get("authMethod") != "claude.ai"
            or data.get("apiProvider") != "firstParty"
            or str(data.get("subscriptionType", "")).lower() != "team"
            or not isinstance(data.get("orgId"), str) or not data["orgId"]):
        raise ControlError(
            "lab_subscription_required",
            "Claude Code must use the pinned claude.ai Team subscription; API-key and other provider routes are disabled.",
        )
    digest = hashlib.sha256(data["orgId"].encode()).hexdigest()
    if expected_org is not None and digest != expected_org:
        raise ControlError("lab_org_changed", "Claude Code Team organization differs from the pinned organization.")
    return digest


def _config(root, *, require_auth=True):
    config = _read(root / "config.json")
    if config.get("contract") != CONTRACT:
        raise ControlError("harness_config", "Unsupported harness configuration.")
    binary = config["claude_bin"]
    if list(binary_identity(binary)) != config["binary_identity"]:
        raise ControlError("claude_binary_changed", "Pinned Claude binary changed; run harness init after review.")
    if require_auth:
        _auth(binary, config["org_sha256"])
    return config


def _execution_preflight(config):
    if not sys.platform.startswith("linux"):
        raise ControlError("harness_platform", "Autonomous harness currently requires Linux.")
    missing = [name for name in ("bwrap",) if not shutil.which(name)]
    socat = config.get("socat_bin")
    if not socat or not Path(socat).is_file() or not os.access(socat, os.X_OK):
        missing.append("socat")
    elif list(binary_identity(socat)) != config.get("socat_identity"):
        raise ControlError("socat_binary_changed", "Pinned socat binary changed; run harness init after review.")
    if missing:
        raise ControlError("sandbox_unavailable", "Claude Code sandbox requires: " + ", ".join(missing))
    version = subprocess.run([socat, "-V"], capture_output=True, text=True,
                             timeout=5, env=_environment())
    if version.returncode != 0:
        raise ControlError("sandbox_unavailable", "Pinned socat executable failed its version probe.")
    from .workspace_sandbox import probe

    result = probe()
    if not result.get("ready"):
        raise ControlError("sandbox_unavailable", str(result.get("reason", "Bubblewrap probe failed.")))


def _git(*argv, cwd=None, timeout=60):
    environment = _environment()
    environment.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                       GIT_TERMINAL_PROMPT="0", GIT_LFS_SKIP_SMUDGE="1")
    result = subprocess.run(["git", *argv], cwd=cwd, capture_output=True, text=True,
                            timeout=timeout, env=environment)
    if result.returncode:
        raise ControlError("git_failed", (result.stderr or result.stdout).strip()[:500])
    return result.stdout.strip()


def _task_path(root, task_id):
    try:
        task_id = str(uuid.UUID(task_id))
    except ValueError:
        raise ControlError("invalid_task", "Task id must be a UUID.") from None
    return root / "tasks" / task_id


def _task(root, task_id):
    path = _task_path(root, task_id)
    data = _read(path / "task.json")
    if data.get("id") != task_id or data.get("contract") != CONTRACT:
        raise ControlError("invalid_task", "Task record does not match its id.")
    return path, data


def _spec(path):
    raw = path.read_bytes()
    if not raw or len(raw) > MAX_SPEC_BYTES:
        raise ControlError("invalid_spec", "Spec must be 1–65536 bytes.")
    return raw.decode("utf-8")


def _mission(spec):
    """Store a short, single-line task label without persisting the full prompt."""
    return next((line.strip()[:160] for line in spec.splitlines() if line.strip()), "")


def _argv(binary, model, session_id, resume):
    validate_model(model)
    return [
        binary, "--restricted", "--settings", _settings_arg(),
        "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
        "--disallowedTools", "mcp__*", "--disable-slash-commands",
        "--tools", "Bash,Read,Edit", "--permission-mode", "acceptEdits",
        "--model", model, "--max-turns", "30",
        *( ["--resume", session_id] if resume else ["--session-id", session_id] ),
        "--output-format", "stream-json", "--verbose", "-p",
    ]


def _head(work):
    return _git("rev-parse", "HEAD", cwd=work)


def _evidence(work, base):
    head = _head(work)
    _git("merge-base", "--is-ancestor", base, head, cwd=work)
    status = _git("status", "--porcelain=v1", "--untracked-files=all", cwd=work)
    files = _git("diff", "--name-only", base, head, cwd=work).splitlines()
    remotes = _git("remote", cwd=work).splitlines()
    return {"base_sha": base, "head_sha": head, "clean": not bool(status),
            "changed_files": files, "remotes": remotes,
            "diffstat": _git("diff", "--stat", base, head, cwd=work)[:4000]}


def _outside_symlinks(work):
    root = work.resolve()
    escaped = []
    for directory, dirs, files in os.walk(work, followlinks=False):
        if Path(directory) == work:
            dirs[:] = [name for name in dirs if name != ".git"]
        for name in dirs + files:
            path = Path(directory) / name
            if path.is_symlink() and not path.resolve().is_relative_to(root):
                escaped.append(str(path.relative_to(work)))
    return escaped


def _run(path, data, config, spec, timeout, *, resume):
    if not 1 <= timeout <= 7200:
        raise ControlError("invalid_timeout", "Timeout must be 1–7200 seconds.")
    if list(binary_identity(config["claude_bin"])) != config["binary_identity"]:
        raise ControlError("claude_binary_changed", "Pinned Claude binary changed before execution.")
    _auth(config["claude_bin"], config["org_sha256"])
    work = path / "work"
    session = data["session_id"]
    turn = len(data["turns"]) + 1
    events = path / f"turn-{turn}.jsonl"
    stderr = path / f"turn-{turn}.stderr"
    prompt = (
        "Complete this task autonomously in the current repository. Explore, edit, and run "
        "relevant checks. Do not run git commit or git push; the controller will commit "
        "successful changes after this turn. Finish with a concise report of changes and "
        "checks.\n\nTask specification:\n" + spec
    )
    data["status"] = "running"
    data.pop("check", None)
    data["turns"].append({"number": turn, "started": time.time(), "status": "running",
                          "spec_sha256": hashlib.sha256(spec.encode()).hexdigest(),
                          "mission": _mission(spec)})
    _write(path / "task.json", data)
    argv = _argv(config["claude_bin"], data["model"], session, resume)
    started = time.monotonic()
    reason = None
    with events.open("wb") as out, stderr.open("wb") as err:
        proc = subprocess.Popen(argv, cwd=work, stdin=subprocess.PIPE, stdout=out,
                                stderr=err, env=_execution_environment(config),
                                start_new_session=True)
        identity = proc_identity(proc.pid)
        data["turns"][-1]["process"] = {
            "pid": proc.pid, "start": identity["start"] if identity else None,
            "boot": boot_id(),
        }
        _write(path / "task.json", data)
        proc.stdin.write(prompt.encode("utf-8"))
        proc.stdin.close()
        while proc.poll() is None:
            if time.monotonic() - started > timeout:
                reason = "timeout"
            elif events.stat().st_size > MAX_EVENTS_BYTES:
                reason = "output_limit"
            if reason:
                os.killpg(proc.pid, signal.SIGTERM)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait()
                break
            time.sleep(0.2)
    try:
        parsed = parse_stream(events, data["model"], session, allowed_tool_names=TOOLS)
    except (OSError, ValueError) as exc:
        parsed = {"validated_success": False, "errors": [str(exc)], "response": ""}
    escaped = _outside_symlinks(work)
    if escaped:
        parsed["validated_success"] = False
        parsed.setdefault("errors", []).append("symlinks escape the isolated clone: " + ", ".join(escaped[:10]))
    if parsed["validated_success"] and proc.returncode == 0 and not reason:
        dirty = _git("status", "--porcelain=v1", "--untracked-files=all", cwd=work)
        if dirty:
            _git("add", "-A", cwd=work)
            _git("-c", "user.name=Claude Control", "-c",
                 "user.email=claude-control@localhost", "commit", "-m",
                 f"ccc: task {data['id']} turn {turn}", cwd=work)
    evidence = _evidence(work, data["base_sha"])
    # Never approve an incomplete turn, uncommitted files, or a no-op commit.
    ready = bool(parsed["validated_success"] and proc.returncode == 0 and not reason
                 and evidence["clean"] and not evidence["remotes"]
                 and evidence["head_sha"] != data["base_sha"])
    data["status"] = "ready" if ready else "needs_attention"
    data["evidence"] = evidence
    data["turns"][-1].update(status="completed" if ready else "needs_attention",
                             finished=time.time(), exit_code=proc.returncode,
                             reason=reason, errors=parsed.get("errors", []),
                             tool_use_count=parsed.get("tool_use_count", 0),
                             actual_models=parsed.get("actual_models", []),
                             response=parsed.get("response", "")[:8000],
                             provider_cost_usd=parsed.get("provider_cost_usd"))
    _write(path / "task.json", data)
    return _summary(data)


def _summary(data):
    result = {k: data[k] for k in ("id", "status", "model", "base_sha")}
    result["clone"] = data["work"]
    result["evidence"] = data.get("evidence")
    if data.get("check"):
        result["check"] = {"head_sha": data["check"]["head_sha"],
                           "passed": data["check"]["passed"],
                           "argv": data["check"]["argv"]}
    if data["turns"]:
        last = data["turns"][-1]
        result["last_turn"] = {k: last.get(k) for k in
                               ("number", "status", "reason", "errors", "tool_use_count",
                                "actual_models", "provider_cost_usd")}
        result["report"] = last.get("response", "")[:2000]
    if data.get("approved_sha"):
        result["approved_sha"] = data["approved_sha"]
    if data["status"] == "running" and data["turns"]:
        result["process_alive"] = _process_alive(data["turns"][-1])
    return result


def _process_alive(turn):
    identity = turn.get("process") or {}
    if not identity.get("start"):
        return False
    return alive(identity.get("pid"), identity["start"], identity.get("boot"))


def execute(args):
    operation = args.harness_command
    if operation == "agents":
        from .harness_status import snapshot

        root = _root_readonly(args)
        return snapshot(root, include_all=args.all)
    root = _root(args)
    if operation == "init":
        current_path = root / "config.json"
        if current_path.exists() and not args.rebind:
            raise ControlError("already_initialized", "Harness is already pinned; use doctor or explicitly rebind.")
        previous = _read(current_path) if current_path.exists() else None
        if previous and any((root / "tasks").glob("*/task.json")):
            raise ControlError("tasks_exist", "Existing harness tasks prevent runtime rebinding; use a new state directory.")
        binary = shutil.which(args.claude_bin)
        if not binary:
            raise ControlError("claude_missing", "Claude Code binary was not found.")
        binary = str(Path(binary).resolve())
        org = _auth(binary)
        help_result = subprocess.run([binary, "--help"], capture_output=True, text=True,
                                     timeout=15, env=_environment())
        required = ("--restricted", "--settings", "--tools", "--session-id", "--resume")
        if help_result.returncode or any(flag not in help_result.stdout for flag in required):
            raise ControlError("claude_unsupported", "Claude Code lacks required restricted CLI flags.")
        socat = args.socat_bin or shutil.which("socat")
        if socat:
            socat = str(Path(socat).resolve())
            if not Path(socat).is_file() or not os.access(socat, os.X_OK):
                raise ControlError("socat_missing", "Selected socat must be an executable file.")
        _write(root / "config.json", {"contract": CONTRACT, "claude_bin": binary,
                                      "binary_identity": list(binary_identity(binary)),
                                      "org_sha256": org, "socat_bin": socat,
                                      "socat_identity": list(binary_identity(socat)) if socat else None})
        return {"org_pinned": True, "billing": "claude.ai Team subscription",
                "next": "harness doctor"}
    config = _config(root, require_auth=operation in ("doctor", "start", "followup"))
    if operation == "doctor":
        try:
            _execution_preflight(config)
            sandbox = {"ready": True}
        except ControlError as exc:
            sandbox = {"ready": False, "reason": str(exc)}
        return {"ready": sandbox["ready"], "billing": "claude.ai Team subscription",
                "claude_bin": config["claude_bin"], "binary_pinned": True,
                "sandbox": sandbox, "platform": sys.platform}
    if operation == "start":
        _execution_preflight(config)
        if not 1 <= args.timeout <= 7200:
            raise ControlError("invalid_timeout", "Timeout must be 1–7200 seconds.")
        model = validate_model(args.model)
        spec = _spec(args.spec_file)
        repo = Path(_git("rev-parse", "--show-toplevel", cwd=args.repo)).resolve()
        base = _git("rev-parse", "--verify", f"{args.base}^{{commit}}", cwd=repo)
        if not args.request_id or len(args.request_id) > 128:
            raise ControlError("invalid_request", "Request id must be 1–128 characters.")
        requests = root / "requests.json"
        intent = json.dumps({"repo": str(repo), "base": base, "model": model,
                             "spec": spec, "timeout": args.timeout}, sort_keys=True)
        fingerprint = hashlib.sha256(intent.encode()).hexdigest()
        deduplicated = False
        with file_lock(root / "requests.lock", exclusive=True):
            mapping = _read(requests) if requests.exists() else {}
            if args.request_id in mapping:
                deduplicated = True
                record = mapping[args.request_id]
                if record["fingerprint"] != fingerprint:
                    raise ControlError("request_conflict", "Request id already belongs to a different task intent.")
                path, data = _task(root, record["id"])
                if data["status"] != "created":
                    return {**_summary(data), "deduplicated": True}
            else:
                task_id = str(uuid.uuid4())
                path = _task_path(root, task_id)
                path.mkdir(parents=True, mode=0o700)
                work = path / "work"
                _git("clone", "--no-local", "--no-hardlinks", "--no-checkout", str(repo), str(work),
                     timeout=600)
                _git("remote", "remove", "origin", cwd=work)
                _git("checkout", "-b", f"ccc/{task_id}", base, cwd=work)
                escaped = _outside_symlinks(work)
                if escaped:
                    raise ControlError("unsafe_tree", "Source contains links outside the clone: " + ", ".join(escaped[:10]))
                data = {"contract": CONTRACT, "id": task_id, "source": str(repo),
                        "work": str(work), "base_sha": base, "model": model,
                        "session_id": str(uuid.uuid4()), "status": "created", "turns": [],
                        "mission": _mission(spec)}
                _write(path / "task.json", data)
                mapping[args.request_id] = {"id": task_id, "fingerprint": fingerprint}
                _write(requests, mapping)
        with file_lock(path / "task.lock", exclusive=True, blocking=False):
            data = _task(root, path.name)[1]
            if data["status"] != "created":
                return {**_summary(data), "deduplicated": True}
            return {**_run(path, data, config, spec, args.timeout, resume=False),
                    "deduplicated": deduplicated}
    path, data = _task(root, args.task)
    if operation == "show":
        summary = _summary(data)
        if data["status"] == "running":
            summary["evidence_current"] = None
        else:
            current = _evidence(path / "work", data["base_sha"])
            summary["evidence_current"] = current == data.get("evidence")
        return summary
    if operation == "reconcile":
        with file_lock(path / "task.lock", exclusive=True, blocking=False):
            data = _task(root, args.task)[1]
            if data["status"] == "running":
                if _process_alive(data["turns"][-1]):
                    raise ControlError("task_busy", "Claude process is still running; cannot reconcile.")
                data["status"] = "unknown"
                data["turns"][-1]["status"] = "unknown"
                _write(path / "task.json", data)
            return _summary(data)
    if operation == "check":
        from .workspace_sandbox import execute as sandbox_execute

        if not 1 <= args.timeout <= 3600:
            raise ControlError("invalid_timeout", "Check timeout must be 1–3600 seconds.")
        raw = args.argv_json.read_bytes()
        if not raw or len(raw) > 8192:
            raise ControlError("invalid_check", "Check argv JSON must be 1–8192 bytes.")
        argv = json.loads(raw)
        if (not isinstance(argv, list) or not argv or len(argv) > 32
                or any(not isinstance(v, str) or not v or len(v) > 2048 for v in argv)
                or not Path(argv[0]).is_absolute()):
            raise ControlError("invalid_check", "Check must be an array of literal arguments with an absolute executable.")
        with file_lock(path / "task.lock", exclusive=True, blocking=False):
            data = _task(root, args.task)[1]
            before = _evidence(path / "work", data["base_sha"])
            if data["status"] != "ready" or before != data.get("evidence"):
                raise ControlError("evidence_stale", "Only clean, unchanged ready tasks can be checked.")
            result = sandbox_execute(path / "work", argv, args.timeout)
            after = _evidence(path / "work", data["base_sha"])
            data["check"] = {"head_sha": before["head_sha"], "argv": argv,
                             "result": result, "passed": result.get("exit_code") == 0 and after == before}
            _write(path / "task.json", data)
            return {"id": data["id"], "check": data["check"]}
    if operation == "followup":
        _execution_preflight(config)
        spec = _spec(args.spec_file)
        with file_lock(path / "task.lock", exclusive=True, blocking=False):
            data = _task(root, args.task)[1]
            if data["status"] == "unknown" and not args.acknowledge_unknown:
                raise ControlError("acknowledgement_required", "Inspect the unknown turn before an explicit follow-up.")
            if data["status"] not in ("ready", "needs_attention", "unknown"):
                raise ControlError("task_busy", "Task is running or has an unknown outcome; no automatic retry.")
            if data.get("approved_sha"):
                raise ControlError("task_approved", "Approved task cannot be changed.")
            if args.restart_session:
                data.setdefault("session_history", []).append(data["session_id"])
                data["session_id"] = str(uuid.uuid4())
            return _run(path, data, config, spec, args.timeout,
                        resume=not args.restart_session)
    if operation == "approve":
        with file_lock(path / "task.lock", exclusive=True, blocking=False):
            data = _task(root, args.task)[1]
            current = _evidence(path / "work", data["base_sha"])
            if (data["status"] != "ready" or current != data.get("evidence")
                    or args.sha != current["head_sha"]):
                raise ControlError("evidence_stale", "Task must be ready with clean, unchanged evidence and exact SHA.")
            check = data.get("check")
            if not args.accept_unchecked and (not check or not check["passed"]
                                              or check["head_sha"] != args.sha):
                raise ControlError("check_required", "Run a passing harness check or explicitly accept unchecked evidence.")
            data["approved_sha"] = args.sha
            data["approved_at"] = time.time()
            data["approved_unchecked"] = bool(args.accept_unchecked)
            data["status"] = "approved"
            _write(path / "task.json", data)
            return _summary(data)
    raise ControlError("invalid_command", "Unknown harness command.")
