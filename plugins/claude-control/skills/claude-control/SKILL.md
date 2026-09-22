---
name: claude-control
description: Manage multiple persistent Claude Code sessions on the current Linux host from Codex. Use for delegating supplied-text implementation, research planning, verification or critique to local Sonnet/Fable, performing bounded edits and checks in explicit private workspaces, inspecting managed work, and stopping or resuming an identified session.
---

# Claude Control

Use the bundled `../../scripts/claude_control_cli.py`, resolving that path from this skill's directory. Public commands return JSON. Python 3.10+ and locally authenticated Claude Code are prerequisites; run `--help` for flags. Version 0.10 supports Linux supplied-text delegation, JSON-schema task reports, base-hashed patch operations, one bounded format repair, reviewer vetoes, typed acceptance evidence, and finite plan/edit/review compositions. Claude native tools and MCP are disabled, with CLI safe mode and empty setting sources. The project-hook nonexecution test passed; administrator-managed policy still applies. For proposals, supply source text and inspect returned edits. For authorized file work, read [the workspace reference](references/workspaces.md) before creating a policy. The controller executes structured read/write/patch/named-check requests on private copies; source integration remains a Codex action.

## Execution settings

For new routine Sonnet work, explicitly choose `effort: "medium"`; for new Fable
design or consequential review choose `effort: "high"`, unless the user specifies
another supported setting. Choose a bounded `timeout` (1–3600 seconds) for each
run; a timeout is an unsuccessful execution, not permission to retry. Effort is
pinned to the session and task. Raw `start` accepts `--effort`; continuation flags
only assert the existing value. Omission inherits on continuation. Never reinterpret
a legacy omitted setting as medium/high or silently replace its session.

Supported values: low, medium, high, xhigh, max. JSON null is invalid. `doctor`
reports `effort_supported`; unsupported settings fail without fallback. `requested_effort`
in invocation/report evidence records what was sent, not independently measured
model reasoning. Parent `CLAUDE_CODE_EFFORT_LEVEL` is excluded from the child environment.
For a new Fable review workflow, explicitly pass `--reviewer-effort high`; worker
effort never supplies the reviewer default. P5 workspace turns reuse the frozen
assignment setting. Read [execution settings](references/execution-settings.md).

## First use

Run `doctor --auth` and `list` against the intended state directory. Default: `$XDG_STATE_HOME/claude-control`, or `~/.local/state/claude-control`. If uninitialized, use `init --claude-bin <absolute executable> --allow-root <authorized project directory> --max-parallel 2`. Do not initialize a second store to bypass an occupied slot or an ambiguous run. Each host/user owns its own configuration and records; this package does not transfer jobs or credentials between servers.

For a custom store, keep the same global option **before the subcommand on every call**: `python3 <resolved-cli-path> --state-dir <absolute-state-dir> <subcommand> ...`.

Claude invocations need network access and write access to its normal local session history. If the host's sandbox prevents these, use its normal approval mechanism for the concrete local invocation. Short-lived PID sandboxes may terminate detached workers when the launching command ends; use the host's supported persistent execution environment. Do not interpret sandbox-denied I/O as a reason to change credentials or model.

Status queries also refresh durable state and need write access to the state database. If a default home-directory store is outside the workspace sandbox, run these commands through the normal approval mechanism too; do not copy the database into another store to work around permissions.

This version uses the normal HOME-based Claude login; custom `CLAUDE_CONFIG_DIR` and API-key environment variables are not forwarded. Doctor uses the same environment as jobs.

## Track a task across revisions

Prefer `task` for assignments that need structured follow-ups or an approval record.
The input assignment schema and role/model choices are described below.

1. `task create --assignment-file <file> --operation-id <stable-id>` records revision 1
   without a model call. Save the task UUID. To attach an existing managed session,
   also give `--session <UUID> --parent-run <exact latest run UUID>`; role/model/project
   must match. Inspect an unsuccessful parent before `--acknowledge-context`.
2. `task submit --task <UUID> --revision 1 --operation-id <stable-id>` reserves and starts
   one turn. Reuse the same operation ID after a lost response; a changed request conflicts.
   Save the returned run ID and use `observe`, `wait`, `report` and `result` as usual.
3. Inspect `task show --task <UUID>` for immutable inputs, run history and decisions.
   `task list` summarizes current states. `queued/awaiting_submit` needs explicit submission;
   no scheduler runs in the background. Check report format, actual model and agent status.
4. For a review, write a bounded JSON evidence object mapping every 1-based acceptance
   criterion (`"1"`, `"2"`, ...) to nonempty evidence. Record it with `task review --task
   <UUID> --revision <N> --run <UUID> --result-sha256 <digest> --evidence-file <file>
   --operation-id <stable-id> --reviewer <label> --recommendation approve|revise|blocked`.
   This records a recommendation and never accepts the result.
5. Only after Codex has independently verified the criteria, use `task accept` with the
   same task/revision/run/digest/evidence flags and its own operation ID. It omits reviewer
   and recommendation flags. Stale revisions, corrupt results, model mismatches and blocked
   reports cannot be accepted. Reviewer labels are audit metadata, not authentication.
6. To request changes, use `task revise --task <UUID> --revision <current N>
   --assignment-file <file> --parent-run <exact latest run UUID> --operation-id <stable-id>`.
   Then submit the returned new revision separately. Role, model, project and assignment ID
   stay fixed; each revision preserves its prompt. Session context is resumed, with a new
   explicit report contract. Failed parent turns require inspected context and deliberate
   `--acknowledge-context`. Intervening controller follow-ups/restarts cause `context_changed`;
   do not silently retarget. Claude turns made outside the controller are not detectable.

`task retry` takes the submit flags with a new operation ID, and only admits terminal
attempts proven never to have started Claude. A process dying after commit but before
worker launch leaves a reservation that must expire before explicit retry; duplicate
submission does not launch again. It cannot retry unknown or executed failed turns. Never treat idempotent replay as permission to launch another worker.

## Queue dependent tasks

Use `task enqueue --task <UUID> --revision <N> --operation-id <stable-id>` to queue
work without reserving a run. Optional `--dependencies-file <file>` is a bounded
JSON array of exact `{"task_id":"<UUID>","revision":1}` parent references (up to 64).
Parents must still be current and explicitly accepted by Codex at admission.
Review recommendations do not open this gate; direct submit/retry cannot bypass it.

Run `dispatch --once` or `dispatch --until-idle --max-seconds <0..3600>` explicitly.
Ready entries follow FIFO; blocked entries do not stop independent tasks. Default
waiting capacity is 100, separate from two execution slots. The deadline ends new
admissions, not existing workers. Inspect `started`, `waiting`, `active`, `attention`
and `reason`; do not equate dispatcher idle with semantic acceptance of results.
No background daemon or automatic Codex wake-up exists.

Use `task events [--task <UUID>] --after <cursor> --limit 100` and retain `next_cursor`.
`task dequeue --queue-id <integer> --operation-id <id>` removes waiting work only.
Dependencies remain pinned after dequeue. Re-enqueue requires identical dependencies;
changing them requires a new revision. Unsubmitted tasks can revise without a parent
run; a new revision supersedes waiting entries and requires its own enqueue/dependencies.

A worker continues independently after a dispatcher exits. Restarting dispatch never
relaunches existing reservations. For an expired, provably unstarted run use explicit
`task retry`; it rebinds the same queue entry and requires identical execution bytes.
Unknown runs retain slots: inspect/reconcile them, never automatically retry or create
replacement sessions. Acceptance has no revocation in P2; a new revision needs new
acceptance. Queued execution v3 copies validated parent reports/provenance within the
1 MiB final input limit; preserve this immutable history.

## Store instructions for the next turn

Use `message enqueue --task <UUID> --base-revision <current N> --content-file <UTF-8 file>
--operation-id <stable-id>` to save instructions, including while Claude is active. Optional
`--session <managed UUID>` asserts the target session. This never creates a run or writes to
an active process. Save returned message IDs. `message list --task <UUID>` shows selections,
bindings and lifecycle state; use its cursor for new registrations, re-query for state updates.

Once the session is idle, add repeatable `--message-id <UUID>` to `task revise`. IDs execute
in stored sequence order, not flag order. Then explicitly submit or enqueue the returned
revision. Nothing is inherited automatically. A stale unbound base requires cancellation
and new enqueue, never silent retargeting. At most 64 selected and 100 unbound messages per task.

To hand off a task result, supply `--source-run <UUID> --source-result-sha256 <digest>` together
at enqueue. Only intact, completed v2/v3/v4/v5 task reports qualify; blocked critic reports are
allowed and do not need acceptance. The report is copied and its provenance retained. Handoff
is not acceptance and does not satisfy a queue dependency's approval gate. Claude cannot
create messages or invoke other agents through this controller.

`message cancel --message <UUID> --operation-id <id>` only cancels unbound messages. If already
selected, that revision's submit will be rejected; revise the selection explicitly. To stop
execution use `stop --run`; bound receipts are immutable. For an inspected stopped failure,
select the message in a new revision with `--redeliver-messages` and deliberate context
acknowledgement as needed. Completed/unknown deliveries cannot use this path. Reconcile unknown
first. A failed attempt may already have read the instruction; redelivery is not exactly-once
processing. An explicitly superseded redelivery draft is historical and cannot execute.

`task retry` only covers proven unstarted attempts of the same revision and requires identical
input bytes. Every reservation appends its own receipt. `bound_to_run` means reserved;
`run_completed` means completed execution with an intact result, not understanding or acceptance.
Always verify the result separately. Full input, including dependencies and messages, must fit
within 1 MiB; overflow creates no run or receipt. Use event cursors, not timestamps, for order.

## Run a bounded proposal/review workflow

Use `workflow create --assignment-file <file> --operation-id <stable-id>` when Codex
has authorized the fixed proposal → independent Fable review → optional revision
flow. Worker role is executor/planner/architect; choose Sonnet for routine proposals
and Fable for planning/design. Reviewer is fixed Fable/critic in a separate session.
Creation makes no model call. Save workflow and both member task UUIDs.

Freeze limits at creation: `--max-revisions` (default 2, 0..10), `--max-calls`
(default 6, 2..64), `--dispatch-window-seconds` (default 900, finite 1..86400).
A reservation consumes one call even if it fails or never launches. The window
starts with the first successful reservation and survives restarts. New worker
revisions need two remaining calls for their proposal and review. No policy change,
automatic retry, or model-selected graph is allowed.

Run `workflow run --workflow <UUID> --once`, or `--until-idle --max-seconds <0..3600>`
explicitly. It uses the existing scheduler scoped to the named workflow. The local
coordinator deadline does not cancel existing workers. The loop waits for live
capacity without dispatching unrelated queued work. Observe with `workflow status`
and `overview --attention`; queries do not start work or advance stages. Re-run the
same active workflow to continue after a coordinator exits, never clone it to bypass
unknown execution. No resident coordinator or automatic Codex wake-up is provided.

The v5 review report adds exact target task/revision/run/digest, fixed criterion IDs
with verdict/evidence, recommendation, unverified items and revision instructions.
Only valid complete results open template-owned reported edges. Ordinary dependencies
still require Codex acceptance. Frozen P3 handoffs carry the worker report and review
instructions across stages. No text keyword matching controls the loop.

`awaiting_codex/approve_recommended` is a recommendation, not acceptance. Independently
verify the current worker result and use `task accept` with exact run/digest and
criterion evidence. Workflow status can show accepted only with that explicit valid
record. Public member submit/retry/revise/enqueue/dequeue and message enqueue/cancel
cannot alter this template.

`awaiting_codex` is terminal for automation, including malformed/blocked/failed/unknown
runs, exhausted limits or changed context. There is no workflow retry/reset. Inspect
artifacts and use explicit acceptance for a valid current result, stop, or a deliberate
new workflow after resolving uncertain execution. `workflow stop --workflow <UUID>
--operation-id <id>` first blocks admissions, then requests owned-run cancellation.
Inspect stopping/stopped; repeated stop or run resumes interrupted cleanup. Unknown
runs remain quarantined until existing `reconcile --run` proves execution is dead.

## Delegate a single report

The legacy `delegate` path remains available for a new bounded assignment:

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
`result`. Use the task revision workflow above for structured multi-turn work.

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

New stores use schema 12. For an existing schema-3 through schema-11 store, finish/stop and reconcile all
managed work, stop all old CLI clients/workers, install the new code, then run
`migrate --status` and `migrate --offline` on that same state directory. Migration
makes a verified SQLite backup and a durable journal. If interrupted, repeat
`migrate --offline` on the original directory; never run jobs against the backup.
Active/unknown executions block migration. New code retains legacy diagnostic and
stop/reconcile commands on schema 3; task lifecycle needs schema 4, queue commands schema 5, messages schema 6, workflows schema 7, workspaces schema 8, explicit effort schema 9, compositions schema 10, run telemetry schema 11, and guarded workspace apply schema 12. Do not use
re-initialization, automatic downgrade or a fresh store to bypass uncertainty.

The package launches workers for jobs, not an always-running coordinator. Codex is not automatically awakened after the conversation ends. A later Codex task can discover the same host-local records via `list`.

## Work in an explicit workspace

Read [workspaces](references/workspaces.md) for policy, lifecycle, scout, frozen review, guarded apply and recovery. Use an immutable Git commit snapshot, bounded readable paths, exact writable files and named check argv authorized for the task. Run `workspace doctor` first. Bind an executor, a read-only Sonnet researcher scout, or a read-only Fable critic/verifier; drive `workspace run` with an explicit finite budget. Never enable native Claude tools to satisfy a workspace request. A completed report and frozen export still require Codex verification and explicit acceptance. `workspace apply` is an explicit operation and never commits, merges, or pushes.

## Compose a plan, controlled edit and frozen review

Use `composition create` only when Codex has authorized the full fixed chain:
bounded P4 plan/review, exact plan acceptance, P5 editor, frozen snapshot reviewer,
and exact final editor acceptance. Supply separate planning, editor and Fable
reviewer assignments plus one editor workspace policy. The controller derives a
read-only verifier policy with the same readable paths. Creation makes no model
call.

Drive the named composition with `composition run --composition <UUID> --once`
or `--until-idle --max-seconds <0..3600>`. It uses the existing child engines and
persists exact task/revision/run/result and frozen-export provenance. The P4
approve recommendation does not start editing until Codex accepts that exact plan.
The reviewer is created automatically only after the editor export is frozen; it
does not need or create editor acceptance. After its own frozen report, the
composition stops at `awaiting_codex/final_review_ready`. Codex then independently
accepts the exact editor result. Status derives `accepted` from that ledger record.

The composition never retries malformed, failed or unknown execution, changes a
model or session, repairs a report, accepts a result, merges, pushes or wakes Codex.
Use `composition stop` for the owned active child. Diagnose and reconcile the exact
child when attention is required; do not create a replacement composition to hide
uncertain execution. Read the repository's `docs/compositions.md` for CLI examples.
