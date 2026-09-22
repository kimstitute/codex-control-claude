# Windows setup and operations

Version 0.14.0 supports same-user Claude Code sessions, tasks, workflows,
compositions and provider-limit monitoring on Windows 10/11. Windows support is
beta: unit tests and the Windows Rust build are automated, but a live Windows and
WSL2 integration run has not yet been completed.

## Architecture and boundary

- `ccc-win-supervisor.exe` runs native Claude processes inside a kill-on-close
  Windows Job Object. It creates each child suspended and resumes it only after
  the controller records its process identity.
- Workspace operations supervise `wsl.exe` with the same helper, unpack into a
  private WSL2 ext4 state directory, then use the existing Bubblewrap sandbox.
- AppContainer and an unconfined workspace fallback are excluded.
- Claude native tools/MCP, cross-host dispatch and automatic merge/push remain
  disabled.

## Prerequisites

Native sessions require Python 3.10+, Git, Claude Code, Codex and an
architecture-matching `ccc-win-supervisor.exe`. Workspaces additionally require
WSL2 and `bwrap` inside the selected WSL distribution.

Check the WSL side:

```bash
command -v python3
command -v bwrap
test -d "$HOME"
```

The controller's WSL state must be on the distribution's ext4 filesystem, not
below `/mnt`. Workspace support stays disabled when this boundary or the live
Bubblewrap probe fails.

## Install

Download the release helper and checksum, then verify and pin the exact bytes:

```powershell
$sha = (Get-FileHash .\ccc-win-supervisor.exe -Algorithm SHA256).Hash.ToLower()
py -3 install.py `
  --windows-helper .\ccc-win-supervisor.exe `
  --windows-helper-sha256 $sha
```

Use the same flags with `--update`. The installer reads the helper once, verifies
the SHA-256, and stores it with an architecture-specific name and manifest. A
missing, mismatched or wrong-architecture helper fails closed.

To build the helper from source, use the Rust MSVC toolchain and Visual Studio
Build Tools:

```powershell
cd native\windows\ccc-win-supervisor
cargo test --release
cargo build --release
```

## Initialize and diagnose

```powershell
py -3 "$HOME\plugins\claude-control\scripts\claude_control_cli.py" init `
  --claude-bin "C:\absolute\path\to\claude.exe" `
  --allow-root "C:\work\project" `
  --max-parallel 2

py -3 "$HOME\plugins\claude-control\scripts\claude_control_cli.py" doctor --auth
py -3 "$HOME\plugins\claude-control\scripts\claude_control_cli.py" workspace doctor
```

The first command checks login and required Claude CLI flags. The workspace
doctor probes the pinned helper, WSL2, WSL Python/Bubblewrap and the private ext4
state boundary. It does not enable workspaces after a failed probe.

## Files and apply guarantees

The controller rejects NTFS reparse points, unsafe state DACLs, Windows device
names, ADS colons, trailing dots/spaces and case-insensitive path-prefix
collisions. Canonical sidecars preserve Git executable modes outside the
model-visible tree and must match the exact file set.

`workspace apply` still requires the exact source HEAD, a clean worktree, a
verified frozen manifest and patch, and Git-normalized blob hashes. It never
commits, merges or pushes. A newly added executable is rejected on Windows
because a native worktree cannot faithfully create its Git mode.

## Troubleshooting

| Symptom | Check |
|---|---|
| Session control disabled | Helper architecture, install manifest and file SHA-256 |
| WSL workspace probe fails | `wsl.exe --status` and the selected/default distribution |
| `bwrap` missing | Install Bubblewrap inside that WSL distribution |
| State path rejected | Use a WSL ext4 path outside `/mnt` |
| Reparse/DACL error | Remove junctions/symlinks and use a current-user-only directory |
| Apply mode error | Apply new executables on Linux or keep the new file non-executable |

