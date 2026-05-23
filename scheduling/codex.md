# Scheduling on Codex CLI (Tier-2)

Codex CLI has two scheduling primitives that fit Maestro:
1. **`/goal` workflows** with persist/resume — Codex's native long-running task model.
2. **External OS scheduler** (cron/launchd/Task Scheduler) invoking `codex` non-interactively.

## Option 1: `/goal` workflow

This is Codex's idiomatic way to run a recurring task. Persist a goal once; resume it on schedule.

```bash
# One-time setup: register Maestro as a /goal
codex goal create maestro-heartbeat \
  --schedule "0 6-16 * * 1-5" \
  --prompt-file prompts/heartbeat.md \
  --config codex.json
```

Codex then triggers the goal on the schedule. State persistence is up to you — for cross-run continuity, wrap the prompt with the same runner sequence used in Anthropic routines (see [`scheduling/claude-routines.md`](claude-routines.md) for the prompt template; replace the Anthropic-specific connector instructions with Codex's MCP config).

Verify the `/goal` flag names against `codex goal --help` — the schema is still maturing.

## Option 2: OS scheduler

Cron entry that runs the heartbeat once an hour during business hours:

```cron
# crontab -e
0 6-16 * * 1-5  cd /path/to/maestro && /usr/local/bin/codex run --config codex.json --headless --input "$(cat prompts/heartbeat.md)" >> /var/log/maestro.log 2>&1
```

On macOS, launchd is more reliable than cron — see [`scheduling/local-cron.md`](local-cron.md) for a launchd plist template.

On Windows:
```powershell
# Register a Task Scheduler task that runs `codex` once an hour
schtasks /Create /TN "Maestro Heartbeat" /TR "powershell -File providers\claude-code\run-codex.ps1" /SC HOURLY
```

(A `run-codex.ps1` helper script isn't bundled — write your own that `cd`s into the repo and invokes `codex` with the right config.)

## Caveats

- **State persistence**: Codex doesn't auto-mount S3 or run `runner/maestro.py state pull/push` for you. If you want cross-run state (recommended for daily-driver use), wire `runner state pull` at job start and `runner state push` at job end. Local mode (`MAESTRO_STATE_BACKEND=local`) works for single-machine setups.
- **Secrets**: Codex respects shell env, so `source .env` before invocation works. Or use a secrets-manager helper like `direnv`.
- **Concurrency**: cron may launch multiple heartbeats if one runs long. Add a lockfile guard in the cron line, e.g. `flock -n /tmp/maestro.lock -c '<command>'`.
