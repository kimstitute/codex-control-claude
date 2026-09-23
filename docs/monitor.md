# Live agent monitor

`monitor` observes the current host's claude-control state without advancing,
retrying, stopping, accepting or applying any work.
It uses curses on Linux and the ANSI/VT console backend on Windows 10/11.

## Run the TUI

```bash
claude_control monitor tui
claude_control monitor tui --refresh-seconds 1 --limits-refresh-seconds 60 --history 250
```

Agent state refreshes every 0.5 seconds by default; valid values are 0.1–60
seconds. Provider limits refresh independently every 60 seconds to avoid
hammering account APIs; valid values are 30–3600 seconds. History may contain
1–1000 runs.

| Key | Action |
|---|---|
| `1`, `2`, `3`, `4`, `5` | Select Graph, Agents, History, Limits or Replay |
| `Tab` | Select the next view |
| `↑`, `↓`, `j`, `k` | Scroll one line |
| `PageUp`, `PageDown` | Scroll one page |
| `a` | Toggle active/attention and full graph |
| `←`, `→` | Move the Replay cursor by one event |
| `[`, `]` | Move the Replay cursor by ten events |
| `Home`, `End` | Jump to the first event or return to live history |
| `Space`, `p` | Play or pause recorded history at the TUI refresh rate |
| `r` | Refresh immediately |
| `q` | Quit |

Graph shows composition → workflow/workspace → task → agent relationships and
task dependencies. Agents shows role, requested and actual model, current work
and state. History shows recent run state, tokens, duration and cost. Limits
shows reported quota use, capacity left, reset times, token/request amounts and
overage spending for signed-in Codex, Claude Code, Gemini CLI and Cursor CLI.
Replay reconstructs the historical graph at a durable cursor. `LIVE` follows the
latest event, `PAUSED` holds a selected cursor and `PLAYING` advances one event
per refresh interval until it reaches the recorded high-water mark.

## Account limits and remaining tokens

```bash
claude_control monitor limits
claude_control monitor snapshot --limits
claude_control monitor limits --local-only
```

The monitor preserves the unit each provider actually reports.

| Provider | Limit source | Absolute token amounts |
|---|---|---|
| Codex | Period percentages and resets from `account/rateLimits/read` | Today's and lifetime used tokens come separately from `account/usage/read`. The subscription token ceiling is not published, so remaining tokens are not calculated |
| Claude Code | Service-reported cache in `~/.claude.json` | 5-hour and weekly percentages are exact, but there is no token ceiling. Tokens run through claude-control are shown separately |
| Gemini CLI | Per-model Google Code Assist quota buckets | Used, remaining and total appear when the bucket includes `remainingAmount` and `tokenType`; a `REQUESTS` bucket remains a request count |
| Cursor | Cursor dashboard billing-cycle percentage | Cursor does not publish the absolute included-token ceiling; the monitor shows included-use percentage and overage spending |

The monitor never converts `75% remaining` into an invented token count. Rows
without an absolute allowance say `absolute tokens unavailable`. Output includes
only installed/signed-in state, never access tokens, account IDs, email or project
IDs. Codex is queried through its own app server. Cursor and Gemini credentials
are sent only to their provider hosts. Claude cache reads and `--local-only` use
no network.

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
claude_control monitor snapshot --history 100 --limits
```

The `claude-control.monitor.v1` response contains graph `nodes` and `edges`,
current `agents`, newest-first `runs`, and final ledger totals under
`summary.telemetry`. `--no-live` reads only SQLite and skips run streams.

## Durable observation history and replay

Schema 14 adds an append-only `claude-control.observation.v1` event ledger. The
ledger is the durable source for the future graph viewer and time-travel replay;
wall-clock timestamps are descriptive while the monotonically increasing cursor
defines order.

```bash
claude_control monitor events --after 0 --limit 100
claude_control monitor events --after 100 --limit 100 --through 500
claude_control monitor replay
claude_control monitor replay --through 500
claude_control monitor agui snapshot --through 500
claude_control monitor agui events --after 100 --limit 100 --through 500
```

`events` uses an exclusive `--after` cursor and an optional inclusive
`--through` high-water mark. `replay` folds the baseline plus later events into
the exact graph state at an inclusive cursor. A migrated store reports
`baseline_only` fidelity when pre-schema-14 transitions cannot be reconstructed;
new transitions after that baseline remain exact.

The observation contract contains stable identifiers, lifecycle states,
relationships, timestamps and numeric telemetry. It excludes prompts, results,
message bodies, policies, project paths, operation receipts, account identity and
credentials. Raw provider JSONL remains private controller evidence and is not an
observation export.

## OpenTelemetry and AG-UI interoperability

```bash
claude_control monitor export --format otlp-json
claude_control monitor export --format otlp-json --through 500
claude_control monitor export --format otlp-json --metadata
```

The default export is an OTLP/HTTP JSON `ExportTraceServiceRequest` containing
`resourceSpans` only. Each terminal Claude run becomes an INTERNAL
`invoke_agent` span with OpenTelemetry GenAI and OpenInference attributes. Trace
and span IDs are deterministic, token/cache/reasoning counts retain provider
semantics, and active or timestamp-incomplete runs are omitted rather than
fabricated. `--metadata` intentionally wraps the standard document with local
profile and omission details; use the default form when sending it to an OTLP
collector.

OpenTelemetry GenAI agent conventions are currently Development, so the export
records the pinned local profile
`claude-control.otel-genai-openinference.v1-development` and does not claim a
stable standard version. Prompt and completion bodies remain absent by default.

The Python adapter `claude_control.observation_agui` maps replay snapshots and
decoded ledger events to AG-UI 1.0 `STATE_SNAPSHOT`, `RUN_STARTED`,
`RUN_FINISHED`, `RUN_ERROR` and `CUSTOM` events. It is the live frontend boundary
for graph and replay viewers; SQLite plus observation cursors remain the durable
history. `monitor agui snapshot` returns one standard event. `monitor agui events`
returns standard events inside a local bounded paging envelope containing
`after`, `through`, `next_cursor` and `has_more`; consumers resume from
`next_cursor` without polling hidden state. The adapter never emits text-message,
reasoning-content or tool-call-content events.
