# Runtime limits (0.29.0)

The legacy text/workspace controller and the optional Linux harness have separate
concurrency budgets. The commands below update an **existing** state store; they
do not reset sessions, tasks, approvals, or the pinned Team organization. Pass the
same global `--state-dir` before every subcommand when using a custom store.

## Legacy controller

```bash
claude_control limits show
claude_control limits configure --max-parallel 4 --max-queued 250
claude_control limits configure --max-unbound-messages 300 --max-selected-messages 96
```

| Setting | Default | Range | Meaning |
|---|---:|---:|---|
| `max_parallel` | 2 | 1–32 | Shared slots for active/unknown runs and workspace checks |
| `max_queued` | 100 | 1–10,000 | Waiting task queue entries |
| `max_unbound_messages` | 100 | 1–10,000 | Unbound messages per task |
| `max_selected_messages` | 64 | 1–512 | Messages selected for one task revision |

`limits configure` requires at least one flag and preserves unspecified values.
`limits show` returns current values and validation bounds. An older store whose
`max_parallel` is 1 remains at 1 until configured; the installed plugin version
does not change that setting. `unknown` runs consume capacity. Lowering a limit
does not cancel existing work or delete queued entries; it restricts new
admissions. Older schema-15 stores use the defaults for the two newly added
message settings until configured.

## Linux harness

```bash
claude_control harness limits show
claude_control harness limits configure --max-parallel 3 --max-turns 80
claude_control harness start --repo /absolute/repo --spec-file /absolute/task.md \
  --model sonnet --request-id task-001 --max-turns 120
claude_control harness followup --task TASK_UUID \
  --spec-file /absolute/followup.md --max-turns 40
```

| Setting | Default | Range | Meaning |
|---|---:|---:|---|
| `max_parallel` | 2 | 1–32 | Simultaneous turns in different harness tasks |
| `max_turns` | 30 | 1–1,000 | Default Claude `--max-turns` for a new turn |

`harness init` accepts the same two limit flags for a new harness. Use `harness
limits configure` after initialization. A per-call `--max-turns` overrides the
current default for only that call and is recorded in `harness show` under
`last_turn.max_turns`. Changing limits does not alter a running turn. A task
lock still serializes follow-ups in the same task; a global admission lock
counts other `running` and `unknown` tasks. An acknowledged follow-up of an
unknown turn reuses its own slot.

A `start` request ID is tied to the effective turn limit as well as the repo,
base SHA, model, spec, and timeout. Replaying the same ID after changing the
default limit may therefore produce `request_conflict`; inspect the existing
task with `harness show`.

## Other adjustable budgets

Per-run `--timeout`, workflow and composition `--max-revisions`/`--max-calls`,
their dispatch windows, workspace policy `max_actions`/`max_calls`, named-check
timeouts, and bounded dispatcher `--max-seconds` were already selectable at
creation or call time. Changing host limits does not rewrite frozen task or
workspace policies.

Input and event-size caps, parser bounds, maximum execution time, restricted
tool lists, sandbox requirements, and Team authentication are safety and
protocol boundaries. They are not general-purpose configuration knobs.
