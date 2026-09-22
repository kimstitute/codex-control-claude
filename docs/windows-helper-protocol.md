# Windows helper protocol (`ccc-win-supervisor`)

`ccc-win-supervisor` is a small native Windows executable that owns the
durable Job Object handle for a supervised child process. The Python
supervisor launches it as a child process and inherits its stdin/stdout as a
control pipe. End users receive a prebuilt `.exe`; no Rust toolchain is
required at runtime.

Source: `native/windows/ccc-win-supervisor`. Native AppContainer support and
WSL integration are explicitly out of scope for this helper.

## Ownership invariant

The helper is the **only** process that ever holds the Job Object handle.
The Python supervisor never creates, duplicates, or closes a Job Object
itself; it only sends commands over the control pipe and observes replies.
If the helper process itself dies, the Job Object handle table entry is
released by the OS and, because `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` is set
at creation, every process still assigned to the job is killed automatically.
This is the backstop that guarantees no orphaned descendants survive a
helper crash.

## Transport

- One JSON object per UTF-8 line on stdin (commands) and stdout (replies /
  events).
- Maximum line length is 1 MiB; oversized lines are rejected with an error
  reply, drained without unbounded allocation, and are not parsed. Invalid
  UTF-8 is rejected as well.
- stdout carries protocol traffic only. All diagnostics go to stderr.
- Protocol version is `1`. `hello` negotiates it; any other protocol value
  is rejected.

## Commands and replies

```
-> {"id":1,"op":"hello","protocol":1}
<- {"id":1,"ok":true,"event":"hello","protocol":1,"version":"0.1.0"}

-> {"id":2,"op":"create","run_id":"...","job_name":"Local\\ccc-...",
    "argv":["..."],"cwd":"...","env":{...},
    "stdin_path":"...","stdout_path":"...","stderr_path":"..."}
<- {"id":2,"ok":true,"event":"created","pid":1234,
    "creation_filetime":"133...","broke_away":true}

-> {"id":3,"op":"resume"}
<- {"id":3,"ok":true,"event":"running"}

-> {"id":4,"op":"stop","grace_ms":5000}
<- {"id":4,"ok":true,"event":"stopping"}

-> {"id":5,"op":"abort"}
<- {"id":5,"ok":true,"event":"aborted"}

<- {"event":"exited","exit_code":0,"reason":null}
```

Errors: `{"id":N,"ok":false,"error":"..."}`. Sending an operation that is
invalid for the current state (e.g. `resume` before `create`, `create`
twice, `resume` twice) is an error, not a crash.

## `create` semantics

1. Create a *new* named Job Object; `ERROR_ALREADY_EXISTS` is rejected.
   The run ID must be a canonical UUID and the job name must use the
   `Local\ccc-` namespace.
2. Set `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` on the job.
3. Open the three stdio files directly via `CreateFileW` (no shell, no
   `cmd.exe`/PowerShell interpretation), inheritable only for the duration
   of process creation, and drop inheritance again afterward.
4. Create the child suspended (`CREATE_SUSPENDED`) with
   `CREATE_NEW_PROCESS_GROUP | CREATE_UNICODE_ENVIRONMENT`, first attempting
   `CREATE_BREAKAWAY_FROM_JOB`. If that specific attempt fails with the
   documented nesting-restriction error, retry once without the breakaway
   flag. `broke_away` in the reply reflects which attempt succeeded.
5. Assign the (still-suspended) child to the job before it can execute.
6. Verify with `IsProcessInJob`.
7. Read the child's creation `FILETIME` via `GetProcessTimes` and report it
   as a decimal string (100ns ticks since 1601-01-01 UTC).
8. On any failure in this sequence, terminate the suspended child and the
   job and fail closed; every handle opened along the way is closed on
   every path.

`resume` releases the primary thread exactly once and closes the thread
handle immediately after.

`stop` sends `CTRL_BREAK_EVENT` to the child's process group when possible
(best effort — some processes ignore it or have no console), waits
`grace_ms` (0..=30000), and if the process has not exited by then calls
`TerminateJobObject`.

`abort` calls `TerminateJobObject` unconditionally.

## Crash / EOF behavior

A dedicated stdin-reader thread feeds parsed lines to the main thread over a
channel, which also receives the child's exit notification (from a waiter
thread blocked in `WaitForSingleObject`) and frames that are rejected for
being oversized. This lets the main thread react to child exit and stdin
traffic without a busy loop.

If stdin reaches EOF before the run has reached its terminal `exited` state,
that means the Python supervisor died unexpectedly. The helper terminates
the Job Object, does not attempt to write to a broken stdout pipe, logs a
diagnostic to stderr, and exits with a nonzero status.

## Command-line and environment construction

Arguments are joined into a single command-line string using the standard
Win32 quoting algorithm for arbitrary argv (matching `CommandLineToArgvW`
parsing rules), so arguments containing spaces, quotes, and trailing
backslashes round-trip correctly. The environment block passed to
`CreateProcessW` is built as `KEY=VALUE\0...\0\0`, with entries sorted
case-insensitively by key, as required for `CREATE_UNICODE_ENVIRONMENT`.

## Build

```
cd native/windows/ccc-win-supervisor
cargo test --release --target x86_64-pc-windows-msvc
cargo build --release --target x86_64-pc-windows-msvc
cargo build --release --target aarch64-pc-windows-msvc
```

Release builds use `panic = "abort"` and the static CRT (via
`.cargo/config.toml` `target-feature=+crt-static`) so the shipped `.exe` has
no dependency on the VC++ redistributable. Rust dependencies are linked into
the executable; runtime dependencies are Windows system libraries only.

Tagged builds publish both architecture-specific executables, `SHA256SUMS`,
and `manifest.json` as GitHub release assets. GitHub artifact attestations bind
the executables to the workflow and commit that produced them.
