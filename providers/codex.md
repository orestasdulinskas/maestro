# Maestro on Codex CLI (Tier-2)

[Codex CLI](https://developers.openai.com/codex/cli/reference) reads `AGENTS.md` natively and supports MCP via its config file. Maestro's prompts and runner work without modification; only the MCP wiring is Codex-specific.

## Prerequisites

- Codex CLI installed: see https://developers.openai.com/codex/cli/reference (Rust binary; `brew install codex` / cargo / npm install -g)
- OpenAI API key (or whichever model backend Codex is configured for)
- Same downstream services as Claude Code: Pipedream account, Atlassian account, optionally AWS

## 1. Clone and bootstrap

```bash
git clone https://github.com/<you>/maestro.git
cd maestro
cp config.example.json config.json
# edit config.json
```

## 2. Wire up the MCP servers

Copy [`mcp/codex.json`](../mcp/codex.json) into your Codex config location:
- Project-local: `./codex.json` or `.codex/config.json`
- User-global: `~/.codex/config.json`

Then set the per-server env vars (Pipedream API token, Atlassian OAuth token, AWS profile) in your shell or in `.env` if Codex reads one.

Codex wraps MCP function names as `<server>.<function>` — e.g. `pipedream.gmail-search-messages`. The prompts use capability language so this is transparent.

## 3. First run

Codex uses a different invocation model than `claude`. The prompts in `prompts/` are written to be runtime-agnostic — paste the heartbeat prompt into a Codex session:

```bash
# One-shot heartbeat
codex --config codex.json run "$(cat prompts/heartbeat.md)"
```

Or use Codex's `/goal` workflow for persistent runs (see https://developers.openai.com/codex/cli/features#goals).

The runner orchestration is invoked the same way as on Claude Code:
- At session start: `python3 runner/maestro.py prepare heartbeat`
- At session end: `python3 runner/maestro.py finalize heartbeat`

## 4. Scheduling

Codex doesn't have a built-in cron-like scheduler at parity with Anthropic Remote Routines as of 2026-05. Two options:
- **`/goal` workflows with persist/resume**: see [scheduling/codex.md](../scheduling/codex.md) for the recipe.
- **Local OS scheduler**: cron / launchd / Task Scheduler invoking `codex` non-interactively. See [scheduling/local-cron.md](../scheduling/local-cron.md).

## Tier-2 caveats

- Mattermost: `lib/mattermost.py` is pure stdlib Python; works fine. Needs the same `MATTERMOST_*` env vars.
- Cognee memory: requires `lib/cognee-venv` populated on the host. Codex doesn't sandbox the way Anthropic Routines do — you have access to the local filesystem.
- AGENTS.md size: Codex limits AGENTS.md to 32 KiB. The bundled AGENTS.md is sized to fit (~32 KB).
- Web search: Codex's built-in `web_search` and `web_fetch` cover the `WebSearch`/`WebFetch` capabilities. No extra MCP needed.

## Verifying

```bash
# After setup, run the auth probe
codex run "$(cat prompts/check-auth.md)"

# Expect: Summary: 6/6 sources healthy
```
