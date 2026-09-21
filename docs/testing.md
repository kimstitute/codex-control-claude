# Testing

## Automated tests: no model calls

From the repository root:

```bash
python3 -m unittest discover -s tests -v
```

The original publication baseline had 47 tests. The assignment suite adds coverage
for the v1 role/report contracts and v0.3 task lifecycle. `test_tasks.py` covers
structured follow-ups, atomic concurrent submission, immutable revisions, exact-result
approvals, blocked/mismatched output and conservative retry. `test_migration.py`
checks v1 compatibility, WAL backup and interruption at each migration boundary. Tests use temporary directories and
fake Claude processes to exercise:

- Parallel capacity, per-session exclusion, and request deduplication.
- Targeted cancellation, timeout, worker failure, and recovery from ambiguous execution.
- Explicit resume/restart behavior and preservation of historical conversation IDs.
- Session/model validation, malformed output, tool-use rejection, and result-file integrity.
- Plugin relocation to a different path, including paths containing spaces.
- Installer serialization, rollback, and preservation of backups if restoration fails.
- Machine-readable handling of state database failures.
- Role instructions reaching Claude on stdin under the existing tool-disabled profile.
- Strict assignment inputs, deterministic retries, and changed-preset conflicts.
- Historical report validation, prompt tampering, and malformed/incorrect report identities.
- Multi-run observation of terminal, active, failed, and unknown outcomes.

These tests require Linux process facilities. They do not sign in, use a Claude account, or make model requests.

Optional static checks, if Ruff is already installed:

```bash
ruff check plugins tests install.py
ruff format --check plugins tests install.py
```

## Live integration: explicit account usage

Live tests invoke real Claude models, consume account usage, and create local Claude session history. They are never included in unittest discovery.

Use a **new output directory outside this checkout**:

```bash
python3 tests/live_smoke.py \
  --claude-bin /absolute/path/to/claude \
  --output /absolute/path/outside-repository/new-live-check
```

The suite creates a dedicated store and project. It checks overlapping Sonnet/Fable runs, independent conversation recall, cancellation of one run while its peer completes, resumption after cancellation, and nonexecution of a test project hook. Both model aliases must be available to your account.

The output includes prompts, responses, session IDs, process metadata, and a report. Treat it as private runtime data. The suite also leaves Claude's own normal persisted conversations on the testing machine. Do not commit these files or attach raw reports to public issues.

Live checks have passed during development. Their raw artifacts are excluded from this public repository; running the suite on your machine establishes evidence for your own CLI version and account.

For the smaller structured-delegation check (one Sonnet executor and one Fable
critic), use another new directory outside the checkout:

```bash
python3 tests/live_assignments.py \
  --claude-bin /absolute/path/to/claude \
  --output /absolute/path/outside-repository/new-assignment-check
```

It checks overlapping execution, role-default model routing, independent task
identities, `observe`, saved-prompt verification, valid structured reports,
`acceptance: unreviewed`, exact supplied-marker output, and zero Claude tool
calls. This small probe does not establish research quality, throughput at larger
parallelism, or correctness of arbitrary model-generated evidence.

## Live task revisions and approval

To deliberately make two real model calls and verify structured same-session recall:

```bash
python3 tests/live_tasks.py \
  --claude-bin /absolute/path/to/claude \
  --output /absolute/path/outside-repository/new-task-check \
  --model sonnet
```

This creates an isolated test store, checks exact token recall without including the
token in the second input, and records explicit criterion-based acceptance for both
revisions. It verifies the same backend, v2 reports, model evidence and an unchanged
project directory. `--cli /absolute/path/to/installed/claude_control_cli.py` can test
a relocated installation. The output includes private prompts and IDs; do not commit it.

A successful run establishes this bounded text-only flow, not arbitrary implementation
correctness or safe execution of Claude shell/edit tools.

## Queue and recovery tests

`test_scheduler.py` and `test_scheduler_recovery.py` exercise dependency cycles,
acceptance gates, manual-submit enforcement, waiting capacity, FIFO dispatch,
concurrent coordinators, exact input copying, oversized-input rollback, unknown
capacity, explicit retry and actual coordinator death before/after worker launch.
They include a deterministic 20-job admission check and actual fake-process overlap.
`test_queue_migration.py` covers schema 4→5 recovery at every checkpoint and
preservation of accepted v2 history. The older migration suite separately retains
schema 3→4 coverage; a new chain test reaches schema 5 from 3.

To explicitly call Sonnet twice and check an approval-gated parent/child workflow:

```bash
python3 tests/live_queue.py \
  --claude-bin /absolute/path/to/claude \
  --output /absolute/path/outside-repository/new-queue-check
```

The child starts only after exact-result acceptance, receives the parent's token
through its frozen dependency report in a separate session, and returns it exactly.
The check verifies persisted events, no duplicate dispatch and an unchanged project.
The optional `--cli` argument can validate installed code. This bounded check makes
no claim about the quality of arbitrary model-generated content.

## Messages and handoff

`test_messages.py`, `test_message_recovery.py` and `test_message_migration.py`
cover next-turn selection, operation deduplication, no implicit model turns, ordered
inputs, cancel races, stale targets, frozen source reports, explicit redelivery,
combined input limits, crash recovery, immutable receipts and schema 5→6 migration.

A deliberate two-call Sonnet integration check is available:

```bash
python3 tests/live_messages.py \
  --claude-bin /absolute/path/to/claude \
  --output /absolute/path/outside-repository/new-message-check
```

It queues two instructions during the first turn, attaches that result as a frozen
handoff, selects IDs in reverse order, and verifies delivery in registration order
on the resumed second turn. Exact returned strings, one receipt per message, no
implicit or duplicate execution, and an unchanged project are checked. Raw prompts
and IDs stay in the private output directory. `--cli` can check installed code.

## Bounded workflows

`test_workflows.py`, `test_workflow_recovery.py`, `test_workflow_cli.py` and
`test_workflow_migration.py` cover fixed policy, independent sessions, exact
structured reviews, revision handoff, reservation budgets, window persistence,
concurrent coordinators, stop races, false-acceptance prevention, finite CLI
bounds and schema 6→7 recovery. The full 3→7 chain also exercises creating,
running and stopping a workflow after migration.

To deliberately call Sonnet once and Fable once:

```bash
python3 tests/live_workflow.py \
  --claude-bin /absolute/path/to/claude \
  --output /absolute/path/outside-repository/new-workflow-check
```

This checks exact target/digest, criterion verdict, independent sessions, absence
of automatic acceptance, explicit Codex acceptance and no repeated execution.
It remains outside automated unittest discovery.

## Workspaces and isolated checks

`test_workspace_policy.py`, `test_workspace_files.py`, `test_workspaces.py`, `test_workspace_launch.py` and `test_workspace_migration.py` cover path/command authority, Git snapshots, bounded text operations, frozen integrity, shared capacity, cancellation, ambiguous recovery, launch identity races and schema 7→8 migration.

`test_workspace_sandbox.py` exercises actual Bubblewrap isolation without model calls: private host files and credential environment, network/GPU access, disposable writes, output/time limits and descendant cleanup after cancellation or coordinator death. It skips if namespace isolation is unavailable; a skip is not evidence that a host can run workspaces. Run it in the host's approved namespace-capable environment.

To deliberately exercise a Sonnet edit/check followed by an independent Fable frozen-file review:

```bash
python3 tests/live_workspace.py \
  --claude-bin /absolute/path/to/claude \
  --output /absolute/path/outside-repository/new-workspace-check
```

This synthetic Git fixture checks four model turns, exact edited content, a named unittest command in a disposable namespace, unchanged original files, independent review sessions, explicit acceptance and no duplicate calls. Claude native tools/MCP remain disabled; file operations are controller requests. Private output includes prompts and session IDs and must not be published.

## 0.7.0 release verification

The final release code passed all **258 tests on both Python 3.10 and Python 3.14**
in complete discovery runs. Both runs included the six actual Bubblewrap isolation
tests with no skips. Ruff, plugin/skill validation and repository whitespace checks
also passed. The automated runs used fake Claude processes; live integration
evidence is documented separately above.

## 0.7.1 real-project verification

The final runtime and test files passed **263 tests on Python 3.14.6**, including
all six real Bubblewrap isolation tests with no skips. The 14 workspace-policy
tests also passed on Python 3.10.18. The source files match the independently
tested copy and the two corrected files match Fable's frozen review snapshot.
Ruff, compilation, plugin/skill validation and whitespace checks passed.

The real editor pilot itself did **not** pass: a Sonnet timeout and a separate
duplicate-key report stopped before writes or checks. Codex corrected and tested
the code proposal independently; a separate read-only Fable review recommended
APPROVE. See the [pilot record](real-project-pilot.md) for provenance, the 6,177
independent boundary checks, and the required follow-up work. The editor tasks
remain unaccepted; acceptance of the review artifact does not change that.


## 0.8.0 execution-setting verification

The effort regression group covers strict setting validation, one explicit flag
per launch, raw and structured session continuation, independent reviewers,
workspace persistence, prior report/fingerprint compatibility, and offline
8→9 migration with crash injection at all five journal checkpoints.

Capability tests exercise unsupported admission without a reservation, support
loss before worker launch, executable replacement between supervisor and exec,
a one-second run with no wrapper probe, and idempotent replay without probing.
Queue tests exercise a new entry arriving during preflight, transient probe
failure without a reservation, and recorded dispatch operation replay.

Python 3.10 passes all 29 new tests. Sonnet supplied the bounded argument/validator
proposal; Codex integrated persistence and lifecycle behavior. Fable reviewed the
design and implementation in one preserved session and approved the repairs,
with the final whole-suite rerun now completed. The initial whole-suite run exposed
loss of the original cleanup error text; the implementation was corrected and
the existing regression passed. The final Python 3.14 run passed all 292 tests,
including six real Bubblewrap isolation tests, with no skips. Python source and
test hashes were held fixed throughout that final run. Ruff, plugin/skill validation
and local documentation links passed.

A separate deliberate live test verified Sonnet `medium` start and exact-session
resume with successful nonce recall, plus Fable `high` invocation. Actual CLI-reported
models were `claude-sonnet-5` and `claude-fable-5-1`. All three runs completed with
zero tool calls and no model substitution. The default store was idle before this
synthetic test store was created; it was not used to bypass an occupied slot.
Evidence records requested flags, not independently measured effective effort.


Installed verification matched all 32 plugin files and upgraded an idle schema-8
store to schema 9 using a verified backup. All prior columns and rowids across
the existing tables were identical: nine sessions and nineteen runs were retained.
Every historical report matched the previous v0.7.1 release both before and after
the migration. Database integrity and foreign keys passed; installed code also
read all three live test runs without making additional model calls.

## Version 0.9 composition verification

The final source tree passed 302 unit tests. One Bubblewrap namespace test class
was skipped because this host denies the required unprivileged namespace; the
controller reports that environment limitation rather than weakening isolation.
The 16 focused composition and schema-migration tests passed on the final tree,
including immutable source-ref replay, crash recovery, both explicit acceptance
gates, frozen reviewer creation, stop races, orphan cleanup and schema 9→10
history preservation. Ruff, formatting, Python compilation and `git diff --check`
also passed.

An independent Fable review used `claude-fable-5-1` with explicit high effort and
returned `APPROVE` for the bounded one-shot composition. Its three confirmation
items are covered by the composition-owned acceptance query, the derived read-only
reviewer policy test, and P5's export verification before snapshot copying. No
real-project execution was performed for this release because that is a separate
pilot scope.
