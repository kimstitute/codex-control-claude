# Testing

## Automated tests: no model calls

From the repository root:

```bash
python3 -m unittest discover -s tests -v
```

The publication baseline has 47 tests. They use temporary directories and fake Claude processes to exercise:

- Parallel capacity, per-session exclusion, and request deduplication.
- Targeted cancellation, timeout, worker failure, and recovery from ambiguous execution.
- Explicit resume/restart behavior and preservation of historical conversation IDs.
- Session/model validation, malformed output, tool-use rejection, and result-file integrity.
- Plugin relocation to a different path, including paths containing spaces.
- Installer serialization, rollback, and preservation of backups if restoration fails.
- Machine-readable handling of state database failures.

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
