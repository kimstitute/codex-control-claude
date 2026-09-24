# Task lifecycle

This document describes the P1 task workflow added to `claude-control`: creating,
submitting, reviewing, and accepting delegated work, plus the one-time migration
required to enable it. Examples use the `claude_control` shell function from
the [CLI reference](cli.md). Assignment files use the existing
[assignment schema](cli.md#delegate-with-a-role-and-an-output-contract).

## Schema and migration

Task lifecycle requires schema 4 or newer; queues require schema 5, messages schema 6, workflows schema 7, and workspaces schema 8.
Explicit effort settings require schema 9; plan/edit/review compositions require schema 10,
run telemetry schema 11, guarded workspace apply schema 12, portable platform
execution identities schema 13, durable observation history schema 14, and the
content-free controller-operation projection schema 15.
New installations (`claude_control init`) start on schema 15. Existing schema-3 stores must migrate before
using any `task` subcommand; a schema-3 store rejects task commands until migrated.

Migration is a maintenance operation, not a background service:

1. Finish or stop managed runs. Inspect/reconcile any unknown execution from its recorded PID namespace. Unresolved executions block migration.
2. Stop old CLI clients and workers. Do not run different controller versions concurrently during migration.
3. Install the new code. Schema-3 stores retain legacy diagnostic/stop/reconcile commands, but task commands require migration.
4. Inspect status, then explicitly upgrade the same state directory:

```shell
claude_control --state-dir /srv/project/.claude-control migrate --status
claude_control --state-dir /srv/project/.claude-control migrate --offline
```

Migration proceeds through every schema from 3 to 15, creating and validating a `schema-<source>-backup.sqlite3`
for each required step using SQLite's
backup API, including committed WAL data. It marks configuration as `migrating`,
changes the database in a transaction, then finishes configuration and the journal.
Normal commands refuse partially migrated stores. `--offline` acknowledges that
all old clients are stopped; it is not a force option for active or unknown runs.
New-version database operations also participate in an exclusive migration lock.
If an old client violates maintenance before DDL begins, the upgrade aborts and
reopens schema-3 diagnostics without changing any run state. Resolve that work
and repeat migration. This does not make mixed-version operation supported.

After an interrupted migration, run the same `migrate --offline` command against
**the original state directory**. The saved journal, DB version and private backup
allow it to finish safely. Do not initialize another store, replace the live DB
with a `.sqlite3`-only file copy, or run a model job against the backup. No automatic
downgrade is provided after upgraded-schema operation begins. Schemas 1 and 2 are unsupported.

`migrate --status` reads the current versions, active run IDs and migration journal;
it makes no model calls. A schema/version mismatch blocks normal commands.

## Task lifecycle

A task holds an assignment (role, model, project, prompt, acceptance criteria)
across one or more immutable **revisions**. Each revision can be **submitted**
to produce one or more **runs**; a completed run is **reviewed** and then
explicitly **accepted**, or the task is **revised** and resubmitted.

### 1. Create

```shell
claude_control task create \
  --assignment-file /srv/project/assignments/refactor-auth.json \
  --operation-id create-refactor-auth-001
```

To attach to an existing managed session, provide both `--session <SESSION_UUID>`
and `--parent-run <RUN_UUID>` identifying its exact latest completed/terminal turn.
Role, model and canonical project must match. After an unsuccessful parent turn,
inspect its partial conversation and use `--acknowledge-context` deliberately.
A later legacy follow-up or restart blocks submission with `context_changed`.
Changes made by resuming Claude outside this controller are not detectable. `<SESSION_UUID>`, `<RUN_UUID>`, and similar angle-bracket
names throughout this document are placeholders, not literal ids to type in.
The response includes the new task's id and its first revision.

### 2. Submit (and retry)

```shell
claude_control task submit \
  --task <TASK_ID> --revision <REVISION> \
  --operation-id submit-refactor-auth-001
```

`submit` reserves a run for a revision and launches a worker; repeating the same
`--operation-id` returns the original run unchanged (`"deduplicated": true`) and
never launches a second worker. If the submitting process died after commit but
before launch, that reservation expires; inspect it and use an explicit `retry`
with a new operation ID. Replaying the old operation does not repair the launch. `retry` takes the same arguments but is only
accepted for attempts that provably never started, for example a reservation
that expired before the worker began. It is never accepted for an attempt with
an unknown outcome or one that actually ran and failed:

```shell
claude_control task retry \
  --task <TASK_ID> --revision <REVISION> \
  --operation-id retry-refactor-auth-001
```

### 3. Inspect

```shell
claude_control task list
claude_control task show --task <TASK_ID>
claude_control report --run <RUN_ID>
```

`task show` returns the task's state, state reason, every revision with its
stored immutable prompt and criteria, the runs against each revision, and the
decision history. `report` (the pre-existing, root-level command) still works
for any run, including task runs, and recognizes the v2 task turn report shape.

### 4. Review

Once a run finishes, a reviewer records findings against each acceptance
criterion in an evidence file. Evidence maps every 1-based criterion id to one
or more typed items, for example
`/srv/project/evidence/refactor-auth-run1.json`:

```json
{
  "1": [{"type": "free_text", "text": "Compared the result with the requirements."}],
  "2": [{
    "type": "check_receipt",
    "run_id": "<workspace-run-uuid>",
    "seq": 0,
    "receipt_sha256": "<sha256-of-the-canonical-receipt>"
  }]
}
```

The file must be at most 1 MiB and, for `accept`, must cover every criterion
required by the revision. Supported types are `free_text`, `check_receipt`,
`diff_hunk`, and `review_result`. The last three are checked against immutable
workspace receipts, frozen manifests, or structured review runs. A legacy string
is normalized to a `free_text` item when recorded. Unknown fields, empty items,
changed digests, and non-numeric criterion keys are rejected as `invalid_evidence`.

```shell
claude_control task review \
  --task <TASK_ID> --revision <REVISION> --run <RUN_ID> \
  --result-sha256 <RESULT_SHA256> \
  --evidence-file /srv/project/evidence/refactor-auth-run1.json \
  --operation-id review-refactor-auth-run1 \
  --reviewer codex-reviewer \
  --recommendation approve
```

`--recommendation` is one of `approve`, `revise`, or `blocked`. A review only
records a recommendation and does not change task state on its own.

### 5. Accept or revise

Acceptance is a separate, explicit decision: a worker reporting success is
never sufficient by itself.

```shell
claude_control task accept \
  --task <TASK_ID> --revision <REVISION> --run <RUN_ID> \
  --result-sha256 <RESULT_SHA256> \
  --evidence-file /srv/project/evidence/refactor-auth-run1.json \
  --operation-id accept-refactor-auth-run1
```

If the work needs changes instead, create a new immutable revision from the
same task. Role, model, and project stay fixed; only the prompt and criteria
change:

```shell
claude_control task revise \
  --task <TASK_ID> --revision <REVISION> \
  --assignment-file /srv/project/assignments/refactor-auth-v2.json \
  --parent-run <RUN_ID> \
  --operation-id revise-refactor-auth-002
```

`revise` only creates the new revision; it does not submit it. Submit the new
revision separately with `task submit` once it is ready. If all stopped runs on
the reserved backend prove Claude never executed, submission uses that same
backend UUID with `--session-id` instead of resuming a nonexistent transcript.
Once there is execution evidence, it never silently resets the conversation.

## Operation ids and `--revision`

Every mutating task command takes `--operation-id`. Reusing the same operation
id with the same inputs returns the original result idempotently; reusing it
with different inputs is rejected as a conflict. `revise`, `submit`, and
`retry` also take `--revision`, naming the revision the caller believes is
current, so a mutation against a stale revision is rejected rather than
silently applied to work that has since moved on.

## Failure handling summary

- A run that fails to start (for example an expired reservation) can be
  retried with `task retry`.
- A run with an unknown outcome, or one that started and failed during
  execution, must not be retried automatically; it requires investigation. Unknown execution must be reconciled before any new turn;
  a stopped failed turn needs a new revision and explicit context acknowledgement.
- `task review` records evidence and a recommendation but never changes task
  state by itself.
- Only `task accept`, an explicit human/Codex decision, marks a revision's
  work as accepted.

Transient result read errors (for example temporary descriptor exhaustion) report
`result_unreadable` / `needs_attention` without permanently changing a completed
run to failed. Missing or altered result artifacts still fail integrity checks.

## Not yet supported

Task management remains text-only and single-host. The [queue guide](queue.md)
covers dependent work and bounded dispatch. Autonomous review loops, worker file/shell
tools and cross-host execution are not supported.
Existing v1 commands (`start`, `followup`, `resume`, `restart`, `status`,
`result`, `report`, `logs`, `stop`, `reconcile`, `wait`, `observe`,
`delegate`) and their reports are unchanged by this slice.

## Next-turn instructions

`task revise` also accepts repeated `--message-id` and the explicit
`--redeliver-messages` flag. See [messages](messages.md) for selection, cancellation,
source handoff and delivery receipt semantics. Without selected messages, existing
v2/v3 contracts remain unchanged.

### Schema 8 to 9

The offline upgrade adds nullable `effort` fields to sessions and runs, plus
constraints enforcing their immutable values. Existing rows retain their rowids,
prior fields, prompt bytes, hashes and decisions. NULL means the controller did
not select an effort; it is not a known model default. The migration creates a
verified `schema-8-backup.sqlite3` and resumable `migration-8-9.json` journal.
Queued revisions remain unchanged and omit the CLI flag when dispatched.
See [execution settings](execution-settings.md) before opting into new settings.
