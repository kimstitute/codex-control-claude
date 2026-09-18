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

## Installation

`install.py` uses Codex's bundled personal-marketplace helpers and `codex plugin add`. A host-local file lock serializes installations. The new tree is staged and validated before replacement; the previous tree is retained for rollback. Runtime state is neither packaged nor removed by updates.

The public repository uses only the maintainer's GitHub handle. Machine-specific implementation notes, account records, credentials, and raw live-session artifacts are intentionally absent.
