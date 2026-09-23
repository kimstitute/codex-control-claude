# Architecture and boundaries

## Components

```text
plugins/claude-control/
  .codex-plugin/plugin.json       Plugin identity and presentation
  assets/logo.png                Project mark
  skills/claude-control/         Codex delegation workflow
  scripts/claude_control_cli.py   Portable entrypoint
  scripts/claude_control/
    cli.py                      JSON command interface
    store.py                    Configuration, SQLite state, admission, recovery
    runner.py                   Worker and owned process-group lifecycle
    protocol.py                 Claude invocation and response validation
    assignments.py              Versioned role prompts and assignment/report contracts
    orchestration.py            Multi-run observation and report inspection
```

The plugin resolves its code relative to its own directory. Runtime configuration, prompts, transcripts, and databases live outside the plugin package.

## A request's lifetime

1. Codex supplies a bounded prompt file, model, role, project, and stable request ID.
2. A SQLite transaction checks host identity, request deduplication, session availability, and the shared concurrency limit.
3. A dedicated local worker claims the run. An exec wrapper records its process identity before starting Claude.
4. Claude runs with the fixed supplied-text profile and an explicit conversation ID.
5. The worker validates the response, cleans up its owned process group, writes the result atomically, and records its digest and terminal state.
6. Codex retrieves the result and verifies its content before using it.

There is no always-running coordinator. Each active job has a worker, and later Codex tasks can discover persisted records. This package does not automatically wake Codex when a chat turn ends.

## Identity and concurrency

`delegate` normalizes a supplied JSON assignment, resolves its role/model, and
passes one canonical JSON envelope through the same reservation and execution
path as `start`. It adds no second task state machine, lease, or scheduler.
See [OMX adoption notes](omx-adoption.md) for the source analysis and later phases.

- A store is bound to its host and user, with a separate installation UUID.
- A managed session has a fixed model, role, and project, and a current backend conversation ID.
- Every run preserves its own backend ID, including after an explicit restart.
- Transactions and a uniqueness constraint allow at most one active run per session.
- Pending, running, stopping, and uncertain executions count toward the concurrency limit.
- A matching duplicate request returns its original run. Conflicting reuse is rejected.

## Process control

Workers record process IDs alongside Linux start ticks, boot identity, and PID namespace. Cancellation is a durable request handled by the owning worker; the public CLI does not kill an arbitrary saved PID.

The worker keeps the process-group leader unreaped while signalling and checking group cleanup. A Linux parent-death signal and the exec wrapper address launch/worker-death races. If the controller cannot establish the execution's outcome, it records `unknown` and requires reconciliation rather than automatically retrying.

These mechanisms manage the owned process group. They are not a cgroup sandbox and do not contain a process that deliberately escapes the group. Project allowlists constrain the selected working directory, not all filesystem access.

## Claude execution profile

The controller requests safe mode, empty setting sources, no built-in tools, disabled slash commands, and strict empty MCP configuration. It uses explicit `--session-id` or `--resume`; it never uses the most recent conversation as an implicit target or specifies a fallback model.

Administrator-managed policy still applies. Live verification has checked that a project SessionStart hook did not run under this profile. That is a tested case, not a claim that the controller overrides every possible administrator configuration.

The environment passed to Claude is limited. Normal HOME-based authentication is supported; custom Claude configuration locations and API-key variables are not forwarded. The local controller does not proxy authentication or provide offline model inference.

## Result validation

Success requires a coherent bounded JSON stream, a matching session ID, actual assistant model evidence matching the requested family, a successful final result, no tool calls, and process exit zero. Initialization metadata and auxiliary model usage alone do not prove which model answered.

Results are written through atomic replacement with file/directory synchronization and a stored SHA-256 digest. Status reads and follow-up admission check artifact integrity. These checks detect missing or changed result files; they do not defend against a malicious user who can rewrite the entire store.

Structured reports use `claude-control.assignment.v1`. The role snapshot is
canonical ASCII JSON (also UTF-8), written with no added newline and delivered
through stdin. Report inspection reconstructs the original fresh-session intent
fingerprint from that prompt and the immutable session/run fields. Validation
uses the saved role, never today's role catalog. Unknown protocol versions are
unsupported; changed snapshots fail closed. A future change to the intent-hash
algorithm needs explicit compatibility handling or old reports will fail closed.

Result bytes are checked again during report reading to avoid trusting a stale
status check. Format validity and an agent's self-reported completion never
change execution state or grant semantic acceptance. Legacy v1 reports remain unreviewed. Version 0.3 adds an append-only decision ledger
for explicitly tracked task revisions, without changing v1 interpretation.

## Task storage and contracts

Schema 4 adds tasks, immutable task revisions, explicit task-run contract links,
operation deduplication and append-only review decisions. A revision stores its
canonical final prompt and digest, fixed role instructions/model/profile, and the
expected parent run/backend. `task submit` checks this context and reserves the
run, session, task link and operation response in one `BEGIN IMMEDIATE` transaction.
The worker starts only after commit. Uncommitted artifact directories are inert: a
worker requires a committed run record and a successful status claim.

Task reports use `claude-control.task.v2` and identify the task UUID and revision.
Their explicit DB binding works for every structured follow-up, independently of
legacy first-run detection. Approval checks the exact stored result bytes, actual
model/session evidence, report status, current revision and run, and criterion
evidence in one write transaction. Review recommendations alone grant no approval.
Reviewer labels are audit metadata, not authenticated security principals.

Task state is derived from persisted revisions, runs and decisions, avoiding a
second completion state machine. `queued/awaiting_submit` requires explicit submission;
queued entries use explicit bounded dispatch. Retry is explicit and limited to provably unstarted terminal
attempts. Unknown executions retain their slots and cannot be retried.

Migration is explicit and offline: verified SQLite backup, durable `migrating` marker,
transactional DDL/user_version, config commit and journal completion. Supported clients
share a lifecycle lock and check DB/config versions; old clients must be stopped.
Interrupted migration resumes from its journal. No automatic downgrade or deletion
is used to recover. See [the task guide](tasks.md#schema-and-migration).

## Installation

`install.py` uses Codex's bundled personal-marketplace helpers and `codex plugin add`. A host-local file lock serializes installations. The new tree is staged and validated before replacement; the previous tree is retained for rollback. Runtime state is neither packaged nor removed by updates. Optional viewer and Windows supervisor binaries are supplied locally with an exact SHA-256; update preservation revalidates their manifests and bytes.

`ccc-viewer` is a separate Rust/Ratatui presentation process. It never opens the
controller database. The Python controller streams content-free AG-UI JSONL from
the observation ledger; the viewer folds those events deterministically, projects
bounded agent cards and renders live or historical state. This keeps ledger schema,
privacy filtering and mutation authority in the Python controller.

The public repository uses only the maintainer's GitHub handle. Machine-specific implementation notes, account records, credentials, and raw live-session artifacts are intentionally absent.

## Durable scheduling

Schema 5 adds immutable dependency sets, FIFO queue entries, frozen execution inputs
and append-only events. `BEGIN IMMEDIATE` serializes admission across coordinators;
per-candidate savepoints keep failed checks from consuming a run. Parent acceptance,
result integrity, final input size and run/task/queue links commit atomically.
The worker launches after commit. Death before launch is resolved by claim expiry
and explicit retry, never by automatic replay. Unknown runs keep capacity.

Queued prompts use `claude-control.task.v3`, carrying the v2 base and copied approved
parent reports with provenance. A child history validates those frozen bytes without
reopening parent files. Parent revision changes before admission block the child.
`dispatch --until-idle` is stateless and finite; existing workers outlive its deadline.
Run status and queue/decision events are recorded in the same database transaction.
See [queue semantics](queue.md).

## Message selection and delivery

Schema 6 adds immutable messages, revision selections and ordered binding receipts,
plus an explicit cancellation ledger. Composite foreign keys bind every receipt
to its exact message, task revision and run. Source reports are verified and copied
at enqueue; later reads do not reopen source artifacts.

Selection uses the existing revision transaction, leaving the v2 base definition
unchanged. At reservation, selected messages extend the materialized input into
contract `claude-control.task.v4`; dependency reports, size checks, execution input,
run reservation and receipts commit together. Inspector checks the base round-trip,
frozen message payloads, selection and run bindings. Explicit unstarted retries
append receipts with identical input; failed executed turns require a deliberate
new revision/redelivery. Worker execution and reconciliation remain unchanged.

Only current revisions can reserve. Historical unsubmitted selections are retained
for audit and never treated as live dispatch authority. Events use monotonic cursor
IDs; timestamps are observational, not an ordering or delivery guarantee.

## P4 workflow template

Schema 7 adds workflows, immutable steps and reservation receipts. Frozen policy fixes worker/reviewer tasks, criterion IDs and call/revision/time budgets. The coordinator uses existing task transactions, P3 messages and scoped P2 admission. All admissions recheck policy at commit; one receipt per step counts even failed/unstarted/unknown attempts.

Task v5 extends the execution snapshot with workflow policy and exact source
identity. It references the frozen source report by its P3 message ID, so the
model receives one copy. The structured review extension validates every criterion
and target digest. Template-only reported edges require complete intact results;
ordinary dependency acceptance remains unchanged. Review recommendations never
create Codex acceptance. Stop durably closes admission before requesting
cancellation, with unknown execution quarantined until reconciliation. See
[workflows](workflows.md).

## P5 controller-mediated workspaces

Schema 8 adds durable creation intents, immutable policy/task bindings, run snapshots, operation intents and receipts, command launcher identities and final export hashes. Creation reserves its UUID before filesystem writes. The task v6 envelope carries the policy, tree inventory, prior receipts and remaining budgets; the unchanged Claude runner still rejects native tool use.

The finite workspace coordinator validates the whole requested batch before recording intents. Each operation produces a receipt; interrupted operations stop for Codex inspection and are never replayed. Named checks share execution capacity with model runs. Their internal exec wrapper persists its PID identity before entering Bubblewrap; reconciliation rejects live owners/launchers and late wrappers cannot start after a receipt is recorded. Stop recovery repeats owned-run cancellation before declaring the workspace stopped.

Git blobs are read without checkout hooks or filters. Source files stay unchanged; exact allowed files can be edited in a private tree. Checks receive disposable copies and fixed system-runtime mounts, with isolated user/PID/network namespaces and sanitized environment. Time/output limits and per-process rlimits are bounded; no aggregate cgroup quota is claimed. Final trees, patches and manifests are revalidated before export and task acceptance. A read-only Fable workspace can review the exact frozen output. See [the workspace guide](workspaces.md).
