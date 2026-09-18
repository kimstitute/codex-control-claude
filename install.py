#!/usr/bin/env python3
"""Install this package using Codex's bundled personal-marketplace helpers."""

import argparse
import fcntl
import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def installation_lock(lock_path):
    """Serialize installation and rollback across CLI processes on this host."""
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "a") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
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
    args = parser.parse_args()
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
            "/usr/bin/python3",
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
