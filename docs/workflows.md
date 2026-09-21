# Bounded proposal and review workflows

Version 0.6 adds one fixed local workflow: worker proposal → independent Fable
review → optional worker revision → review again → Codex decision. The worker
uses its assignment's explicit model, normally Sonnet; planner/architect workers
can use Fable. Both agents receive supplied text with tools and MCP disabled.

## Create, run and inspect

Use the assignment format and `claude_control` helper in the [CLI guide](cli.md).
The worker role must be `executor`, `planner` or `architect`.

```bash
claude_control workflow create --assignment-file /absolute/path/assignment.json \
  --operation-id proposal-workflow-1 \
  --max-revisions 2 --max-calls 6 --dispatch-window-seconds 900
claude_control workflow run --workflow <workflow-uuid> --once
claude_control workflow run --workflow <workflow-uuid> --until-idle --max-seconds 60
claude_control workflow status --workflow <workflow-uuid>
claude_control overview --attention
```

Creation records the two member tasks, frozen policy and initial queue entry;
it makes no model call. Save the workflow UUID. Repeated identical create
operations return the same workflow, while changed policy conflicts.

`run --once` advances completed work and performs one admission pass.
`--until-idle` requires a finite `--max-seconds` from 0 to 3600; this bounds the
current coordinator call. The workflow's own persistent dispatch window is a
separate limit. Re-run the same workflow to continue after a coordinator exits.
Already reserved runs are observed, never relaunched. This command dispatches
only the named workflow; ordinary FIFO dispatch also applies the same admission
guards to any workflow queue entries it encounters.
The finite loop waits for capacity occupied by other live runs without dispatching
their queued work.

There is no resident coordinator or automatic Codex wake-up. `status` and
`overview` refresh execution evidence but do not create a stage or call a model.
Overview includes workflow budgets, task states, unresolved runs and an event
cursor. Event IDs define ordering; wall-clock timestamps are observational.

## Policy and budgets

| Setting | Default | Allowed values | Meaning |
|---|---:|---|---|
| `max-revisions` | 2 | Integer 0–10 | Worker revisions after the initial proposal |
| `max-calls` | 6 | Integer 2–64 | Reserved model turns, including unknown or failed attempts |
| `dispatch-window-seconds` | 900 | Finite 1–86400 | Time allowed for new reservations after the first successful reservation |

The first reservation and its timestamp commit together. Queue delays before
that reservation do not consume the window. Restarting a coordinator never
resets counters or time. Capacity/size failures roll back before any call receipt
is committed. A committed reservation consumes one call even if its worker never
launches; no implicit refund or retry occurs.

A new worker revision needs at least two calls remaining, including its review.
Models cannot increase limits or change roles, membership or dependencies.
The window uses persisted wall-clock time; a clock before the first reservation
blocks admission. When it expires, existing runs retain their individual timeout
and may finish, but no new run is admitted. The window is not a total completion
deadline.
If a completed review is waiting when the coordinator resumes after the window,
its recommendation is still recorded. If expiry is observed while a run is still
active, automation stops and its eventual result remains available for Codex.
Codex can inspect a finished review and explicitly record it with `task review`.

## Exact reviews and final acceptance

The task v5 input freezes the workflow policy, criterion IDs and exact source
task/revision/run/result digest. Reviewer stages use a different persistent
session from the worker. Each review has a new task revision, including the first
review after the unused reviewer placeholder. P3 messages carry frozen worker
results to reviews and revision instructions back to the worker. The source report
appears once in that message; `workflow.source.message_id` identifies it alongside
the exact source identity and digest.

A reviewer returns the normal task report plus a structured `review` object:

```json
{
  "target": {
    "task_id": "<worker-task-uuid>", "revision": 1,
    "run_id": "<worker-run-uuid>", "result_sha256": "<exact-result-digest>"
  },
  "recommendation": "approve",
  "criteria": {"1": {"verdict": "pass", "evidence": "Concrete supplied evidence."}},
  "unverified": [],
  "revision_instructions": ""
}
```

Every fixed criterion ID must appear exactly once. Verdicts are `pass`, `fail`
or `unknown`. Approval requires all criteria pass and no unverified items;
`revise` needs nonempty revision instructions. Missing/wrong digests, malformed
reports and contradictory approvals do not trigger another model call.

An intact completed report with agent status `complete` can release a private
workflow `reported` edge without Codex acceptance. Only this fixed template can
create such edges. Ordinary task dependencies still require explicit acceptance.
Sources are revalidated at admission; historical reports use their frozen copies.

`approve` records a review recommendation and returns `awaiting_codex`, with
reason `approve_recommended`. It never writes an acceptance decision. Inspect the
worker result and use [task accept](tasks.md) with the exact current revision,
run, result digest and criterion evidence. Status reports `accepted` only when
that exact result has a valid explicit Codex acceptance. `accepted_revision`
identifies it; the review's target remains separately inspectable.

## Stop and recover

```bash
claude_control workflow stop --workflow <workflow-uuid> --operation-id stop-1
claude_control workflow status --workflow <workflow-uuid>
```

Stop first durably blocks new reservations and cancels waiting member entries,
then requests cancellation through existing managed-run control. It returns
`stopping` while owned processes remain unresolved, and `stopped` after termination
is established. Repeating stop or running the stopping workflow resumes cleanup
after interruption. An unknown run remains quarantined: use the existing
`reconcile --run` command from the owning PID namespace once execution is known
to be dead. Never turn unknown into success or free its slot by editing the DB.

Failure, malformed/blocked output, context divergence, exhausted limits and
unknown execution end automation in `awaiting_codex`. There is no workflow retry
or reset in this release. Inspect the artifacts, reconcile when needed, stop the
workflow, or explicitly approve a valid current worker result. A new workflow is
a separate decision and must not bypass unresolved execution. Public member
submit/retry/revise/enqueue/dequeue commands and public message enqueue/cancel
commands cannot alter the fixed template.

## Compatibility

New stores use schema 8. Existing schema 3–6 stores require explicit offline
migration; the 6→7 step makes a verified backup and preserves all P3 message,
receipt, task and approval rows. All clients and workers must be stopped and
unknown executions resolved before migration. See [migration](tasks.md#schema-and-migration).

Legacy sessions and task contracts v1–v4 keep their existing behavior. This phase
adds neither Claude file/shell tools nor cross-host job delivery.
