# Maestro on deep-agents CLI (Tier-3, best-effort)

[deep-agents](https://docs.langchain.com/oss/python/deepagents/overview) (LangChain/LangGraph) is the newest of the four target runtimes. As of 2026-05, the CLI is on PyPI as [`deepagents-cli`](https://pypi.org/project/deepagents-cli/) but the schema is still evolving. The instructions below reflect the current shape — verify against upstream docs before deploying.

Tier-3 means: prompts load, MCP servers connect, a heartbeat runs to completion in a smoke test. Daily-driver fidelity (Tier-1) is not guaranteed.

## Prerequisites

- Python ≥ 3.11
- `pip install deepagents-cli`
- LangSmith API key (deep-agents leans on LangSmith for traces)
- A backend model API key (Anthropic, OpenAI, or other supported)
- Same downstream services: Pipedream, Atlassian, optionally AWS

## 1. Clone and bootstrap

```bash
git clone https://github.com/<you>/maestro.git
cd maestro
cp config.example.json config.json
# edit config.json
```

## 2. Wire the agent spec

[`mcp/deep-agents.yaml`](../mcp/deep-agents.yaml) is the agent spec. Copy it to `./.deepagents/agent.yaml` or point deep-agents at it directly:

```bash
deepagents dev --agent-spec mcp/deep-agents.yaml
```

deep-agents reads `AGENTS.md` natively. Tool name wrapping is configurable per agent — verify with `deepagents tool list` after start.

## 3. First run

```bash
# Auth probe
deepagents dev --agent-spec mcp/deep-agents.yaml --prompt "$(cat prompts/check-auth.md)"

# Heartbeat — interactive TUI (Textual)
deepagents dev --agent-spec mcp/deep-agents.yaml

# Headless / CI
deepagents run --agent-spec mcp/deep-agents.yaml --prompt "$(cat prompts/heartbeat.md)"
```

## 4. Scheduling

deep-agents supports `deepagents deploy` for cloud execution on LangGraph Platform. For Maestro:

```bash
deepagents deploy --agent-spec mcp/deep-agents.yaml --schedule "0 6-16 * * 1-5"
```

This runs the agent on LangGraph Platform with the same cron the Anthropic routine uses. Verify the schedule field name against current deep-agents docs.

## Tier-3 caveats

- **Workspace path restriction**: deep-agents restricts writes to `./workspace/` by default. Maestro's `daily/`, `knowledge/`, etc. need to be inside the workspace root (the agent spec sets `workspace.root: .`).
- **MCP support**: works via LangGraph's MCP integration but is less polished than Codex/opencode. Some servers may need extra config.
- **Hooks**: no hook infrastructure; the runner's `write` subcommand and AGENTS.md rules carry safety.
- **SKILL.md**: deep-agents also supports a `SKILL.md` file pattern (alongside AGENTS.md) — Maestro doesn't use it but you could add Maestro-specific skills here later.

## Verifying

```bash
deepagents run --agent-spec mcp/deep-agents.yaml --prompt "$(cat prompts/check-auth.md)"

# Expect: Summary: 6/6 sources healthy
```

If `Runner` shows `✗` but everything else is healthy, check that `python3` is on the PATH inside the deep-agents sandbox.
