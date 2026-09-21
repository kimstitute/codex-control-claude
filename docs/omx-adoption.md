# OMX Adoption Notes

This document records the part of the installed oh-my-codex (OMX) design that is
appropriate for the current controller implementation. It is an adoption note,
not a claim that the controller is already a team runtime.

## Source basis

The reference project is [Yeachan-Heo/oh-my-codex](https://github.com/Yeachan-Heo/oh-my-codex).
The local installation reviewed for this note was OMX 0.20.5. Relevant source
paths are relative to that installation:

- `dist/agents/definitions.js` — role records: name, description, reasoning
  effort, posture, model class, routing role, tool class, and category.
- `dist/agents/native-config.js` — deterministic posture/model overlays and the
  native-subagent leaf guard.
- `skills/team/SKILL.md` — the distinction between native subagents and the
  durable tmux team runtime, plus state-first dispatch and terminal monitoring.
- `skills/worker/SKILL.md` — claim-safe task lifecycle, worker status, ACKs, and
  mailbox delivery.
- `prompts/architect.md`, `prompts/critic.md`, `prompts/executor.md`, and
  `prompts/verifier.md` — role-specific boundaries and acceptance criteria.

These files are evidence for reusable contracts. They are not dependencies of
this controller and their runtime code is not copied into it.

## Current implementation slice

The current slice is deliberately smaller than OMX orchestration:

1. **Role routing** is explicit and persisted with each assignment. Routine
   implementation and bounded fixes use Sonnet. Planning, architecture, critique,
   and consequential verification use Fable. A caller-supplied model/role
   model override is recorded and wins over the default policy; no silent fallback is
   allowed.
2. **Prompt snapshots** are deterministic. The controller stores the normalized
   role, task, resolved model, capability guidance, and output contract as one
   canonical JSON prompt. The existing run-intent fingerprint covers that exact
   prompt and launch parameters. There is no second task database or sidecar file.
3. **Structured assignments** have an explicit task identity, owner, expected
   model, and bounded supplied-text payload. The controller delegates one
   well-defined assignment to the existing local Claude process boundary.
4. **Reporting and acceptance are separate.** Claude returns a structured report
   with summary, deliverable, evidence, limitations, and handoff. The controller
   checks its format. Legacy v1 reports remain `unreviewed`; v0.3 task revisions
   add explicit Codex approval bound to the exact run and digest. A reviewer
   recommendation alone is not approval.
5. **Task revisions and an approval ledger** preserve immutable input, exact parent
   context, operation deduplication and append-only criterion evidence. Submission
   links the revision and run in the same reservation transaction.
6. **Observation is read-only with respect to execution.** The controller can
   inspect several existing local run IDs and their terminal records without
   adopting, steering, or replacing an unknown session.

This slice adds no queue, DAG scheduler, lease service, mailbox, long-running
daemon, tmux runtime, Claude tools/MCP, or cross-host dispatch. It is intended to
preserve the existing Linux supplied-text profile while making role intent and
verification evidence explicit.

## Native subagents versus OMX teams

OMX describes native subagents as bounded in-session fan-out whose leader waits
for direct results. Its tmux team mode is a different operational surface: it
adds durable shared task state, worker mailboxes, dispatch files, lifecycle
commands, and panes that can outlive one reasoning burst. The current controller
adopts bounded-assignment semantics and explicit local task/review records. It does not pretend that
a Claude subprocess is an OMX worker and does not require tmux, pane text, or
synthetic key presses for correctness.

The portable rule is state-first: an assignment must be accepted by the local
controller before launch, and its result must be tied back to the same assignment
ID. Any future asynchronous coordinator must preserve that rule and add a real
durable queue rather than using process output or a UI as the queue.

## Existing protections that remain authoritative

OMX role adoption does not weaken the controller's existing protections:

- per-session serialization and one active writer;
- idempotent request identity and rejection of ambiguous retries;
- server/installation ownership checks and host mismatch rejection;
- explicit requested-model versus actual-model recording;
- process identity checks using Linux start ticks, boot identity, and PID namespace,
  rather than PID alone;
- supplied-text execution with Claude tools and MCP disabled;
- local workspace, credential, transcript, and controller-state boundaries.

The existing SQLite claim and durable run records remain the source of truth for
local acceptance. A prompt role or a report cannot grant a lease, unlock a
workspace, adopt an unknown session, or authorize a second writer.

## Later-phase invariants

If asynchronous delegation is added later, it must be introduced as a separate
phase with tests and a reviewed state-machine change. At minimum it must prove:

- claim is atomic and ownership-bound;
- every claim has a single-use idempotency key and durable revision;
- a lost response does not cause an ambiguous Claude execution to be retried;
- cancellation and process termination are reported separately from request
  acknowledgement;
- result publication is atomic and references the exact assignment and prompt
  snapshot;
- stale workers cannot update a newer controller epoch;
- terminal monitoring reaches a verified state before a reservation is released.

OMX's mailbox/ACK and state-first lifecycle are useful design references for
that phase, but they do not replace the controller's SQLite claim, process
fencing, or reconciliation rules. Likewise, tmux can become an optional display
or launch adapter only after these invariants are independent of it.

## Non-goals and open concerns

This note does not establish Windows support, cross-host communication, remote
MCP, GitHub transport, or unattended operation. It also does not establish that
Fable session resume/fork or arbitrary existing-session adoption is safe. Those
capabilities require separate host-local probes and explicit contracts.

The main open concern is semantic acceptance: structured output makes evidence
machine-readable, but it does not prove that a research or code result is true.
The caller or a separate verifier must supply the relevant tests, review, or
artifact checks before using the result. Execution status never means acceptance.

## Implemented P2: durable local dispatch

Version 0.4 implements the next slice: bounded FIFO waiting, exact task/revision
dependencies gated by Codex acceptance, immutable copied execution input, finite
explicit dispatch and persisted events. SQLite transactions and existing worker
claims prevent duplicate reservations; no tmux, daemon or lease service is added.
Coordinator restart observes prior runs without relaunching them. See
[queue operations](queue.md). P3 messages and P4 review workflows build on this
dispatch layer, as described below.

## Implemented P3: explicit next-turn instructions

Version 0.5 adds Codex-selected instruction messages and frozen result handoff,
with exact base-revision checks and append-only delivery receipts. It adopts the
useful durable-message idea without injecting text into terminal panes or granting
agents recursive delegation. An execution receipt is distinct from comprehension
and semantic acceptance. Bounded review/fix workflows are added by P4 below.

## Implemented P4: fixed review workflow

Version 0.6 combines task revisions, queue admission and messages into a fixed proposal/review/revision template. It adds persistent budgets, exact criterion-level review decisions and attention summaries. It does not adopt OMX tmux supervision, recursive spawning or a resident coordinator. See [workflow behavior](workflows.md).

## P5: explicit workspace authority

Version 0.7 adds a separate finite workspace loop for controller-mediated file reads, full-file writes and named isolated checks. It preserves existing Claude safe mode and task history, freezes results for independent review, and leaves integration to Codex. This adopts explicit ownership and verification boundaries without introducing tmux control, native tool access, recursive agents or automatic merges. See [workspaces](workspaces.md).
