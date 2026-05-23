# Scenario 01 — Quiet Friday

A late-Friday-afternoon heartbeat where almost nothing has happened.

## Setup
- Day: Friday 16:00 EEST
- Sources healthy: Gmail in/sent, Jira, Confluence
- Sources disabled: Google Drive (re-auth pending), Calendar (broken)
- 1 watchlist item is stale (>5 days), 1 is active
- 0 new emails, 0 Jira changes, 1 Confluence page silently edited by a teammate

## What "good" looks like for this scenario
- Email IS sent (because Confluence silent edit is worth surfacing + degraded sources need announcing)
- Email mentions: Drive re-auth needed, Calendar broken, the silent Confluence edit, the stale watchlist item
- Email does NOT spam-mention every healthy source
- Watchlist gets one update (the stale item escalated)
- Briefing is rewritten and stays under 60 lines

This scenario tests:
1. Status-change notification fires for Drive + Calendar
2. Quiet-hour proactive investigation (Confluence re-read) catches silent edits
3. Watchlist staleness detection works
4. Email is not skipped just because no new emails arrived
