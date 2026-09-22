"""Bounded, receipt-oriented file snapshots for controlled workspaces."""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import stat
import subprocess
import tempfile
from pathlib import Path

from . import workspace_policy
from .store import ControlError

MAX_FILES = 1024
MAX_FILE_BYTES = 256 * 1024
MAX_TOTAL_BYTES = 16 * 1024 * 1024
MAX_TEXT_BYTES = 128 * 1024


def _canonical(value) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode("ascii")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _error(code: str, message: str, exc: BaseException | None = None):
    if exc is None:
        raise ControlError(code, message)
    raise ControlError(code, message) from exc


def _normalized(policy):
    return workspace_policy.normalize_policy(policy)


def _prepare_destination(destination: str | os.PathLike[str]) -> Path:
    target = Path(destination)
    try:
        if target.exists() or target.is_symlink():
            info = target.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                _error("workspace_integrity", "Destination must be a new or empty directory.")
            if info.st_uid != os.getuid() or any(target.iterdir()):
                _error("workspace_integrity", "Destination must be owned and empty.")
            target.chmod(0o700)
        else:
            target.mkdir(mode=0o700, parents=False)
    except ControlError:
        raise
    except OSError as exc:
        _error("workspace_integrity", "Cannot create private destination.", exc)
    marker = target / ".incomplete"
    marker.write_bytes(b"")
    marker.chmod(0o600)
    return target


def _reject_overlap(destination, *sources: Path) -> None:
    try:
        target = Path(destination).resolve(strict=False)
        for source in sources:
            resolved = source.resolve(strict=True)
            if (
                target == resolved
                or target.is_relative_to(resolved)
                or resolved.is_relative_to(target)
            ):
                _error("workspace_integrity", "Destination cannot overlap a source tree.")
    except ControlError:
        raise
    except OSError as exc:
        _error("workspace_integrity", "Cannot validate destination isolation.", exc)


def _finish_destination(target: Path) -> None:
    marker = target / ".incomplete"
    marker.unlink()
    directory_fd = os.open(target, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _manifest(files: dict[str, dict]) -> dict:
    ordered = {name: files[name] for name in sorted(files)}
    return {"files": ordered, "sha256": _sha(_canonical(ordered))}


def _bounds(files: dict[str, dict]) -> None:
    if len(files) > MAX_FILES:
        _error("workspace_integrity", "Workspace exceeds the 1024-file limit.")
    if sum(value["size"] for value in files.values()) > MAX_TOTAL_BYTES:
        _error("workspace_integrity", "Workspace exceeds the 16 MiB total limit.")


def _git(repo: Path, *arguments: str, binary: bool = False) -> bytes | str:
    command = [
        "/usr/bin/git",
        "--no-pager",
        "--no-replace-objects",
        "-c",
        "core.hooksPath=/dev/null",
        "-C",
        str(repo),
        *arguments,
    ]
    environment = {
        "PATH": "/usr/bin:/bin",
        "LANG": "C",
        "LC_ALL": "C",
        "HOME": "/nonexistent",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_NO_REPLACE_OBJECTS": "1",
    }
    try:
        completed = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            env=environment,
        )
    except OSError as exc:
        _error("workspace_source", "Git could not inspect the repository.", exc)
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace")[:500].strip()
        _error("workspace_source", f"Git rejected the repository or ref: {detail}")
    return completed.stdout if binary else completed.stdout.decode("utf-8", errors="strict")


def _safe_repo(repo: str | os.PathLike[str]) -> Path:
    try:
        value = Path(repo)
        if value.is_symlink():
            _error("workspace_source", "Repository path cannot be a symlink.")
        value = value.resolve(strict=True)
        if not value.is_dir():
            _error("workspace_source", "Repository must be a directory.")
        return value
    except ControlError:
        raise
    except OSError as exc:
        _error("workspace_source", "Repository is unavailable.", exc)


def _selected(policy: dict, relative: str) -> bool:
    if not workspace_policy.permits(policy["read_paths"], relative):
        return False
    try:
        workspace_policy.check_access(policy, relative)
    except ControlError as exc:
        if exc.code == "workspace_path_denied":
            return False
        raise
    return True


def _protected(relative: str) -> bool:
    parts = relative.split("/")
    final = parts[-1]
    return (
        any(
            part in {".git", ".claude", ".codex", ".omx", ".agents", ".env"}
            or part.startswith(".env.")
            for part in parts
        )
        or final in {"credentials.json", "auth.json"}
        or final.endswith((".pem", ".key"))
    )


def _write_blob(root: Path, relative: str, data: bytes, mode: str) -> None:
    current = root
    parts = relative.split("/")
    for component in parts[:-1]:
        current = current / component
        if current.exists() or current.is_symlink():
            info = current.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                _error("workspace_integrity", "Selected path crosses an unsafe component.")
        else:
            current.mkdir(mode=0o700)
    output = current / parts[-1]
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(output, flags, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        output.chmod(0o755 if mode == "100755" else 0o644)
    except OSError as exc:
        _error("workspace_integrity", "Cannot materialize selected file.", exc)


def resolve_commit(repo, ref):
    source = _safe_repo(repo)
    if not isinstance(ref, str) or not ref or ref.startswith("-") or "\x00" in ref:
        _error("workspace_source", "Ref must be a non-option Git reference.")
    commit = _git(source, "rev-parse", "--verify", ref + "^{commit}").strip()
    if len(commit) != 40 or any(char not in "0123456789abcdef" for char in commit):
        _error("workspace_source", "Git did not resolve a canonical commit.")
    return commit


def create_snapshot(repo, ref, destination, policy):
    policy = _normalized(policy)
    source = _safe_repo(repo)
    _reject_overlap(destination, source)
    commit = resolve_commit(source, ref)
    raw = _git(source, "ls-tree", "-r", "-z", "--full-tree", commit, binary=True)
    selected: list[tuple[str, str, str]] = []
    for record in raw.split(b"\0"):
        if not record:
            continue
        relative = None
        try:
            header, encoded_path = record.split(b"\t", 1)
            mode, kind, object_id = header.decode("ascii").split(" ")
            relative = encoded_path.decode("utf-8")
            relative = workspace_policy.path(relative)
        except (ValueError, UnicodeError, ControlError) as exc:
            if isinstance(relative, str) and _protected(relative):
                continue
            _error("workspace_source", "Git tree contains an unsafe path.", exc)
        if not workspace_policy.permits(policy["read_paths"], relative):
            continue
        if not _selected(policy, relative):
            continue
        if kind != "blob" or mode not in ("100644", "100755"):
            _error("workspace_source", "Selected Git entry is not a regular file.")
        selected.append((relative, object_id, mode))
    if len(selected) > MAX_FILES:
        _error("workspace_source", "Selected Git tree exceeds the file limit.")
    collision = workspace_policy.find_case_collision(relative for relative, _, _ in selected)
    if collision is not None:
        first, second = collision
        _error(
            "workspace_source",
            f"Git tree has a case-insensitive path collision: {first!r} vs {second!r}.",
        )
    target = _prepare_destination(destination)
    files: dict[str, dict] = {}
    total = 0
    for relative, object_id, mode in sorted(selected):
        size_text = _git(source, "cat-file", "-s", object_id).strip()
        try:
            size = int(size_text)
        except ValueError as exc:
            _error("workspace_source", "Git reported an invalid blob size.", exc)
        if size < 0 or size > MAX_FILE_BYTES:
            _error("workspace_source", "Selected Git blob exceeds the file limit.")
        total += size
        if total > MAX_TOTAL_BYTES:
            _error("workspace_source", "Selected Git tree exceeds the total size limit.")
        data = _git(source, "cat-file", "blob", object_id, binary=True)
        if len(data) != size:
            _error("workspace_source", "Git blob size changed while reading.")
        _write_blob(target, relative, data, mode)
        files[relative] = {"sha256": _sha(data), "size": size, "mode": mode}
    _finish_destination(target)
    return {"base_commit": commit, **_manifest(files)}


def _directory_root(directory, *, code="workspace_integrity") -> Path:
    value = Path(directory)
    try:
        info = value.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            _error(code, "Workspace root must be a real directory.")
        return value
    except ControlError:
        raise
    except OSError as exc:
        _error(code, "Workspace directory is unavailable.", exc)


def _directory_relevant(policy: dict, relative: str) -> bool:
    prefix = relative + "/"
    return any(
        entry == "."
        or entry == prefix
        or entry.startswith(prefix)
        or (entry.endswith("/") and prefix.startswith(entry))
        for entry in policy["read_paths"]
    )


def inspect_tree(directory, policy):
    policy = _normalized(policy)
    root = _directory_root(directory)
    files: dict[str, dict] = {}
    total = 0

    def visit(current: Path, prefix: str = "") -> None:
        nonlocal total
        try:
            entries = sorted(os.scandir(current), key=lambda item: item.name)
        except OSError as exc:
            _error("workspace_integrity", "Workspace tree cannot be enumerated.", exc)
        for entry in entries:
            relative = f"{prefix}/{entry.name}" if prefix else entry.name
            try:
                relative = workspace_policy.path(
                    relative, allow_directory=entry.is_dir(follow_symlinks=False)
                )
                info = entry.stat(follow_symlinks=False)
            except (OSError, ControlError) as exc:
                _error("workspace_integrity", "Workspace contains an unsafe path.", exc)
            if stat.S_ISLNK(info.st_mode):
                _error("workspace_integrity", "Workspace symlinks are forbidden.")
            if stat.S_ISDIR(info.st_mode):
                if not _directory_relevant(policy, relative):
                    _error("workspace_path_denied", "Workspace contains an unauthorized directory.")
                visit(Path(entry.path), relative)
                continue
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                _error("workspace_integrity", "Workspace contains a special or hard-linked file.")
            try:
                workspace_policy.check_access(policy, relative)
            except ControlError:
                raise
            mode_bits = stat.S_IMODE(info.st_mode)
            if mode_bits not in (0o644, 0o755):
                _error("workspace_integrity", "Workspace file mode is unsupported.")
            if info.st_size > MAX_FILE_BYTES:
                _error("workspace_integrity", "Workspace file exceeds the file limit.")
            if len(files) >= MAX_FILES:
                _error("workspace_integrity", "Workspace exceeds the file limit.")
            total += info.st_size
            if total > MAX_TOTAL_BYTES:
                _error("workspace_integrity", "Workspace exceeds the total size limit.")
            try:
                data = Path(entry.path).read_bytes()
            except OSError as exc:
                _error("workspace_integrity", "Workspace file cannot be read.", exc)
            if len(data) != info.st_size:
                _error("workspace_integrity", "Workspace file changed while reading.")
            files[relative] = {
                "sha256": _sha(data),
                "size": len(data),
                "mode": "100755" if mode_bits == 0o755 else "100644",
            }

    visit(root)
    return _manifest(files)


def copy_tree(source, destination, policy):
    policy = _normalized(policy)
    root = _directory_root(source)
    _reject_overlap(destination, root)
    manifest = inspect_tree(root, policy)
    target = _prepare_destination(destination)
    for relative, receipt in manifest["files"].items():
        data = (root / relative).read_bytes()
        if _sha(data) != receipt["sha256"]:
            _error("workspace_integrity", "Source changed during copy.")
        _write_blob(target, relative, data, receipt["mode"])
    _finish_destination(target)
    copied = inspect_tree(target, policy)
    if copied != manifest:
        _error("workspace_integrity", "Copied workspace differs from its source.")
    return copied


def _safe_file(
    directory, relative, policy, *, write=False, missing=False
) -> tuple[Path, os.stat_result | None]:
    root = _directory_root(directory)
    try:
        relative = workspace_policy.check_access(policy, relative, write=write)
    except ControlError:
        raise
    current = root
    parts = relative.split("/")
    for component in parts[:-1]:
        current = current / component
        try:
            info = current.lstat()
        except FileNotFoundError:
            if write and missing:
                current.mkdir(mode=0o700)
                continue
            _error("workspace_integrity", "Workspace parent is missing.")
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            _error("workspace_integrity", "Workspace path crosses an unsafe component.")
    target = current / parts[-1]
    try:
        info = target.lstat()
    except FileNotFoundError:
        return target, None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        _error("workspace_integrity", "Workspace target is not a private regular file.")
    return target, info


def _text(data: bytes) -> str:
    if len(data) > MAX_TEXT_BYTES:
        _error("workspace_integrity", "Text file exceeds the 128 KiB limit.")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        _error("workspace_integrity", "Workspace text must be UTF-8.", exc)


def read_text(directory, relative, policy):
    policy = _normalized(policy)
    target, info = _safe_file(directory, relative, policy)
    if info is None:
        _error("workspace_integrity", "Workspace file does not exist.")
    try:
        data = target.read_bytes()
    except OSError as exc:
        _error("workspace_integrity", "Workspace text cannot be read.", exc)
    content = _text(data)
    return {"path": workspace_policy.path(relative), "content": content, "sha256": _sha(data)}


def write_text(directory, relative, content, policy):
    policy = _normalized(policy)
    if not isinstance(content, str):
        _error("workspace_integrity", "Workspace content must be text.")
    data = content.encode("utf-8")
    if len(data) > MAX_TEXT_BYTES:
        _error("workspace_integrity", "Workspace text exceeds the 128 KiB limit.")
    target, info = _safe_file(directory, relative, policy, write=True, missing=True)
    before = None
    mode = 0o644
    if info is not None:
        try:
            old = target.read_bytes()
        except OSError as exc:
            _error("workspace_integrity", "Existing workspace text cannot be read.", exc)
        _text(old)
        before = _sha(old)
        mode = stat.S_IMODE(info.st_mode)
        if mode not in (0o644, 0o755):
            _error("workspace_integrity", "Workspace file mode is unsupported.")
    temporary = None
    try:
        descriptor, name = tempfile.mkstemp(prefix=".write-", dir=target.parent)
        temporary = Path(name)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(mode)
        os.replace(temporary, target)
        temporary = None
        directory_fd = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError as exc:
        _error("workspace_integrity", "Atomic workspace write failed.", exc)
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
    return {
        "path": workspace_policy.path(relative),
        "before_sha256": before,
        "after_sha256": _sha(data),
    }


def apply_patch(directory, relative, base_sha256, hunks, policy):
    """Apply validated line hunks to an existing UTF-8 file with an exact base hash."""
    current = read_text(directory, relative, policy)
    if current["sha256"] != base_sha256:
        _error("workspace_conflict", "Patch base SHA-256 does not match the current file.")
    source = current["content"].splitlines(keepends=True)
    newline = "\r\n" if any(line.endswith("\r\n") for line in source) else "\n"
    output = []
    source_index = 0
    for hunk in hunks:
        start = hunk["old_start"] - 1
        if start < source_index or start > len(source):
            _error("workspace_conflict", "Patch hunk is outside the current file.")
        output.extend(source[source_index:start])
        if hunk["new_start"] != len(output) + 1:
            _error("workspace_conflict", "Patch new_start does not match the result position.")
        cursor = start
        for line in hunk["lines"]:
            marker, payload = line[0], line[1:]
            if marker in " -":
                if cursor >= len(source) or (
                    source[cursor] != payload
                    and source[cursor].removesuffix("\n").removesuffix("\r") != payload
                ):
                    _error("workspace_conflict", "Patch context differs from the current file.")
                original = source[cursor]
                cursor += 1
            if marker == " ":
                # Preserve the source terminator when a logical context line omitted it.
                output.append(original)
            elif marker == "+":
                output.append(
                    payload if payload.endswith(("\n", "\r")) else payload + newline
                )
        if cursor - start != hunk["old_count"]:
            _error("workspace_conflict", "Patch consumed an unexpected source range.")
        source_index = cursor
    output.extend(source[source_index:])
    result = write_text(directory, relative, "".join(output), policy)
    result["hunks"] = len(hunks)
    return result


def _diff(path: str, before: bytes | None, after: bytes | None) -> str:
    before_text = "" if before is None else _text(before)
    after_text = "" if after is None else _text(after)
    lines = list(
        difflib.unified_diff(
            before_text.splitlines(keepends=True),
            after_text.splitlines(keepends=True),
            fromfile="/dev/null" if before is None else "a/" + path,
            tofile="/dev/null" if after is None else "b/" + path,
        )
    )
    output = []
    for line in lines:
        output.append(line)
        if not line.endswith("\n"):
            output.append("\n\\ No newline at end of file\n")
    return "".join(output)


def freeze(baseline, working, destination, policy):
    policy = _normalized(policy)
    baseline_root = _directory_root(baseline)
    working_root = _directory_root(working)
    _reject_overlap(destination, baseline_root, working_root)
    before_manifest = inspect_tree(baseline_root, policy)
    after_manifest = inspect_tree(working_root, policy)
    changes = []
    patch_parts = []
    for relative in sorted(set(before_manifest["files"]) | set(after_manifest["files"])):
        before = before_manifest["files"].get(relative)
        after = after_manifest["files"].get(relative)
        if before == after:
            continue
        try:
            workspace_policy.check_access(policy, relative, write=True)
        except ControlError:
            raise
        if before and after and before["mode"] != after["mode"]:
            _error("workspace_integrity", "Workspace mode changes are forbidden.")
        before_data = (baseline_root / relative).read_bytes() if before else None
        after_data = (working_root / relative).read_bytes() if after else None
        patch_parts.append(_diff(relative, before_data, after_data))
        changes.append(
            {
                "path": relative,
                "before_sha256": before["sha256"] if before else None,
                "after_sha256": after["sha256"] if after else None,
            }
        )
    target = _prepare_destination(destination)
    tree = target / "tree"
    copy_tree(working_root, tree, policy)
    patch = "".join(patch_parts).encode("utf-8")
    patch_path = target / "patch.diff"
    patch_path.write_bytes(patch)
    patch_path.chmod(0o600)
    manifest = {
        "files": after_manifest["files"],
        "tree_sha256": after_manifest["sha256"],
        "changes": changes,
        "patch_sha256": _sha(patch),
    }
    raw = _canonical(manifest)
    manifest_path = target / "manifest.json"
    manifest_path.write_bytes(raw)
    manifest_path.chmod(0o600)
    _finish_destination(target)
    return {**manifest, "manifest_sha256": _sha(raw)}


def verify_frozen(destination, policy, manifest_sha256):
    policy = _normalized(policy)
    root = _directory_root(destination)
    if not isinstance(manifest_sha256, str) or len(manifest_sha256) != 64:
        _error("workspace_integrity", "Frozen manifest digest is invalid.")
    try:
        entries = {entry.name: entry for entry in os.scandir(root)}
    except OSError as exc:
        _error("workspace_integrity", "Frozen workspace cannot be enumerated.", exc)
    if set(entries) != {"tree", "manifest.json", "patch.diff"}:
        _error("workspace_integrity", "Frozen workspace contains unexpected entries.")
    for name in ("manifest.json", "patch.diff"):
        info = entries[name].stat(follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            _error("workspace_integrity", "Frozen receipt is not a regular private file.")
    try:
        raw = (root / "manifest.json").read_bytes()
        patch = (root / "patch.diff").read_bytes()
    except OSError as exc:
        _error("workspace_integrity", "Frozen workspace receipt is incomplete.", exc)
    if _sha(raw) != manifest_sha256:
        _error("workspace_integrity", "Frozen manifest digest changed.")
    try:
        manifest = json.loads(raw.decode("ascii"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        _error("workspace_integrity", "Frozen manifest is invalid.", exc)
    if not isinstance(manifest, dict) or set(manifest) != {
        "files",
        "tree_sha256",
        "changes",
        "patch_sha256",
    }:
        _error("workspace_integrity", "Frozen manifest shape is invalid.")
    if raw != _canonical(manifest) or _sha(patch) != manifest["patch_sha256"]:
        _error("workspace_integrity", "Frozen receipt bytes changed.")
    tree = inspect_tree(root / "tree", policy)
    if tree["files"] != manifest["files"] or tree["sha256"] != manifest["tree_sha256"]:
        _error("workspace_integrity", "Frozen tree differs from its manifest.")
    if not isinstance(manifest["changes"], list):
        _error("workspace_integrity", "Frozen changes are invalid.")
    previous = None
    for change in manifest["changes"]:
        if not isinstance(change, dict) or set(change) != {
            "path",
            "before_sha256",
            "after_sha256",
        }:
            _error("workspace_integrity", "Frozen change receipt is invalid.")
        relative = change["path"]
        try:
            workspace_policy.check_access(policy, relative, write=True)
        except ControlError as exc:
            _error("workspace_integrity", "Frozen change exceeds write authority.", exc)
        if previous is not None and relative <= previous:
            _error("workspace_integrity", "Frozen changes are not unique and ordered.")
        previous = relative
        for key in ("before_sha256", "after_sha256"):
            digest = change[key]
            if digest is not None and (
                not isinstance(digest, str)
                or len(digest) != 64
                or any(char not in "0123456789abcdef" for char in digest)
            ):
                _error("workspace_integrity", "Frozen change digest is invalid.")
        if change["before_sha256"] is None and change["after_sha256"] is None:
            _error("workspace_integrity", "Frozen change has no before or after file.")
    return {**manifest, "manifest_sha256": manifest_sha256}
