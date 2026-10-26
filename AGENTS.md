# Contributing to Codex Control Claude

This repository contains a Python standard-library controller and a Codex plugin.
Legacy session control supports Linux, macOS and Windows. The opt-in autonomous
`harness` path currently runs only on Linux.
The plugin ID is `claude-control`; the repository name is `codex-control-claude`.

- Keep runtime code and its skill together under `plugins/claude-control/`.
- Preserve explicit model selection, per-session serialization, request idempotency, and host ownership.
- Never silently replace a conversation, switch a requested model, or retry ambiguous execution.
- Keep the supplied-text profile: Claude tools and MCP remain disabled.
- Keep `harness` separate from the supplied-text profile. Require a pinned
  claude.ai Team organization, restricted tools, a working sandbox and an
  independent clone; never fall back to personal API billing.
- Preserve versioned assignment snapshots; report formatting is separate from semantic acceptance.
- Keep credentials, real session transcripts, local paths, host inventories, and runtime databases out of commits.
- Use synthetic examples and temporary directories in tests. Do not change HOME or target unrelated processes.
- Run `python3 -m unittest discover -s tests -v` before submitting behavior changes. These tests use fake processes.
- Live tests require an explicit decision to call Claude and consume the caller's account usage.
- Check plugin metadata and the bundled skill when changing commands or installation behavior.

See [README.md](README.md), [CLI reference](docs/cli.md), and [architecture](docs/architecture.md).
