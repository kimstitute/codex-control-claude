<p align="center">
  <img src="plugins/claude-control/assets/logo.png" alt="Codex Control Claude logo" width="128">
</p>

<h1 align="center">Codex Control Claude</h1>

<p align="center">Let Codex coordinate multiple local Claude Code sessions.</p>

<p align="center">
  <strong>Linux</strong> · <strong>Python 3.10+</strong> · <strong>Standard-library runtime</strong>
</p>

<p align="center">
  English · <a href="README.ko.md">한국어</a> · <a href="docs/cli.md">CLI reference</a> · <a href="docs/architecture.md">Architecture</a>
</p>

Codex Control Claude gives Codex a practical way to delegate work to Claude, keep conversations separate, and collect their results. It bundles a local session controller with a Codex skill, so you can ask for a review or a small implementation task without managing terminal panes yourself.

**Version 0.1 works with supplied text.** Claude returns analysis, proposed code, or review notes; Codex checks the result and applies any edits. Claude's file and shell tools and MCP are disabled in this profile.

## What you can do

| Capability | Behavior |
|---|---|
| Assign roles and models | Use explicit `sonnet` or `fable` sessions for different tasks. |
| Work in parallel | Run independent conversations within a configurable limit; the default is two. |
| Continue a conversation | Send a follow-up to its exact managed session ID. |
| Inspect progress | Read status, bounded logs, and structured JSON results. |
| Cancel one task | Stop an owned run while other sessions continue. |
| Recover deliberately | Reconcile uncertain runs or explicitly start a new backend conversation. |
| Check execution evidence | Validate the reported model, session ID, exit status, and result integrity. |

Each installation controls Claude **on the same host and under the same user**. Installing the package on several machines creates independent local controllers.

## Before you start

- **Linux and Python 3.10 or newer.** Windows and macOS are not supported by this release.
- **Claude Code installed and signed in locally.** This version uses the normal HOME-based login. Custom `CLAUDE_CONFIG_DIR` and API-key environment variables are not forwarded.
- **Codex CLI with plugin support and its bundled `plugin-creator` helpers.** The installer checks for these tools.
- **An existing Python 3.10+ with PyYAML for Codex's plugin validator.** This is an installer-helper requirement; the controller itself uses only the Python standard library. The installer does not install dependencies.

The controller accepts `sonnet` and `fable`. Model availability depends on your Claude installation and account. An unavailable model is an error; the controller does not silently substitute another one. `doctor` checks CLI capabilities and, with `--auth`, login status; it does not prove access to a particular model.

## Quick start

### 1. Install the plugin

```bash
git clone https://github.com/kimstitute/codex-control-claude.git
cd codex-control-claude
python3 install.py
```

The installer copies the plugin to `~/plugins/claude-control`, registers it in your personal Codex marketplace, and installs it through Codex. It leaves runtime state separate from the package.

If your Codex helpers or validator Python are in a different location:

```bash
python3 install.py \
  --plugin-creator-root /absolute/path/to/plugin-creator \
  --helper-python /absolute/path/to/python-with-pyyaml
```

### 2. Initialize this machine

Replace the two example paths below with your Claude executable and project directory:

```bash
python3 ~/plugins/claude-control/scripts/claude_control_cli.py init \
  --claude-bin /absolute/path/to/claude \
  --allow-root /absolute/path/to/your/project \
  --max-parallel 2

python3 ~/plugins/claude-control/scripts/claude_control_cli.py doctor --auth
```

To locate Claude, run `command -v claude`. You can repeat `--allow-root` when initializing to authorize more than one project directory. A ready diagnostic reports `"ready": true`.

### 3. Ask Codex to delegate

Open a new Codex task so it can discover the installed **`claude-control`** skill. For example:

> Use $claude-control to ask Sonnet for a small refactoring proposal and Fable for an independent critique. Give both the relevant code as text, keep their sessions separate, and verify the answers before changing files.

The skill guides Codex through creating sessions, recording IDs, collecting results, and handling failures. A model's successful execution is evidence that the call worked; Codex still needs to check the answer itself.

## Prefer the command line?

Public operation commands return JSON. Prepare a prompt file and submit a task:

```bash
python3 ~/plugins/claude-control/scripts/claude_control_cli.py start \
  --name implementation \
  --model sonnet \
  --role implementer \
  --project /absolute/path/to/your/project \
  --prompt-file /absolute/path/to/task.txt \
  --request-id implement-example-001 \
  --timeout 300
```

Keep the returned `id` for the **run**, and `session_id` for the **conversation**. Use a stable request ID if you need to retry the same submission after losing its response.

```bash
python3 ~/plugins/claude-control/scripts/claude_control_cli.py list
python3 ~/plugins/claude-control/scripts/claude_control_cli.py wait --run <run-uuid> --seconds 30
python3 ~/plugins/claude-control/scripts/claude_control_cli.py result --run <run-uuid>
```

See the [CLI reference](docs/cli.md) for follow-ups, cancellation, restart, and recovery.

## What stays on your machine

Controller configuration and records live in `$XDG_STATE_HOME/claude-control`, or `~/.local/state/claude-control` by default. They include prompts, responses, session IDs, process metadata, and logs, and use user-only permissions. Claude also maintains its own normal local session history.

**Local control does not mean local model inference.** Claude Code sends model requests to the Claude service using your existing authentication. The controller does not provide cross-host dispatch or add a separate telemetry service.

The project-path allowlist controls the requested working directory; it is not an operating-system filesystem sandbox. Process cleanup covers the owned process group, not processes that escape it. Administrator-managed Claude policy still applies. See [architecture and boundaries](docs/architecture.md).

## Update

Finish or stop managed runs before updating:

```bash
git pull --ff-only
python3 install.py --update
```

Open a new Codex task after reinstalling. Updates preserve the separate runtime store. State schema 3 does not automatically migrate the earlier development schemas; see [recovery notes](docs/cli.md#state-and-recovery).

## Tests

```bash
python3 -m unittest discover -s tests -v
```

The automated suite uses fake Claude processes and makes **no model calls**. It covers concurrency, request deduplication, cancellation, failure recovery, stream validation, relocation, and installation rollback. Separate live integration checks have exercised parallel models, isolated conversation recall, targeted cancellation, and resume. Raw live logs are not included in this repository.

To deliberately run the live suite using your own Claude account, see [testing](docs/testing.md).

## Current scope

Version 0.1 supports Linux, supplied-text tasks, and sessions created by this controller. File/shell execution by Claude, adopting arbitrary existing sessions, conversation forking, Windows/macOS support, an MCP adapter, cross-host dispatch, and automatic Codex wake-up are future work.

Maintained by [kimstitute](https://github.com/kimstitute). This is an independent project, not an official OpenAI or Anthropic integration.
