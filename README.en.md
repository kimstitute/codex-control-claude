<p align="center">
  <img src="plugins/claude-control/assets/logo.png" alt="Codex Control Claude logo" width="128">
</p>

<h1 align="center">Codex Control Claude</h1>

<p align="center">Let Codex coordinate persistent local Claude Code sessions.</p>

<p align="center">
  <strong>Linux · Windows 10/11</strong> · <strong>Python 3.10+</strong> · <strong>Standard-library runtime</strong>
</p>

<p align="center">
  English · <a href="README.md">한국어</a> · <a href="docs/cli.md">CLI reference</a> · <a href="docs/architecture.md">Architecture</a> · <a href="CHANGELOG.md">Changelog</a>
</p>

Codex Control Claude is a Codex plugin and local controller for delegating work
to Claude Code. It keeps conversations, runs, task revisions, evidence and
approval decisions in a durable host-local store. Each role can use a Sonnet,
Opus, Haiku or Fable family alias or an exact versioned model ID with an optional
effort level while preserving the exact conversation session.

Version 0.9 adds an explicit **plan → review → accept → edit → frozen review →
accept** composition. It connects the existing bounded planning workflow to a
private controlled workspace without adding automatic acceptance, retry, merge,
push or cross-host delivery.

Version 0.10 adds Claude Code `--json-schema` for structured tasks, base-hashed
hunk patches, one side-effect-free format repair, final-reviewer vetoes, and
ledger-verified acceptance evidence.

Version 0.11 passes a live Sonnet edit/check/freeze/Fable-review pilot and adds
raw provider usage, cost and latency telemetry, explicit HEAD-pinned
`workspace apply`, and a read-only Sonnet scout before planning.

Version 0.12 adds a live terminal monitor for the agent/task/workspace/workflow/
composition graph, run history, token usage and provider-reported cost.

Version 0.13 adds Codex, Claude Code, Gemini CLI and Cursor CLI account limits
to the same TUI. It preserves provider-reported percentages, resets, token or
request units and overage spending, and never estimates an unpublished token
ceiling.

Version 0.14 adds same-host Claude sessions, tasks, workflows, compositions,
provider limits and an ANSI/VT TUI on Windows 10/11. A SHA-256-pinned
`ccc-win-supervisor.exe` owns native sessions. Windows workspaces run only through
Bubblewrap inside WSL2. AppContainer and an unconfined fallback are excluded.

Version 0.15 adds immutable Codex leader specifications, frozen tests that the
editor cannot write, final-tree check gates and read-only composition evaluation.

Version 0.16 records task type, risk, routing policy, model-selection reason and
escalation lineage; fairly advances multiple compositions within the global
concurrency limit; and optionally reuses exact, reverified Sonnet scout results.

Version 0.17 adds host-local per-role model and effort settings, exact version ID
pinning, and strict verification of the provider-reported model identity.

## Choose the right workflow

| Goal | Use | What it adds |
|---|---|---|
| Ask one quick question | `start` | A persistent unstructured Claude session |
| Get one structured answer | `delegate` | Role instructions and a validated report contract |
| Revise and approve a task | `task` | Immutable revisions, exact result hashes and acceptance evidence |
| Generate and critique a plan | `workflow` | A bounded worker plus an independent Fable critic |
| Let Claude edit selected files | `workspace` | Private Git snapshot, exact write paths and named checks |
| Plan, implement and review | `composition` | P4 planning plus P5 editing and frozen Fable verification |
| Schedule dependent tasks | `task enqueue` + `dispatch` | FIFO admission and exact accepted-parent gates |
| Save instructions for the next turn | `message` | Explicit next-revision delivery and result handoff |
| Observe agents and account limits | `monitor tui` | Role/model/work graph, run tokens and Codex/Claude/Gemini/Cursor limits |
| Configure model and effort per role | `models show/configure/reset` | Aliases, exact version IDs, atomic replacement and frozen existing work |

If you use Codex interactively, ask Codex to apply the installed
`$claude-control` skill. Use the CLI directly when you want to inspect or operate
the controller yourself.

```bash
claude_control monitor tui
claude_control monitor limits
```

See the [live monitor guide](docs/monitor.md) for keys and token semantics.
See the [model settings guide](docs/models.md) for per-role defaults, exact
version pinning and assignment overrides.

## Safety model

- Each installation controls Claude Code on the **same host and under the same
  operating-system user**.
- Claude native tools and MCP are disabled by the controller. Supplied-text
  tasks cannot read a path merely because the prompt names it.
- Controlled edits happen in a private copy of a committed Git tree. The source
  repository is not changed automatically.
- Workspace checks are selected by name from a fixed policy and run in a
  Bubblewrap sandbox. Claude cannot provide arbitrary shell commands.
- A completed model response, a review recommendation and Codex acceptance are
  separate states.
- Failed, malformed, timed-out or unknown runs are not retried automatically.
- There is no resident scheduler. `workflow run`, `workspace run`,
  `composition run` and `dispatch` advance work only when explicitly invoked.
- Runtime state, prompts, responses and session identifiers stay outside the
  plugin source tree.

Local control does not mean local model inference. Claude Code sends model
requests to the Claude service through your existing login.

## Requirements

### Common

- Linux or Windows 10/11
- Python 3.10 or newer
- Claude Code installed and signed in for the same user
- Codex CLI with plugin support and its bundled `plugin-creator` helpers
- A Python 3.10+ interpreter with PyYAML for plugin validation during install

The controller runtime itself uses only the Python standard library. The
installer does not install missing packages.

### Linux workspace and composition workflows

- Git
- Bubblewrap (`bwrap`)
- Usable unprivileged user, PID and network namespaces

### Windows

- An architecture-matching `ccc-win-supervisor.exe` and its published SHA-256
- WSL2 and `bwrap` inside the selected WSL distribution for workspaces/compositions
- A private ext4 state path outside `/mnt` for WSL workspace state

Native Windows sessions do not require WSL2. See the [Windows guide](docs/windows.md)
for the exact setup and support boundary.

Run `workspace doctor` before the first controlled edit. The controller refuses
workspace work if isolation is unavailable; it has no unconfined fallback.

Supported family aliases are `sonnet`, `opus`, `haiku`, and `fable`; exact
`claude-...` model IDs pin a version. Availability depends on your Claude account.
The controller reports an error instead of silently substituting another model.

## Install

```bash
git clone https://github.com/kimstitute/codex-control-claude.git
cd codex-control-claude
python3 install.py
```

On Windows, verify and pin the native helper during installation:

```powershell
$sha = (Get-FileHash .\ccc-win-supervisor.exe -Algorithm SHA256).Hash.ToLower()
py -3 install.py --windows-helper .\ccc-win-supervisor.exe --windows-helper-sha256 $sha
```

A missing or mismatched helper fails closed; session control is never silently
routed through another launcher.

The installer:

1. validates the plugin;
2. copies it to `$HOME/plugins/claude-control`;
3. registers it in the personal Codex marketplace;
4. installs a cache-busted copy through Codex.

Runtime state is not stored in that plugin directory. If the helper or validator
Python is elsewhere, provide both paths explicitly:

```bash
python3 install.py \
  --plugin-creator-root /absolute/path/to/plugin-creator \
  --helper-python /absolute/path/to/python-with-pyyaml
```

Open a new Codex task after installation so the app discovers the installed
skill.

## Initialize one host

Find the Claude executable and choose the project roots this controller may use:

```bash
command -v claude

python3 "$HOME/plugins/claude-control/scripts/claude_control_cli.py" init \
  --claude-bin /absolute/path/to/claude \
  --allow-root /srv/my-project \
  --max-parallel 2 \
  --max-queued 100
```

Repeat `--allow-root` to authorize several roots at initialization. Paths passed
later as assignment projects must be inside one of these roots.

Verify the controller and login:

```bash
python3 "$HOME/plugins/claude-control/scripts/claude_control_cli.py" doctor --auth
python3 "$HOME/plugins/claude-control/scripts/claude_control_cli.py" workspace doctor
```

`doctor --auth` deliberately excludes account identifiers. A healthy result has
`"ready": true`. It verifies the CLI and login, but a particular model is proven
available only by an actual explicit model request.

For shorter examples, define a shell function:

```bash
claude_control() {
  python3 "$HOME/plugins/claude-control/scripts/claude_control_cli.py" "$@"
}
```

All public operation commands print JSON.

## Use it from Codex

In a new Codex task, describe the work and name the skill:

> Use $claude-control. Give Sonnet a bounded implementation task, then ask an
> independent Fable session to verify the result. Preserve both session IDs and
> do not accept or apply anything until you inspect the exact outputs.

For the complete v0.9 flow:

> Use $claude-control composition for this repository. Let Fable review the
> plan, wait for my/Codex's explicit plan acceptance, let Sonnet edit only the
> authorized files and run the named checks, then create a read-only Fable
> review from the frozen result. Do not merge or push automatically.

Codex should manage session IDs, result digests and approval evidence. A Claude
process exiting successfully proves execution, not correctness.

## First CLI example: one persistent conversation

Create `/tmp/claude-task.txt` with the context Claude needs. `start` does not
grant file access, so include relevant source text when needed.

```bash
claude_control start \
  --name parser-review \
  --model sonnet \
  --effort medium \
  --role implementer \
  --project /srv/my-project \
  --prompt-file /tmp/claude-task.txt \
  --request-id parser-review-001 \
  --timeout 300
```

Save both returned identifiers:

| JSON field | Meaning | Used by |
|---|---|---|
| `id` | One execution/run | `status`, `wait`, `logs`, `result`, `stop`, `reconcile` |
| `session_id` | Controller-managed conversation | `followup`, `resume`, `restart` |
| `backend_id` | Claude's persisted conversation identity | Evidence and diagnosis only |

Inspect and collect the run:

```bash
claude_control status --run <run-uuid>
claude_control wait --run <run-uuid> --seconds 30
claude_control logs --run <run-uuid> --stream stderr --bytes 8192
claude_control result --run <run-uuid>
```

Continue the exact conversation after it becomes idle:

```bash
claude_control followup \
  --session <managed-session-uuid> \
  --prompt-file /tmp/followup.txt \
  --request-id parser-review-002
```

`resume` is an alias for `followup`. The session's model and effort are pinned.
Use a unique request ID for each logical mutation. Repeating the same request ID
with identical input is idempotent; reusing it with changed input is rejected.

## Structured one-shot delegation

Use `delegate` when you want role instructions and a machine-validated report.
Save this as `/tmp/parser-review.json`:

```json
{
  "id": "parser-review",
  "name": "Parser review",
  "role": "critic",
  "model": "fable",
  "project": "/srv/my-project",
  "objective": "Review the supplied parser behavior for ambiguous inputs.",
  "context": "Paste the relevant requirements, source text and test evidence here.",
  "scope": ["Only the supplied parser and tests"],
  "acceptance_criteria": [
    "Identify concrete failure cases",
    "Separate verified facts from assumptions"
  ],
  "deliverable": "A concise review with regression-test proposals.",
  "effort": "high",
  "timeout": 300
}
```

```bash
claude_control roles
claude_control delegate \
  --assignment-file /tmp/parser-review.json \
  --request-id parser-review-delegate-001
claude_control wait --run <run-uuid> --seconds 30
claude_control report --run <run-uuid>
```

Default role routing is Sonnet for `executor` and `researcher`, and Fable for
`planner`, `architect`, `critic` and `verifier`. An explicit assignment model
overrides the preset. `report` validates identity and structure; it never marks
the content accepted.

## Full v0.9 example: plan, edit and frozen review

This is the recommended path for consequential repository changes.

### 1. Start from a committed tree

The source ref is resolved to one immutable commit at composition creation.
Dirty tracked changes and untracked files are not included.

```bash
git -C /srv/my-project status --short
git -C /srv/my-project rev-parse HEAD
mkdir -p /tmp/claude-composition
```

### 2. Prepare the planning assignment

Save as `/tmp/claude-composition/plan.json`:

```json
{
  "id": "ledger-plan",
  "name": "Plan the ledger fix",
  "role": "planner",
  "model": "fable",
  "project": "/srv/my-project",
  "objective": "Produce a bounded implementation and test plan for the supplied ledger defect.",
  "context": "Paste the defect report, relevant source excerpts and constraints here. The planning stage cannot read repository paths directly.",
  "scope": ["src/ledger.py", "tests/test_ledger.py"],
  "acceptance_criteria": [
    "The plan names the exact files and behavior to change",
    "The plan defines a regression test and the named verification command"
  ],
  "deliverable": "A file-by-file implementation plan with risks and verification steps.",
  "effort": "high",
  "timeout": 600
}
```

### 3. Prepare the editor assignment

Save as `/tmp/claude-composition/editor.json`:

```json
{
  "id": "ledger-editor",
  "name": "Implement the ledger fix",
  "role": "executor",
  "model": "sonnet",
  "project": "/srv/my-project",
  "objective": "Implement the accepted plan in the controlled workspace.",
  "context": "Use only controller-mediated reads, full-file writes and named checks. The accepted plan provenance is appended by the composition.",
  "scope": ["src/ledger.py", "tests/test_ledger.py"],
  "acceptance_criteria": [
    "src/ledger.py implements the accepted behavior",
    "tests/test_ledger.py covers the regression",
    "The unit named check passes"
  ],
  "deliverable": "A complete workspace report with no pending operations.",
  "effort": "medium",
  "timeout": 600
}
```

### 4. Define exact file and check authority

Save as `/tmp/claude-composition/editor-policy.json`:

```json
{
  "version": 1,
  "role": "executor",
  "read_paths": ["src/ledger.py", "tests/test_ledger.py"],
  "write_paths": ["src/ledger.py", "tests/test_ledger.py"],
  "checks": {
    "unit": {
      "argv": ["/usr/bin/python3", "-m", "unittest", "tests.test_ledger"],
      "timeout": 60
    }
  },
  "max_actions": 12,
  "max_calls": 6
}
```

Every writable path must also be readable. Check executables must be absolute
`/usr/bin/<name>` paths. Claude may request `unit`; it cannot change the command.

### 5. Prepare the independent reviewer assignment

Save as `/tmp/claude-composition/reviewer.json`:

```json
{
  "id": "ledger-reviewer",
  "name": "Verify the frozen ledger fix",
  "role": "verifier",
  "model": "fable",
  "project": "/srv/my-project",
  "objective": "Independently assess the frozen editor result.",
  "context": "Review the frozen selected files and report evidence and limitations. Do not request writes or checks.",
  "scope": ["src/ledger.py", "tests/test_ledger.py"],
  "acceptance_criteria": [
    "src/ledger.py implements the accepted behavior",
    "tests/test_ledger.py covers the regression",
    "The unit named check passes"
  ],
  "deliverable": "A read-only verification report for Codex.",
  "effort": "high",
  "timeout": 600
}
```

The reviewer's acceptance criteria must exactly match the editor's. The
controller derives a reviewer policy with the same readable paths, no writable
paths and no checks.

### 6. Create the composition

```bash
claude_control composition create \
  --planning-assignment-file /tmp/claude-composition/plan.json \
  --editor-assignment-file /tmp/claude-composition/editor.json \
  --editor-policy-file /tmp/claude-composition/editor-policy.json \
  --reviewer-assignment-file /tmp/claude-composition/reviewer.json \
  --repo /srv/my-project \
  --ref HEAD \
  --operation-id ledger-composition-create-001 \
  --max-revisions 2 \
  --max-calls 6 \
  --dispatch-window-seconds 900 \
  --reviewer-effort high \
  --routing-metadata-file /tmp/claude-composition/routing.json
```

Creation pins the Git commit and records the P4 workflow. It makes no model
call. Save the returned composition UUID.

For a leader-authored specification, replace `--planning-assignment-file` with
`--leader-spec-file` and a Fable `--critic-assignment-file`. The optional
`--test-contract-file` checks the baseline before editor binding and opens the
reviewer only when frozen test paths are unchanged and every required check
passes on the final tree. See the [composition guide](docs/compositions.md) for
the JSON contracts and complete examples.

Reuse an exact finished scout with
`--scout-cache-assignment-file /tmp/claude-composition/scout.json`; an absent or
invalid match fails explicitly. Advance several compositions with the finite
foreground dispatcher:

```bash
claude_control composition dispatch --all --once
claude_control composition dispatch --all --until-idle --max-seconds 60
```

Evaluate recorded success, cost and routing strata with:

```bash
claude_control composition evaluate --all
claude_control composition evaluate --composition <uuid> --composition <uuid>
claude_control composition evaluate --all --stratify risk_class --stratify editor_model
```

Evaluation does not mutate the ledger or estimate missing cost or token values.

### 7. Run until plan acceptance is required

```bash
claude_control composition run \
  --composition <composition-uuid> \
  --until-idle --max-seconds 60

claude_control composition status --composition <composition-uuid>
```

Repeat the bounded `run` after inspecting status if a live call was still in
progress. The expected gate is:

```text
state:  awaiting_codex
reason: plan_acceptance_required
```

The nested workflow should show `approve_recommended`. This is a review
recommendation, not acceptance.

### 8. Inspect and accept the exact plan

Read the plan task and run identified by composition status:

```bash
claude_control task show --task <plan-task-uuid>
claude_control report --run <plan-run-uuid>
claude_control result --run <plan-run-uuid>
```

Create `/tmp/claude-composition/plan-evidence.json` with one non-empty entry for
every 1-based plan criterion:

```json
{
  "1": [{"type": "free_text", "text": "Codex verified the exact file and behavior boundaries."}],
  "2": [{"type": "free_text", "text": "Codex verified the proposed regression test and named check."}]
}
```

Accept only the exact revision, run and result digest shown by the controller:

```bash
claude_control task accept \
  --task <plan-task-uuid> \
  --revision <plan-revision> \
  --run <plan-run-uuid> \
  --result-sha256 <plan-result-sha256> \
  --evidence-file /tmp/claude-composition/plan-evidence.json \
  --operation-id ledger-plan-accept-001
```

The composition rejects this acceptance unless the independent plan review is
exactly at `approve_recommended`.

### 9. Run the editor and frozen reviewer

```bash
claude_control composition run \
  --composition <composition-uuid> \
  --until-idle --max-seconds 60

claude_control composition status --composition <composition-uuid>
```

After plan acceptance, the coordinator creates the editor from the commit pinned
at creation. A finished editor is frozen before the read-only configured reviewer is
created from that frozen tree. Continue bounded runs until status reaches:

```text
state:  awaiting_codex
reason: final_review_ready
```

No source repository file has been merged or changed by this process.

### 10. Inspect frozen evidence and accept the editor result

Composition status identifies editor and reviewer workspace UUIDs and exact
result pins. Inspect both:

```bash
claude_control workspace status --workspace <editor-workspace-uuid>
claude_control workspace export --workspace <editor-workspace-uuid>
claude_control workspace status --workspace <reviewer-workspace-uuid>
claude_control workspace export --workspace <reviewer-workspace-uuid>
claude_control report --run <reviewer-run-uuid>
```

Create `/tmp/claude-composition/editor-evidence.json` covering all editor
criteria:

```json
{
  "1": [{"type": "free_text", "text": "Codex inspected the frozen implementation against the accepted plan."}],
  "2": [{"type": "free_text", "text": "Codex inspected the frozen regression test."}],
  "3": [{"type": "free_text", "text": "Codex verified the recorded named-check receipt and reviewer evidence."}]
}
```

```bash
claude_control task accept \
  --task <editor-task-uuid> \
  --revision <editor-revision> \
  --run <editor-run-uuid> \
  --result-sha256 <editor-result-sha256> \
  --evidence-file /tmp/claude-composition/editor-evidence.json \
  --operation-id ledger-editor-accept-001

claude_control composition status --composition <composition-uuid>
```

Status derives `accepted` from that exact acceptance ledger entry and intact
editor/reviewer exports. Applying the frozen patch or copying files into the
source repository remains a separate Codex/user action.

## Understand coordinator states

| State/reason | Meaning | Safe next step |
|---|---|---|
| `active` | The named coordinator may have work to advance | Run one bounded coordinator pass |
| `awaiting_codex/plan_acceptance_required` | Reviewed plan is ready | Inspect and explicitly accept the exact plan |
| `awaiting_codex/final_review_ready` | Editor and frozen reviewer completed | Inspect both exports and accept the exact editor result |
| `awaiting_codex/final_review_revise` | Reviewer found a failed criterion | Inspect revision instructions and decide on another edit |
| `awaiting_codex/final_review_blocked` | Reviewer lacks required evidence | Supply evidence before attempting acceptance |
| `awaiting_codex/<failure>` | A failure, malformed report, budget limit or uncertainty stopped automation | Inspect the named child; do not create an automatic replacement |
| `accepted` | Exact editor acceptance exists and evidence is intact | Decide separately whether to integrate the frozen result |
| `stopping` | Owned children are being drained | Re-run `stop` or `run` to finish cleanup, then inspect |
| `stopped` | Admissions are closed and cleanup completed | Preserve records or start a deliberate new composition |

Stop a composition without deleting its evidence:

```bash
claude_control composition stop \
  --composition <composition-uuid> \
  --operation-id ledger-composition-stop-001
```

## Task approval in isolation

For work that does not need file operations, the explicit lifecycle is:

```text
task create → task submit → report/result → task review → task accept
                                      ↘ task revise → task submit
```

`task review` records `approve`, `revise` or `blocked` as a recommendation.
Only `task accept` creates acceptance, and it requires the exact task revision,
run ID, result SHA-256 and complete criterion evidence. See
[docs/tasks.md](docs/tasks.md) for revision and retry rules.

## Parallel work, queues and messages

Observe several already-started runs:

```bash
claude_control observe \
  --run <first-run-uuid> \
  --run <second-run-uuid> \
  --seconds 30
```

Queue tasks and admit ready work explicitly:

```bash
claude_control task enqueue \
  --task <task-uuid> --revision 1 \
  --operation-id enqueue-task-001

claude_control dispatch --until-idle --max-seconds 60
```

Parents satisfy dependencies only after exact Codex acceptance. Save later-turn
instructions with `message enqueue`, select their IDs during `task revise`, and
submit the new revision separately. See [queue](docs/queue.md) and
[messages](docs/messages.md).

## Stop and recover deliberately

Request cancellation of one owned run, then verify its terminal state:

```bash
claude_control stop --run <run-uuid>
claude_control wait --run <run-uuid> --seconds 30
```

An `unknown` run retains capacity because execution might still exist. Inspect
logs and records, then reconcile the same run from the owning PID namespace:

```bash
claude_control status --run <run-uuid>
claude_control logs --run <run-uuid> --stream worker --bytes 8192
claude_control reconcile --run <run-uuid>
```

Do not delete the database, initialize a second store or create a replacement
session to bypass uncertainty. A failed or cancelled conversation may have read
part of its input; inspect it before an explicit acknowledged follow-up.

## State directory and multiple hosts

The default state directory is `$XDG_STATE_HOME/claude-control`, or
`$HOME/.local/state/claude-control` when `XDG_STATE_HOME` is unset. It stores
configuration, prompts, responses, task history, process evidence and private
workspace trees with user-only permissions.

Use `--state-dir` before the subcommand to select another already-initialized
store:

```bash
claude_control --state-dir /srv/controller-state list
```

Every host has a separate store and separate Claude login/session history. This
release does not send work between machines.

## Update and migrate

Finish or stop managed work and resolve unknown executions before updating:

```bash
git pull --ff-only
python3 install.py --update
```

Open a new Codex task after reinstalling. Inspect the existing state with the new
CLI, then migrate it while all old clients and workers are stopped:

```bash
claude_control migrate --status
claude_control migrate --offline
claude_control migrate --status
```

New stores use schema 13. Existing schema 3–12 stores are upgraded step by step
with verified SQLite backups and a durable migration journal. Repeat
`migrate --offline` on the original state directory after an interrupted
migration. Never run jobs against a backup or replace the original database with
a single copied SQLite file.

## Troubleshooting

| Symptom | Meaning and next step |
|---|---|
| `doctor` reports missing Claude flags | Update Claude Code to a version that supports the listed options |
| `doctor --auth` is not ready | Complete the normal local Claude Code login, then run it again |
| Explicit model fails | Confirm the account can use that alias; there is no model fallback |
| `project_not_allowed` | Use a path inside an initialized `--allow-root`; do not create a second state store |
| `workspace doctor` fails on Linux | Fix Bubblewrap or unprivileged namespaces; checks will not run unconfined |
| Session control is disabled on Windows | Verify the architecture-matching `ccc-win-supervisor.exe` and the SHA-256 pin used during installation |
| Workspace is disabled on Windows | Verify WSL2, `bwrap` inside WSL and a private ext4 state path; state below `/mnt` is rejected |
| `capacity_full` or busy session | Inspect existing runs, wait, or intentionally stop the owned run |
| `context_changed` | Inspect the exact latest turn and create an explicit revision/follow-up with the required acknowledgement |
| `unknown` | Inspect and reconcile the original run; never retry or replace it automatically |
| `invalid_report` | Read `result`; correct the next explicit revision rather than launching an automatic repair call |
| `migration_required` | Stop clients/runs, inspect `migrate --status`, then run `migrate --offline` |
| Installer cannot import `yaml` | Pass `--helper-python` pointing to a Python 3.10+ interpreter with PyYAML |

## Documentation map

- [CLI and report contracts](docs/cli.md)
- [Task revisions, evidence and migration](docs/tasks.md)
- [Queues, dependencies and dispatch](docs/queue.md)
- [Next-turn messages and result handoff](docs/messages.md)
- [Bounded worker/reviewer workflows](docs/workflows.md)
- [Controlled workspace policy and isolation](docs/workspaces.md)
- [Plan/edit/frozen-review compositions](docs/compositions.md)
- [Live agent monitor](docs/monitor.md)
- [Execution settings](docs/execution-settings.md)
- [Architecture and trust boundaries](docs/architecture.md)
- [Testing and live-test opt-in](docs/testing.md)
- [Observed real-project pilot](docs/real-project-pilot.md)
- [OMX adoption decisions](docs/omx-adoption.md)
- [Windows setup and operations](docs/windows.md)

## Tests

```bash
python3 -m unittest discover -s tests -v
ruff check .
ruff format --check .
```

The normal suite uses fake Claude processes and makes no model calls. Live tests
are opt-in and use your own Claude account; see [docs/testing.md](docs/testing.md).

## Current scope

Version 0.16 supports supplied-text delegation, persistent sessions,
review-gated task revisions, finite queues/workflows, explicit plan/edit/review
compositions, multi-composition dispatch, routing provenance and evaluation,
verified scout reuse, and a read-only live TUI on Linux and Windows 10/11. Linux workspaces
use native Bubblewrap; Windows workspaces use Bubblewrap inside WSL2. Native
Claude file/shell tools, arbitrary existing-session adoption, conversation forks,
macOS, AppContainer, an MCP adapter, cross-host dispatch and automatic Codex
wake-up remain outside this release. The native Windows helper, Job Object and a
real tool-free Sonnet call passed on physical Windows hardware. Final
WSL2/Bubblewrap validation remains blocked by WSL-service `E_ACCESSDENIED` under
the Codex sandbox token and must run from a normal Windows terminal, so Windows
workspace support remains beta.

Maintained by [kimstitute](https://github.com/kimstitute). This is an independent
project, not an official OpenAI or Anthropic integration.
