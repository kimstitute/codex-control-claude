#!/usr/bin/env python3
"""Install this package using Codex's bundled personal-marketplace helpers."""

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import uuid
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "plugins/claude-control/scripts"))

from claude_control.platform.locks import file_lock  # noqa: E402

WINDOWS_HELPER_LIMIT = 16 * 1024 * 1024


def windows_helper_target(machine=None):
    value = (platform.machine() if machine is None else machine).lower()
    if value in ("amd64", "x86_64"):
        return "x86_64-pc-windows-msvc"
    if value in ("arm64", "aarch64"):
        return "aarch64-pc-windows-msvc"
    raise ValueError(f"Unsupported Windows helper architecture: {value}")


def stage_windows_helper(staged, helper_path, expected_sha256, *, machine=None):
    """Copy one verified helper from bytes read once and write its runtime manifest."""
    source = Path(helper_path)
    if source.is_symlink() or not source.is_file():
        raise ValueError("Windows helper must be an existing regular file, not a symlink.")
    data = source.read_bytes()
    if not data or len(data) > WINDOWS_HELPER_LIMIT:
        raise ValueError("Windows helper size must be between 1 byte and 16 MiB.")
    expected = str(expected_sha256).strip().lower()
    if len(expected) != 64 or any(character not in "0123456789abcdef" for character in expected):
        raise ValueError("Windows helper SHA-256 must be 64 hexadecimal digits.")
    actual = hashlib.sha256(data).hexdigest()
    if actual != expected:
        raise ValueError("Windows helper SHA-256 does not match the supplied pin.")
    target = windows_helper_target(machine)
    name = f"ccc-win-supervisor-{target}.exe"
    binary_dir = Path(staged) / "bin"
    binary_dir.mkdir(mode=0o700, exist_ok=True)
    (binary_dir / name).write_bytes(data)
    (binary_dir / "windows-helper-manifest.json").write_text(
        json.dumps(
            {
                "protocol": 1,
                "artifacts": [{"name": name, "sha256": actual, "size": len(data)}],
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )


def preserve_windows_helper(existing, staged):
    """Carry forward only a manifest-pinned helper from this installer's old tree."""
    source = Path(existing) / "bin"
    manifest_path = source / "windows-helper-manifest.json"
    if not manifest_path.is_file() or manifest_path.is_symlink():
        return False
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        artifacts = manifest["artifacts"]
        if manifest.get("protocol") != 1 or len(artifacts) != 1:
            return False
        item = artifacts[0]
        name = item["name"]
        if (
            set(item) != {"name", "sha256", "size"}
            or not isinstance(name, str)
            or not name.startswith("ccc-win-supervisor-")
            or not name.endswith(".exe")
            or Path(name).name != name
        ):
            return False
        binary = source / name
        data = binary.read_bytes()
        if (
            binary.is_symlink()
            or binary.parent != source
            or len(data) != item["size"]
            or hashlib.sha256(data).hexdigest() != item["sha256"]
        ):
            return False
    except (KeyError, OSError, TypeError, ValueError):
        return False
    destination = Path(staged) / "bin"
    destination.mkdir(mode=0o700, exist_ok=True)
    (destination / name).write_bytes(data)
    (destination / manifest_path.name).write_bytes(manifest_path.read_bytes())
    return True


@contextmanager
def installation_lock(lock_path):
    """Serialize installation and rollback across CLI processes on this host."""
    with file_lock(lock_path, exclusive=True):
        yield


def activate(staged, target, marketplace_name):
    """Under installation_lock, preserve the previous tree until installation succeeds."""
    backup = target.with_name(".claude-control-backup-" + uuid.uuid4().hex)
    had_target = target.exists()
    if had_target:
        target.rename(backup)
    try:
        staged.rename(target)
        install = subprocess.run(
            ["codex", "plugin", "add", "claude-control@" + marketplace_name, "--json"],
            text=True,
            capture_output=True,
            check=False,
        )
        if install.returncode:
            raise RuntimeError(install.stderr or install.stdout)
        result = json.loads(install.stdout)
    except BaseException:
        if target.exists():
            target.rename(staged)
        if had_target:
            backup.rename(target)
        raise
    if had_target:
        shutil.rmtree(backup)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    default_helpers = (
        Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
        / "skills/.system/plugin-creator"
    )
    parser.add_argument("--plugin-creator-root", type=Path, default=default_helpers)
    parser.add_argument(
        "--helper-python",
        help="Existing Python 3.10+ with PyYAML for Codex's validators; runtime is stdlib-only.",
    )
    parser.add_argument(
        "--update",
        action="store_true",
        help="Replace this installer's previously installed source and refresh Codex cache.",
    )
    parser.add_argument(
        "--windows-helper",
        type=Path,
        help="Optional local ccc-win-supervisor.exe to bundle for native Windows sessions.",
    )
    parser.add_argument(
        "--windows-helper-sha256",
        help="Required SHA-256 pin for --windows-helper.",
    )
    args = parser.parse_args()
    if bool(args.windows_helper) != bool(args.windows_helper_sha256):
        parser.error("--windows-helper and --windows-helper-sha256 must be supplied together.")
    plugin_parent = Path.home() / "plugins"
    plugin_parent.mkdir(parents=True, exist_ok=True)
    with installation_lock(plugin_parent / ".claude-control-install.lock"):
        return install_package(args, parser)


def install_package(args, parser):
    source = Path(__file__).resolve().parent / "plugins/claude-control"
    target = Path.home() / "plugins/claude-control"
    marketplace = Path.home() / ".agents/plugins/marketplace.json"
    helpers = args.plugin_creator_root.resolve() / "scripts"
    required = (
        "create_basic_plugin.py",
        "validate_plugin.py",
        "read_marketplace_name.py",
        "update_plugin_cachebuster.py",
    )
    if not all((helpers / name).is_file() for name in required) or not shutil.which("codex"):
        parser.error(
            "Codex CLI and its plugin-creator helpers are required; no dependencies are installed automatically."
        )
    candidates = (
        [args.helper_python]
        if args.helper_python
        else [
            sys.executable,
            *([] if os.name == "nt" else ["/usr/bin/python3"]),
            *(
                shutil.which(f"python3.{minor}")
                for minor in range(10, max(15, sys.version_info.minor + 1))
            ),
        ]
    )
    helper_python = next(
        (
            candidate
            for candidate in candidates
            if candidate
            and Path(candidate).is_file()
            and subprocess.run(
                [
                    candidate,
                    "-c",
                    "import sys; sys.exit(1) if sys.version_info < (3, 10) else None; import yaml",
                ],
                capture_output=True,
                check=False,
            ).returncode
            == 0
        ),
        None,
    )
    if helper_python is None:
        parser.error(
            "Codex's validator needs an existing Python 3.10+ with PyYAML; specify --helper-python. Runtime has no PyYAML dependency."
        )

    def helper(name, *values):
        return subprocess.check_output(
            [helper_python, str(helpers / name), *map(str, values)], text=True
        ).strip()

    helper("validate_plugin.py", source)
    existing = target.exists()
    if target.is_symlink():
        parser.error("Refusing a symlinked installation target.")
    if existing:
        marker = target / ".claude-control-install.json"
        if (
            not args.update
            or not marker.exists()
            or json.loads(marker.read_text()).get("package") != "claude-control"
        ):
            parser.error(
                "Target already exists; use --update only for a package installed by this installer."
            )
        name = helper("read_marketplace_name.py")
        catalog = json.loads(marketplace.read_text())
        entries = [p for p in catalog.get("plugins", []) if p.get("name") == "claude-control"]
        if len(entries) != 1 or entries[0].get("source") != {
            "source": "local",
            "path": "./plugins/claude-control",
        }:
            parser.error("Existing personal marketplace entry does not match this local source.")
    else:
        existing_entry = False
        if marketplace.exists():
            helper("read_marketplace_name.py")
            entries = [
                p
                for p in json.loads(marketplace.read_text()).get("plugins", [])
                if p.get("name") == "claude-control"
            ]
            if entries:
                if (
                    not args.update
                    or len(entries) != 1
                    or entries[0].get("source")
                    != {"source": "local", "path": "./plugins/claude-control"}
                ):
                    parser.error(
                        "Marketplace already has this name; verify its source and use --update to repair a missing local install."
                    )
                existing_entry = True
        flags = [] if existing_entry else ["--with-marketplace"]
        helper(
            "create_basic_plugin.py", "claude-control", *flags, "--with-skills", "--with-scripts"
        )
        name = helper("read_marketplace_name.py")
    # Replace the entire source tree, never traverse existing file symlinks or retain stale code.
    # Runtime state lives elsewhere and is never copied or removed.
    with tempfile.TemporaryDirectory(prefix=".claude-control-stage-", dir=target.parent) as temp:
        staged = Path(temp) / "claude-control"
        shutil.copytree(
            source, staged, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo")
        )
        if args.windows_helper:
            try:
                stage_windows_helper(
                    staged,
                    args.windows_helper,
                    args.windows_helper_sha256,
                )
            except (OSError, ValueError) as exc:
                parser.error(str(exc))
        elif existing:
            preserve_windows_helper(target, staged)
        (staged / ".claude-control-install.json").write_text(
            json.dumps({"package": "claude-control", "source": str(source)})
        )
        if existing:
            helper("update_plugin_cachebuster.py", staged)
        helper("validate_plugin.py", staged)
        install_result = activate(staged, target, name)
    print(
        json.dumps(
            {
                "installed": True,
                "source": str(target),
                "marketplace": str(marketplace),
                "marketplace_name": name,
                "codex_result": install_result,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
