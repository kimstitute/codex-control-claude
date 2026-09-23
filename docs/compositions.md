# Plan, edit and frozen-review compositions

Version 0.9 adds one finite composition over the existing P4 workflow and P5
workspace engines:

```text
bounded plan + independent Fable critique
  -> exact Codex acceptance of the plan
  -> controlled editor workspace and named checks
  -> frozen editor export
  -> independent read-only Fable workspace from that export
  -> exact Codex acceptance of the editor result
```

The coordinator does not accept a result, retry a failed or unknown execution,
replace a session, merge a branch, push, or wake Codex. Each `composition run`
call is explicit and bounded. Existing `workflow` and `workspace` commands keep
their standalone behavior.

## Create and run

Prepare four JSON files: the planning assignment, editor assignment, editor
workspace policy and reviewer assignment. The reviewer policy is derived from
the editor policy with the same readable paths, no writable paths, no checks and
the verifier role. The reviewer assignment must use Fable with role `critic` or
`verifier`.

```bash
claude_control composition create \
  --planning-assignment-file /absolute/path/plan.json \
  --editor-assignment-file /absolute/path/editor.json \
  --editor-policy-file /absolute/path/editor-policy.json \
  --reviewer-assignment-file /absolute/path/reviewer.json \
  --scout-workspace <optional-finished-scout-workspace-uuid> \
  --repo /absolute/path/repository --ref HEAD \
  --operation-id feature-composition-001 \
  --max-revisions 2 --max-calls 6 \
  --dispatch-window-seconds 900 --reviewer-effort high

claude_control composition run --composition <uuid> --once
claude_control composition run --composition <uuid> \
  --until-idle --max-seconds 60
claude_control composition status --composition <uuid>
```

Creation resolves the requested Git ref to one immutable commit, records the
composition and P4 workflow, and makes no model call. Later repository movement
cannot retarget the editor snapshot. The optional scout must be a finished
read-only Sonnet researcher workspace with the same commit and readable paths.
Its validated report and exact provenance are copied into the planner context.
The
planning workflow stops at `approve_recommended`. Inspect its exact worker task,
revision, run and result digest, then record `task accept`. A later bounded
composition run copies that verified plan report and provenance into the editor
assignment before creating the editor workspace.

### Leader-authored specification

Version 0.15 can keep specification authorship with the Codex leader. Replace
the planning assignment with a strict leader-spec document and a Fable critic
assignment:

```json
{
  "version": 1,
  "id": "feature-spec",
  "name": "Feature specification",
  "objective": "Describe the required behavior.",
  "context": "Relevant constraints and evidence.",
  "scope": ["Allowed implementation scope."],
  "acceptance_criteria": ["Observable criterion."],
  "implementation_plan": ["Ordered implementation step."]
}
```

```bash
claude_control composition create \
  --leader-spec-file /absolute/path/spec.json \
  --critic-assignment-file /absolute/path/fable-critic.json \
  --editor-assignment-file /absolute/path/editor.json \
  --editor-policy-file /absolute/path/editor-policy.json \
  --reviewer-assignment-file /absolute/path/reviewer.json \
  --repo /absolute/path/repository --ref HEAD \
  --operation-id feature-composition-001 --reviewer-effort high
```

The critic must use Fable and the `critic` role. It cannot replace the immutable
specification. Its bounded workflow permits no critic revision; a requested spec
change requires a new leader document and composition. Editing still waits for
the independent critique recommendation and exact Codex acceptance. The editor
then receives the leader specification, its digest and the accepted critique as
separate provenance.

### Frozen test contract

Add `--test-contract-file` to run controller-owned baseline checks before the
editor is bound and require matching check receipts on the final tree before the
frozen Fable reviewer is created:

```json
{
  "version": 1,
  "frozen_paths": ["tests/test_feature.py", "tests/fixtures/"],
  "checks": {
    "unit": {"baseline": "fail", "post": "pass"}
  }
}
```

Every check name must already exist in the editor policy. `baseline` is `pass`
for regression/refactor work or `fail` for a red test. `post` is always `pass`.
Frozen paths must be readable and cannot overlap editor writes. The baseline
receipt, frozen-file hashes and final-tree check receipts are recorded through
the existing idempotent operation ledger. Missing, failed or stale final-tree
receipts stop the composition before reviewer creation.

### Evaluate recorded compositions

```bash
claude_control composition evaluate --all
claude_control composition evaluate \
  --composition <uuid> --composition <uuid>
```

Evaluation performs SELECT queries only. It reports terminal readiness,
acceptance and unassisted-success rates with 95% Wilson intervals. Cost and the
four provider token fields are aggregated only when every attributed run has
complete telemetry; partial and missing records stay separate. Complete-case
cost per success includes measured failed attempts in its numerator.

When the editor freezes a final export, the next composition step automatically
creates a separate reviewer workspace from that frozen tree. It never reads the
live repository or a mutable editor tree. The reviewer returns a structured
verdict for every editor criterion plus an `approve`, `revise`, or `blocked`
recommendation. It cannot accept or change the editor result, but `revise` and
`blocked` veto editor acceptance.

After both exports exist, an approval reports
`awaiting_codex/final_review_ready`. Other recommendations report
`final_review_revise` or `final_review_blocked` and keep the acceptance gate closed.
Inspect the plan, editor export and reviewer export, then use `task accept` on the
exact editor task revision, run and result digest. Composition status derives
`accepted` only from that exact ledger record and intact frozen evidence.

## Failure, stop and recovery

Malformed or blocked reports, exhausted budgets, changed context, model mismatch,
unknown execution and interrupted workspace creation stop the composition at an
attention state. Running it again does not repair, retry or replace that work.
Use the existing run or workspace diagnostics and reconcile only the identified
uncertain child.

```bash
claude_control composition stop --composition <uuid> --operation-id stop-001
```

Stop delegates to the currently active workflow or workspace and preserves all
plans, receipts, snapshots and exports. Repeating the same stop operation is
idempotent.

## Provenance and compatibility

Schema 10 adds only composition, member and exact-result tables. It does not
rewrite historical workflow, workspace, task, decision, session or run rows.
Every stage pins task, revision, run and result digest. Workspace stages also pin
the frozen manifest and tree digests. Upgrade an idle schema-9 store with the
normal verified `migrate --offline` procedure.
