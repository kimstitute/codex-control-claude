# Changelog

## 0.19.0 — 2026-09-23

- Upgrade stores to schema 14 with an append-only, cursor-ordered observation
  ledger, deterministic baseline replay and durable graph relationships.
- Add the fifth TUI Replay view on Linux and Windows, including one/ten-event
  seeking, first/latest jumps and timed play/pause over recorded history.
- Export terminal runs as OTLP/HTTP JSON traces with deterministic IDs,
  provider-faithful token attributes and no prompt, reasoning or tool bodies.
- Add AG-UI 1.0 snapshot and lifecycle adapters plus bounded
  `monitor agui snapshot/events` commands with resumable cursor metadata.
- Keep account identity, credentials, project paths, prompts and result bodies
  outside the observation contract and interoperability exports.

## 0.18.0 — 2026-09-23

- Add `models catalog`, which uses Claude Code's Agent SDK initialization
  exchange to read the signed-in account's effective model selectors, resolved
  model IDs and advertised effort capabilities without sending a model prompt.
- Reduce the provider response to an explicit model-field allowlist. Account,
  email, organization and credential fields are neither returned nor persisted.
- Accept Claude Code's explicit `[1m]` selector suffix while preserving exact or
  family model identity checks and the existing no-fallback rule.
- Update Korean and English model documentation and current-scope summaries for
  the v0.18 catalog workflow.

## 0.17.0 — 2026-09-23

- Add atomic host-local model and effort defaults for executor, researcher,
  planner, architect, critic and verifier roles.
- Accept Sonnet, Opus, Haiku and Fable family aliases plus exact versioned
  `claude-...` model IDs. Exact selectors require exact provider identity;
  family aliases still reject cross-family substitution.
- Resolve role defaults before task/session creation while preserving explicit
  assignment overrides and immutable existing work. Unsupported model/effort
  combinations fail without fallback.
- Remove Sonnet/Fable family requirements from scout, verifier, reviewer and
  leader-critic phases while retaining their role and sandbox constraints.
- Add `models show/configure/reset`, workflow reviewer model overrides, tests,
  and Korean/English model configuration documentation.

## 0.16.0 — 2026-09-23

- Add optional immutable routing metadata to composition policy: task type, risk
  class, routing-policy version, model-selection reason and verified escalation
  lineage. Read-only evaluation can stratify recorded outcomes by these fields,
  editor settings, origin or outcome while marking legacy records `unrecorded`.
- Add a foreground multi-composition dispatcher with deterministic FIFO sweeps,
  global single-dispatcher exclusion, per-composition busy isolation and the
  existing global worker-capacity limit. It does not retry, accept or apply work.
- Add opt-in scout reuse by exact normalized assignment, source commit, readable
  paths and workspace task protocol. Cache hits are reverified from frozen
  artifacts; misses fail explicitly and default composition behavior is unchanged.
- Keep store schema 13. All new provenance lives in immutable composition policy
  or is derived with read-only queries, so no database migration is required.

## 0.15.0 — 2026-09-23

- Add immutable leader-authored composition specifications. Fable is restricted
  to a critic assignment, the critique cannot revise the specification, and the
  existing exact Codex acceptance gate still precedes editor creation.
- Add optional frozen test contracts with controller-run baseline expectations,
  editor write exclusion, frozen-file verification and required named-check
  receipts bound to the final tree before reviewer creation.
- Add read-only `composition evaluate` reports for readiness, acceptance and
  unassisted-success rates with Wilson intervals, plus complete-case provider
  cost and token aggregates without imputation.
- Keep store schema 13. The new immutable contracts use existing composition
  policy, operation and workspace receipt ledgers, so no migration is required.
- Update the implementation plan and Korean/English composition documentation.

## 0.14.1 — 2026-09-23

- Deduplicate Windows child environments case-insensitively before native
  supervisor launch, including the WSL transport path.
- Remove the unsupported `--permission-prompts` flag while retaining safe mode,
  empty native tools/MCP and non-interactive permission handling.
- Give the WSL workspace probe canonical empty mode metadata so it reaches the
  live WSL2/Bubblewrap checks on an empty scratch tree.
- Record the physical Windows validation boundary: the native supervisor, Job
  Object and a tool-free `claude-sonnet-5` model call passed. The WSL2/Bubblewrap
  check remains pending because the Codex sandbox token cannot access the WSL
  service (`E_ACCESSDENIED`).

## 0.14.0 — 2026-09-22

- Add same-host Windows 10/11 session control through the pinned native
  `ccc-win-supervisor.exe`, which creates suspended children inside kill-on-close
  Job Objects and records process creation identity before resume.
- Add Windows ANSI/VT monitoring and portable locks, private-state DACL checks,
  reparse-point rejection, boot/process identity and provider paths.
- Add a Windows workspace backend that transports bounded deterministic archives
  to private WSL2 ext4 state and runs the existing networkless Bubblewrap sandbox.
- Reject Windows-ambiguous Git paths and preserve Git executable modes in
  canonical sidecars; verify apply results with Git-normalized blob hashes.
- Upgrade stores to schema 13. Native AppContainer is excluded and there is no
  unconfined fallback. Physical Windows/WSL2 live validation remains pending.

## 0.13.0 — 2026-09-22

- Add a fourth TUI view for signed-in Codex, Claude Code, Gemini CLI and Cursor
  quota windows, remaining percentages, reset times and provider-specific units.
- Read Codex rate limits and account token activity through the supported app
  server, Claude's service-reported local cache, Gemini Code Assist quota buckets
  and Cursor's dashboard usage endpoint.
- Keep account quota percentages separate from token activity. Never infer an
  absolute or remaining token count when the provider does not publish a token
  ceiling.
- Add `monitor limits`, `monitor snapshot --limits`, bounded provider polling,
  stale-reading labels and credential-redacted JSON output.

## 0.12.0 — 2026-09-22

- Add a read-only curses TUI with graph, agent and run-history views.
- Show each managed agent's role, requested and actual model, current work and
  runtime state, with composition/workflow/workspace/task relationship edges.
- Read Claude stream token estimates while a run is active and clearly separate
  them from final provider usage, cost and latency records.
- Add `monitor snapshot` as a bounded JSON observation interface for scripts.

## 0.11.0 — 2026-09-22

- Pass one bounded live Sonnet edit/check/freeze and independent Fable review
  pilot without changing the source repository.
- Persist append-only raw provider usage, model usage, reported cost and latency
  telemetry for new runs; historical runs remain explicitly unmeasured.
- Add guarded, idempotent `workspace apply` with exact base HEAD, clean-worktree,
  frozen-manifest and patch checks. It does not commit, merge or push.
- Add a read-only Sonnet scout whose repository brief and file-line evidence can
  be pinned into composition planning context.
- Upgrade new stores to schema 12 with offline 10→11→12 migrations.

## 0.10.0 — 2026-09-22

- Use Claude Code JSON-schema output for structured assignments and accept its
  synthetic `StructuredOutput` event without permitting native tools.
- Add base-hashed hunk patch operations, one bounded side-effect-free format
  repair, final-review veto enforcement and ledger-typed acceptance evidence.

## 0.9.0 — 2026-09-21

- Add a finite `composition` coordinator over existing P4 workflows and P5
  workspaces: reviewed plan, exact plan acceptance, controlled edit and checks,
  frozen read-only Fable review, then exact final acceptance.
- Pin exact task, revision, run, result, manifest and tree provenance between
  stages. Reviewer work starts from the frozen editor export, never the live tree.
- Preserve explicit acceptance boundaries and existing failure behavior: no
  automatic acceptance, retry, repair call, session replacement, merge or push.
- Add offline schema 9→10 migration with additive composition tables; existing
  task, workflow, workspace, decision, session and run records are unchanged.

## 0.8.0 — 2026-09-21

- Add explicit, session-pinned Claude effort to raw sessions, immutable task
  assignments, queues, workflow revisions and workspace turns.
- Add independent `workflow create --reviewer-effort` and requested-effort evidence.
- Probe CLI support before admission and again before launch; preserve model
  selection, safe mode, tools/MCP restrictions and environment isolation.
- Add resumable offline schema 8→9 migration. Legacy effort omission, prompt
  bytes, fingerprints and review decisions remain unchanged.
- Adopt normalized launch-setting patterns from OMX without a runtime dependency.
  Structured-output recovery remains separate follow-up work.

## 0.7.1 — 2026-09-21

- Reject non-UTF-8 surrogate text in workspace paths and named-check arguments
  at policy validation, before filesystem or process operations.
- Preserve valid Unicode, existing NUL rejection and existing policy limits;
  add five focused regression tests.
- Document the [real-project pilot](docs/real-project-pilot.md), including the
  timeout, rejected duplicate-key report and Codex-supervised correction. The
  automatic editor sequence did not pass; strict validation remains enabled.
- State schema remains 8. No database migration is needed from 0.7.0; finish
  or stop managed work before updating with `python3 install.py --update`.

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
