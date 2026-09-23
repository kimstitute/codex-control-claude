# Next-turn messages and handoff

Version 0.5 lets Codex store instructions while a managed Claude task is running,
then explicitly choose them for a later revision. Enqueue and selection make no
model call and never write to the active Claude process.

Use the `claude_control` command helper from the [CLI guide](cli.md).

## Register, select and submit

Write instruction text to a UTF-8 file and name the task's exact current revision:

```bash
claude_control message enqueue --task <task-uuid> --base-revision 1 \
  --content-file /absolute/path/instruction.txt --operation-id instruction-1
claude_control message list --task <task-uuid>
```

Save the returned message UUID. `--session <managed-session-uuid>` optionally
asserts the task's session. Task and existing backend identity are pinned; restarting
the conversation does not redirect messages to the new backend. If the task has no
session yet, enqueue leaves it unassigned and later checks its first task backend.

After the current run settles, revise the task with the exact latest parent run and
select message IDs. You can select up to 64 distinct IDs; input order does not
change their delivery order, which follows their stored registration sequence.

```bash
claude_control task revise --task <task-uuid> --revision 1 \
  --parent-run <latest-run-uuid> --assignment-file /absolute/path/revised.json \
  --message-id <message-uuid> --operation-id revision-2
claude_control task submit --task <task-uuid> --revision 2 --operation-id submit-2
```

For an unsubmitted task with no session, omit `--parent-run`. Failed parent context
requires inspection and deliberate `--acknowledge-context` before executing a new
turn. You may enqueue revision 2 in the [task queue](queue.md) instead of submitting
it directly. Both paths apply the same message checks.

Only the current revision can execute. A new revision does not inherit selected
messages. An unbound message whose base no longer matches must be cancelled and
registered against the current base with a new operation ID; it never moves silently.
Repeated identical operation IDs return the original result; changed inputs conflict.

## Result handoff

Codex can attach a verified, frozen result to a message:

```bash
claude_control message enqueue --task <recipient-task-uuid> --base-revision 1 \
  --content-file /absolute/path/handoff-instruction.txt \
  --source-run <completed-source-run-uuid> --source-result-sha256 <exact-digest> \
  --operation-id handoff-1
```

The source must be a managed structured task run (v2, v3, v4 or v5), completed with an
intact result and a valid report. A complete or blocked report can be supplied;
this supports passing a critic's findings before any acceptance decision. Source
run/digest must be supplied together. Legacy `delegate` and unstructured results
are not accepted as structured sources.

This is an explicit Codex selection. It gives Claude no authority to call other
agents or create tasks. It does not accept the source or satisfy the separate
parent-acceptance gate of a queued dependency. The copied report and its source
run/digest remain fixed even if the source artifact later disappears. Automatic revision requires a separately created [bounded workflow](workflows.md).

## State and history

| State | Meaning |
|---|---|
| `queued` | No run has bound the message. `selected_for_revision` identifies a selected, unsubmitted message. |
| `bound_to_run` | The reservation includes the message; the run is pending or active. |
| `run_completed` | The latest bound execution completed with an intact result. |
| `needs_attention` | The latest delivery failed, was cancelled, became unknown, or has an integrity/read problem. |
| `cancelled` | Codex explicitly cancelled the unbound message. |

Binding is a reservation receipt, not proof that Claude read or understood the
instruction. `run_completed` is also not semantic acceptance. Verify the report
and use `task accept` with criterion evidence separately.

`message list [--task <uuid>] --after <sequence> --limit <1..1000>` returns message
content, source provenance, selections, binding history and `next_cursor`. Its
cursor discovers newly registered messages; to refresh an older message's changing
state, query its page again. Bindings have their own monotonic sequence. The
`redelivered` flag means multiple reservation attempts, which may include a retry
that never launched Claude. Inspect each bound run for execution evidence.

Use `task events --task <uuid> --after <cursor>` to follow changes. Event IDs, not
wall-clock timestamps, define ordering. Historical selections remain visible even
after a newer revision supersedes them; only the current revision is eligible.

## Cancel and deliberate redelivery

```bash
claude_control message cancel --message <message-uuid> --operation-id cancel-1
```

Cancel works only before the first binding. Cancelling a selected but unsubmitted
message invalidates that revision's submission; create a new revision with the
intended selection. Cancel and reservation are serialized: either cancellation
wins and no run binds it, or binding wins and cancel is rejected. Bound message
history is never erased. Use `stop --run` to cancel execution itself.

After a bound run fails, inspect its result and process state. To deliberately
include the same message in a new revision, pass both its `--message-id` and
`--redeliver-messages` to `task revise`. This freezes the previous delivery run ID
and preserves the earlier receipt. Completed deliveries and unresolved `unknown`
runs cannot be redelivered. Reconcile unknown execution first. Claude may already
have read a failed attempt; deliberate redelivery does not promise exactly-once
understanding or external effects.

An unreserved redelivery draft may be explicitly superseded by another redelivery
revision. The old selection is historical and cannot be submitted. No implicit
carry-over occurs.

`task retry` remains narrower: it is allowed only when every attempt of the same
revision is proven never to have started Claude. Its prompt must be byte-identical,
and its new reservation appends another message receipt. There is no automatic
resend after a crash, cancellation or timeout.

## Limits and migration

Each task can have 100 unbound, uncancelled messages, including stale or selected
ones. Cancel stale entries to free space. Each message plus its copied source, and
each revision plus selected messages, must fit within 1 MiB. At reservation, the
combined task, dependency reports and messages must also fit; failure consumes no
run or binding. Queued task capacity and execution slots remain separate limits.

New stores use schema 14. Existing stores need explicit offline
migration for messages. The 5→6 step preserves P2 rows and creates a verified backup
and recovery journal. Stop clients/workers and resolve unknown runs first; repeat
`migrate --offline` after interruption. See [migration](tasks.md#schema-and-migration).
