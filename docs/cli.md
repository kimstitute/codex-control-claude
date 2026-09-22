# CLI reference

All examples assume the plugin has been installed. For a shorter command in your current shell:

```bash
claude_control() {
  python3 "$HOME/plugins/claude-control/scripts/claude_control_cli.py" "$@"
}
```

`--help` and `--version` are available. Public operation commands return JSON; a nonzero exit status indicates an error. Replace example paths and UUID placeholders with your own values.

## Pick a state store

The default is `$XDG_STATE_HOME/claude-control`, or `~/.local/state/claude-control` when XDG_STATE_HOME is unset. To choose another location, put the global option **before the subcommand on every call**:

```bash
claude_control --state-dir /absolute/path/to/private-state list
```

Use the same store across Codex tasks to share concurrency limits and discover existing work. Do not create a second store to get around an occupied slot or an uncertain run. `status` and `list` refresh durable state and therefore also require database write access.

## Initialize and diagnose

```bash
claude_control init \
  --claude-bin /absolute/path/to/claude \
  --allow-root /absolute/path/to/project \
  --max-parallel 2
claude_control doctor --auth
```

The executable and project paths are explicit. The store belongs to its originating host and user. Initialization does not grant filesystem isolation or change Claude authentication.

## Live monitor and usage history

```bash
claude_control monitor tui --refresh-seconds 0.5 --history 100
claude_control monitor snapshot --history 100
claude_control monitor limits
```

The TUI provides graph, agents, history and provider-limit views without mutating
the ledger or advancing work. `snapshot` returns observation data as JSON;
`limits` reads signed-in Codex, Claude Code, Gemini CLI and Cursor quota sources.
See the [live monitor guide](monitor.md) for keys and the distinction between live
estimates and final provider values.

## Tasks with revisions and approval

Use the [task lifecycle reference](tasks.md) for `task create/list/show/submit/revise/retry/review/accept`
and `migrate --status/--offline`. New task turns use an explicitly recorded v2 contract,
including structured follow-ups. Existing delegate and unstructured commands keep their semantics.

## Start a conversation

### Delegate with a role and an output contract

`roles` lists the bundled role instructions and resolved default models without
requiring an initialized store:

```bash
claude_control roles
```

| Role | Default model | Supplied-text task |
|---|---|---|
| `executor` | `sonnet` | Propose a bounded code or configuration change |
| `researcher` | `sonnet` | Summarize supplied sources and identify missing evidence |
| `planner` | `fable` | Plan research, experiments, or implementation |
| `architect` | `fable` | Design interfaces and assess technical tradeoffs |
| `critic` | `fable` | Identify weaknesses and unsupported claims |
| `verifier` | `fable` | Assess supplied results against acceptance criteria |

Write an assignment JSON file, using an existing project in your configured roots:

```json
{
  "id": "review-parser",
  "name": "parser-review",
  "role": "critic",
  "project": "/absolute/path/to/project",
  "objective": "Assess the supplied parser for ambiguous input handling.",
  "context": "Paste the relevant source code and existing test evidence here.",
  "scope": ["Only the supplied parser"],
  "acceptance_criteria": ["Identify concrete failure cases and distinguish assumptions"],
  "deliverable": "A concise review with proposed regression cases.",
  "effort": "high",
  "timeout": 300
}
```

```bash
claude_control delegate --assignment-file /absolute/path/to/assignment.json \
  --request-id parser-review-001
```

`model` and `timeout` are optional; defaults are the role's model and 300 seconds.
Optional `effort` accepts `low`, `medium`, `high`, `xhigh` or `max`; omission preserves
CLI behavior, while JSON null is invalid. See [execution settings](execution-settings.md).
An explicit `"model": "sonnet"` or `"model": "fable"` overrides the preset. The
resolved model is always passed explicitly to Claude. There is no automatic
classifier or model fallback. All other fields are required. `context` may be
empty; scope and acceptance criteria must be nonempty string lists. IDs match
`[a-z0-9][a-z0-9_-]{0,63}`; names are nonempty and at most 120 characters. Timeout
must be a finite number from 1 to 3600 seconds. Unknown fields, duplicate JSON
keys, null options, and wrong types are rejected.

Each delegate creates a **new named session**. A matching request ID and inputs
return the original run; a different request with an existing name is rejected.
Changes to context, timeout, model, or the bundled role instructions conflict
with an existing request ID. The absolute project path is checked and
canonicalized before rendering. Both the assignment file and the rendered
prompt must fit within 1 MiB; JSON escaping and role instructions count toward
the latter. Context is supplied text, never an instruction for the controller
to read another file.

The exact versioned role instructions and task are stored as canonical JSON in
`prompt.txt`, sent over stdin, and covered by the run-intent fingerprint. They
do not grant Claude tool access.

### Use an unstructured prompt

```bash
claude_control start \
  --name implementation \
  --model sonnet \
  --role implementer \
  --project /absolute/path/to/project \
  --prompt-file /absolute/path/to/task.txt \
  --request-id example-implementation-001 \
  --timeout 300
```

- Use `sonnet` for bounded routine work and `fable` for important planning or critique, when available to your account.
- Give the prompt enough source text and context to solve the task without tools. Prompts are limited to 1 MiB.
- The model, role, and project are fixed for that managed session.
- `start --role` is a free-form metadata label; only `delegate` injects preset role instructions.
- A returned run record means the request was accepted; it does not mean it finished.
- Keep the same request ID and inputs when retrying an uncertain submission. Reusing the ID with different inputs is rejected.

| Field | Meaning | Use it for |
|---|---|---|
| `id` | A single execution, or run | Status, logs, result, stop, wait, reconcile |
| `session_id` | The controller's conversation record | Follow-up, resume, restart |
| `backend_id` | Claude's persisted conversation UUID | Evidence and diagnosis; selected by the controller |

Display names are not accepted as session IDs. Do not substitute a backend ID for a managed session ID.

## Inspect and collect

```bash
claude_control list
claude_control status --run <run-uuid>
claude_control wait --run <run-uuid> --seconds 30
claude_control logs --run <run-uuid> --stream events --bytes 8192
claude_control result --run <run-uuid>
```

`wait` is bounded to at most 60 seconds per call. A wait timeout leaves the run running. Log streams are `events`, `stderr`, and `worker`; each response is bounded to at most 65,536 bytes.

A `completed` result requires a valid response stream, matching session and model, exit code zero, and a verified result artifact. It does not prove the answer is correct.

### Observe several executions

```bash
claude_control observe --run <first-run-uuid> --run <second-run-uuid> --seconds 30
```

Select 1–128 distinct managed runs. Duplicate IDs count once. The call returns
when all selected executions are terminal, a failed/cancelled/interrupted/unknown
execution needs attention, or the bounded wait ends (0–60 seconds). It returns
`runs`, `counts`, `execution_done`, `needs_attention` (run IDs), and `wait_reason`.
An `unknown` run needs attention but is not terminal. Observation does not stop,
retry, queue, or launch work. It refreshes durable status and needs database
write access. `execution_done` describes process outcomes only; it does not
parse reports or imply correct answers.

### Inspect a delegated report

```bash
claude_control report --run <run-uuid>
```

The response separates `execution_status`, `contract_status`, `format_status`,
`agent_status`, and `acceptance`. Acceptance is always `unreviewed`: Codex must
check the substance. A nonterminal execution has pending format status. An
ordinary `start` or unstructured follow-up has an unsupported contract; use
`result` for its text. Resume/follow-up still preserves the conversation, but
does not create a new structured assignment or reuse the earlier report contract.

For a completed delegated execution, the controller verifies the original
prompt against the stored run intent and verifies the exact result bytes being
read. It then checks a report containing exactly these fields:

```json
{
  "task_id": "review-parser",
  "role": "critic",
  "status": "complete",
  "summary": "Assessment of the supplied text.",
  "deliverable": "Concrete findings and proposed tests go here.",
  "evidence": [{"claim": "A concrete finding", "basis": "supplied_context", "reference": "Relevant supplied excerpt"}],
  "limitations": ["No tests were executed by this reviewer."],
  "handoff": "Codex should run and assess the proposed tests."
}
```

`status` is `complete` or `blocked`; evidence basis is `supplied_context` or
`reasoning`. Blocked reports and reports with no evidence require nonempty
limitations. One enclosing bare or `json` Markdown fence is accepted; leading
or trailing prose, duplicate keys, wrong identities, missing/extra fields, and
responses over 1 MiB are invalid. A syntactically valid evidence claim can still
be false. Role instructions prohibit fabricated tool/test claims; the validator
does not mechanically prove compliance or verify cited material.

Inspect the original output with `result` if formatting fails. There is no
automatic repair call, retry, acceptance decision, or verification-model launch.
An unavailable report with a nonempty `errors` list needs attention: its saved
contract or result could not be verified, rather than merely lacking a result.

## Continue or resume

```bash
claude_control followup \
  --session <managed-session-uuid> \
  --prompt-file /absolute/path/to/followup.txt \
  --request-id example-implementation-002
```

`resume` is an alias for `followup`. Both select the exact persisted conversation. Additional instructions are sent as a new turn after the current turn ends; this interface does not type into an active terminal.

Only one run may be active in a managed session at a time. Different sessions can execute in parallel within the store-wide limit.

## Stop an owned run

```bash
claude_control stop --run <run-uuid>
claude_control wait --run <run-uuid> --seconds 30
```

The first response acknowledges a cancellation request. Confirm the terminal state; acknowledgement alone does not prove the process stopped. Inspect any partial result before continuing a failed or cancelled conversation:

```bash
claude_control followup \
  --session <managed-session-uuid> \
  --acknowledge-context \
  --prompt-file /absolute/path/to/recovery-context.txt \
  --request-id example-recovery-001
```

## Start a fresh backend conversation

If the first turn ended before Claude saved a conversation, an ordinary resume can fail. After inspecting the failure, explicitly start over:

```bash
claude_control restart \
  --session <managed-session-uuid> \
  --acknowledge-context \
  --prompt-file /absolute/path/to/fresh-context.txt \
  --request-id example-restart-001
```

`restart` creates a new Claude conversation under the same managed session. **It does not retain the previous conversation's context.** Supply the context again. Historical run IDs, backend IDs, and logs remain available. A normal resume never silently turns into a restart.

## State and recovery

An `unknown` run retains its slot and blocks another turn. Inspect its evidence before asking the controller to confirm that execution has stopped:

```bash
claude_control reconcile --run <run-uuid>
```

On the same boot, reconcile must run in the worker's PID namespace. It releases uncertainty only when the recorded worker and process group are no longer live, or when a host reboot proves the earlier processes cannot still be running. It does not blindly signal a saved PID. A conversation with a mismatched backend session remains blocked.

New stores use schema 12. Existing schema 3–11 stores require [explicit offline migration](tasks.md#schema-and-migration) for newer features; legacy diagnosis and stop/reconcile remain available before migration. Schemas 1 and 2 are unsupported. Never delete or replace state to bypass an active or unknown execution.

## Troubleshooting

| Symptom | Next step |
|---|---|
| `doctor` reports missing options | Use a Claude Code version supporting the reported flags. |
| Login is not ready | Sign in with the normal local Claude Code flow, then repeat `doctor --auth`. |
| Model request fails | Inspect the run's result and stderr; verify the explicit model alias is available. |
| State database cannot be opened | Check ownership and write access. In Codex, use the normal approval mechanism when the state path is outside the sandbox. |
| Worker disappears after `start` | A short-lived PID sandbox may have killed detached workers. Use a supported persistent host execution environment and inspect/reconcile the original run. |
| Session is busy or capacity is full | Inspect existing work; wait, or intentionally cancel the relevant owned run. |
| Installer cannot find its validator | Point `--plugin-creator-root` and `--helper-python` to existing compatible tools. |

Do not modify the fixed profile to enable Claude tools, forward credentials, or bypass the host's approval policy.

## Queue, dispatch and events

- `init` accepts `--max-queued <1..10000>` (default 100), separate from `--max-parallel`.
- `task enqueue --task <UUID> --revision <N> --operation-id <id> [--dependencies-file <JSON>]`.
- `task dequeue --queue-id <integer> --operation-id <id>` cancels waiting work.
- `dispatch --once` or `dispatch --until-idle --max-seconds <0..3600>`.
- `task events [--task <UUID>] [--after <cursor>] [--limit <1..1000>]`.

Dependency JSON is an array of exact `task_id`/`revision` references. See the
[queue guide](queue.md) for approval gates, FIFO order, deadline semantics and recovery.

## Next-turn messages

- `message enqueue --task <UUID> --base-revision <N> --content-file <UTF-8 file> --operation-id <id> [--session <UUID>] [--source-run <UUID> --source-result-sha256 <digest>]`.
- `message list [--task <UUID>] [--after <sequence>] [--limit <1..1000>]`.
- `message cancel --message <UUID> --operation-id <id>`.
- `task revise` adds repeatable `--message-id <UUID>` and explicit `--redeliver-messages`.

See [message and handoff semantics](messages.md). Registration and revision selection
make no model calls. Delivery receipts bind only at explicit submit or dispatch.

## Bounded workflows and attention overview

`workflow create/run/status/stop` and `overview --attention` are documented in the [workflow guide](workflows.md). Creation makes no model call; `run --until-idle` requires an explicit finite `--max-seconds`. Use the workflow commands for owned member tasks; ordinary task mutation cannot change their policy.

## Controlled workspaces

`workspace doctor/create/task/run/status/list/export/apply/stop/reconcile` are documented in the [workspace guide](workspaces.md). Creation reserves one immutable workspace identity before copying. Use explicit file/check policies, a finite run admission window, and a frozen export for review. Schema 12 adds guarded source application; workspaces require a working Linux Bubblewrap backend.

## Explicit execution settings

`start`, `followup`, `resume` and `restart` accept `--effort <level>`.
An existing session pins its original setting, including omission; a different
explicit value is rejected. Structured assignments use the optional `effort` key.
`workflow create --reviewer-effort high` pins the independent reviewer setting.
`doctor` reports `effort_supported`; this checks CLI flag availability, not model
support for every effort value. Explicit effort requires schema 9.

See [execution settings and compatibility](execution-settings.md).

Schema 11 run objects include `telemetry`: raw provider `usage`, per-model
`model_usage`, `provider_cost_usd` when Claude reports it, `duration_api_ms`, and
controller-observed `duration_ms`. Historical pre-migration runs have `null`
telemetry; the controller does not estimate missing prices.

## Compositions

`composition create/run/status/stop` connects the existing bounded planning
workflow to controlled editor and frozen reviewer workspaces. It requires schema
10 and keeps plan and final-result acceptance explicit. See the
[composition guide](compositions.md) for files, ordering and recovery behavior.
