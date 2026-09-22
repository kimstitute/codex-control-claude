# Controlled workspaces

The P5 workspace API lets the controller mediate a finite set of file reads,
full-file writes, and named checks against an immutable Git snapshot. Claude
itself remains supplied-text only; it has no file, shell, built-in tool, or MCP
access in this workflow.

Read the complete command, policy, snapshot-review, isolation, recovery, and
schema-8–13 migration contract in the installed-skill reference:
[controlled workspaces](../plugins/claude-control/skills/claude-control/references/workspaces.md).

P4 workflows do not automate this API. Creating a workspace, binding its one
task, admitting calls, exporting a frozen result, and accepting it remain
explicit controller/Codex actions.

Schema 11 stores per-run usage, model usage, provider-reported cost and latency.
Schema 12 adds explicit `workspace apply`, guarded by the frozen manifest, exact
source HEAD and a clean worktree. A read-only Sonnet `scout` workspace can be
pinned into `composition create --scout-workspace`; see the full reference above.

The [real-project pilot](real-project-pilot.md) records actual timeout and
report-format failures, independent verification, and the next reliability gates.

On Windows 10/11, an architecture-matching SHA-256-pinned native supervisor owns
`wsl.exe`. Selected files cross the boundary as a bounded deterministic archive,
are verified in private WSL2 ext4 state, and then enter the existing networkless
Bubblewrap backend. State below `/mnt`, reparse points, incomplete mode sidecars,
or a failed live probe stop workspace execution. See [Windows setup](windows.md).
