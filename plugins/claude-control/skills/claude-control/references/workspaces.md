# Controlled workspaces

`workspace` is a finite, controller-mediated file-work API for one local Git
repository snapshot. It is separate from ordinary supplied-text sessions,
tasks, queues, and P4 workflows. It requires a schema-8 state store and a
working Bubblewrap probe; there is no unconfined check fallback.

The Claude CLI remains in safe mode: its built-in tools and MCP servers are
disabled. Claude does not receive a shell, files, or an MCP controller tool.
Instead, it returns a bounded report requesting `read`, `write`, or
`run_check`; the controller validates and performs those requests. Anthropic
states that safe mode does not load customizations, including MCP servers:
[CLI reference](https://code.claude.com/docs/en/cli-reference).

## Create an executor workspace

Use a repository path that was configured as an allowed project root. The
source is an immutable Git commit: `--ref` is resolved with
`git rev-parse --verify <ref>^{commit}`, then selected blobs are read from that
commit. Dirty tracked changes and untracked files are ignored.

Create a bounded policy file. Paths are relative, selected explicitly, and
cannot name protected controller/Git/environment paths or the credential filenames rejected by the policy. This filename denylist is not content-based secret detection; select only task-authorized source files.

```json
{
  "version": 1,
  "role": "executor",
  "read_paths": ["src/", "tests/"],
  "write_paths": ["src/ledger.py", "tests/test_ledger.py"],
  "checks": {
    "unit": {
      "argv": ["/usr/bin/python3", "-m", "unittest", "tests.test_ledger"],
      "timeout": 30
    }
  },
  "max_actions": 12,
  "max_calls": 6
}
```

The exact create command is:

```bash
claude_control workspace create \
  --repo /example/ledger \
  --ref 0123456789abcdef0123456789abcdef01234567 \
  --policy-file /example/ledger-control/editor-policy.json \
  --operation-id ledger-editor-create-001
```

The response contains a generated workspace UUID, exact `base_commit`, and
`baseline_sha256`. Save that UUID. Repeating the same operation ID with the
same inputs deduplicates; changing its inputs conflicts. A creation interrupted
after its durable reservation can appear as `preparing` with
`creation_incomplete`. Inspect it; do not automatically replay or create a
replacement workspace.

## Create a read-only Sonnet scout

Before creating a composition, a Sonnet researcher can inspect the exact source
commit through the same controller loop. Use role `scout`, the editor policy's
exact `read_paths`, and no writes or checks. Bind a `researcher` assignment with
model `sonnet`; ask for a compact brief with `file:line` citations. Run it to a
finished frozen export, then pass its UUID to composition creation:

```bash
claude_control composition create \
  --scout-workspace <finished-scout-workspace-uuid> \
  --planning-assignment-file /absolute/path/plan.json \
  --editor-assignment-file /absolute/path/editor.json \
  --editor-policy-file /absolute/path/editor-policy.json \
  --reviewer-assignment-file /absolute/path/reviewer.json \
  --repo /absolute/path/repository --ref <same-commit> \
  --operation-id feature-composition-001
```

The controller accepts the scout only when its repository, base commit and
readable paths exactly match the composition. It pins the scout task, run,
result, manifest and tree digests plus the validated report into the planning
assignment. Composition creation still makes no model call.

Policy schema version 1 requires `role`, `read_paths`, `write_paths`, and
`checks`. It permits 1–64 readable paths, 0–64 writable paths, and at most
eight named checks. Every writable path must also be readable. Paths and check
arguments must be UTF-8-encodable; surrogate code points are rejected before
file or process operations. Valid Unicode is preserved without normalization.
Check commands
are fixed `argv` arrays whose executable is an absolute `/usr/bin/<basename>`;
the model can request a check by name but cannot supply an argv. A check timeout
is an integer from 1 to 60 seconds. `max_actions` is 1–100 (default 32) and
`max_calls` is 1–32 (default 8). Budgets count durably reserved requests and model calls, including failed, unstarted or unknown attempts; a crash does not refund them.

## Bind one task and run it

The workspace has one exclusive task. Its assignment's `project` must equal
the source repository path used at creation. The controller records the original
assignment but gives Claude only its private control directory, not the source
or working-tree paths.

```json
{
  "id": "ledger-editor",
  "name": "ledger-editor",
  "role": "executor",
  "model": "sonnet",
  "project": "/example/ledger",
  "objective": "Correct the supplied ledger edge case.",
  "context": "Apply the approved bounded change through workspace operations.",
  "scope": ["Only src/ledger.py and tests/test_ledger.py"],
  "acceptance_criteria": ["The named unit check passes"],
  "deliverable": "A complete workspace operation report.",
  "timeout": 300
}
```

```bash
claude_control workspace task \
  --workspace <editor-workspace-uuid> \
  --assignment-file /example/ledger-control/editor-assignment.json \
  --operation-id ledger-editor-task-001

claude_control workspace run \
  --workspace <editor-workspace-uuid> \
  --until-idle --max-seconds 30

claude_control workspace status --workspace <editor-workspace-uuid>
```

Use `--once` alone for one coordinator pass. `--until-idle` requires a finite
`--max-seconds` from 0 through 3600. This is a new-call admission window, not a
wall-clock cancellation: calls admitted before the deadline may finish later. An already-admitted synchronous operation batch may also finish after this window, under each check's timeout.
There is no background coordinator and no automatic Codex wake-up; invoke
`run` again deliberately after inspecting status. A stopped, failed, malformed,
blocked, budget-exhausted, or unknown execution is not automatically retried.

An intermediate Claude report has `status: "blocked"` and 0–8 requested
operations. `read` names a readable file, `write` replaces the full UTF-8
contents of one exactly writable file (at most 128 KiB), `patch` applies ordered
line hunks only when `base_sha256` matches the current file, and `run_check` names
one policy check. The controller records every request and result receipt; it
will not claim an operation succeeded without a returned receipt. A final report
has `operations: []` and model status `complete`.

Structured task turns pass the frozen report schema to Claude Code with
`--json-schema`; raw `start` and `followup` calls keep their existing unstructured
behavior. If a completed workspace call has an invalid report, no requests or
receipts, an unchanged tree, and remaining call budget, the controller creates
one same-session format-repair revision. A malformed repair is never retried.

## Export and accept the completed result

When a final valid report completes, the controller freezes the final working tree, writes its diff against the retained baseline and a verified manifest, and marks the
workspace `finished`. Only then is export available:

```bash
claude_control workspace export --workspace <editor-workspace-uuid>
```

The export contains the final run identity/digest, `base_commit`,
`baseline_sha256`, manifest, and immutable frozen directory. It is not a Codex
acceptance decision. A model report is also not acceptance. Codex must inspect
the frozen export and independently record `task accept` against that exact
final run and result digest; the controller rejects acceptance without an intact
final frozen export.

## Apply a frozen result to the source

Schema 12 adds one explicit source-application command:

```bash
claude_control workspace apply \
  --workspace <finished-editor-workspace-uuid> \
  --operation-id ledger-apply-001
```

Apply requires an intact nonempty frozen patch, source `HEAD` equal to the
workspace `base_commit`, and a clean tracked/untracked worktree. It runs
`git apply --check`, records an `applying` intent before mutation, applies the
exact frozen patch, then verifies the changed path set, content hashes and
executable modes. Success records `applied`; any failure after reservation is
`unknown` and must be inspected manually. Repeating the same operation ID only
returns its ledger record. Apply does not commit, merge or push.

## Read-only independent Fable review from a frozen snapshot

Create a reviewer only after the editor has a frozen export. A snapshot reviewer
must be role `verifier`, have no writable paths or checks, and use exactly the
editor policy's readable paths.

```json
{
  "version": 1,
  "role": "verifier",
  "read_paths": ["src/", "tests/"],
  "write_paths": [],
  "checks": {},
  "max_actions": 4,
  "max_calls": 3
}
```

```bash
claude_control workspace create \
  --from-snapshot <editor-workspace-uuid> \
  --policy-file /example/ledger-control/reviewer-policy.json \
  --operation-id ledger-reviewer-create-001
```

Bind an independent Fable assignment with role `critic` or `verifier`; its workspace policy role stays `verifier`. Fable is mandatory for this read-only snapshot lane.

```json
{
  "id": "ledger-review",
  "name": "ledger-review",
  "role": "verifier",
  "model": "fable",
  "project": "/example/ledger",
  "objective": "Review the frozen ledger change against the supplied criterion.",
  "context": "Return evidence and any limitations. Do not request changes.",
  "scope": ["Only the frozen selected files"],
  "acceptance_criteria": ["Assess the editor result independently"],
  "deliverable": "A read-only complete workspace report.",
  "timeout": 300
}
```

```bash
claude_control workspace task \
  --workspace <reviewer-workspace-uuid> \
  --assignment-file /example/ledger-control/reviewer-assignment.json \
  --operation-id ledger-reviewer-task-001

claude_control workspace run \
  --workspace <reviewer-workspace-uuid> --until-idle --max-seconds 30
```

The reviewer snapshot derives from the editor's frozen tree rather than the
live repository or editor working tree. Its report remains evidence for Codex;
it cannot accept the editor result or change the frozen snapshot.

## Check isolation and its boundaries

`run_check` runs only a fixed policy command against a disposable copy of the
workspace tree. Bubblewrap must start with a private user/PID/filesystem/network
setup; a failed probe rejects workspace creation, binding, and execution rather
than running a host check. The check has no host network, no controller or Claude
credential mount, no host project/home mount, and no explicit GPU/accelerator
mount. It receives only the disposable `/workspace`, generated `/proc` and
`/dev`, `/tmp`, and read-only runtime library paths needed for the approved
binary.

The launcher also sets finite per-process limits: CPU time, virtual address
space, file size, open files, process count, and no core dump. They are not
aggregate CPU, RAM, disk, or PID quotas for the entire sandbox or a cgroup.
Linux documents `RLIMIT_NPROC` as a real-UID thread limit and `RLIMIT_AS` as a
per-process virtual-address-space limit; resource limits are inherited by child
processes and preserved across exec:
[getrlimit(2)](https://man7.org/linux/man-pages/man2/getrlimit.2.html).

For namespace bootstrap, the process limit is computed from visible current
same-UID threads plus a fixed margin, capped by the inherited hard limit. This
avoids a low fixed NPROC limit failing before Bubblewrap can create its namespace;
it is deliberately not an aggregate quota or a guarantee against concurrent host
activity.

## Stop, reconcile, and migration

```bash
claude_control workspace stop \
  --workspace <workspace-uuid> --operation-id ledger-stop-001
claude_control workspace status --workspace <workspace-uuid>
claude_control workspace reconcile --workspace <workspace-uuid>
```

`stop` durably blocks new admissions and requests cancellation of owned active
runs. It remains `stopping` until owned workers and commands drain, then becomes
`stopped`. `reconcile` records an unresolved check as unknown only after its
recorded owner and launcher are proven dead in the recorded PID namespace. It
never replays a command or a model call. An `operating` workspace found after an
interrupted controller is moved to `awaiting_codex` with `operation_unknown`;
inspect receipts and state before any new decision.

Workspace creation and execution require schema 8; telemetry requires schema 11
and guarded apply requires schema 12. Existing older stores need the same
explicit offline migration discipline as earlier features: stop/reconcile active
or unknown work, stop old clients/workers, inspect `migrate --status`, then run
`migrate --offline` on the original state directory. Do not initialize a new
store or replay a preparing/unknown operation to bypass uncertainty.

Standalone P4 workflows and P5 workspaces remain separate. Schema-10
`composition` commands can explicitly connect a reviewed plan to an editor and
then to a read-only frozen-snapshot reviewer. The composition still never accepts
a result or integrates the frozen tree.
