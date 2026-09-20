# Testing

## Automated tests: no model calls

From the repository root:

```bash
python3 -m unittest discover -s tests -v
```

The original publication baseline had 47 tests. The assignment suite adds coverage
for the version-0.2 role and report contracts. Tests use temporary directories and
fake Claude processes to exercise:

- Parallel capacity, per-session exclusion, and request deduplication.
- Targeted cancellation, timeout, worker failure, and recovery from ambiguous execution.
- Explicit resume/restart behavior and preservation of historical conversation IDs.
- Session/model validation, malformed output, tool-use rejection, and result-file integrity.
- Plugin relocation to a different path, including paths containing spaces.
- Installer serialization, rollback, and preservation of backups if restoration fails.
- Machine-readable handling of state database failures.
- Role instructions reaching Claude on stdin under the existing tool-disabled profile.
- Strict assignment inputs, deterministic retries, and changed-preset conflicts.
- Historical report validation, prompt tampering, and malformed/incorrect report identities.
- Multi-run observation of terminal, active, failed, and unknown outcomes.

These tests require Linux process facilities. They do not sign in, use a Claude account, or make model requests.

Optional static checks, if Ruff is already installed:

```bash
ruff check plugins tests install.py
ruff format --check plugins tests install.py
```

## Live integration: explicit account usage

Live tests invoke real Claude models, consume account usage, and create local Claude session history. They are never included in unittest discovery.

Use a **new output directory outside this checkout**:

```bash
python3 tests/live_smoke.py \
  --claude-bin /absolute/path/to/claude \
  --output /absolute/path/outside-repository/new-live-check
```

The suite creates a dedicated store and project. It checks overlapping Sonnet/Fable runs, independent conversation recall, cancellation of one run while its peer completes, resumption after cancellation, and nonexecution of a test project hook. Both model aliases must be available to your account.

The output includes prompts, responses, session IDs, process metadata, and a report. Treat it as private runtime data. The suite also leaves Claude's own normal persisted conversations on the testing machine. Do not commit these files or attach raw reports to public issues.

Live checks have passed during development. Their raw artifacts are excluded from this public repository; running the suite on your machine establishes evidence for your own CLI version and account.

For the smaller structured-delegation check (one Sonnet executor and one Fable
critic), use another new directory outside the checkout:

```bash
python3 tests/live_assignments.py \
  --claude-bin /absolute/path/to/claude \
  --output /absolute/path/outside-repository/new-assignment-check
```

It checks overlapping execution, role-default model routing, independent task
identities, `observe`, saved-prompt verification, valid structured reports,
`acceptance: unreviewed`, exact supplied-marker output, and zero Claude tool
calls. This small probe does not establish research quality, throughput at larger
parallelism, or correctness of arbitrary model-generated evidence.
