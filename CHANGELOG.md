# Changelog

## 0.7.0 — 2026-09-21

This release brings the task, queue, message, review-workflow and controlled-workspace
features developed since 0.2.0 into one published version.

### Added

- Persistent tasks with immutable revisions, structured follow-ups, exact-result
  review records and explicit Codex acceptance.
- FIFO dependency queues, bounded dispatch, durable events and conservative
  recovery without automatic replay of uncertain execution.
- Explicit next-turn messages and verified result handoffs between tasks.
- Bounded proposal, independent Fable review and revision workflows with fixed
  call, revision and admission-window budgets.
- Private Git snapshots with explicit readable paths, exact writable files and
  named test commands. Claude requests operations in reports; the controller
  validates and executes them.
- Disposable Bubblewrap check environments, recorded process ownership, frozen
  output manifests and patches, and independent read-only Fable review snapshots.
- Offline schema 3–7 upgrades to schema 8 with verified backups and recovery journals.

### Compatibility and upgrade

- Linux and Python 3.10+ remain required. Workspace operations additionally need
  Git, Bubblewrap and usable unprivileged user, PID and network namespaces.
- Claude's native tools and MCP remain disabled. Existing supplied-text sessions
  and earlier report contracts remain supported.
- Finish or stop managed work and resolve unknown executions before updating.
  Install with `python3 install.py --update`, then inspect `migrate --status` and
  run `migrate --offline` on the existing store. The installer does not migrate
  a live store or install missing dependencies.
- Workspace output needs explicit Codex verification and source integration.
  P4 proposal/review workflows do not automatically orchestrate P5 workspaces.
- Controller state, credentials, transcripts and machine-specific configuration
  are excluded from the distribution.

See the [workspace guide](docs/workspaces.md), [migration guide](docs/tasks.md#schema-and-migration)
and [test instructions](docs/testing.md).

## 0.2.0

- Versioned role assignments, structured reports and multi-run observation.
- Explicit Sonnet/Fable selection and persistent host-local Claude sessions.
