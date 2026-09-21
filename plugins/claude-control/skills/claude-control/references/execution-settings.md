# Execution settings

Choose a bounded timeout and explicit effort for new work:

```json
{
  "model": "sonnet",
  "effort": "medium",
  "timeout": 300
}
```

These are fields added to a complete assignment, not a complete assignment file.
Raw sessions accept `start --model sonnet --effort medium --timeout 300` alongside
usual identity/project/prompt flags. For research design or consequential review,
use Fable with an explicitly chosen effort, commonly `high`.

## Persistent settings

Effort accepts exactly `low`, `medium`, `high`, `xhigh` or `max`. Empty strings,
JSON null, `auto`, case variants and other values are rejected. This is the
controller's supported subset; model availability is determined by the installed
Claude CLI. There is no effort or model fallback.

A managed session pins its effort at creation. Every run copies that value, and
both the supervisor and exec wrapper validate it before launch. Resume sends the
same explicit flag again. An omitted flag on continuation inherits the stored
setting; an explicit different value is rejected. Restart also preserves effort.
Choose a new explicitly created task/session to use a different setting.

An omitted setting on a new session means no `--effort` flag. It is preserved as
NULL in schema 9 and is never relabeled as a known default. A legacy session
cannot acquire an explicit effort through followup, restart or revision.

Assignments preserve omission in historical prompt snapshots. A new task attached
to an existing session inherits its pin; a revision inherits the prior frozen
assignment. Supplied request fingerprints are computed before inheritance, while
run fingerprints include the resolved effort only if it is present. Old queued
work, hashes, reports and idempotency records remain interpretable.

## Independent reviewer

`workflow create --assignment-file task.json --reviewer-effort high --operation-id <id>`
records the reviewer's own effort. Without `--reviewer-effort`, its setting stays
omitted even when the worker has an explicit effort. Workflow revisions retain
both independent settings. Workspace turns retain their original assignment
setting; workspace policies do not grant new effort-changing authority.

## Time limits and evidence

`timeout` is the per-run process deadline, defaults to 300 seconds and accepts
1–3600 seconds. Task revisions may explicitly change timeout; effort stays fixed.
Workflow/workspace admission windows stop new calls and do not extend a run's
timeout. `wait --seconds` is only an observation interval.

`doctor.effort_supported` means the configured CLI advertises `--effort`. Explicit
effort is probed before admission outside database write transactions and freshly
checked by the worker. The wrapper verifies the worker's exact executable identity
immediately before exec, without spawning more probes inside the run deadline. If
support disappears or that executable changes, no model process is started and the
run fails with `effort_unsupported`; completed prior executions
are never replayed. A model-specific CLI rejection is a failed run with stderr
retained. A transient local probe error uses `effort_probe_failed`; a queued entry
remains waiting without a run reservation and can be examined again on a later
dispatch. No value is silently removed or substituted.

The child environment excludes `CLAUDE_CODE_EFFORT_LEVEL`, which can otherwise
override the flag. Invocation evidence stores the exact argv and `requested_effort`
when present. Reports expose requested effort only when present; the controller
does not claim an independently observed effective reasoning level.

## Upgrade

Finish or stop managed work and reconcile unknown executions, update the plugin,
then run `migrate --status` and `migrate --offline` against the existing store.
Schema 8→9 makes a verified backup and adds nullable fields without rewriting
existing prompts, results, fingerprints or acceptance decisions. Earlier formats
remain readable; explicit effort requires schema 9.
