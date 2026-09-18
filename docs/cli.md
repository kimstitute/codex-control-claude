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

## Start a conversation

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

State schema 3 does not migrate development schemas 1 or 2. Retain earlier stores as private history, stop and account for their managed work, then initialize a fresh supported store. Never delete or replace state to bypass an active or unknown execution.

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
