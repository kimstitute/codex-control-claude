---
name: claude-control
description: Manage multiple persistent Claude Code sessions on the current Linux host from Codex. Use for delegating supplied-text implementation, research planning, verification or critique to local Sonnet/Fable, inspecting managed work, and stopping or resuming an identified session.
---

# Claude Control

Use the bundled `../../scripts/claude_control_cli.py`, resolving that path from this skill's directory. Public commands return JSON. Python 3.10+ and locally authenticated Claude Code are prerequisites; run `--help` for flags. Version 0.2 is Linux and **supplied-text tasks only**: tools and MCP are disabled, with CLI safe mode and empty setting sources. The project-hook nonexecution test passed; administrator-managed policy still applies. For code work, provide the relevant code, obtain proposed edits as text, then inspect and apply them through Codex's normal authorized tools.

## First use

Run `doctor --auth` and `list` against the intended state directory. Default: `$XDG_STATE_HOME/claude-control`, or `~/.local/state/claude-control`. If uninitialized, use `init --claude-bin <absolute executable> --allow-root <authorized project directory> --max-parallel 2`. Do not initialize a second store to bypass an occupied slot or an ambiguous run. Each host/user owns its own configuration and records; this package does not transfer jobs or credentials between servers.

For a custom store, keep the same global option **before the subcommand on every call**: `python3 <resolved-cli-path> --state-dir <absolute-state-dir> <subcommand> ...`.

Claude invocations need network access and write access to its normal local session history. If the host's sandbox prevents these, use its normal approval mechanism for the concrete local invocation. Short-lived PID sandboxes may terminate detached workers when the launching command ends; use the host's supported persistent execution environment. Do not interpret sandbox-denied I/O as a reason to change credentials or model.

Status queries also refresh durable state and need write access to the state database. If a default home-directory store is outside the workspace sandbox, run these commands through the normal approval mechanism too; do not copy the database into another store to work around permissions.

This version uses the normal HOME-based Claude login; custom `CLAUDE_CONFIG_DIR` and API-key environment variables are not forwarded. Doctor uses the same environment as jobs.

## Delegate and collect

Prefer the structured path for a new bounded assignment:

1. Run `roles` to inspect versioned role instructions. `executor` and `researcher`
   default to Sonnet; `planner`, `architect`, `critic`, and `verifier` default to
   Fable. Researcher only summarizes supplied sources; important research planning
   belongs to planner/architect. An explicit assignment `model` overrides the
   preset. Do not silently substitute an unavailable model.
2. Write JSON with required `id`, `name`, `role`, absolute `project`, `objective`,
   `context`, `scope`, `acceptance_criteria`, and `deliverable`. `scope` and
   `acceptance_criteria` are nonempty string lists. `context` is supplied text,
   not a path to auto-read. IDs match `[a-z0-9][a-z0-9_-]{0,63}`; names are at most
   120 characters. Optional `model` is sonnet/fable and `timeout` is 1–3600 seconds
   (default 300). Raw input and rendered prompt must each fit within 1 MiB.
3. Run `delegate --assignment-file <path> --request-id <stable ID>`. This creates
   one new named session, injects the complete role instructions and report
   contract, and preserves the exact prompt. Keep the returned run and session
   IDs. Repeated identical requests deduplicate; changed timeout, context, or
   preset instructions conflict with the old request ID. Do not create another
   store or duplicate request to bypass capacity or uncertain execution.
4. Delegate independent assignments within available slots. There is no batch
   transaction or queue: collect every accepted run ID even if a later submission
   fails. `observe --run <id> --run <id> --seconds 30` observes those runs together.
   `execution_done` only means all selected executions are terminal; examine
   `needs_attention` and each outcome. A wait deadline does not cancel work.
5. Use `report --run <id>` after completion. Check execution status, contract
   status, format status, and the agent-reported status separately. The report
   includes summary, deliverable, evidence, limitations, and handoff. `acceptance`
   always stays `unreviewed`; independently verify the content before applying
   proposed code or approving a plan. Malformed output does not trigger an
   automatic repair/model call. Use `result` to inspect its raw answer.

To continue the identified conversation, use the existing followup/resume commands
below. They preserve Claude context but do not create another structured task
contract; their `report` is unsupported and their text remains available through
`result`. Use a new delegate for a new independently tracked structured task.

The unstructured path remains available. `start --role` is metadata only and does
not inject role instructions:

1. Choose explicit `--model sonnet` for routine implementation or small tasks; choose `--model fable` for research/experiment design, architecture, consequential review or critique. Record a concise role with `--role`. The model choice is fixed for the session; actual assistant model IDs are recorded per run. Never silently substitute a model.
2. Put a bounded prompt in a file. State the task, expected output, relevant supplied context, and how you will verify it. Prompts are at most 1 MiB. Do not supply credentials. Give a stable unique `--request-id` to the logical request and keep it if a response is lost.
3. Start with `start --name <display-name> --model <alias> --role <role> --project <absolute path> --prompt-file <path> --request-id <id> --timeout 300`. Save both returned `id` (run) and `session_id` (managed conversation). A returned run means accepted, not completed. Independent sessions can run concurrently up to the configured limit.
4. Use `status --run <run UUID>`, bounded `wait --run <UUID> --seconds 30`, and `logs --run <UUID>` for progress. Use `result --run <UUID>` to retrieve the answer and verification metadata. `completed` requires valid stream, matching session/model, exit 0 and a verified result artifact. It does not mean the answer itself is correct; verify the substance before integration.
5. Continue only the intended idle conversation with `followup --session <managed session UUID> --prompt-file <path> --request-id <new-id>`. `resume` is an alias with the same arguments. Names are for display; do not substitute a backend UUID or use "latest". The tool records and selects the backend UUID itself. Additional instructions run on a subsequent turn, not by typing into a live terminal.

## Stop and recover

`stop --run <UUID>` requests cancellation of that managed run only. Check for `cancelled`; acknowledgement alone does not prove termination. Other sessions continue independently. After a cancelled/failed/interrupted turn, inspect the partial result before an intentional `--acknowledge-context` followup because the transcript may be incomplete.

If no backend conversation was saved before the first turn stopped, a normal resume can fail. After checking that failure, use an explicit `restart --session <managed UUID> --acknowledge-context --prompt-file <fresh context> --request-id <new id>` to start a **new backend conversation** under the same managed session. It does not preserve conversation context; supply the needed context again. Historical run/backend IDs and transcripts remain recorded. Never perform this automatically as a silent fallback. A divergent or still-active session cannot restart.

`unknown` reserves its concurrency slot and blocks another turn. Inspect the record and use `reconcile --run <UUID>` from the same PID namespace as the worker. Reconciliation releases an execution only when its recorded worker and process group are no longer live, or the host has rebooted. It does not signal an arbitrary saved PID. Do not edit the database, start a duplicate session, or force a retry to bypass uncertainty. A divergent backend session remains blocked.

Code updates should happen with managed runs stopped. Runtime data is separate from the plugin, and no uninstall or update should remove it. Process-group cleanup covers the managed tools-disabled execution; it is not a cgroup/filesystem sandbox or a promise about processes that escape their group. Do not enable shell/edit tools by modifying this CLI's fixed profile.

This release uses state schema 3. Earlier development stores are not migrated; retain them as historical evidence and initialize a fresh schema-3 store only after all earlier managed work has stopped. Do not use re-initialization to bypass an active or unknown run.

The package launches workers for jobs, not an always-running coordinator. Codex is not automatically awakened after the conversation ends. A later Codex task can discover the same host-local records via `list`.
