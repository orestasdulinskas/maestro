#!/usr/bin/env python3
"""Maestro state.json manager.

Provides atomic read/update/write for the run-state ledger.
Called from run.sh before and after each Claude invocation.

Usage:
  python3 lib/state.py run-start heartbeat --prompt-hash abc123def
  python3 lib/state.py run-complete heartbeat --exit-code 0
  python3 lib/state.py get last_run.heartbeat.completed_at
  python3 lib/state.py source-ok gmail_in
  python3 lib/state.py source-fail jira
  python3 lib/state.py metric emails_sent
  python3 lib/state.py cache atlassian_cloud_id 12345-abcde
  python3 lib/state.py inject-context heartbeat
"""

import io
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

# Force UTF-8 on stdout/stderr so emoji + em-dash render correctly on Windows.
# run.sh exports LANG/LC_ALL/PYTHONIOENCODING in production, but defensive here too.
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
if sys.stderr.encoding and sys.stderr.encoding.lower() != "utf-8":
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

STATE_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "state.json")

SCHEMA_VERSION = 1

DEFAULT_SOURCE = {
    "last_success": None,
    "last_failure": None,
    "consecutive_failures": 0,
    "last_known_status": "healthy",   # "healthy" | "degraded" | "disabled"
    "notification_pending": False,    # set on transition INTO degraded/disabled
    "status_changed_at": None,        # ISO timestamp of last status transition
}

# Capability-gating thresholds.
# - DEGRADED_THRESHOLD: degraded but still attempted (warn the agent + user).
# - HARD_DISABLE_THRESHOLD: assumed permanently broken; drop from prompt.
# - NEVER_SUCCEEDED_THRESHOLD: if last_success is None and we hit this many failures,
#   the source has never worked on this install — treat as disabled (catches misconfig).
DEGRADED_THRESHOLD = 3
HARD_DISABLE_THRESHOLD = 30
NEVER_SUCCEEDED_THRESHOLD = 5


def source_status(source_state):
    """Classify a source's health.

    Returns: (status, reason) where status is one of "healthy", "degraded", "disabled".
    Reason is a human-readable string suitable for the prompt; empty when healthy.
    """
    fails = source_state.get("consecutive_failures", 0) or 0
    last_success = source_state.get("last_success")
    last_failure = source_state.get("last_failure")

    if last_success is None and fails >= NEVER_SUCCEEDED_THRESHOLD:
        return "disabled", f"never succeeded in {fails} attempts since install (likely misconfigured or unauthorized)"
    if fails >= HARD_DISABLE_THRESHOLD:
        since = last_failure or "unknown"
        return "disabled", f"{fails} consecutive failures since {since} — assumed broken"
    if fails >= DEGRADED_THRESHOLD:
        since = last_failure or "unknown"
        return "degraded", f"{fails} consecutive failures since {since}"
    return "healthy", ""

DEFAULT_STATE = {
    "schema_version": SCHEMA_VERSION,
    "last_run": {
        "heartbeat": {
            "started_at": None,
            "completed_at": None,
            "duration_seconds": None,
            "exit_code": None,
            "prompt_hash": None,
        },
        "eod": {
            "started_at": None,
            "completed_at": None,
            "duration_seconds": None,
            "exit_code": None,
            "prompt_hash": None,
        },
    },
    "sources": {
        "gmail_in": dict(DEFAULT_SOURCE),
        "gmail_sent": dict(DEFAULT_SOURCE),
        "calendar": dict(DEFAULT_SOURCE),
        "jira": dict(DEFAULT_SOURCE),
        "confluence_user": dict(DEFAULT_SOURCE),
        "confluence_team": dict(DEFAULT_SOURCE),
        "google_drive": dict(DEFAULT_SOURCE),
    },
    "cached": {
        "atlassian_cloud_id": None,
        "atlassian_account_id": None,
        "installed_config_hash": None,
    },
    "metrics": {
        "today": {
            "date": None,
            "emails_sent": 0,
            "emails_skipped": 0,
            "web_searches": 0,
            "suggestions_made": 0,
            "consecutive_quiet_runs": 0,
            "mattermost_messages_sent": 0,
        },
        "week": {
            "week_start": None,
            "heartbeat_runs": 0,
            "eod_runs": 0,
            "total_emails_sent": 0,
            "total_emails_skipped": 0,
            "total_web_searches": 0,
            "total_suggestions": 0,
            "total_mattermost_messages_sent": 0,
        },
    },
}


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def today_str():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def week_start_str():
    """Monday of the current week as YYYY-MM-DD."""
    d = datetime.now(timezone.utc)
    monday = d - timedelta(days=d.weekday())
    return monday.strftime("%Y-%m-%d")


def load_state():
    """Load state.json, returning a fresh DEFAULT_STATE on missing or corrupt files.

    Corrupt-file recovery matches the contract documented in CLAUDE.md:
    "If the file is missing or corrupt, continue the run without it — run.sh will recreate it."
    """
    if not os.path.exists(STATE_FILE):
        return json.loads(json.dumps(DEFAULT_STATE))
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        sys.stderr.write(
            f"state.py: state.json unreadable ({type(exc).__name__}: {exc}); "
            f"falling back to DEFAULT_STATE for this run.\n"
        )
        return json.loads(json.dumps(DEFAULT_STATE))


def save_state(state):
    """Atomic write: write to temp file, then rename."""
    dir_name = os.path.dirname(STATE_FILE)
    fd, tmp_path = tempfile.mkstemp(dir=dir_name, suffix=".tmp", prefix="state_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp_path, STATE_FILE)
    except Exception:
        os.unlink(tmp_path)
        raise


def ensure_metrics_period(state):
    """Roll over daily/weekly metrics if the period changed."""
    today = today_str()
    if state["metrics"]["today"]["date"] != today:
        state["metrics"]["today"] = {
            "date": today,
            "emails_sent": 0,
            "emails_skipped": 0,
            "web_searches": 0,
            "suggestions_made": 0,
            "consecutive_quiet_runs": 0,
            "mattermost_messages_sent": 0,
        }

    ws = week_start_str()
    if state["metrics"]["week"]["week_start"] != ws:
        state["metrics"]["week"] = {
            "week_start": ws,
            "heartbeat_runs": 0,
            "eod_runs": 0,
            "total_emails_sent": 0,
            "total_emails_skipped": 0,
            "total_web_searches": 0,
            "total_suggestions": 0,
            "total_mattermost_messages_sent": 0,
        }
    return state


def resolve_dotpath(obj, path):
    """Resolve a dot-separated path like 'last_run.heartbeat.completed_at'."""
    for key in path.split("."):
        if isinstance(obj, dict) and key in obj:
            obj = obj[key]
        else:
            return None
    return obj


# ── Commands ────────────────────────────────────────────────

def cmd_run_start(args):
    run_type = args[0]
    prompt_hash = None
    if "--prompt-hash" in args:
        idx = args.index("--prompt-hash")
        prompt_hash = args[idx + 1] if idx + 1 < len(args) else None

    state = load_state()
    state = ensure_metrics_period(state)
    state["last_run"][run_type]["started_at"] = now_iso()
    state["last_run"][run_type]["prompt_hash"] = prompt_hash

    # Increment run counter
    counter_key = f"{run_type}_runs"
    if counter_key in state["metrics"]["week"]:
        state["metrics"]["week"][counter_key] += 1

    save_state(state)


def cmd_run_complete(args):
    run_type = args[0]
    exit_code = 0
    if "--exit-code" in args:
        idx = args.index("--exit-code")
        exit_code = int(args[idx + 1]) if idx + 1 < len(args) else 0

    state = load_state()
    state = ensure_metrics_period(state)

    started = state["last_run"][run_type].get("started_at")
    completed = now_iso()
    state["last_run"][run_type]["exit_code"] = exit_code

    # On a successful run, assume the agent saw the pending-notification block
    # in inject-context and emitted the alert. Clear flags so we don't spam.
    # If the agent failed to deliver, exit_code != 0 and we keep flags pending.
    if exit_code == 0:
        for src in state.get("sources", {}).values():
            if src.get("notification_pending"):
                src["notification_pending"] = False

    # Sanity-check timestamps before persisting. We've seen `completed_at` end up
    # earlier than `started_at` (concurrent run overwrite or clock skew). Refuse
    # to persist that — keep the previous completed_at and log to stderr.
    if started:
        try:
            start_dt = datetime.fromisoformat(started)
            end_dt = datetime.fromisoformat(completed)
            duration = (end_dt - start_dt).total_seconds()
            if duration < 0:
                print(
                    f"[state.py] WARN: run-complete for {run_type} would set completed_at ({completed}) "
                    f"before started_at ({started}). Refusing to write inverted timestamp.",
                    file=sys.stderr,
                )
                # Persist exit code only; leave completed_at + duration untouched.
                save_state(state)
                return
            state["last_run"][run_type]["completed_at"] = completed
            state["last_run"][run_type]["duration_seconds"] = int(duration)
        except (ValueError, TypeError) as e:
            print(f"[state.py] WARN: timestamp parse failed: {e}", file=sys.stderr)
            state["last_run"][run_type]["completed_at"] = completed
    else:
        state["last_run"][run_type]["completed_at"] = completed

    save_state(state)


def cmd_get(args):
    path = args[0]
    state = load_state()
    value = resolve_dotpath(state, path)
    if value is None:
        print("")
    else:
        print(value)


def _ensure_source_fields(s):
    """Backfill fields added after the initial schema."""
    for k, v in DEFAULT_SOURCE.items():
        s.setdefault(k, v)
    return s


def _detect_transition(source_state, prev_status):
    """If status changed from prev_status, update tracking fields.

    Notification rules:
      - Transition *into* "degraded" or "disabled": set notification_pending=true.
      - Transition *into* "healthy" (recovery): set notification_pending=true so
        the agent can announce "X recovered" — useful confirmation after re-auth.
    """
    new_status, _ = source_status(source_state)
    if new_status != prev_status:
        source_state["last_known_status"] = new_status
        source_state["status_changed_at"] = now_iso()
        source_state["notification_pending"] = True


def cmd_source_ok(args):
    source = args[0]
    state = load_state()
    if source not in state["sources"]:
        state["sources"][source] = dict(DEFAULT_SOURCE)
    s = _ensure_source_fields(state["sources"][source])
    prev_status = s.get("last_known_status", "healthy")
    s["last_success"] = now_iso()
    s["consecutive_failures"] = 0
    _detect_transition(s, prev_status)
    save_state(state)


def cmd_source_fail(args):
    source = args[0]
    state = load_state()
    if source not in state["sources"]:
        state["sources"][source] = dict(DEFAULT_SOURCE)
    s = _ensure_source_fields(state["sources"][source])
    prev_status = s.get("last_known_status", "healthy")
    s["last_failure"] = now_iso()
    s["consecutive_failures"] = (s.get("consecutive_failures", 0) or 0) + 1
    _detect_transition(s, prev_status)
    save_state(state)


def cmd_source_notified(args):
    """Agent calls this after sending the user a re-auth or recovery notification.

    Clears notification_pending so we don't spam every run while a source is broken.
    """
    source = args[0]
    state = load_state()
    if source in state["sources"]:
        state["sources"][source]["notification_pending"] = False
        save_state(state)


def cmd_metric(args):
    metric_name = args[0]
    state = load_state()
    state = ensure_metrics_period(state)

    if metric_name in state["metrics"]["today"]:
        state["metrics"]["today"][metric_name] += 1
    # Also roll up to weekly totals
    weekly_key = f"total_{metric_name}"
    if weekly_key in state["metrics"]["week"]:
        state["metrics"]["week"][weekly_key] += 1

    save_state(state)


def cmd_cache(args):
    key = args[0]
    value = args[1] if len(args) > 1 else None
    state = load_state()
    state["cached"][key] = value
    save_state(state)


def cmd_inject_context(args):
    """Print a block of context for run.sh to prepend to the prompt.

    Outputs lines like:
      ## Run Context (from state.json)
      - Last successful heartbeat: 2026-03-27T14:00:00+00:00
      - Search window: since 2026-03-27T14:00:00+00:00
      - Degraded sources: jira (3 consecutive failures)
      - Cached Atlassian cloudId: 12345-abcde
    """
    run_type = args[0]
    # Optional --interval flag (minutes) for catch-up detection
    interval_minutes = 60
    if "--interval" in args:
        idx = args.index("--interval")
        interval_minutes = int(args[idx + 1]) if idx + 1 < len(args) else 60

    state = load_state()

    lines = ["## Run Context (from state.json)", ""]

    # Last successful run timestamp — this is the search window
    last_completed = state["last_run"].get(run_type, {}).get("completed_at")
    is_catchup = False
    if last_completed:
        lines.append(f"- **Last successful {run_type}**: {last_completed}")
        lines.append(f"- **Search window**: Use this timestamp instead of relative time filters like `newer_than:1h`. Search for activity since `{last_completed}`.")

        # Catch-up detection: if last run was more than 2x the interval ago
        last_dt = datetime.fromisoformat(last_completed)
        now_dt = datetime.now(timezone.utc)
        gap_minutes = (now_dt - last_dt).total_seconds() / 60
        if gap_minutes > interval_minutes * 2:
            is_catchup = True
            lines.append(f"- **CATCH-UP MODE**: Last run was {int(gap_minutes)} minutes ago ({int(gap_minutes / 60)}h). "
                         f"Normal interval is {interval_minutes}min. One or more runs were missed. "
                         f"Expand your search windows to cover the full gap. Be thorough -- events may have been missed.")
    else:
        lines.append(f"- **Last successful {run_type}**: Never (first run)")
        lines.append("- **Search window**: Use `newer_than:1d` as a safe default for first run.")

    # Previous run exit code (detect failed previous run)
    prev_exit = state["last_run"].get(run_type, {}).get("exit_code")
    if prev_exit is not None and prev_exit != 0:
        lines.append(f"- **Previous run failed** (exit code {prev_exit}). Check for issues that may have carried over.")

    # Prompt hash
    prompt_hash = state["last_run"].get(run_type, {}).get("prompt_hash")
    if prompt_hash:
        lines.append(f"- **Current prompt hash**: `{prompt_hash}`")

    # Source health classification (healthy / degraded / disabled)
    degraded = []
    disabled = []
    pending_notifications = []  # status transitions the user hasn't been told about yet
    for name, info in state.get("sources", {}).items():
        status, reason = source_status(info)
        if status == "disabled":
            disabled.append(f"{name}: {reason}")
        elif status == "degraded":
            degraded.append(f"{name} ({reason})")
        if info.get("notification_pending"):
            pending_notifications.append({
                "name": name,
                "status": status,
                "reason": reason or "recovered",
                "changed_at": info.get("status_changed_at"),
            })

    if pending_notifications:
        lines.append("- 🚨 **STATUS CHANGE — USER NOTIFICATION REQUIRED**:")
        for n in pending_notifications:
            verb = "broken — RE-AUTH MAY BE NEEDED" if n["status"] in ("degraded", "disabled") else "recovered ✓"
            lines.append(f"  - **{n['name']}** transitioned to `{n['status']}` ({verb}) at {n['changed_at']}: {n['reason']}")
        lines.append("  - **You MUST**: include a clearly-marked alert at the top of the next email (and the Mattermost urgent message if you stage one) naming the affected source(s) and the likely cause (e.g. 'Pipedream Drive auth may have expired — please re-authorize at pipedream.com'). For disabled sources, suggest the specific re-auth step.")
        lines.append("  - **After delivering the alert**, the run.sh runner will call `python3 lib/state.py source-notified <name>` to clear the pending flag. (You do not need to call this yourself — it is handled by the wrapper.)")

    if disabled:
        lines.append(f"- **Disabled sources** (do NOT attempt these tools — they will fail and waste time):")
        for d in disabled:
            lines.append(f"  - {d}")
        lines.append("  - Skip the corresponding step in your gather-context routine. In the email, note: 'X disabled — see system notes' so the user knows to investigate. Do NOT retry these.")
    if degraded:
        lines.append(f"- **Degraded sources** (still attempt, but expect failure): {'; '.join(degraded)}")
        lines.append("  - For degraded sources: note the outage in the daily log, include a warning in the email if sending, but do not claim 'nothing new' -- say 'could not check'.")
    if not degraded and not disabled:
        lines.append("- **All sources healthy** (no consecutive failure streaks)")

    # Cached identifiers
    cloud_id = state.get("cached", {}).get("atlassian_cloud_id")
    account_id = state.get("cached", {}).get("atlassian_account_id")
    if cloud_id:
        lines.append(f"- **Cached Atlassian cloudId**: `{cloud_id}` (use this if `getAccessibleAtlassianResources` fails)")
    if account_id:
        lines.append(f"- **Cached Atlassian accountId**: `{account_id}` (use instead of `currentUser()` if needed)")

    # Quiet-run tracking
    quiet_count = state.get("metrics", {}).get("today", {}).get("consecutive_quiet_runs", 0)
    if quiet_count >= 2 and not is_catchup:
        lines.append(f"- **Quiet period**: {quiet_count} consecutive quiet runs. Consider one proactive investigation (stale watchlist item, backlog check, or workflow review).")

    # Monday morning enrichment
    d = datetime.now(timezone.utc)
    if d.weekday() == 0 and run_type == "heartbeat":  # Monday
        first_run_today = state["metrics"].get("today", {}).get("date") != today_str()
        if first_run_today or state["metrics"].get("week", {}).get("heartbeat_runs", 0) <= 1:
            lines.append("- **MONDAY MORNING**: This is the first run of the week. Include a **Week Ahead** section in the email:")
            lines.append("  - Surface all watchlist deadlines falling this week")
            lines.append("  - Highlight stale items that should be resolved or explicitly dropped")
            lines.append("  - Check calendar for the full week's meetings (via Gmail invites if Calendar unavailable)")
            lines.append("  - Note any patterns from last week's recalled memories that are relevant")

    # Today's metrics
    m = state.get("metrics", {}).get("today", {})
    if m.get("date") == today_str():
        lines.append(f"- **Today's metrics**: {m.get('emails_sent', 0)} emails sent, {m.get('emails_skipped', 0)} skipped, {m.get('web_searches', 0)} web searches, {m.get('suggestions_made', 0)} suggestions")

    lines.append("")
    print("\n".join(lines))


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 lib/state.py <command> [args...]", file=sys.stderr)
        print("Commands: run-start, run-complete, get, source-ok, source-fail, metric, cache, inject-context", file=sys.stderr)
        sys.exit(1)

    command = sys.argv[1]
    args = sys.argv[2:]

    commands = {
        "run-start": cmd_run_start,
        "run-complete": cmd_run_complete,
        "get": cmd_get,
        "source-ok": cmd_source_ok,
        "source-fail": cmd_source_fail,
        "source-notified": cmd_source_notified,
        "metric": cmd_metric,
        "cache": cmd_cache,
        "inject-context": cmd_inject_context,
    }

    if command not in commands:
        print(f"Unknown command: {command}", file=sys.stderr)
        sys.exit(1)

    commands[command](args)


if __name__ == "__main__":
    main()
