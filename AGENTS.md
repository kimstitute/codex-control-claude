# Contributing to Codex Control Claude

This repository contains a Linux-only, Python standard-library controller and a Codex plugin.
The plugin ID is `claude-control`; the repository name is `codex-control-claude`.

- Keep runtime code and its skill together under `plugins/claude-control/`.
- Preserve explicit model selection, per-session serialization, request idempotency, and host ownership.
- Never silently replace a conversation, switch a requested model, or retry ambiguous execution.
- Keep version 0.1's supplied-text profile: Claude tools and MCP remain disabled.
- Keep credentials, real session transcripts, local paths, host inventories, and runtime databases out of commits.
- Use synthetic examples and temporary directories in tests. Do not change HOME or target unrelated processes.
- Run `python3 -m unittest discover -s tests -v` before submitting behavior changes. These tests use fake processes.
- Live tests require an explicit decision to call Claude and consume the caller's account usage.
- Check plugin metadata and the bundled skill when changing commands or installation behavior.

See [README.md](README.md), [CLI reference](docs/cli.md), and [architecture](docs/architecture.md).
