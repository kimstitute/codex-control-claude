# macOS setup and support boundary

Starting with 0.27.0, Codex Control Claude can control and observe Claude Code
signed in as the same user on Intel and Apple Silicon macOS. It follows Claude
Code's platform floor of macOS 10.15 and requires Python 3.10 or newer.

## Feature matrix

| Feature | macOS | Boundary |
|---|---:|---|
| `start`, `followup`, `resume`, `restart`, `stop`, `reconcile` | Supported | POSIX process groups plus boot/PID-start identity checks |
| `delegate`, `task`, queues and `workflow` | Supported | Existing immutable ledger and structured-output contracts |
| Per-role models/effort and `models catalog` | Supported | Probed through the local Claude Code CLI |
| Telemetry, provider limits and Python TUI | Supported | Reads local user state and provider-reported values only |
| Rataflow graph/replay viewer | Supported | `x86_64-apple-darwin` and `aarch64-apple-darwin` releases |
| `terminal start/logs/attach/stop/shell` | Supported | Requires the installed Claude Code background-agent commands |
| `workspace` | Blocked | No supported fail-closed native sandbox backend |
| Composition planning/review stages | Supported | Supplied-text task/workflow path |
| Composition edit/check/freeze stages | Blocked | Same isolation requirement as `workspace` |
| Cross-host delivery | Blocked | State and execution remain on the current Mac |

## Why workspace execution is blocked

Linux uses Bubblewrap user, PID, network and mount namespaces. Windows executes
the same Bubblewrap contract inside WSL2. Apple's supported App Sandbox is tied
to signed applications and entitlements; it is not a general sandbox that an
ordinary Python CLI can apply to an arbitrary child process. This project does
not treat the old `sandbox-exec` interface as a supported long-term boundary.

On macOS, `workspace doctor` therefore returns `ready: false`, and workspace
creation or execution stops with `sandbox_unavailable`. The controller never
runs checks directly on the host or silently substitutes Docker or a VM.

## Install

```bash
git clone https://github.com/kimstitute/codex-control-claude.git
cd codex-control-claude
python3 install.py
```

For a release viewer, download the artifact that matches the Mac architecture,
verify its published SHA-256, and pass both to the installer. To build locally:

```bash
cargo build --locked --release --manifest-path viewer/Cargo.toml
viewer_sha=$(shasum -a 256 viewer/target/release/ccc-viewer | awk '{print $1}')
python3 install.py --update \
  --viewer viewer/target/release/ccc-viewer \
  --viewer-sha256 "$viewer_sha"
```

The default state directory is
`~/Library/Application Support/codex-control-claude/state`. If
`XDG_STATE_HOME` is set, the controller uses `$XDG_STATE_HOME/claude-control`.

## Validate

Sign in to Claude Code as the same macOS user, then run:

```bash
claude_control doctor --auth --platform --json
claude_control workspace doctor
claude_control models catalog
claude_control monitor snapshot --no-live
```

`doctor` should report `platform.platform` as `macos`; `session_control`, `tui`
and `provider_usage` should be supported. `workspace` and `sandbox` should be
unsupported with an explicit reason. For interactive terminals, also confirm
`terminal_supported`.

The Linux development host's deterministic unit tests and macOS GitHub Actions
cover the Python core and both Mac viewer targets. Real-account model calls and
attach input can only be validated on the target Mac, so run one opt-in smoke
after installing a release.

## Process and state safety

- The store is bound to a SHA-256 of the hardware UUID, the user UID and boot time.
- Runs record the process start time and process group rather than trusting a PID alone.
- If macOS rejects `fsync` on a directory descriptor, the controller conservatively
  synchronizes state metadata before continuing.
- macOS has no Linux `PR_SET_PDEATHSIG`; the worker terminates the whole process
  group on normal paths, while a still-live group after abnormal worker loss remains
  quarantined as `unknown` and is never retried automatically.
