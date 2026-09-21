<p align="center">
  <img src="plugins/claude-control/assets/logo.png" alt="Codex Control Claude logo" width="128">
</p>

<h1 align="center">Codex Control Claude</h1>

<p align="center">Let Codex coordinate multiple local Claude Code sessions.</p>

<p align="center">
  <strong>Linux</strong> · <strong>Python 3.10+</strong> · <strong>Standard-library runtime</strong>
</p>

<p align="center">
  English · <a href="README.ko.md">한국어</a> · <a href="docs/cli.md">CLI reference</a> · <a href="docs/architecture.md">Architecture</a> · <a href="CHANGELOG.md">Changelog</a>
</p>

Codex Control Claude gives Codex a practical way to delegate work to Claude, keep conversations separate, and collect their results. It bundles a local session controller with a Codex skill, so you can ask for a review or a small implementation task without managing terminal panes yourself.

**Version 0.7 adds controlled workspace editing and checks.** Codex can authorize a private Git snapshot, exact writable files and named test commands. Claude requests operations in structured reports; the controller performs them and returns receipts. Claude's native tools and MCP remain disabled. Ordinary supplied-text delegation continues to work.

**Version 0.8 adds explicit Claude effort settings.** Pin `low`, `medium`, `high`,
`xhigh` or `max` when creating a session or assignment; continuations reuse the
stored value. Review workflows accept an independent `--reviewer-effort`. Existing
unspecified settings stay unspecified. See [execution settings](docs/execution-settings.md).

**Version 0.9 connects the bounded plan, controlled edit and frozen-review stages.**
An explicit composition waits for exact plan acceptance, gives that immutable plan
to a private editor workspace, creates an independent read-only Fable reviewer
from the frozen result, and then waits for exact final acceptance. See
[compositions](docs/compositions.md).

See the [real-project pilot](docs/real-project-pilot.md) for observed timeout and
report-format failures, the verified input-validation fix, and the next reliability gates.

## What you can do

| Capability | Behavior |
|---|---|
| Delegate by role | Inject versioned role instructions and a structured assignment; resolve an explicit `sonnet` or `fable` model. |
| Work in parallel | Run independent conversations within a configurable limit; the default is two. |
| Continue a conversation | Send a follow-up to its exact managed session ID. |
| Inspect progress | Observe several runs together, or read individual status, bounded logs, and results. |
| Inspect structured reports | Check task identity and report format while leaving content acceptance to Codex. |
| Track task revisions | Keep immutable inputs and structured reports across follow-up turns. |
| Record review and approval | Bind criterion evidence to the exact revision, run and result digest. |
| Queue next-turn instructions | Select stored messages explicitly when revising; keep every delivery attempt. |
| Hand off a result | Copy a verified task report with its exact source run and digest. |
| Queue dependent tasks | FIFO admission, exact parent acceptance gates and a bounded dispatcher. |
| Run bounded review workflows | Independent Fable reviews, limited worker revisions and explicit Codex acceptance. |
| Edit a private snapshot | Explicit file policy, full-file writes and named checks in a disposable Linux sandbox. |
| Review frozen changes | Read-only Fable snapshot, verified patch and manifest, then explicit Codex acceptance. |
| Compose plan, edit and review | Persist exact stage provenance while keeping both Codex acceptance gates explicit. |
| Resume coordination | Discover budgets, blocked tasks and unknown runs with `overview --attention`. |
| Read task events | Persist revision, queue, execution and decision changes with a replay cursor. |
| Upgrade existing state | Explicit offline schema-3/4/5/6/7 migration with verified backups and crash recovery. |
| Cancel one task | Stop an owned run while other sessions continue. |
| Recover deliberately | Reconcile uncertain runs or explicitly start a new backend conversation. |
| Check execution evidence | Validate the reported model, session ID, exit status, and result integrity. |

Each installation controls Claude **on the same host and under the same user**. Installing the package on several machines creates independent local controllers.

## Before you start

- **Linux and Python 3.10 or newer.** Windows and macOS are not supported by this release.
- **For workspace operations:** Git and Bubblewrap with usable unprivileged user/PID/network namespaces. `workspace doctor` tests the required isolation. No unconfined fallback or automatic installation is provided.
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
  --effort medium \
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

For role presets and a structured output contract, use `roles`, then `delegate
--assignment-file <file> --request-id <id>`. Collect its report with `report
--run <run-uuid>`. `observe --run <id> --run <id> --seconds 30` watches selected
executions together. The [structured delegation guide](docs/cli.md#delegate-with-a-role-and-an-output-contract)
includes a complete assignment example. Existing `start --role` remains a
metadata label; it does not inject preset instructions.

For work that needs revisions and approval, use the [task lifecycle](docs/tasks.md):
`task create → task submit → report → task review / task accept`. Use
`task revise` followed by `task submit` to continue the same conversation under
a new structured contract. Creation alone makes no model call.

The role and reporting design draws on an analysis of OMX, with a smaller
host-local implementation. See [what we adopted and deferred](docs/omx-adoption.md).

## What stays on your machine

Controller configuration and records live in `$XDG_STATE_HOME/claude-control`, or `~/.local/state/claude-control` by default. They include prompts, responses, session IDs, process metadata, and logs, and use user-only permissions. Claude also maintains its own normal local session history.

**Local control does not mean local model inference.** Claude Code sends model requests to the Claude service using your existing authentication. The controller does not provide cross-host dispatch or add a separate telemetry service.

The project-path allowlist controls the Claude working directory. Workspace checks separately use a disposable Bubblewrap namespace with no host home, controller state, network or GPU devices. Process resource limits are per-process, not an aggregate cgroup quota. Administrator-managed Claude policy still applies. See [workspace boundaries](docs/workspaces.md) and [architecture](docs/architecture.md).

## Update

Finish or stop managed runs before updating:

```bash
git pull --ff-only
python3 install.py --update
```

Open a new Codex task after reinstalling. Updates preserve the separate runtime store. New stores use schema 10. Existing schema-3/4/5/6/7/8/9 stores require explicit `migrate --offline` for all new features; see [migration and recovery](docs/tasks.md#schema-and-migration). Updates do not migrate a live store automatically.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

The automated suite uses fake Claude processes and makes **no model calls**. It covers concurrency, request deduplication, cancellation, failure recovery, stream validation, relocation, installation rollback, task concurrency, stale-approval rejection, and migration interruption/recovery. Separate live integration checks have exercised parallel models, isolated conversation recall, targeted cancellation, and resume. Raw live logs are not included in this repository.

To deliberately run the live suite using your own Claude account, see [testing](docs/testing.md).

## Current scope

Version 0.9 supports Linux, supplied-text tasks, bounded controller-mediated workspace operations, explicit plan/edit/review compositions, and sessions created by this controller. Native file/shell tools in Claude, adopting arbitrary existing sessions, conversation forking, Windows/macOS support, an MCP adapter, cross-host dispatch, and automatic Codex wake-up are future work.

Maintained by [kimstitute](https://github.com/kimstitute). This is an independent project, not an official OpenAI or Anthropic integration.

## Queue work for dispatch

Use `task enqueue` to register ready or dependent tasks, then `dispatch --once` or
`dispatch --until-idle --max-seconds 60` to admit them within available slots.
Parents require exact-result Codex acceptance before children can start.
See the [queue guide](docs/queue.md) for dependencies, events and restart recovery.

## Deliver instructions on the next turn

`message enqueue` stores instructions while a task runs. Select message IDs with
`task revise --message-id`, then submit or queue the new revision. The active
Claude process is never interrupted or fed hidden input. You can also attach a
verified result as a frozen handoff. See [messages and handoff](docs/messages.md).

## Bounded review workflows

Use `workflow create/run/status/stop` to coordinate a proposal and independent Fable review with persistent call, revision and time limits. Approval recommendations wait for Codex acceptance. See the [workflow guide](docs/workflows.md).

## Controlled workspace work

Use `workspace create/task/run/status/export/stop` for a bounded edit-and-check loop. A committed Git snapshot is copied into private storage; dirty and untracked source files are excluded. Named checks run against disposable copies, and a completed result is frozen for review. `workspace create --from-snapshot` creates an independent read-only Fable review workspace. Source integration remains an explicit Codex action. See the [workspace guide](docs/workspaces.md) for policy and recovery examples.

## Composed plan, edit and review

Use `composition create/run/status/stop` to connect a bounded P4 plan, exact plan
acceptance, a controlled P5 editor, an automatic read-only Fable review of its
frozen export, and exact final acceptance. The coordinator remains finite and
never retries or accepts work itself. See the [composition guide](docs/compositions.md).
