# Maestro on Claude Code (Tier-1)

Claude Code is the original target runtime. Local heartbeats run via the bundled `providers/claude-code/run.sh`; cloud runs go through Anthropic Remote Routines — see [`scheduling/claude-routines.md`](../scheduling/claude-routines.md) for that.

## Prerequisites

- `claude` CLI: https://docs.claude.com/claude-code (install + `claude login`)
- Python ≥ 3.10, plus `jq` and `curl`
- A Pipedream account with the Google connectors authorized (Gmail, Calendar, Drive)
- An Atlassian account (Jira + Confluence) — used via the Atlassian MCP server
- AWS credentials (only if you want S3 state and Secrets Manager — otherwise set `MAESTRO_STATE_BACKEND=local`)

## 1. Clone and bootstrap

```bash
git clone https://github.com/<you>/maestro.git
cd maestro
cp config.example.json config.json
# edit config.json — set email.recipient to your address
```

## 2. Install the MCP servers

Two paths:

**A) Via claude.ai (recommended)**. Open https://claude.ai/customize/connectors and add:
- Pipedream (Gmail, Google Calendar, Google Drive)
- Atlassian (Jira, Confluence)
- AWS (S3, Secrets Manager) — only if using cloud state

Claude Code picks these up automatically. No file edits needed.

**B) Via `.mcp.json` (alternative, project-local)**. Copy [`mcp/claude-code.mcp.json`](../mcp/claude-code.mcp.json) to `.mcp.json` at the repo root and fill in any tokens. Use this path if you need stdio MCP servers (e.g., a local AWS MCP) or don't want to use claude.ai-side OAuth.

## 3. Configure hooks and permissions

The bundled `.claude/settings.json` enables the PreToolUse write-path hook ([`providers/claude-code/hooks/check_write_path.py`](claude-code/hooks/check_write_path.py)) and pins the allow/deny list to read-only data sources + `runner/maestro.py` + `lib/mattermost.py`. Don't edit this file unless you know which guarantees you're relaxing.

## 4. Set up Mattermost (optional)

If you want the urgent-nudge channel, create a `.env` file at the repo root (gitignored) with the contents of [`.env.example`](../.env.example) and fill in your Mattermost bot credentials. See [ARCHITECTURE.md → Mattermost](../ARCHITECTURE.md#how-the-agent-learns-from-you) for the bot-account setup.

Skip Mattermost: leave `.env` empty. The runner's `mattermost` subcommand becomes a no-op staging that the post-run hook just deletes.

## 5. First run (manual)

```bash
# Auth probe — read-only, verifies all MCP servers are reachable
bash providers/claude-code/run.sh check-auth

# One heartbeat cycle (interactive — Claude Code session)
bash providers/claude-code/run.sh heartbeat
```

If the auth probe shows `✗` for any source, fix the connection before continuing. The heartbeat will partially work even with degraded sources but produces less useful output.

## 6. Schedule local heartbeats

Pick the OS scheduler that fits your machine:
- **macOS/Linux**: `cron` or `launchd` — see [`scheduling/local-cron.md`](../scheduling/local-cron.md)
- **Windows**: `Task Scheduler` (PowerShell helper script in `providers/claude-code/`)

For cloud runs (no laptop needed), use [`scheduling/claude-routines.md`](../scheduling/claude-routines.md).

## Tool-name wrapper

Claude Code wraps MCP function names as `mcp__<server>__<function>`. E.g.:
- `gmail-search-messages` (Pipedream MCP function) → `mcp__pipedream__gmail-search-messages` (Claude Code tool name)
- `searchJiraIssuesUsingJql` (Atlassian) → `mcp__atlassian__searchJiraIssuesUsingJql`

You'll see these wrapped names in `.claude/settings.json` allow/deny lists. The prompts use capability language, so the wrapper is transparent at run time.

## Known quirks

- The claude.ai-side Gmail/Calendar connectors (`mcp__claude_ai_Gmail__*`) are read-only. Send goes through Pipedream. For consistency across providers, the prompts now default to Pipedream-only.
- Hook-enforced write protection only fires for the `Write`/`Edit` tools. If you run `Bash(echo > foo.md)`, the hook is bypassed — `.claude/settings.json` denies `Bash` for exactly this reason.
- On Windows, `lib/cognee-venv/Scripts/python.exe` is the venv interpreter; on macOS/Linux it's `lib/cognee-venv/bin/python3`. The runner detects which.
