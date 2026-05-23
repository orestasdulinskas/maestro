#!/usr/bin/env bash
# Maestro Agent Runner — Claude Code (local) flavor
#
# NOTE on the public-template refactor (2026-05):
#   Some paths referenced in this script changed when Maestro became
#   provider-agnostic:
#     - The project doc is AGENTS.md (was CLAUDE.md). Claude Code reads either.
#     - .claude/settings.json now ships as providers/claude-code/settings.json.example.
#       Copy it to .claude/settings.json before first use.
#     - The hook script is providers/claude-code/hooks/check_write_path.py
#       (was lib/hooks/...). The settings.example references the new path.
#     - mcp-servers.json is at providers/claude-code/mcp-servers.json (was root).
#     - For cloud runs or non-Claude-Code runtimes, use runner/maestro.py
#       (see scheduling/claude-routines.md and providers/*.md).
#   Lines below that still reference the old paths (./mcp-servers.json,
#   ./.claude, etc.) need updating before this script will work standalone.
#   If you only run Maestro in the cloud (Anthropic Routines), you don't need
#   this script at all.
#
# Runs Claude Code on a schedule to monitor work context.
#
# Usage:
#   ./run.sh --once        Single heartbeat check (skips outside working hours)
#   ./run.sh --force       Single heartbeat now, bypassing schedule gate (ops/verification)
#   ./run.sh --eod         End-of-day review (skips weekends)
#   ./run.sh --dry-run     Full heartbeat, skip email send
#   ./run.sh --check-auth  Probe every data source with a cheap read; print green/red dashboard
#   ./run.sh --replay <fixture> [--no-run]  Replay a regression-test fixture
#   ./run.sh --morning     Send current briefing as email (no source checks)
#   ./run.sh --install     Create Windows scheduled tasks (hourly heartbeat + EOD; Windows only)
#   ./run.sh --uninstall   Remove Windows scheduled tasks
#   ./run.sh --status      Show scheduled task status + recent log
#   ./run.sh               Fallback: run in foreground loop

set -euo pipefail

# Force UTF-8 end-to-end — prevents mojibake on Windows/Git Bash.
# PYTHONUTF8=1 is the critical one: it forces Python's open() default encoding to UTF-8
# (instead of Windows cp1252) for every subprocess. Without this, any non-ASCII byte
# in Claude's JSON output (e.g. Lithuanian characters via memory recall) crashes the
# inline `python3 -c "json.load(open(...))"` extractor with UnicodeDecodeError on cp1252.
# PYTHONIOENCODING only affects stdin/stdout/stderr; PYTHONUTF8 affects open() too.
export LANG="${LANG:-en_US.UTF-8}"
export LC_ALL="${LC_ALL:-en_US.UTF-8}"
export PYTHONIOENCODING="${PYTHONIOENCODING:-utf-8}"
export PYTHONUTF8="${PYTHONUTF8:-1}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_FILE="$SCRIPT_DIR/config.json"
ENV_FILE="$SCRIPT_DIR/.env"
LOG_FILE="$SCRIPT_DIR/maestro.log"
PROMPTS_DIR="$SCRIPT_DIR/prompts"
DAILY_DIR="$SCRIPT_DIR/daily"
LOCK_DIR="$SCRIPT_DIR/.maestro.lock"
TMP_DIR="$SCRIPT_DIR/.tmp"
mkdir -p "$TMP_DIR"
STATE_PY="$SCRIPT_DIR/lib/state.py"
MEMORY_PY="$SCRIPT_DIR/lib/memory_cognee.py"
COGNEE_PY="$SCRIPT_DIR/lib/cognee-venv/Scripts/python.exe"
DRY_RUN=false

TASK_NAME_HEARTBEAT="Maestro"
TASK_NAME_EOD="MaestroEOD"

# Ensure claude is findable — Task Scheduler doesn't source .bash_profile
if ! command -v claude &>/dev/null; then
  export PATH="$HOME/.local/bin:$HOME/bin:$PATH"
fi
CLAUDE_BIN="$(command -v claude 2>/dev/null || echo "")"
if [[ -z "$CLAUDE_BIN" ]]; then
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] ERROR: claude not found in PATH" | tee -a "${SCRIPT_DIR}/maestro.log"
  exit 1
fi

# --- Logging ---
log() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE"
}

# --- State helpers ---
state_cmd() {
  python3 "$STATE_PY" "$@"
}

memory_cmd() {
  if [[ -x "$COGNEE_PY" ]]; then
    "$COGNEE_PY" "$MEMORY_PY" "$@" 2>/dev/null
  else
    log "WARNING: cognee venv missing at $COGNEE_PY — skipping memory $1"
    return 0
  fi
}

# --- Liveness ping (Healthchecks.io or equivalent) ---
# Configure via config.json: { "monitoring": { "healthcheck_url": "https://hc-ping.com/<uuid>" } }
# Empty/missing → no-op. Append "/fail" on non-zero exit. 10s timeout, silent on success.
liveness_ping() {
  local exit_code="$1"
  local url
  url=$(load_config_json monitoring.healthcheck_url "")
  [[ -z "$url" || "$url" == "None" ]] && return 0
  if ! command -v curl &>/dev/null; then
    log "WARNING: curl not available, skipping liveness ping"
    return 0
  fi
  local target="$url"
  [[ "$exit_code" -ne 0 ]] && target="${url%/}/fail"
  curl -fsS -m 10 -o /dev/null "$target" 2>/dev/null || log "WARNING: liveness ping to $target failed"
}

compute_prompt_hash() {
  local file="$1"
  if command -v sha256sum &>/dev/null; then
    sha256sum "$file" | cut -c1-8
  elif command -v python3 &>/dev/null; then
    python3 -c "import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],'rb').read()).hexdigest()[:8])" "$file"
  else
    echo "unknown"
  fi
}

check_config_changed() {
  local installed_hash current_hash
  installed_hash=$(state_cmd get cached.installed_config_hash 2>/dev/null)
  if [[ -z "$installed_hash" ]]; then
    return  # No hash recorded — first run or pre-install
  fi
  current_hash=$(compute_prompt_hash "$CONFIG_FILE")
  if [[ "$installed_hash" != "$current_hash" ]]; then
    log "WARNING: config.json has changed since last --install (was $installed_hash, now $current_hash). Run './run.sh --install' to apply schedule changes."
  fi
}

# --- Config ---
load_config_value() {
  local key="$1" default="$2"
  local value
  if ! command -v python3 &>/dev/null; then
    echo "$default"; return
  fi
  if [[ ! -f "$CONFIG_FILE" ]]; then
    echo "$default"; return
  fi
  value=$(python3 -c "import json,sys; print(json.load(open(sys.argv[1]))['schedule'][sys.argv[2]])" "$CONFIG_FILE" "$key" 2>&1)
  if [[ $? -ne 0 ]] || ! [[ "$value" =~ ^[0-9]+$ ]]; then
    echo "$default"
  else
    echo "$value"
  fi
}

load_config_json() {
  # Read an arbitrary dotted path from config.json (e.g. "logging.log_file")
  local path="$1" default="$2"
  if ! command -v python3 &>/dev/null || [[ ! -f "$CONFIG_FILE" ]]; then
    echo "$default"; return
  fi
  python3 -c "
import json, sys
keys = sys.argv[2].split('.')
d = json.load(open(sys.argv[1]))
for k in keys:
    d = d[k]
print(d)
" "$CONFIG_FILE" "$path" 2>/dev/null || echo "$default"
}

START_HOUR=$(load_config_value start_hour 8)
END_HOUR=$(load_config_value end_hour 20)
INTERVAL=$(load_config_value interval_minutes 60)
EOD_HOUR=$(load_config_value eod_hour 19)
MAX_LOG_MB=$(load_config_json logging.max_log_size_mb 10)
SLA_SECONDS=$(load_config_value sla_seconds 300)
MODEL=$(load_config_json model "")

# Override LOG_FILE if configured
CONFIGURED_LOG=$(load_config_json logging.log_file "")
if [[ -n "$CONFIGURED_LOG" ]]; then
  # Resolve relative to SCRIPT_DIR
  case "$CONFIGURED_LOG" in
    /*) LOG_FILE="$CONFIGURED_LOG" ;;
    *)  LOG_FILE="$SCRIPT_DIR/$CONFIGURED_LOG" ;;
  esac
fi

# --- Log rotation ---
rotate_log_if_needed() {
  if [[ -f "$LOG_FILE" ]]; then
    local max_bytes=$((MAX_LOG_MB * 1024 * 1024))
    local size
    size=$(stat -c%s "$LOG_FILE" 2>/dev/null || stat -f%z "$LOG_FILE" 2>/dev/null || echo 0)
    if [[ $size -gt $max_bytes ]]; then
      mv "$LOG_FILE" "${LOG_FILE}.$(date '+%Y%m%d-%H%M%S').bak"
      echo "[$(date '+%Y-%m-%d %H:%M:%S')] Log rotated (previous was ${size} bytes)" > "$LOG_FILE"
    fi
  fi
}

# --- Lock (mkdir-based, atomic on all platforms including MSYS2) ---
# Lock stores PID and epoch timestamp. Staleness is detected by timestamp
# (more reliable than kill -0 on Windows where PID reuse is common).
LOCK_MAX_AGE=$((SLA_SECONDS * 2))

acquire_lock() {
  if mkdir "$LOCK_DIR" 2>/dev/null; then
    echo "$$ $(date +%s)" > "$LOCK_DIR/pid"
    trap release_lock EXIT
    return 0
  fi
  # Lock dir exists — check age and PID
  local lock_info lock_pid lock_time
  lock_info=$(cat "$LOCK_DIR/pid" 2>/dev/null || echo "")
  lock_pid=$(echo "$lock_info" | awk '{print $1}')
  lock_time=$(echo "$lock_info" | awk '{print $2}')

  # Check timestamp-based staleness first (works on Windows)
  if [[ -n "$lock_time" ]]; then
    local now age
    now=$(date +%s)
    age=$((now - lock_time))
    if [[ $age -gt $LOCK_MAX_AGE ]]; then
      log "WARNING: Lock is ${age}s old (max ${LOCK_MAX_AGE}s). Force-removing stale lock."
      rm -rf "$LOCK_DIR"
      if mkdir "$LOCK_DIR" 2>/dev/null; then
        echo "$$ $(date +%s)" > "$LOCK_DIR/pid"
        trap release_lock EXIT
        return 0
      fi
    fi
  fi

  # Fall back to PID check
  if [[ -n "$lock_pid" ]] && kill -0 "$lock_pid" 2>/dev/null; then
    log "Another run is in progress (PID $lock_pid). Skipping."
    exit 0
  fi
  # PID dead or unreadable — stale lock
  log "WARNING: Stale lock found (PID $lock_pid). Removing."
  rm -rf "$LOCK_DIR"
  if mkdir "$LOCK_DIR" 2>/dev/null; then
    echo "$$ $(date +%s)" > "$LOCK_DIR/pid"
    trap release_lock EXIT
    return 0
  fi
  log "ERROR: Could not acquire lock after stale removal. Skipping."
  exit 0
}

release_lock() {
  rm -rf "$LOCK_DIR"
}

# --- Helpers ---
is_weekday() {
  local dow
  dow=$(date '+%u')  # 1=Monday, 7=Sunday
  [[ $dow -le 5 ]]
}

is_within_hours() {
  local hour
  hour=$(date '+%H' | sed 's/^0//')
  is_weekday && [[ $hour -ge $START_HOUR && $hour -lt $END_HOUR ]]
}

is_eod() {
  local hour
  hour=$(date '+%H' | sed 's/^0//')
  [[ $hour -eq $EOD_HOUR ]]
}

archive_old_daily_logs() {
  # Move daily logs older than 30 days to daily/archive/
  local archive_dir="$DAILY_DIR/archive"
  local cutoff_date
  cutoff_date=$(date -d "30 days ago" '+%Y-%m-%d' 2>/dev/null || date -v-30d '+%Y-%m-%d' 2>/dev/null || return 0)

  local moved=0
  for f in "$DAILY_DIR"/*.md; do
    [[ -f "$f" ]] || continue
    local basename
    basename=$(basename "$f" .md)
    # Only process files matching YYYY-MM-DD pattern
    if [[ "$basename" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]] && [[ "$basename" < "$cutoff_date" ]]; then
      mkdir -p "$archive_dir"
      mv "$f" "$archive_dir/"
      moved=$((moved + 1))
    fi
  done

  if [[ $moved -gt 0 ]]; then
    log "Archived $moved daily log(s) older than 30 days to daily/archive/"
  fi
}

ensure_daily_file() {
  local today
  today=$(date '+%Y-%m-%d')
  local daily_file="$DAILY_DIR/$today.md"
  if [[ ! -d "$DAILY_DIR" ]]; then
    mkdir -p "$DAILY_DIR" || { log "ERROR: Could not create daily directory"; return 1; }
  fi
  if [[ ! -f "$daily_file" ]]; then
    echo "# Daily Log — $today" > "$daily_file"
    echo "" >> "$daily_file"
  fi
}

run_claude() {
  local prompt_file="$1"
  local run_type="$2"  # "heartbeat" or "eod"
  if [[ ! -f "$prompt_file" ]]; then
    log "ERROR: Prompt file not found: $prompt_file"
    return 1
  fi

  # Compute prompt hash for version tracking
  local prompt_hash
  prompt_hash=$(compute_prompt_hash "$prompt_file")

  # Record run start in state.json
  state_cmd run-start "$run_type" --prompt-hash "$prompt_hash"
  log "Prompt hash: $prompt_hash"

  # Build augmented prompt: state context + original prompt
  local tmp_prompt
  tmp_prompt=$(mktemp "${TMP_DIR}/prompt_XXXXXX.md")
  trap "rm -f '$tmp_prompt'" RETURN

  # Inject state context at the top of the prompt
  state_cmd inject-context "$run_type" --interval "$INTERVAL" > "$tmp_prompt"

  # Inject recalled memories from the semantic index
  if [[ -f "$MEMORY_PY" ]] && command -v python3 &>/dev/null; then
    echo "" >> "$tmp_prompt"
    memory_cmd recall >> "$tmp_prompt" || true
  fi

  # Inject Mattermost feedback received since the last successful run.
  # Only for real runs (skip in dry-run so test invocations don't consume the
  # watermark — that would silently swallow real user feedback). Skip on missing
  # creds rather than failing the heartbeat: feedback is optional, not critical.
  if [[ "$DRY_RUN" != "true" ]] && [[ -f "$SCRIPT_DIR/lib/mattermost.py" ]] && [[ -f "$ENV_FILE" ]] && command -v python3 &>/dev/null; then
    set -a; source "$ENV_FILE"; set +a
    if [[ -n "${MATTERMOST_BOT_TOKEN:-}" ]]; then
      local mm_block
      mm_block=$(python3 "$SCRIPT_DIR/lib/mattermost.py" fetch-recent 2>>"$LOG_FILE")
      if [[ -n "$mm_block" ]]; then
        echo "" >> "$tmp_prompt"
        printf '%s\n' "$mm_block" >> "$tmp_prompt"
        log "Mattermost feedback injected into prompt (block size: ${#mm_block} chars)"
      fi
    fi
  fi

  # Add dry-run notice if applicable
  if [[ "$DRY_RUN" == "true" ]]; then
    echo "" >> "$tmp_prompt"
    echo "## DRY RUN MODE" >> "$tmp_prompt"
    echo "This is a dry run. Do NOT send any emails. Perform all other steps normally (check sources, update files, write daily log). Skip only the email send step." >> "$tmp_prompt"
    echo "" >> "$tmp_prompt"
  fi

  # Append the original prompt
  cat "$prompt_file" >> "$tmp_prompt"

  # Prompt budget check — warn if total prompt is very large
  # Approximate tokens: chars / 4
  local prompt_chars prompt_tokens
  prompt_chars=$(wc -c < "$tmp_prompt" 2>/dev/null || echo 0)
  prompt_tokens=$((prompt_chars / 4))
  if [[ $prompt_tokens -gt 30000 ]]; then
    log "WARNING: Prompt is ~${prompt_tokens} tokens (${prompt_chars} chars). Consider reducing recalled memories or briefing size."
  fi
  log "Prompt budget: ~${prompt_tokens} tokens (${prompt_chars} chars)"

  # Run claude from the maestro directory so it picks up .claude/settings.json and CLAUDE.md
  # --mcp-config is required because --print mode does not load user-configured MCP servers automatically
  local claude_bin="${CLAUDE_BIN:-claude}"
  local mcp_config_file="$SCRIPT_DIR/mcp-servers.json"

  # Build MCP config: start with base Pipedream config, merge Atlassian from desktop config if available
  local desktop_config="$APPDATA/Claude/claude_desktop_config.json"
  if [[ -f "$desktop_config" ]] && command -v python3 &>/dev/null; then
    python3 -c "
import json, sys
base = json.load(open(sys.argv[1]))
try:
    desktop = json.load(open(sys.argv[2]))
    atlassian = desktop.get('mcpServers', {}).get('atlassian')
    if atlassian:
        base['mcpServers']['atlassian'] = atlassian
except Exception:
    pass
json.dump(base, sys.stdout)
" "$mcp_config_file" "$desktop_config" > "${mcp_config_file}.runtime" 2>/dev/null
    local mcp_arg="${mcp_config_file}.runtime"
  else
    local mcp_arg="$mcp_config_file"
  fi

  local model_flag=""
  if [[ -n "$MODEL" && "$MODEL" != "null" ]]; then
    model_flag="--model $MODEL"
  fi

  # Run Claude with JSON output to capture both text and usage stats
  local json_output
  json_output=$(mktemp "${TMP_DIR}/claude_out_XXXXXX.json")

  (cd "$SCRIPT_DIR" && unset CLAUDECODE && "$claude_bin" --print --output-format json $model_flag --mcp-config "$mcp_arg" < "$tmp_prompt") > "$json_output" 2>> "$LOG_FILE"
  local claude_exit=$?

  # Extract text output for the log
  if [[ -s "$json_output" ]] && command -v python3 &>/dev/null; then
    python3 -c "
import json, sys
try:
    data = json.load(open(sys.argv[1]))
    # Print text result to log
    result = data.get('result', '')
    if result:
        print(result)
    # Extract and record usage stats
    usage = data.get('usage', {})
    if usage:
        tokens_in = usage.get('input_tokens', 0)
        tokens_out = usage.get('output_tokens', 0)
        cost = usage.get('cost_usd', 0)
        print(f'[Usage] tokens_in={tokens_in} tokens_out={tokens_out} cost_usd={cost:.4f}', file=sys.stderr)
except Exception as e:
    # If JSON parsing fails, dump raw content
    with open(sys.argv[1]) as f:
        print(f.read())
" "$json_output" >> "$LOG_FILE" 2>> "$LOG_FILE"
  else
    cat "$json_output" >> "$LOG_FILE" 2>/dev/null
  fi

  rm -f "$json_output" "${mcp_config_file}.runtime" 2>/dev/null
  return $claude_exit
}

run_heartbeat() {
  log "Starting hourly heartbeat..."
  ensure_daily_file

  local start_time exit_code end_time duration
  start_time=$(date +%s)

  run_claude "$PROMPTS_DIR/heartbeat.md" heartbeat
  exit_code=$?

  # Retry once on failure with 30s backoff (catches transient API errors)
  if [[ $exit_code -ne 0 ]]; then
    log "WARNING: Heartbeat failed (exit $exit_code). Retrying in 30s..."
    sleep 30
    run_claude "$PROMPTS_DIR/heartbeat.md" heartbeat
    exit_code=$?
    if [[ $exit_code -ne 0 ]]; then
      log "ERROR: Heartbeat retry also failed (exit $exit_code)."
    else
      log "Heartbeat retry succeeded."
    fi
  fi

  end_time=$(date +%s)
  duration=$((end_time - start_time))

  # Record completion in state.json
  state_cmd run-complete heartbeat --exit-code "$exit_code"
  liveness_ping "$exit_code"

  if [[ $duration -gt $SLA_SECONDS ]]; then
    log "WARNING: Heartbeat OVERRAN SLA (${duration}s > ${SLA_SECONDS}s threshold)"
  fi

  # Post-run: send any Mattermost urgent messages the agent staged
  send_mattermost_urgent

  # Post-run: index new/changed files into memory
  if [[ -f "$MEMORY_PY" ]] && command -v python3 &>/dev/null; then
    memory_cmd index >> "$LOG_FILE" 2>&1 || log "WARNING: Memory indexing failed"
  fi

  log "Heartbeat completed in ${duration}s (exit: $exit_code)"
  return $exit_code
}

send_mattermost_urgent() {
  # If the agent staged urgent messages, deliver them via lib/mattermost.py and clean up.
  # File format: one message per non-empty line, written by the heartbeat prompt.
  # Cap of 2 is also enforced by the prompt; we re-enforce here defensively.
  local marker="$TMP_DIR/mattermost_urgent.txt"
  [[ -f "$marker" ]] || return 0

  local count
  count=$(grep -c -v '^[[:space:]]*$' "$marker" 2>/dev/null || echo 0)
  if [[ "$count" -eq 0 ]]; then
    rm -f "$marker"
    return 0
  fi
  if [[ "$count" -gt 2 ]]; then
    log "WARNING: agent staged $count Mattermost messages (cap is 2); truncating."
    head -n 2 "$marker" > "${marker}.capped" && mv "${marker}.capped" "$marker"
    count=2
  fi

  log "Sending $count Mattermost message(s)..."
  if [[ -f "$ENV_FILE" ]]; then
    set -a; source "$ENV_FILE"; set +a
  fi
  python3 "$SCRIPT_DIR/lib/mattermost.py" send-file "$marker" >> "$LOG_FILE" 2>&1
  local mm_exit=$?
  if [[ $mm_exit -eq 0 ]]; then
    log "Mattermost delivery OK ($count message(s))."
    rm -f "$marker"
  else
    log "ERROR: Mattermost delivery failed (exit $mm_exit). Preserving marker file at $marker for inspection."
  fi
}

run_eod() {
  log "Starting end-of-day review..."
  ensure_daily_file
  archive_old_daily_logs

  local start_time exit_code end_time duration
  start_time=$(date +%s)

  run_claude "$PROMPTS_DIR/end-of-day.md" eod
  exit_code=$?

  # Retry once on failure with 60s backoff
  if [[ $exit_code -ne 0 ]]; then
    log "WARNING: EOD review failed (exit $exit_code). Retrying in 60s..."
    sleep 60
    run_claude "$PROMPTS_DIR/end-of-day.md" eod
    exit_code=$?
    if [[ $exit_code -ne 0 ]]; then
      log "ERROR: EOD review retry also failed (exit $exit_code)."
    else
      log "EOD review retry succeeded."
    fi
  fi

  end_time=$(date +%s)
  duration=$((end_time - start_time))

  # Record completion in state.json
  state_cmd run-complete eod --exit-code "$exit_code"
  liveness_ping "$exit_code"

  if [[ $duration -gt $((SLA_SECONDS * 3)) ]]; then
    log "WARNING: EOD review OVERRAN SLA (${duration}s > $((SLA_SECONDS * 3))s threshold)"
  fi

  # Post-run: index new/changed files into memory
  if [[ -f "$MEMORY_PY" ]] && command -v python3 &>/dev/null; then
    memory_cmd index >> "$LOG_FILE" 2>&1 || log "WARNING: Memory indexing failed"
  fi

  log "EOD review completed in ${duration}s (exit: $exit_code)"
  return $exit_code
}

run_morning() {
  log "Starting morning briefing send..."
  local briefing_file="$SCRIPT_DIR/briefing.md"
  if [[ ! -f "$briefing_file" ]]; then
    log "ERROR: No briefing.md found. Run a heartbeat or EOD first."
    return 1
  fi

  # Create a minimal prompt that just sends the briefing
  local tmp_prompt
  tmp_prompt=$(mktemp "${TMP_DIR}/prompt_XXXXXX.md")
  trap "rm -f '$tmp_prompt'" RETURN

  cat > "$tmp_prompt" <<'MORNING_PROMPT'
## 0. Data Safety
Follow all data safety rules from CLAUDE.md.

## Task
Read `briefing.md` and `config.json`, then send the briefing as a morning email.

- Read `config.json` to get email recipient and subject prefix
- Read `briefing.md` for the content
- Send via `mcp__pipedream__gmail-send-email`
- Subject: `[Heartbeat] Good morning — briefing for today`
- Body: The contents of briefing.md, prefixed with "Here's your morning briefing:" and a blank line
- **ONLY** send to the exact email in config.json > email.recipient. No CC, no BCC.
- Use plain text — no HTML.
- Do NOT check any data sources, update any files, or do anything else.
MORNING_PROMPT

  local claude_bin="${CLAUDE_BIN:-claude}"
  local mcp_config_file="$SCRIPT_DIR/mcp-servers.json"

  local model_flag=""
  if [[ -n "$MODEL" && "$MODEL" != "null" ]]; then
    model_flag="--model $MODEL"
  fi

  (cd "$SCRIPT_DIR" && unset CLAUDECODE && "$claude_bin" --print --output-format text $model_flag --mcp-config "$mcp_config_file" < "$tmp_prompt") >> "$LOG_FILE" 2>&1
  local exit_code=$?
  log "Morning briefing send completed (exit: $exit_code)"
  return $exit_code
}

run_check_auth() {
  # Read-only diagnostic. No lock, no state.json mutation, no email send.
  # Output the dashboard from prompts/check-auth.md to stdout.
  local prompt_file="$PROMPTS_DIR/check-auth.md"
  if [[ ! -f "$prompt_file" ]]; then
    echo "ERROR: $prompt_file not found" >&2
    return 1
  fi

  # Build MCP config (same merge as run_heartbeat: Pipedream + Atlassian from desktop)
  local mcp_config_file="$SCRIPT_DIR/mcp-servers.json"
  local mcp_arg="$mcp_config_file"
  local desktop_config="$APPDATA/Claude/claude_desktop_config.json"
  if [[ -f "$desktop_config" ]] && command -v python3 &>/dev/null; then
    python3 -c "
import json, sys
base = json.load(open(sys.argv[1]))
try:
    desktop = json.load(open(sys.argv[2]))
    atlassian = desktop.get('mcpServers', {}).get('atlassian')
    if atlassian:
        base['mcpServers']['atlassian'] = atlassian
except Exception:
    pass
json.dump(base, sys.stdout)
" "$mcp_config_file" "$desktop_config" > "${mcp_config_file}.runtime" 2>/dev/null
    mcp_arg="${mcp_config_file}.runtime"
  fi

  local claude_bin="${CLAUDE_BIN:-claude}"
  local model_flag=""
  if [[ -n "$MODEL" && "$MODEL" != "null" ]]; then
    model_flag="--model $MODEL"
  fi

  (cd "$SCRIPT_DIR" && unset CLAUDECODE && "$claude_bin" --print --output-format text $model_flag --mcp-config "$mcp_arg" < "$prompt_file")
  return $?
}

# --- Windows path helpers ---
to_win_path() {
  cygpath -w "$1" 2>/dev/null || echo "$1" | sed 's|^/\([a-zA-Z]\)/|\1:\\|; s|/|\\|g'
}

get_bash_exe() {
  # Use Git Bash's bin/bash.exe (not usr/bin/bash.exe) — it sets up the MINGW environment
  local git_dir
  git_dir=$(cd "/c/Program Files/Git" 2>/dev/null && pwd || echo "")
  if [[ -n "$git_dir" && -f "$git_dir/bin/bash.exe" ]]; then
    to_win_path "$git_dir/bin/bash.exe"
  else
    local bash_path
    bash_path=$(which bash 2>/dev/null)
    to_win_path "$bash_path"
  fi
}

# --- Task Scheduler (PowerShell) ---
install_tasks() {
  local win_script
  win_script=$(to_win_path "$SCRIPT_DIR/run.sh")
  local bash_exe
  bash_exe=$(get_bash_exe)

  echo "Installing Windows scheduled tasks..."
  echo "  Bash: $bash_exe"
  echo "  Script: $win_script"
  echo ""

  local eod_time_fmt
  eod_time_fmt=$(printf "%02d:00" "$EOD_HOUR")

  # Use PowerShell to create tasks — gives full control over battery settings
  # DisallowStartIfOnBatteries=false + StopIfGoingOnBatteries=false = runs on battery
  echo "Creating Maestro heartbeat task (every ${INTERVAL}min)..."
  powershell.exe -NoProfile -Command "
    \$action = New-ScheduledTaskAction -Execute '${bash_exe}' -Argument '--login \"${win_script}\" --once'
    \$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes ${INTERVAL})
    \$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
    Unregister-ScheduledTask -TaskName '${TASK_NAME_HEARTBEAT}' -Confirm:\$false -ErrorAction SilentlyContinue
    Register-ScheduledTask -TaskName '${TASK_NAME_HEARTBEAT}' -Action \$action -Trigger \$trigger -Settings \$settings -Description 'Maestro — hourly work context monitor'
  " 2>&1 || true

  echo ""
  echo "Creating EOD task (daily at ${eod_time_fmt})..."
  powershell.exe -NoProfile -Command "
    \$action = New-ScheduledTaskAction -Execute '${bash_exe}' -Argument '--login \"${win_script}\" --eod'
    \$trigger = New-ScheduledTaskTrigger -Daily -At '${eod_time_fmt}'
    \$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
    Unregister-ScheduledTask -TaskName '${TASK_NAME_EOD}' -Confirm:\$false -ErrorAction SilentlyContinue
    Register-ScheduledTask -TaskName '${TASK_NAME_EOD}' -Action \$action -Trigger \$trigger -Settings \$settings -Description 'Maestro — end-of-day review'
  " 2>&1 || true

  # Record config hash at install time
  local config_hash
  config_hash=$(compute_prompt_hash "$CONFIG_FILE")
  state_cmd cache installed_config_hash "$config_hash"
  echo ""
  echo "Done. Tasks installed:"
  echo "  $TASK_NAME_HEARTBEAT — every ${INTERVAL}min (runs on battery, script skips nights/weekends)"
  echo "  $TASK_NAME_EOD — daily at ${eod_time_fmt} (runs on battery, script skips weekends)"
  echo "  Config hash: $config_hash"
  echo ""
  echo "Run './run.sh --status' to verify."
  echo "Run './run.sh --uninstall' to remove."
}

uninstall_tasks() {
  echo "Removing scheduled tasks..."
  powershell.exe -NoProfile -Command "
    Unregister-ScheduledTask -TaskName '${TASK_NAME_HEARTBEAT}' -Confirm:\$false -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName '${TASK_NAME_EOD}' -Confirm:\$false -ErrorAction SilentlyContinue
  " 2>&1 || true
  echo "Done."
}

show_status() {
  echo "=== Heartbeat Task ==="
  MSYS_NO_PATHCONV=1 schtasks.exe /query /tn "$TASK_NAME_HEARTBEAT" /v /fo LIST 2>&1 || echo "Not installed"
  echo ""
  echo "=== EOD Task ==="
  MSYS_NO_PATHCONV=1 schtasks.exe /query /tn "$TASK_NAME_EOD" /v /fo LIST 2>&1 || echo "Not installed"
  echo ""
  echo "=== Recent Log ==="
  tail -20 "$LOG_FILE" 2>/dev/null || echo "No log file yet"
}

# --- Main ---

rotate_log_if_needed
check_config_changed

case "${1:-}" in
  --once)
    acquire_lock
    if is_within_hours; then
      run_heartbeat
    else
      log "Outside working hours or weekend. Skipping."
    fi
    exit $?
    ;;
  --force)
    acquire_lock
    log "FORCED heartbeat run (schedule gate bypassed — for verification/ops only)."
    run_heartbeat
    exit $?
    ;;
  --eod)
    acquire_lock
    if ! is_weekday; then
      log "Weekend — skipping EOD review."
      exit 0
    fi
    run_eod
    exit $?
    ;;
  --dry-run)
    DRY_RUN=true
    acquire_lock
    log "DRY RUN: Running heartbeat without email send..."
    run_heartbeat
    exit $?
    ;;
  --replay)
    # Fixture replay regression test. Does NOT acquire lock — fixtures run in isolation
    # and should not block real heartbeats.
    if [[ -z "${2:-}" ]]; then
      echo "Usage: $0 --replay <fixture_name> [--no-run]" >&2
      echo "Available fixtures:" >&2
      ls -1 "$SCRIPT_DIR/fixtures" 2>/dev/null | grep -v '^README' >&2 || echo "  (none)" >&2
      exit 2
    fi
    shift  # drop --replay
    python3 "$SCRIPT_DIR/lib/dryrun.py" "$@"
    exit $?
    ;;
  --check-auth)
    run_check_auth
    exit $?
    ;;
  --morning)
    acquire_lock
    run_morning
    exit $?
    ;;
  --install)
    install_tasks
    exit $?
    ;;
  --uninstall)
    uninstall_tasks
    exit $?
    ;;
  --status)
    show_status
    exit 0
    ;;
  --help)
    echo "Usage: $0 [--once|--force|--eod|--dry-run|--check-auth|--morning|--install|--uninstall|--status|--help]"
    echo ""
    echo "  --once       Run a single heartbeat check (skips if outside working hours)"
    echo "  --force      Run a single heartbeat now, bypassing the schedule gate (ops/verification)"
    echo "  --eod        Run end-of-day review (skips weekends)"
    echo "  --dry-run    Run full heartbeat but skip email send"
    echo "  --replay <fixture> [--no-run]  Replay a regression-test fixture (see fixtures/README.md)"
    echo "  --check-auth Probe every source with a cheap read; print green/red dashboard (read-only, no state writes)"
    echo "  --morning    Send current briefing.md as email (no source checks)"
    echo "  --install    Create Windows scheduled tasks (hourly heartbeat + EOD)"
    echo "  --uninstall  Remove Windows scheduled tasks"
    echo "  --status     Show task scheduler status and recent logs"
    echo "  --help       Show this help"
    echo "  (no args)    Fallback: run in foreground loop"
    exit 0
    ;;
esac

# Fallback: continuous loop mode (use --install for production)
log "=== Maestro Agent Started (loop mode) ==="
log "TIP: Use './run.sh --install' to set up Windows Task Scheduler instead"
log "Schedule: ${START_HOUR}:00-${END_HOUR}:00, every ${INTERVAL}min, EOD at ${EOD_HOUR}:00"

eod_done_today=""

while true; do
  if is_within_hours; then
    today=$(date '+%Y-%m-%d')

    acquire_lock
    if is_eod && [[ "$eod_done_today" != "$today" ]]; then
      run_eod || log "WARNING: EOD review failed"
      eod_done_today="$today"
    else
      run_heartbeat || log "WARNING: Heartbeat failed"
    fi
    release_lock

    rotate_log_if_needed
    log "Sleeping ${INTERVAL} minutes..."
    sleep $((INTERVAL * 60))
  else
    log "Outside working hours (${START_HOUR}:00-${END_HOUR}:00). Checking again in 15 minutes..."
    eod_done_today=""
    sleep 900
  fi
done
