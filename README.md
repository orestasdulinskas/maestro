# Maestro

A provider-agnostic, read-only monitoring agent. Maestro runs on a schedule, scans your work surfaces (Gmail, Calendar, Jira, Confluence, Drive), synthesizes findings, and delivers actionable briefings via email — with a Mattermost urgent-nudge for items worth interrupting you for.

Runs on **Claude Code**, **Codex CLI** (OpenAI), **opencode**, **deep-agents CLI** (LangChain), and in the cloud via **Anthropic Remote Routines** — the prompts, runner code, and MCP server set are shared; only the per-provider config wrapper differs.

## What it does

- **Hourly heartbeat** (Mon–Fri, working hours): scans data sources, synthesizes findings, sends email if anything is actionable.
- **End-of-day review**: comprehensive day summary plus a draft briefing for tomorrow morning.
- **Quiet-time productivity**: when sources are empty, re-reads Confluence, cross-references peer activity, audits stale knowledge — no idle cycles.
- **Friday self-assessment**: agent reports on its own source health, watchlist hygiene, knowledge freshness, output quality.

Maestro is **read-only**. It cannot create, modify, or delete external resources. Its only outbound writes are:
- Email to a single recipient locked in `config.json` (enforced by `runner/maestro.py send-email`)
- A short Mattermost post to a single dedicated channel locked in `MATTERMOST_CHANNEL_ID` (enforced by `runner/maestro.py mattermost`)

## Quickstart

Pick your runtime:
- **Claude Code** (local) — [providers/claude-code.md](providers/claude-code.md)
- **Codex CLI** (OpenAI) — [providers/codex.md](providers/codex.md)
- **opencode** — [providers/opencode.md](providers/opencode.md)
- **deep-agents CLI** (LangChain) — [providers/deep-agents.md](providers/deep-agents.md)

For unattended cloud scheduling:
- **Anthropic Remote Routines** — [scheduling/claude-routines.md](scheduling/claude-routines.md)
- **OS scheduler** (cron / launchd / systemd / Task Scheduler) — [scheduling/local-cron.md](scheduling/local-cron.md)

All four runtimes share the same agent identity ([AGENTS.md](AGENTS.md)), prompts ([prompts/](prompts)), runner ([runner/maestro.py](runner/maestro.py)), and library code ([lib/](lib)). Only the MCP config wrapper differs — see [mcp/](mcp).

## Repo structure

```
AGENTS.md                  Agent operating rules + Provider Adapter (tool-name table).
                           Cross-provider standard (Linux Foundation / Agentic AI Foundation).
ARCHITECTURE.md            High-level architecture, deployment topology, learning loop.
ROADMAP.md                 Phased delivery plan and design history.
prompts/
  heartbeat.md             Hourly heartbeat procedure
  end-of-day.md            EOD review procedure
  check-auth.md            Read-only diagnostic auth probe
runner/
  maestro.py               Provider-agnostic Python CLI: prepare, finalize, write,
                           send-email, mattermost, state pull/push, secrets pull
lib/
  state.py                 state.json lifecycle (atomic writes, source health, metrics)
  memory_cognee.py         Cognee-backed knowledge-graph + vector recall (optional)
  mattermost.py            Mattermost HTTP client (pure-stdlib Python)
  dryrun.py                Fixture replay harness for regression tests
mcp/
  claude-code.mcp.json     MCP server template for Claude Code
  codex.json               MCP block for Codex CLI
  opencode.json            opencode config (JSONC)
  deep-agents.yaml         deep-agents agent spec
providers/
  claude-code.md           Quickstart for Claude Code
  codex.md                 Quickstart for Codex CLI
  opencode.md              Quickstart for opencode
  deep-agents.md           Quickstart for deep-agents CLI
  claude-code/
    settings.json.example  Claude Code permissions + hooks template
    hooks/                 PreToolUse path-validation hook
    run.sh                 Local-bash orchestration (legacy, optional)
    mcp-servers.json       Legacy MCP config used by run.sh
scheduling/
  claude-routines.md       Anthropic Remote Routines setup + RemoteTrigger body
  codex.md                 Codex /goal workflow and local cron
  local-cron.md            cron / launchd / systemd / Task Scheduler recipes
workflows/
  _template.md             Empty starter for user-defined workflow docs
fixtures/                  Scenario-based regression test fixtures
config.example.json        Copy to config.json (gitignored) and set email.recipient
.env.example               Copy to .env (gitignored) and fill secrets — Mattermost etc.
```

State the agent writes locally (all gitignored — see `.gitignore`):
```
daily/                     Per-day audit log
knowledge/                 user-profile, active-context, watchlist, decay log
briefing.md                Current short-form briefing
feedback.md                User-authored feedback + agent's feedback log
state.json                 Machine-readable run state, source health, metrics
memory.db                  Cognee semantic index (regenerable)
.tmp/                      Run-local scratch
```

For cloud runs, state lives in an S3 bucket instead (`maestro-state-<your-slug>`); the runner's `state pull` / `state push` handles the sync at run boundaries.

## Design principles

- **Read-only by default**, with narrowly scoped write surfaces (email to one recipient; Mattermost to one channel).
- **Email is the authoritative channel**; Mattermost is an additive "look at this now" tap, never a replacement. Routing flexes with channel health.
- **State lives in markdown**, machine state in `state.json`, semantic recall in `memory.db`. Markdown is human-readable and git-diffable — the agent's "diary" is auditable.
- **Defensive runner, declarative agent**: deterministic decisions (when to retry, which paths are writable, which recipient) live in `runner/maestro.py`; LLM judgment lives in `prompts/` and `AGENTS.md`.
- **Capability gating**: sources that fail repeatedly are auto-disabled and re-probed by `prompts/check-auth.md`.
- **Fail loudly, recover safely**: every run captures a structured daily-log summary; failures preserve marker files for inspection.

## Status

This is an open-source template. It works as a daily-driver on Claude Code and Anthropic Remote Routines (Tier-1); has been validated end-to-end on Codex CLI and opencode (Tier-2); and runs in smoke-test on deep-agents CLI (Tier-3, best-effort).

The original implementation was built and battle-tested as a single-user agent before being sanitized for public release. If you find rough edges in the templates, the [providers/](providers) docs are the best place to start when filing issues — they cover the per-runtime quirks.

## License

[MIT](LICENSE). Built on Anthropic's [AGENTS.md](https://agents.md) cross-provider spec and the [Model Context Protocol](https://modelcontextprotocol.io) (both Linux Foundation stewardship).
