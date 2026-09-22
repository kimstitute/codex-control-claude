# Live agent monitor

`monitor` observes the current host's claude-control state without advancing,
retrying, stopping, accepting or applying any work.

## Run the TUI

```bash
claude_control monitor tui
claude_control monitor tui --refresh-seconds 1 --history 250
```

The default refresh interval is 0.5 seconds; valid values are 0.1–60 seconds.
History may contain 1–1000 runs.

| Key | Action |
|---|---|
| `1`, `2`, `3` | Select Graph, Agents or History |
| `Tab` | Select the next view |
| `↑`, `↓`, `j`, `k` | Scroll one line |
| `PageUp`, `PageDown` | Scroll one page |
| `a` | Toggle active/attention and full graph |
| `r` | Refresh immediately |
| `q` | Quit |

Graph shows composition → workflow/workspace → task → agent relationships and
task dependencies. Agents shows role, requested and actual model, current work
and state. History shows recent run state, tokens, duration and cost.

## Token and cost semantics

- A value such as `~1.2k` is Claude stream `estimated_tokens` while generation is
  active. It is explicitly an estimate.
- Finished runs use provider-returned input, cache-read, cache-creation, output
  and thinking token fields.
- Cost appears only when the provider returns `total_cost_usd`; the controller
  never estimates it from a price table.
- Runs completed before schema 11 have no ledger telemetry. History can read a
  preserved terminal stream, but immutable ledger totals are not backfilled.

## JSON snapshot

```bash
claude_control monitor snapshot --history 100
claude_control monitor snapshot --history 100 --no-live
```

The `claude-control.monitor.v1` response contains graph `nodes` and `edges`,
current `agents`, newest-first `runs`, and final ledger totals under
`summary.telemetry`. `--no-live` reads only SQLite and skips run streams.

