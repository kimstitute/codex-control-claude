# Live agent monitor

The observation and replay paths in `monitor` show the current host's state
without advancing, retrying, stopping, accepting or applying work. Explicit
ATTACH or SHELL actions in Local Detail temporarily leave the viewer and enter a
separately user-operated terminal. Two interfaces are available:

- `monitor viewer`: a Rust/Ratatui spatial graph and historical replay viewer;
- `monitor tui`: a Python standard-library fallback and provider quota dashboard.

## Run the graph/replay viewer

```bash
claude_control monitor viewer
claude_control monitor viewer --inspect
claude_control monitor viewer --tree
tmux new -s claude-control-viewer 'claude_control monitor viewer'
```

The default remains the content-free Safe Observer. Opt in explicitly to read
transcripts stored on this host.

```bash
claude_control monitor viewer --local-detail       # keyboard/mouse picker
claude_control monitor viewer --detail-current     # current directory
claude_control monitor viewer --detail-dir /absolute/project
claude_control monitor viewer --detail-file /absolute/transcript.jsonl
claude_control monitor viewer --detail-id <provider-session-id>
claude_control monitor local sessions --source all
claude_control monitor viewer --detail-source '<selector>'
```

Local Detail scans bounded JSONL data only from managed run artifacts and the
default `~/.claude/projects` and `~/.codex/sessions` roots. Override them with
`--claude-root` and `--codex-root`. `Esc` closes the picker and continues with
the Safe Observer without opening transcript content.
`monitor local stream` writes prompt and response bodies directly to JSONL
stdout. Treat that stream as sensitive local output, and redirect or pipe it
only when explicitly intended.

The viewer renders controller, composition, workflow, workspace, task and Claude
session cards with directional relationships. Session cards aggregate role,
model, current state, run counts and final output tokens. Runs do not become an
unbounded set of cards, so graph size remains close to the number of agents.
The default `focus` scope follows the latest connected work. Press `a` to rotate
through `focus`, bounded `recent`, and the cursor-complete `all` graph.

The viewer uses a semantic xterm-256 palette that remains stable over SSH and
tmux. It keeps gold history, green live paths, amber waits and red failures
visible even when a parent Codex or Claude process exports `NO_COLOR=1` for
machine-readable commands. Use `claude_control monitor viewer --no-color` only
when an explicitly monochrome screen is required.

| Mouse | Action |
|---|---|
| Click a card | Select it and open the 30/70 Safe Inspector |
| Drag a card | Reposition that card without changing controller state |
| Drag empty canvas | Pan the graph in manual camera mode |
| Wheel over the canvas | Zoom around the pointer |
| Wheel over the inspector | Scroll detailed history by three lines |
| Click or drag the timeline | Scrub the event cursor |
| Click `PLAY` or `LIVE` | Toggle historical playback / return to the latest cursor |
| Click `FOCUS`, `RECENT`, or `ALL` | Rotate to the next graph scope |
| Click previous/next `ERA` | Jump to the previous or next run-start era |
| Click the speed chip | Cycle 0.25× → 0.5× → 1× → 2× → 4× → 8× |
| Click the `GAP` chip | Toggle uniform and timestamp-compressed playback |
| Click the `M:*` chip | Rotate all, prompt, tool, failure and agent marker filters |
| Click an Inspector tab | Open Overview, Provenance, Tools, Activity or Terminal |
| Click `ATTACH` | Attach as the single operator of the selected native background terminal |
| Click `SHELL` | Open a separate user shell in the selected source's authorized project |
| Right-click | Close the inspector |

| Key | Action |
|---|---|
| `[`, `]` | Seek by one historical event |
| `{`, `}` | Jump to the previous or next run-start era |
| `p`, `P` | Jump to the previous or next prompt era |
| `/`, `n`, `N` | Enter search, move to the next match, or move to the previous match |
| `m`, `v` | Rotate the marker filter or Inspector tab |
| `,`, `.` | Decrease or increase playback speed from 0.25× to 8× |
| `z` | Toggle uniform (`GAP OFF`) and timestamp-compressed (`GAP ON`) playback |
| `Home`, `End` | Jump to the baseline or latest live cursor |
| `Space` | Play or pause from the selected cursor |
| `PageUp`, `PageDown` | Scroll the open inspector by one viewport |
| `Tab`, `Shift-Tab`, arrows | Select the next, previous, or spatially adjacent card |
| `Enter` | Select the first card when nothing is selected |
| `o`, `f`, `r`, `c` | Overview, follow latest work, relayout, or center selection |
| `a` | Rotate graph scope: `focus` → `recent` → `all` |
| `HJKL`, `+`, `-`, `0` | Pan, zoom, or reset zoom |
| `t`, `s` | Attach to the selected background agent / open a separate project shell |
| `x`, `i`, `?`, `q` | Toggle mouse capture, show information/help or quit |

The viewer opens in `focus` scope with a fitted overview. Rataflow provides node
scratch-buffer clipping, step-routed edges, semantic zoom, viewport interaction
and the minimap in one coordinate system. Existing positions survive state-only
updates; `r` is the explicit full relayout. Selecting a card opens the Safe
Inspector with related-run actual models, effort, timing, provider token/cost and
controller-operation history. Active work follows newly appended rows until the
operator scrolls away. Card `R/W/P/C` chips count reads, writes, patches and named
checks; open operations are shown separately. The timeline separates event markers,
weighted two-row activity and the playhead; its order comes from the monotonic
observation cursor rather than wall-clock time.

The inspector and operation projection are content-free. They exclude project
paths, command argv, prompts, reasoning, request/response bodies, results, hashes
and error text. Operation kind is normalized to the closed set `read`, `write`,
`patch`, `named_check` or `unknown`.

When enabled, Local Detail divides the Inspector into **Overview**,
**Provenance**, **Tools**, **Activity** and **Terminal**. The first four show safe
run/operation data, bounded prompt and response/reasoning, tool
state/duration/summary, and timestamped prompt/tool/spawn/failure events. Terminal
shows bounded recent screen output, state and attachability for a native background
session created with `terminal start`. `partial / bounded` means malformed rows were
skipped or byte/record bounds omitted older input. The content is composed in
memory and never written to the SQLite observation ledger, AG-UI, OTLP,
`--inspect` or `--tree`.
Streaming assistant rows with the same `(requestId, message.id)` are one response.
Local Detail aggregates exact input, cache-creation, cache-read, output and thinking
tokens from the latest consistent row, and marks missing, conflicting or bounded
coverage as `incomplete`. Cost stays `unavailable` when the transcript does not
report it; no price table is used. A selected `terminal:` source with no transcript
promotes to the unique matching `claude:` source when the file appears, without a
viewer restart or a new session-card identity.
Parent/child agents, prompts, responses and tool states replay at their recorded
times. A tool that finishes after the selected cursor remains `pending`, and
transcript silence alone is never treated as agent completion.

ATTACH temporarily leaves the viewer alternate screen and enters Claude Code's
native terminal. One local operator may control a session at a time; `Ctrl+Z`
returns and restores the viewer. SHELL does not grant tools to the agent or inject
a command. It opens a separate user-operated shell in an authorized project, whose
commands bypass controller workspace isolation and the approval ledger. Existing
headless `-p` runs and interactive sessions without a native background ID remain
observable but cannot be attached.

Headless `--inspect` preserves its v1 contract and prints fidelity, cursor, node,
edge and agent counts plus final output-token total as JSON. Schema-15 operation
nodes do not change those legacy counts. `--tree` emits a deterministic ASCII tree
from the controller through every composition/workflow/workspace/task/session,
including runs hidden by interactive scopes and their operation summaries. Both
modes work without a TTY and contain only content-free fields.

```bash
claude_control monitor agui stream > session.agui.jsonl
claude_control monitor viewer --stream-file session.agui.jsonl
claude_control monitor viewer --inspect --stream-file session.agui.jsonl
claude_control monitor viewer --tree --stream-file session.agui.jsonl
```

The JSONL file contains content-free AG-UI events. The viewer never opens SQLite
directly. Live mode holds one controller `monitor agui stream` child process.
Duplicate cursors are ignored; cursor gaps fail closed instead of fabricating a
historical state.

### Install or build the viewer

Pass the verified SHA-256 of a release or locally built binary to the installer.
The installer never downloads a viewer and rejects a hash mismatch.

```bash
cargo build --locked --release --manifest-path viewer/Cargo.toml
sha256sum viewer/target/release/ccc-viewer
python3 install.py --update \
  --viewer viewer/target/release/ccc-viewer \
  --viewer-sha256 <verified-sha256>
```

On Windows use `ccc-viewer.exe` and `Get-FileHash -Algorithm SHA256`. If no
viewer is bundled, `monitor viewer` returns `viewer_unavailable` while
`monitor tui` remains usable. Updates retain the old viewer only when both its
manifest and bytes still verify.

The product interaction takes inspiration from
[Zoetrope](https://github.com/furkankly/zoetrope)'s spatial graph, camera and
timeline. No Zoetrope source or assets are vendored. The viewer uses the public
MIT-licensed [Rataflow](https://github.com/furkankly/rataflow) crate; its notice
is preserved in [`THIRD_PARTY_NOTICES.md`](../THIRD_PARTY_NOTICES.md). Event
folding, Claude Control projection, scopes, cards, timeline and launcher are
implemented in this repository.

## Run the Python TUI

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

Schema 14 adds an append-only `claude-control.observation.v1` event ledger. Schema
15 adds controller-operation start/settlement metadata to that ledger. The ledger
is the durable source for the graph viewer and time-travel replay;
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
new transitions after that baseline remain exact. The schema-14-to-15 migration
adds one final operation summary per existing request without inventing historical
start/settlement transitions.

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
