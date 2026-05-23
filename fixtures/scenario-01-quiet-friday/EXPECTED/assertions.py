"""Structural assertions for scenario-01-quiet-friday.

Each assertion takes the agent's parsed output (a dict with keys: email,
email_skip_reason, daily_log, watchlist, briefing, suggestions, alerts) and
returns (passed: bool, message: str).

DESIGN PRINCIPLE: assert STRUCTURAL FACTS, not exact strings. LLM phrasing
varies — we check that the right *concepts* appear, not specific words.
"""

# ── Helpers ──────────────────────────────────────────────────


def _lower(s):
    return (s or "").lower()


def _email(out):
    return out.get("email") or ""


def _alerts_for(out, source_substr):
    """Return alerts whose 'source' field mentions a given substring."""
    s = source_substr.lower()
    return [a for a in (out.get("alerts") or []) if s in _lower(a.get("source", "")) or s in _lower(a.get("summary", ""))]


def _has_any(text, keywords):
    t = _lower(text)
    return any(k in t for k in keywords)


# ── Assertions ───────────────────────────────────────────────


def assert_email_was_sent(out):
    """Email body must exist — silent Confluence edit + 2 pending status alerts warrant a send."""
    body = _email(out).strip()
    if not body:
        skip = out.get("email_skip_reason") or "(no reason given)"
        return False, f"email was skipped ({skip}) — expected a send: silent Confluence edit + Drive/Calendar alerts pending"
    if len(body) < 50:
        return False, f"email is suspiciously short ({len(body)} chars) — likely empty or truncated"
    return True, f"email produced ({len(body)} chars)"


def assert_email_mentions_drive_reauth(out):
    """Email must alert about Drive re-auth (notification_pending=true in fixture state)."""
    body = _lower(_email(out))
    alerts = _alerts_for(out, "drive")
    drive_keywords = ["drive"]
    reauth_keywords = ["re-auth", "reauth", "reauthor", "re-authoriz", "authorization", "authorize", "credentials"]

    in_body = _has_any(body, drive_keywords) and _has_any(body, reauth_keywords)
    in_alerts = any(_has_any(a.get("summary", ""), reauth_keywords) for a in alerts) or any(
        _lower(a.get("kind", "")) == "reauth" for a in alerts
    )
    if not (in_body or in_alerts):
        return False, "no Drive re-auth signal in email body or alerts (expected: notification_pending=true)"
    where = "email" if in_body else "alerts[]"
    return True, f"Drive re-auth surfaced in {where}"


def assert_email_mentions_calendar_broken(out):
    """Email must alert about Calendar being disabled (notification_pending=true in fixture state)."""
    body = _lower(_email(out))
    alerts = _alerts_for(out, "calendar")
    cal_keywords = ["calendar"]
    broken_keywords = ["disabled", "broken", "unavailable", "failing", "down", "re-auth", "reauth", "not working"]

    in_body = _has_any(body, cal_keywords) and _has_any(body, broken_keywords)
    in_alerts = bool(alerts)
    if not (in_body or in_alerts):
        return False, "no Calendar-broken signal in email body or alerts (expected: notification_pending=true)"
    where = "email" if in_body else "alerts[]"
    return True, f"Calendar broken-state surfaced in {where}"


def assert_email_surfaces_silent_confluence_edit(out):
    """Email should mention Maria's silent edit to 'API Architecture v2' — the main 'finding' of the run."""
    body = _lower(_email(out))
    log = _lower(out.get("daily_log", ""))
    text = body + "\n" + log

    # Must mention the editor or the page (one is enough — agent may use either as the anchor)
    person_or_page = _has_any(text, ["maria", "api architecture", "rate-limiter design", "rate limiter design"])
    # Must convey it's an edit/update, not a fresh request
    edit_signal = _has_any(text, ["edited", "updated", "added", "modified", "silent", "new section", "open questions"])

    if not person_or_page:
        return False, "email/log does not name Maria or 'API Architecture v2' — silent edit not surfaced"
    if not edit_signal:
        return False, "email/log mentions Maria/page but does not convey it is an edit/update"
    return True, "silent Confluence edit surfaced (person/page + edit signal both present)"


def assert_watchlist_escalates_stale_maria_mention(out):
    """The 6-day-stale Maria @mention should be escalated — Maria is now active (silently edited the same page)."""
    wl = out.get("watchlist") or ""
    wl_lower = _lower(wl)

    if "maria" not in wl_lower:
        return False, "watchlist no longer contains Maria @mention (expected: still tracked, now escalated)"

    # The status should be escalated — Maria is active (just edited the page) but the @mention is unresponded.
    # CLAUDE.md spec: "Upgrade the watchlist item ... 'X is active ... but hasn't responded'"
    if "escalated" not in wl_lower:
        return False, "watchlist still has Maria item but status was not promoted to 'escalated' (she edited the page she @mentioned on)"
    return True, "Maria @mention escalated as expected"


def assert_briefing_under_60_lines(out):
    """The briefing.md content must be ≤ 60 lines per CLAUDE.md spec."""
    briefing = out.get("briefing") or ""
    line_count = len(briefing.splitlines())
    if line_count == 0:
        return False, "briefing is empty"
    if line_count > 60:
        return False, f"briefing has {line_count} lines (limit: 60)"
    return True, f"briefing is {line_count} lines (under 60-line limit)"


def assert_disabled_sources_not_retried(out):
    """The agent must NOT claim it checked disabled sources — that would be lying.

    Bonus assertion: tests the inverse of the gating policy. The fixture has
    Drive and Calendar disabled; if the agent's daily_log says 'checked Drive'
    or 'no new files in Drive', that's a regression of the gating behavior.
    """
    log = _lower(out.get("daily_log", ""))
    body = _lower(_email(out))
    text = log + "\n" + body

    # Phrases that would indicate the agent pretended to check
    bad_phrases_drive = [
        "checked drive", "drive: no new", "drive shows", "no new files in drive",
        "drive returned", "scanned drive",
    ]
    bad_phrases_cal = [
        "checked calendar", "calendar: no events", "calendar shows", "calendar returned",
        "no meetings on calendar",
    ]

    hits = [p for p in bad_phrases_drive + bad_phrases_cal if p in text]
    if hits:
        return False, f"agent claimed to check disabled source(s): {hits}"
    return True, "no false claims about disabled sources"
