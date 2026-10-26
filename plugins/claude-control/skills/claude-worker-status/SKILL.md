---
name: claude-worker-status
description: Show a one-screen, read-only snapshot of Claude Code harness workers and Codex subagents in the current chat. Use when the user asks which delegated agents are running, what they are doing, or their model, tokens and elapsed time.
---

# Claude worker status

Collect two read-only views when the current Codex environment provides the `collaboration.list_agents` tool:

1. Call `collaboration.list_agents` for the **current chat's** Codex agent tree. Exclude `/root` from the subagent rows. Use each returned agent path and status; use `last_task_message` as its mission only if the tool actually returns it. If that field is absent, use `—`. This tool may omit model, token usage and start time; show `—` for each missing value. Do not infer them from the parent model, task name, filesystem timestamps or token budget. If the tool is unavailable, say that the Codex section could not be inspected; do not report zero agents.
2. Run the bundled CLI at `../../scripts/claude_control_cli.py`, resolved from this skill's directory:

```bash
python3 <resolved-cli> harness agents
```

If the user configured a custom state directory, place `--state-dir <absolute path>` before `harness`. This command reads local records only. It does not call Claude or consume tokens. Do not initialize a store or start a worker merely to answer a status request.

Present both sources in one compact table with source, agent, mission, status, model, tokens and elapsed time. For Claude workers, use the observed model when available and identify it as observed. Show `tokens.total` with input, cache and output breakdown if useful. Label `token_scope: provisional` as an observed lower bound; `final` means the CLI emitted terminal usage for every recorded turn. Show `—` for unavailable fields. Distinguish an empty Codex tree from an unavailable Codex tool, and an empty Claude worker list from a failed CLI call. Never sum or compare unknown Codex usage against known Claude usage.

The Claude section covers workers started by `harness start` and `harness followup`. The Codex section covers only the agent tree of the chat invoking this skill, not subagents in other chats. The current harness profile does not launch nested Claude `Agent` tools. Use `harness agents --all` only when the user asks to see completed or attention-needed Claude tasks too. Surface any `warnings` rather than silently implying a complete snapshot. Do not use a separate OpenAI API key or private Codex databases to fill unavailable metrics.
