# Per-role model and effort settings

Codex Control Claude can assign a Claude model and optional effort level to each
controller role. Changes apply to newly created work. Existing tasks and sessions
keep the resolved settings frozen at creation time.

## Model selectors

Use either a moving family alias (`sonnet`, `opus`, `haiku`, or `fable`) or an
exact `claude-...` model ID. Aliases verify the provider-reported model family.
Exact IDs require byte-for-byte equality, so use an exact ID when a version must
be pinned. UI labels such as `Opus 5` are display names, not configuration values.

The controller does not maintain an account-specific version catalog or silently
fall back. An unavailable selector fails visibly on its first invocation.

Effort accepts `low`, `medium`, `high`, `xhigh`, or `max`. Omit the key to use
provider behavior or for a model that does not support effort. Unsupported
model/effort combinations fail without dropping or changing the flag.

## Show and replace settings

```bash
claude_control models show
claude_control models configure --file /absolute/path/to/role-models.json
claude_control models reset
```

The replacement file must define every supported role exactly once:

```json
{
  "contract": "claude-control.role-models.v1",
  "roles": {
    "executor": {"model": "claude-sonnet-5", "effort": "medium"},
    "researcher": {"model": "claude-haiku-4-5-20251001"},
    "planner": {"model": "claude-opus-5", "effort": "high"},
    "architect": {"model": "claude-fable-5-1", "effort": "xhigh"},
    "critic": {"model": "claude-fable-5-1", "effort": "high"},
    "verifier": {"model": "claude-opus-5", "effort": "high"}
  }
}
```

The validated document replaces the private host-local settings atomically. A
partial or invalid document leaves the prior settings intact. `reset` restores
the compatible built-in Sonnet/Fable defaults.

An assignment's explicit `model` or `effort` overrides its role default only for
that work item. Internal workflow reviewers use the critic default and may be
overridden with `--reviewer-model` and `--reviewer-effort`. The resolved selector
and effort are frozen in task/session provenance; the actual provider model ID is
recorded separately and a mismatch fails as `model_mismatch`.
