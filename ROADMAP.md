# Maestro — Tasks & Backlog

## Initiatives

These are the major directions for the project. Individual bugs, features, and cleanup tasks below feed into these.

### Implementation Plan

```
Phase 0 (state.json) — DONE
  ├── Phase 1 (reliability) — DONE
  │     └── Phase 2 (RAG/memory) — DONE
  │           └── Phase 3 (chat — Google Chat) — REMOVED 2026-05-09 (user found it useless; hard-removed code)
  │                 ├── Phase 3.5 (pre-cloud stabilization) — IN PROGRESS
  │                 │     ├── Phase 3.6 (Mattermost bot — replaces removed Google Chat) — DONE 2026-05-11 (outbound only; inbound deferred to Phase 4)
  │                 │     └── Phase 3.7 (repo cleanup + private GitHub push, two-repo split) — PLANNED 2026-05-10
  │                 │           └── Phase 4 (cloud, AWS Lambda) — runs after GitHub push; gog/gws CLIs are blocked by laptop antivirus, so cloud is the only path to direct Google APIs
  │                 │                 └── Phase 4.5 (bypass Pipedream, direct Google APIs via google-api-python-client) — happens IN cloud, not before; pure Python so antivirus is not a factor on Linux runtime
  │                 │                       └── Phase 5 (open source) — extract proven template, publish; will inherit repo split from Phase 3.7 (harness-only repo is already public-shaped)
```

#### Phase 0 — `state.json` (foundation) — DONE

Completed 2026-03-27. Created `state.json` schema, `lib/state.py` CLI helper, integrated into `run.sh` for run lifecycle tracking, prompt hash computation, and dynamic context injection.

Files created: `state.json`, `lib/state.py`, `mcp-servers.json`

#### Phase 1 — Reliability — DONE

Completed 2026-03-27. All backlog items implemented:

- [x] Calendar fallback (Gmail invite search via `has:invite`)
- [x] Google Drive wired into prompts (Pipedream tool names, heartbeat + EOD)
- [x] Atlassian cloudId caching (state.json cached section, inject-context fallback)
- [x] Time-window fix (dynamic `after:EPOCH` / `updated >= "YYYY/MM/DD HH:mm"` from state)
- [x] Rate limiting / retry / backoff (single retry with 30s/60s backoff)
- [x] Run overlap / overrun detection and SLA logging (`sla_seconds` config)
- [x] Catch-up mode after failed or skipped runs (gap detection in inject-context)
- [x] Degraded-mode email policy (always email when sources degraded)
- [x] Character encoding (UTF-8 forced via LANG/LC_ALL/PYTHONIOENCODING)
- [x] Prompt/runtime capability mismatch (Calendar fallback, honest capability docs)
- [x] Lock file timestamp-based staleness (epoch stored alongside PID)
- [x] `run.sh` log file config fix (`load_config_json` reads `logging.log_file`)
- [x] Source health dashboard / weekly metrics (state.json counters, Friday self-assessment reads them)
- [x] `--dry-run` flag, `--morning` mode, configurable model
- [x] Prompt/CLAUDE.md dedup (data safety section consolidated)
- [x] `feedback.md` structure (Ignored Topics, Always Include, Current Context, Preferences)
- [x] Heartbeat reply parsing (dedicated step 1.1, before general Gmail check)
- [x] Watchlist resolved-item pruning (mandatory, archived to `resolved-archive.md`)
- [x] Config change detection (hash at install, check each run)
- [x] Apps-script cleanup (archived with SUPERSEDED note)
- [x] Project naming residue (hooks fixed heartbeat/ → maestro/, heartbeat.log → maestro.log)
- [x] Quiet-run research budgeting (`consecutive_quiet_runs` tracking, proactive investigation)
- [x] Cost / token tracking per run (JSON output format, usage extraction)
- [x] Atlassian MCP wired into `--print` mode (dynamic merge from desktop config)

#### Phase 2 — RAG / Memory — DONE

Completed 2026-03-28. Built custom OpenClaw-style memory system using SQLite + sqlite-vec + FTS5 (no Milvus dependency, no litellm). Local CPU-only embeddings via sentence-transformers/all-MiniLM-L6-v2.

Files created: `lib/memory.py`, `memory.db` (runtime)

- [x] Memory system implementation (SQLite: 4 tables — files, chunks, chunks_fts, chunks_vec)
- [x] Hybrid search (70% vector cosine + 30% BM25, merged by chunk ID)
- [x] Content-hash dedup (SHA-256 per chunk, skip re-embedding unchanged files)
- [x] Post-run indexing hook in `run.sh` (after heartbeat and EOD)
- [x] Pre-run recall (search briefing + watchlist keywords → inject `## Recalled Memories`)
- [x] Prompt budget management (token estimation, warning at >30K tokens)
- [x] Daily log archiving (30-day archive during EOD)
- [x] Monday morning briefing enrichment (week-ahead section via inject-context)
- [x] Recalled memories integrated into both heartbeat and EOD prompts

#### Phase 3 — Interactive Chat — REMOVED 2026-05-09

Originally completed 2026-03-28 with `lib/chat.py`, `prompts/chat-reply.md`, `--chat-poll` mode, outbound Chat in heartbeat, and 7 `google_chat-*` permissions in `.claude/settings.json`.

**Why removed**: User found Google Chat output useless — never noticed messages on phone/desktop. Email was the better channel. The `Chat-Alert Action Loop` pattern in `user-profile.md` (5 observations) turned out to be coincidence — chat correctly predicted *what* the user would do but had zero causal effect on *whether* they did it (they would have done it anyway from normal Jira/email workflow).

**What was removed (2026-05-09)**:
- `lib/chat.py`, `prompts/chat-reply.md` deleted
- `chat` block stripped from `config.json` and `state.json`
- 7 `mcp__pipedream__google_chat-*` permissions stripped from `.claude/settings.json`
- `run_chat_poll()`, `--chat-poll` mode, `TASK_NAME_CHAT`, `CHAT_PY` stripped from `run.sh`
- `MaestroChat` scheduled task unregistered (was never installed in the running system because `chat.poll_enabled = false`, but cleared defensively)
- All chat references stripped from `CLAUDE.md` and `prompts/heartbeat.md`
- `Chat-Alert Action Loop` pattern removed from `user-profile.md`
- `chat_messages_sent` metrics removed from `state.json`

Inbound polling never worked end-to-end (Pipedream Connect-side `chat.messages.readonly` scope was never grantable). Outbound did work briefly (last 2 messages were 2026-05-04). Net: this phase never paid for its complexity.

#### Phase 3.5 — Pre-Cloud Stabilization (in progress)

Before cloud, the agent must be regression-testable and free of integration debt.

Completed 2026-04-26:
- [x] Capability gating (lib/state.py `source_status()` — disables sources after threshold; injects "Disabled Sources" block into prompt)
- [x] Auth-failure notification mechanism (per-source status transitions; `notification_pending` flag; agent emails user on healthy→degraded→disabled)
- [x] EOD timestamp-inversion guard (refuses to write `completed_at < started_at`)
- [x] Heading-based memory chunking (kills append-churn; ~95% chunk reuse on daily-log appends — note: superseded by switch to cognee backend in `lib/memory_cognee.py`)
- [x] Liveness ping (`run.sh > liveness_ping`; `config.json > monitoring.healthcheck_url` — currently unset, ready to wire to Healthchecks.io)
- [x] Fixture replay harness (`./run.sh --replay <fixture>`; `lib/dryrun.py`; `fixtures/scenario-01-quiet-friday/` with 7 structural assertions; `fixtures/README.md` anonymization recipe)
- (Two Chat-related items previously listed here were Phase 3 work that was fully removed on 2026-05-09 — see Phase 3 section above. Removed from this Phase 3.5 list to avoid implying they're still in the code.)

Completed 2026-05-09:
- [x] Hook portability fix: `grep -oP` → `jq -r '.tool_input.file_path'` in `.claude/settings.json` PreToolUse hooks
- [x] `--force` mode in `run.sh` (bypasses schedule gate, for ops/verification — added during Pipedream-recovery test)
- [x] `--check-auth` mode (Option A — read-only probe, no state.json mutation). `prompts/check-auth.md` + `run_check_auth()` in `run.sh`. Probes Gmail (read), Calendar, Jira, Confluence, Drive with cheapest-read calls. Gmail (send) reported informationally from state.json (cannot probe without spamming user). Smoke test 2026-05-09: 2/5 healthy (Atlassian only; Gmail/Calendar `claude_ai_*` MCPs return "permission not granted" in `--print` mode — possibly only available interactively, worth investigating). Drive correctly flagged as Pipedream Connect URL.

Outstanding for Phase 3.5:
- [ ] Investigate why `claude_ai_Gmail` / `claude_ai_Google_Calendar` MCPs report "permission not granted" in `--print` mode despite being in the allow list. May mean heartbeat has been blind to these sources for longer than the Pipedream outage.
- [ ] Anonymize 1-2 real recent days as fixtures (the "spine" of the test suite)
- [ ] Add fixtures for: prompt-injection attempt, Monday morning first run, Friday EOD with self-assessment
- [ ] Wire Healthchecks.io URL into `config.json > monitoring.healthcheck_url`

#### Phase 3.6 — Mattermost Bot (replaces removed Google Chat) — DONE 2026-05-11 (outbound only)

**Why this exists**: Phase 3 (Google Chat) was removed 2026-05-09 because the user never saw Chat messages — wrong channel for the user's working environment. Mattermost is the company's daily-driver comms channel, so message visibility is fundamentally higher. This is *not* a re-do of Phase 3 — it's the first chat channel that has any chance of being read.

**Completed 2026-05-11 (outbound)**:
- Admin granted a bot account and Personal Access Token. (Mattermost admins create bots via System Console → Integrations → Bot Accounts.)
- `.env` stores the bot's credentials: `MATTERMOST_BASE_URL` (your Mattermost host, e.g. `https://chat.example.com`), `MATTERMOST_BOT_TOKEN`, `MATTERMOST_BOT_USER_ID`, `MATTERMOST_USER_ID` (the human recipient), `MATTERMOST_USERNAME`. The bot needs `system_user` and `system_post_all_public` (or equivalent) roles. `.env` and `bin/` are gitignored — see `.env.example` for the schema.
- **Important distinction caught during setup**: Mattermost's "Bot ID" displayed in admin UI is the *bot object ID* (the bot record itself); the API uses the underlying *user ID* for posting. Store both in `.env` (`MATTERMOST_BOT_USER_ID` vs `MATTERMOST_BOT_OBJECT_ID`) since some endpoints want one and some the other.
- **Cloudflare bot-management gotcha**: if your Mattermost is behind Cloudflare, Python `urllib`'s default `Python-urllib/3.x` User-Agent often hits a 403 / CF error 1010. `lib/mattermost.py` sets a deliberate UA (`maestro-bot/1.0`) so the request is recognized as Maestro rather than a generic bot. Worth remembering for any other enterprise endpoint behind Cloudflare.
- `lib/mattermost.py`: pure-stdlib client (`urllib`, `json`, `pathlib` — no `requests` dependency). Subcommands `send` and `send-file`. UTF-8 clean (em-dash, Lithuanian `ąčęėįšųūž` verified). Resolves DM channel via `state.json > cached.mattermost_dm_channel_id` with API-create fallback.
- `state.json > cached.mattermost_dm_channel_id` stores the resolved channel id once `lib/mattermost.py` creates (or finds) it — subsequent runs skip the discovery roundtrip. (We observed Mattermost sometimes returning a channel id that happens to match the bot object id; that's a coincidence of how the deployment was provisioned, not something to rely on.)
- `run.sh > send_mattermost_urgent()`: post-heartbeat hook reads `.tmp/mattermost_urgent.txt`, sends each non-empty line via `lib/mattermost.py send-file`, deletes file on success, preserves on failure (for inspection). Cap of 2 messages re-enforced in shell after the prompt also enforces it.
- `prompts/heartbeat.md` section 6a rewritten: dual-channel routing (email = comprehensive, Mattermost = "look at this now" tap, urgent-only). Suppression check: same urgent finding not re-messaged within 6h (scans `daily/YYYY-MM-DD.md` for `Mattermost sent:` log lines). Logging contract: agent writes `Mattermost sent:` line per staged message.
- `CLAUDE.md` updated: Identity & Constraints section now lists Mattermost DM as a permitted (indirect) output channel. Core Principle reframed as "Email + (for urgent only) Mattermost", explicitly noting the dual-channel design is a response to Phase 3's failure mode.
- Metrics: `state.json > metrics.today.mattermost_messages_sent` (counts agent intent — i.e. lines written to the marker file — not delivery success).

**Lessons applied from removed Google Chat**:
- Don't infer "the agent is being read" from user actions during working hours — too easy to confuse coincidence with causation. Will add an explicit user-action signal (e.g., reply-with-emoji-react) before claiming a Mattermost-routing pattern works — but defer that until we have a few weeks of data.
- Keep `feedback.md` as the "tell the agent its routing is wrong" channel; don't rely on absence-of-action.
- The dual-channel design (email = canonical, Mattermost = nudge) means a missed Mattermost message is recoverable from the email. Different failure mode from Google Chat (where if the chat was missed, the urgency was lost).

**Outstanding (deferred to Phase 4 cloud)**:
- Inbound polling: `/api/v4/channels/{id}/posts?since=<ts>` against the DM channel. Same poll cadence as heartbeat. Requires consistent always-on host — fragile from a laptop, natural in Lambda. Deferred to cloud.
- Slash command (`/maestro <question>`) for ad-hoc queries: needs public HTTPS endpoint, so requires API Gateway + Lambda. Phase 4+.

#### Phase 3.7 — Repo Cleanup + GitHub Push (two-repo split)

**Why this exists**: Maestro currently lives in `assistant/` with no git history. Pre-cloud, we want version control + remote backup. Pre-Phase-5, we want the harness already shaped public-eligible — doing it once now avoids redoing it later.

**Repo split** (decided 2026-05-10):
- **`maestro` (public-eligible, private for now)**: harness only — `run.sh`, `lib/`, `prompts/`, `CLAUDE.md`, `config.json` (with placeholder values), `.claude/settings.json`, `.gitignore`, `mcp-servers.json` (base, no creds), `fixtures/` (anonymized), `apps-script/` (archived note).
- **`maestro-state` (private, always)**: agent's writable surface — `daily/`, `knowledge/`, `briefing.md`, `state.json`, `feedback.md`. NEVER public. This is the "agent's diary" the user can git-log to see what Maestro learned/forgot.
- **Not in either repo**: `memory.db` (binary, S3 in cloud, gitignored locally), `mcp-servers.json.runtime` (creds), `maestro.log`, `.tmp/`, `.cognee_data/`, `.secrets/`, `.env`.

**Why the split**: (1) cleanly separates the "code that runs Maestro" (shareable) from "data Maestro produced about my work" (never shareable). (2) Makes Phase 5 essentially free — `maestro` repo is already template-shaped. (3) State commits don't pollute harness git log; harness changes don't churn state diffs.

**Tasks**:
- Audit current `.gitignore` against the must-not-push list (including `mcp-servers.json.runtime`, `memory.db`, `daily/`, `knowledge/`, `briefing.md`, `state.json`, `maestro.log`, `.tmp/`, `.cognee_data/`, `.secrets/`, `.env*`)
- Redact `config.json > email.recipient` to a placeholder before committing (real value via env var or local override)
- Decide trunk branch name (`main`)
- `git init` two repos, initial commits, push to private GitHub
- README.md for each (skeleton OK, fuller content during Phase 5)

**Effort**: ~3 hours.

#### Phase 4 — Cloud Deployment (AWS Lambda)

Move to always-on cloud. Removes laptop dependency — most importantly removes the antivirus block on direct Google API tooling that Phase 4.5 needs.

**Architecture (decided 2026-05-09)**:
- **Compute**: Lambda container image (Claude Code CLI + Python + jq + bash; ~1.5GB; 10GB cap headroom). User accepted the 15-min ceiling risk on EOD (last EOD was 13.4 min) — will pivot to Step Functions split if it bites.
- **Trigger**: EventBridge Scheduler — `cron(0 8-19 ? * MON-FRI *)` for heartbeat, daily for EOD.
- **State**: hybrid — git for human-readable state (the `maestro-state` repo from Phase 3.7), S3 for `memory.db` (binary; `VACUUM INTO` snapshot at run-end). Run boundaries: clone+pull state repo, download `memory.db` from S3 → run → push state repo, upload `memory.db`.
- **Secrets**: Secrets Manager (Anthropic API key, Atlassian token, Pipedream creds for the moment, Mattermost bot token, GitHub PAT for state-repo push, Google OAuth refresh token after Phase 4.5).
- **IaC**: Terraform — `terraform/` directory in `maestro` repo with `s3.tf`, `lambda.tf`, `eventbridge.tf`, `iam.tf`, `secrets.tf`.
- **Logs**: CloudWatch Logs (replaces `maestro.log`); structured JSON lines from `run.sh`. Alarms on `Duration > 720s`, `Errors > 0`, `Invocations < 8` over 12h on weekdays.
- **Failure capture**: full Claude response saved to `s3://maestro-state/runs/<timestamp>/response.txt` for last 30 days — needed because LLM "succeeded but did wrong thing" failures are invisible to infra logging.
- **CI/CD**: GitHub Actions — push to `main` → ECR build → Lambda update.
- **Region**: TBD by user (lean eu-north-1 = Stockholm, closest + cheapest).

**Migration sequence (~5 days focused)**:
1. Containerize `run.sh` and run heartbeat locally in Linux container — proves Lambda compatibility (0.5d)
2. State repo split: implement clone-on-start / push-on-end in `run.sh` (0.5d)
3. S3 sync wrapper for `memory.db` (`lib/s3_sync.py`) (0.5d)
4. Terraform module + first apply (1d)
5. Cutover: deploy, smoke-test, run laptop + Lambda in parallel for 2-3 days, disable laptop scheduled tasks (0.5d)
6. GitHub Actions CI/CD pipeline (0.5d)
7. Phase 4.5 (Pipedream rip-out) immediately follows in cloud (1-2d)

**Risks**:
- Lambda Init Duration on a ~1.5GB container ~15-30s. Acceptable for hourly cron.
- The `claude_ai_Gmail` / `claude_ai_Google_Calendar` MCP "permission not granted" issue surfaced 2026-05-09 by `--check-auth` is moot in cloud — Phase 4.5 replaces those connectors entirely.

#### Phase 4.5 — Bypass Pipedream (Direct Google API via Python SDK)

**Why this exists**: Pipedream is a fragile two-layer dependency (Workflows OAuth + Connect OAuth, separately configured) that introduces failure modes we cannot debug or own. Documented evidence:
- Drive: never worked since install (140+ consecutive failures, never authorized correctly)
- Per-MCP-session auth lifecycle: each new `claude --print` session may need a fresh Connect grant — incompatible with 24/7 cloud runs
- Pipedream is third-party uptime risk: a 3am OAuth refresh failure in cloud would be undebuggable
- Confirmed 2026-04-24 onward: 16-day continuous Gmail/Drive/Calendar outage with no recovery path under Pipedream

**Tooling decision (2026-05-10)**: Originally considered `gog` (openclaw/gogcli) and `gws` (googleworkspace/cli) CLIs. Both blocked by laptop antivirus. **Final choice: `google-api-python-client` (Google's official Python SDK)** — pip-installable, narrow per-call scope control, pure Python, antivirus-friendly on Linux. Same end result as a CLI wrapper, more code but cleaner scope hygiene matches Maestro's "read-only observer with email-send-only" safety story.

**Plan**:
- `lib/gmail.py` — `gmail.send` scope only for sending; `gmail.readonly` for reading (replaces both Pipedream Gmail and the broken `claude_ai_Gmail` connector)
- `lib/google_calendar.py` — `calendar.readonly` scope only
- `lib/google_drive.py` — `drive.metadata.readonly` + `drive.readonly` for content fetch
- OAuth refresh tokens stored in Secrets Manager; auth helper at `lib/google_auth.py` handles browser-flow OAuth grant once (laptop, before cloud goes live), outputs refresh token for upload to Secrets Manager
- Removes Pipedream entirely; `mcp-servers.json` shrinks to Atlassian only

**Effort**: ~1-2 days of focused work in cloud. The hardest part (browser-based OAuth grant) happens once on the laptop where antivirus doesn't block Python — only the resulting refresh token goes to cloud.

**Validation gate**: smoke-test parity — for one week run Lambda with new Python SDK paths in parallel with the laptop's last Pipedream snapshot. If briefings match in shape, cut Pipedream out.

#### Phase 5 — Open Source

Extract the battle-tested system into a reusable template.

Tasks from backlog:
- Strip personal content, generalize config
- Agent-agnostic design (configurable CLI command, not Claude-specific)
- Documentation (setup guide, architecture diagram, customization guide)
- Example prompts and placeholder config
- Cross-platform installer
- Google Tasks integration (ship as optional example connector)


## Bugs / Things Broken

All Phase 0/1 bugs have been resolved. Remaining items are tracked in individual phase backlogs above.

## Features — Remaining Backlog

- **Google Tasks integration** — Pipedream has `google_tasks-list-tasks`. Low priority until confirmed useful. (Phase 5)
- **Notification routing config** — Urgency-to-channel mapping (`config.json > routing`). (Phase 3)
- **Secrets and auth refresh** — Document all credentials, add `--check-auth` flag, secrets manager. (Phase 4)
- **Fixture corpus for known incidents** — Replayable regression tests. (Phase 4)
- **Liveness monitoring** — Healthchecks.io ping after each run. (Phase 4)

## Technical Debt / Cleanup

All items from the original backlog have been resolved in Phase 0/1:

- [x] `apps-script/heartbeat-sender.gs` — archived with SUPERSEDED note
- [x] Prompt / CLAUDE.md duplication — consolidated, prompts reference CLAUDE.md
- [x] Project naming residue — hooks and log references fixed
- [x] Operational state in prose-heavy markdown — `state.json` is the structured companion
- [x] `feedback.md` has no structure — now has structured sections
- [x] Reinstall required after config changes — hash check warns on mismatch
- [x] Watchlist resolved items accumulate — mandatory pruning + `resolved-archive.md`
- [x] No prompt version tracking — `[prompt:HASH]` in daily log headers
- [x] Prompt vs shell responsibilities — deterministic decisions moved to `run.sh`/`state.py`

Remaining tech debt:
- **No explicit test harness** — `--dry-run` exists but no fixture corpus or diff assertions yet (Phase 4)
