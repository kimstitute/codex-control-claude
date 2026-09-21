# Queue and dependencies

Version 0.4 adds a durable queue to the existing task/revision workflow. Codex
explicitly invokes the dispatcher; enqueue alone makes no model call.

## Register and dispatch

Use the `claude_control` command helper from the [CLI guide](cli.md). Create
parent and child tasks with `task create`, then save their returned UUIDs.
Write a dependencies file containing exact references:

```json
[{"task_id":"<parent-task-uuid>","revision":1}]
```

```bash
claude_control task enqueue --task <child-task-uuid> --revision 1 \
  --dependencies-file /absolute/path/dependencies.json --operation-id queue-child-1
claude_control task enqueue --task <parent-task-uuid> --revision 1 \
  --operation-id queue-parent-1
claude_control dispatch --once
```

The parent starts; the child remains waiting. Inspect `task show`, `wait` and
`report` for the parent. Independently verify the acceptance criteria, then use
`task accept` with the exact revision, run, result digest and criterion evidence
as described in [task approval](tasks.md). An advisory `task review` is insufficient.

```bash
claude_control dispatch --until-idle --max-seconds 60
claude_control task events --task <child-task-uuid> --after 0 --limit 100
```

The second dispatch can now start the child. Its completion still needs Codex
review and explicit acceptance. Status checks and approval records make no model
calls. This release does not schedule reviews or automatically accept results.

## Ordering and bounds

- Ready tasks are admitted in FIFO order. Blocked entries are skipped so that
  independent work can proceed.
- `init --max-queued 100` sets the waiting-entry limit, separately from
  `--max-parallel 2`. Waiting entries include blocked work; reserved and cancelled
  entries do not count. The supported queue limit is 1–10000.
- `dispatch --once` makes one admission pass. It does not take `--max-seconds`.
- `dispatch --until-idle` requires `--max-seconds`, between 0 and 3600. The bound
  applies to new admissions; workers already reserved and started continue.
  It is not a hard real-time process-kill deadline for filesystem I/O or launching
  previously committed reservations.
- If only blocked or unknown work remains, the loop returns `needs_attention`.
  Normal local polling is every 0.2 seconds while executions can make progress.
  No background dispatcher or Codex wake-up is installed.

## Exact dependencies and input history

Each child revision pins at most 64 unique parent task/revision references.
Self-dependencies, cycles, missing tasks and noncurrent parent revisions are
rejected before enqueue. Changing a pinned parent's revision later blocks the
waiting child with `dependency_changed`; there is no implicit retargeting.

Admission checks the current parent run, its intact result bytes, model/session
execution evidence, valid complete report and exact Codex acceptance. These
checks and the child reservation share one write transaction. Direct `task submit`
and `task retry` enforce the same gates, even after the child has been dequeued.

The immutable base definition uses task contract v2. A queued execution uses v3,
copying approved reports plus parent task/revision/run/digest/decision IDs. The
final input must fit within 1 MiB; overflow consumes no reservation. Historical
child reports validate the frozen input, without reopening subsequently changed
parent files. Duplicate approvals select the first matching acceptance record,
so an explicit retry receives identical prompt bytes. Acceptance has no revocation
operation in P2; review recommendations are advisory. A new parent revision needs
its own acceptance.

## Dequeue, revise and recover

`task dequeue --queue-id <integer> --operation-id <id>` cancels a waiting entry,
including blocked entries. It does not stop a reserved run; use `stop --run` for
that. Re-enqueue of an unsubmitted revision requires the same dependency set and
receives a new FIFO position.

`task revise` can revise an unsubmitted task without `--parent-run`; after a
session exists, the exact latest parent run is required. Revision supersedes the
old waiting entry. The new revision needs a fresh dependency declaration and
explicit enqueue; neither is inherited automatically.

A crash after reservation commit but before launch leaves a pending run. It expires
under the normal claim deadline. Only explicit `task retry` with proof that every
attempt never launched Claude can bind a new run to the same reserved queue entry.
Retry checks dependencies again and requires byte-identical execution input.

A crash after launch leaves the detached worker responsible for completion on a
supported persistent Linux host. Starting another dispatcher reads existing
reservations and does not launch duplicates. `unknown` keeps its capacity slot;
inspect and `reconcile` before any new turn. Unknown or executed failed turns
cannot use `task retry`; after reconciliation, a new revision may be required.

## Events and migration

`task events [--task <uuid>] --after <cursor> --limit <1..1000>` returns append-only
revision, reservation, run-state, queue-state and decision events. Reuse
`next_cursor` to avoid duplicates. Repeated unchanged blocking observations do not
create events. Pre-migration events are not invented or backfilled.

New stores use schema 10; the queue itself requires schema 5 or newer. Existing stores require explicit `migrate --offline` for
queue features. Schema 3 upgrades through each intermediate schema to 8; each step creates a verified
SQLite backup and durable journal. Stop clients/workers and resolve unknown runs
first. An interrupted upgrade resumes with the same command. See
[migration and recovery](tasks.md#schema-and-migration).

A revision can also explicitly select [next-turn messages](messages.md). Dispatch
reserves their receipts together with the run and checks the combined dependency
and message input size. Enqueueing a message alone never queues a new task turn.
