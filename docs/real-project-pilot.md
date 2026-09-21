# First real-project pilot

The first pilot used this repository's workspace-policy validator as a real,
bounded maintenance task. The intended sequence was Sonnet editing, an isolated
unit check, independent Fable review, and explicit Codex verification.

**The automatic editor sequence did not pass this pilot.** A completed model
process and a useful code proposal were insufficient to authorize operations.
The controller correctly retained the failed attempts without executing their
proposed writes or checks.

## What happened

- The first Sonnet session read the two authorized files. Its next call exceeded
  a 300-second limit and returned no usable result. The workspace halted.
- After confirming termination and preserving the first attempt, Codex explicitly
  created a separate attempt with supplied source text and a 900-second limit.
  This was not an automatic retry or a replacement model.
- That call completed in approximately 431 seconds, but repeated top-level JSON
  keys. Strict report validation rejected it before any file operation.
- Codex inspected the proposed file text separately. The proposal also changed
  the existing NUL-argument guard incorrectly; an existing regression test caught
  that change. Codex restored the guard and expanded the Unicode tests.
- The corrected candidate was placed in an immutable private Git snapshot for
  read-only Fable review. This is a separate Codex-supervised recovery path, not
  a frozen export or acceptance of the rejected editor task.
- Fable completed two valid report turns, read both corrected files through the
  controller and recommended **APPROVE**. It explicitly distinguished supplied
  test evidence from checks it had not run. Actual completed model IDs were
  `claude-sonnet-5` and `claude-fable-5-1`; native tool calls were zero.

These observations concern one bounded task. They do not establish typical
latency or a model success rate. The failed attempts, original responses and
identities remain in private controller records; they are not distributed here.

## Defect and correction

The original policy accepted Python surrogate code points in paths and check
arguments. Some accepted paths raised `UnicodeEncodeError` during later file
access; surrogateescape values could produce filenames that were not UTF-8.

The correction requires strict UTF-8 encodability after the existing string and
length checks. Invalid policy text raises `invalid_workspace_policy`; access to
an invalid operation path raises `workspace_path_denied`. Valid Korean, accented,
combining and supplementary-plane characters retain their exact values. Existing
NUL checks, path restrictions and character-count limits remain in place.

The five added unit tests cover malformed paths, directory selections, operation
access, malformed executable/argument text, and valid Unicode preservation.
An independent probe covered all 2,048 surrogate code points and valid Unicode
file I/O, including adjacent scalar boundaries and a JSON-decoded emoji. Its
6,177 checks passed. Applying the new tests to the old implementation produced
15 failing subtests, confirming that they detect the original defect.

## Next implementation gates

1. **Explicit effort and timeout policy.** Validate supported CLI options, record
   the requested effort with immutable execution inputs, and preserve it across
   identified continuations. Do not infer the cause of slow calls from duration
   alone or silently change a model.
2. **Structured-output compatibility.** Evaluate the installed CLI's
   `--json-schema` behavior with native tools and MCP disabled. Verify stream
   parsing, duplicate/unknown fields, exact task identity and malformed results
   before selecting a runtime integration. Keep strict validation enabled.
3. **Explicit recovery.** Design a Codex-authorized path for a known terminal
   malformed response, with exact parent run, retained history, charged budgets
   and no replay of uncertain side effects. A new attempt must remain visible.
4. **Repeat the real-project gate.** Require an actual editor write, a passing
   isolated check, a review of the editor's exact frozen export and explicit Codex
   acceptance. Only then evaluate automatic P4/P5 composition.

The 0.7.1 input-validation correction does not implement these four follow-ups.
See [workspaces](workspaces.md) for the current execution and recovery boundaries.
