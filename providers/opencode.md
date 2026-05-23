# Maestro on opencode (Tier-2)

[opencode](https://opencode.ai/) is an open-source TUI agent that reads `AGENTS.md` and supports MCP via `opencode.json` (JSONC).

## Prerequisites

- opencode installed: see https://opencode.ai/docs/install/
- Model backend: configure per `opencode.json` (Claude, GPT, Gemini, local — any of these work)
- Same downstream services as Claude Code: Pipedream, Atlassian, optionally AWS

## 1. Clone and bootstrap

```bash
git clone https://github.com/<you>/maestro.git
cd maestro
cp config.example.json config.json
# edit config.json
```

## 2. Wire MCP servers

Copy [`mcp/opencode.json`](../mcp/opencode.json) to `./opencode.json` (project-local) or `~/.config/opencode/opencode.json` (user-global). Fill in env vars in your shell or `.env`.

opencode wraps MCP function names as `<server>:<function>` — e.g. `pipedream:gmail-search-messages`.

## 3. First run

```bash
# Auth probe
opencode run "$(cat prompts/check-auth.md)"

# Heartbeat — interactive TUI
opencode

# Then in the TUI, paste the heartbeat prompt or use it via /load prompts/heartbeat.md
```

For headless/CI invocation:
```bash
opencode run --headless "$(cat prompts/heartbeat.md)"
```

The runner subcommands (`runner/maestro.py prepare`, `finalize`, `mattermost`, `send-email`, `state`, `secrets`) work identically — they're plain Python.

## 4. Scheduling

opencode is local-only as of 2026-05 — no cloud scheduling layer. Use your OS scheduler:
- macOS/Linux: cron or launchd
- Windows: Task Scheduler

See [scheduling/local-cron.md](../scheduling/local-cron.md).

## Tier-2 caveats

- AGENTS.md is loaded at session bootstrap and immutable during the run.
- opencode doesn't have hook infrastructure like Claude Code's PreToolUse. Path enforcement relies on the runner's `write` subcommand. The agent's prompt-level rules (AGENTS.md → File Write Restrictions) carry more weight here.
- Mattermost: same — pure-stdlib client, works fine.
- Web search: opencode bundles search tools depending on which provider is configured. Common: Tavily, Serper, Brave, or `browser-use` MCP. Configure one of those in `opencode.json` to enable Step 3 research in heartbeats.

## Verifying

```bash
opencode run --headless "$(cat prompts/check-auth.md)"

# Expect: Summary: 6/6 sources healthy
```
