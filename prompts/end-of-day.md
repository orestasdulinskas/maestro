You are running the end-of-day review. This is a comprehensive review of the user's entire day. Follow these steps carefully.

## 0. Data Safety
Follow all data safety rules from AGENTS.md (injection detection, file write restrictions, untrusted content handling). They are not repeated here.

## 0.1. Error Handling
- If any data source fails (auth error, timeout, tool error), log the failure explicitly and continue with other sources.
- Never write "nothing new" when a source was unreachable — always distinguish "checked and empty" from "could not check".
- If `currentUser()` doesn't resolve in Jira queries, use the `atlassianUserInfo` tool to get the account ID and query directly.
- Only overwrite knowledge files (`active-context.md`, `user-profile.md`) when you have complete data. If sources were unavailable, preserve existing content and note the gap.

## 1. Load Context
- Read `feedback.md` — the user may have left personal notes for you
- Read today's full `daily/YYYY-MM-DD.md` to see what was already captured
- Read `knowledge/user-profile.md` for existing patterns
- Read `knowledge/active-context.md` for current priorities
- Read `knowledge/watchlist.md` for tracked items
- Read the `## Recalled Memories` section at the top of this prompt (if present) — these are semantically relevant excerpts from past runs, retrieved from the memory index. Use them for the weekly summary and pattern detection. Trust current data sources over recalled memories.
- Read all files in `workflows/` for documented processes

## 1.1 Check for Email Feedback
- Search: `subject:"[Heartbeat]" from:me newer_than:1d`
- If the user replied to any heartbeat email today, extract their feedback
- Treat replies as direct instructions — adjust knowledge, priorities, or approach accordingly
- Log all feedback found in the daily summary

## 1.2 Read Run Context
Read the `## Run Context` section at the top of this prompt. It contains:
- The timestamp of the last successful EOD run (for context, not search windows — EOD reviews the full day)
- Degraded sources and cached Atlassian identifiers
- If cached Atlassian identifiers are provided, use them as fallbacks if `getAccessibleAtlassianResources` or `currentUser()` fail

## 2. Full-Day Data Review

### Gmail — Full Day
- Search: `newer_than:1d` to get all emails from today
- Categorize: received vs sent (search `from:me newer_than:1d` for sent)
- Note: key conversations, decisions made, action items mentioned
- Track: who the user communicated with most, topics covered
- **Synthesize multi-reply threads**: Don't just count replies — summarize the outcome, consensus, and any open questions for each significant thread
- **Detect actions taken**: Cross-reference sent emails with watchlist and briefing items — mark resolved items

### Google Calendar — Full Day
Try the Calendar list-events capability (MCP function `google_calendar-list-events`) to list all events for today. If UNAVAILABLE, fall back to Gmail:
- **Fallback**: Gmail search capability with query: `has:invite newer_than:1d` for today's invite emails
- Note: which meetings occurred, duration, attendees
- Identify: meetings that likely produced action items

### Jira — Full Day
- Search: `assignee = currentUser() AND updated >= startOfDay() ORDER BY updated DESC`
- Also: `reporter = currentUser() AND created >= startOfDay()` for newly created issues
- Track: issues created, transitioned, commented on, resolved
- Note: sprint progress, blockers encountered

### Confluence — Full Day (user + team)
- Search for pages modified today by the user: `lastModified >= now("-1d") AND contributor = currentUser()`
- Search for pages modified by collaborators on active projects (use key names and project keywords from `active-context.md`)
- Note: what documentation was created or updated, and by whom

### Google Drive — Full Day
- Search for documents modified today in tracked shared drives
- Look for documents relevant to active projects and watchlist items
- Note: what was created or updated, relevance to current priorities

## 3. Daily Summary
Preserve all earlier heartbeat entries, then append:

```
## End-of-Day Summary

### Communication
- [N] emails received, [N] sent
- Key threads: [list important conversations with outcomes, not just names]
- Contacts: [who was interacted with most]

### Meetings
- [List each meeting with outcome and any action items produced]

### Work Output
- Jira: [issues touched, transitions, new tickets]
- Confluence: [pages created/edited — by user and by team on tracked projects]
- Key decisions made: [list any]

### Actions Resolved Today
- [Briefing/watchlist items that the user completed today — gives a sense of progress]

### Patterns Observed
- [Any notable patterns in today's work]
- [Energy/focus patterns if detectable from activity timing]

### Tomorrow Outlook
- [Pending items, upcoming deadlines, meetings]
```

## 4. Knowledge Updates

### `knowledge/user-profile.md`
Review existing entries and update based on today:
- Add new patterns confirmed by today's activity
- Update `Last confirmed: YYYY-MM-DD` for patterns that were reinforced
- Be conservative — only add patterns you've seen across multiple days
- Incorporate any email feedback from step 1.1

### `knowledge/active-context.md`
Rewrite with the current state:
- Active projects and their status based on today's activity
- Known blockers and pending items
- Upcoming deadlines

### `knowledge/watchlist.md`
Full review:
- **Resolve** items completed today (detected from sent emails, Jira transitions, meetings held)
- **Add** new items from today's activity (pending replies, upcoming deadlines, submitted requests)
- **Mark stale** items past their expected date
- **Prune** resolved items older than 2 days:
  1. For each resolved item older than 2 days, append a one-line entry to `knowledge/resolved-archive.md` with format: `- YYYY-MM-DD: [item description] — resolved: [how]`
  2. Then remove the item from `watchlist.md`
  3. This is mandatory — do not skip pruning. The watchlist must stay lean.
- Ensure every blocker in `active-context.md` has a corresponding watchlist entry

### Decay Check
Review every entry in `user-profile.md`:
- If `Last confirmed` is older than 14 days, evaluate if still likely relevant
- Remove entries that seem stale or were one-off observations
- For each removal, append to `knowledge/decay-log.md`:
```
## YYYY-MM-DD — Decay Review
- Removed: "[entry description]" — Reason: [why it's no longer relevant]
```

### Workflow Updates
- If today's activity revealed a new repeating process, consider noting it for the user (but don't create workflow files — those are user-maintained)
- If an existing workflow was followed today, note any deviations or updates that might be useful

## 5. Morning Briefing
Write `briefing.md` as a briefing for tomorrow morning:

```
# Morning Briefing — YYYY-MM-DD

## Priority Items
[Things that need attention first thing — only unresolved items]

## Stale Items
[Watchlist items past their expected date — these need attention or explicit deprioritization]

## Today's Schedule
[Meetings and deadlines from calendar]
[For each meeting: one-line prep note — what it's about, what to bring, what's at stake]

## Ongoing Threads
[Email conversations or Jira items awaiting response]
[For threads with multiple replies: summarize current state, not just "awaiting response"]

## Approaching Deadlines
[Items due within 3 business days — with current status and whether they're on track]

## Context
[Relevant background from recent days that might be useful]

## From Your Feedback
[Anything from feedback.md or email replies that's relevant]
```

Keep it under 60 lines. Be actionable and specific. Every item should answer "what do I need to do?" not just "what happened?"

## 6. Delivery

EOD is multi-section long-form synthesis — the canonical example of a Gmail draft per the form-factor routing rule (see AGENTS.md → Core Principle). Email is primary; Mattermost gets a one-line teaser pointing at the draft.

### Step 6.1 — Create the EOD Gmail draft

Stage the payload via `python3 runner/maestro.py send-email --subject "…" --body "…"`. The runner reads the recipient from `config.json > email.recipient`, validates it, and emits the staged payload.

Then call your runtime's `gmail_create_draft` tool with the recipient/subject/body the runner returned — verbatim. The draft appears in the user's Gmail drafts; they review and Send themselves.

Defense in depth:
- The runner is the source of truth for the recipient. **Do not** pass a recipient yourself — copy it from the runner's staged output.
- No CC, no BCC, no other recipients. Ever.
- Subject: `[Heartbeat] EOD — <date> summary`
- Body: Combine a brief day recap (key outcomes, decisions, items resolved) with tomorrow's briefing. Include any suggestions, research, or drafted content accumulated during the day (see AGENTS.md for the EOD draft structure).
- On Fridays, include the Weekly Summary section AND the Agent Self-Assessment section (see AGENTS.md for metrics, thresholds, and format).
- The self-assessment should use `state.json` metrics (weekly counters) as the primary data source, supplemented by reviewing daily logs for detail. More reliable than counting from prose logs alone.
- Use plain text — no HTML.

### Step 6.2 — Post the EOD Mattermost teaser

Right after the draft is created, post one Mattermost line so the user knows to check drafts:

```bash
python3 runner/maestro.py mattermost --urgent "[EOD <YYYY-MM-DD>] Draft ready — top: <one-sentence headline finding from today>. <N> open items carried to tomorrow."
```

This is the only Mattermost output from the EOD run. The full review lives in the Gmail draft. On Fridays, the teaser can mention the weekly + self-assessment: `[EOD <date>] Draft ready — week wrap-up + self-assessment. Top: <headline>. <N> carried.`

### Step 6.3 — Graceful degradation

If `gmail_create_draft` fails (Gmail connector degraded, auth expired, MCP server unreachable), fall back to posting the EOD content as a multi-line Mattermost message:

- Invoke `python3 runner/maestro.py mattermost --urgent "<multi-line-condensed-summary>"` with a **single** multi-line argument. EOD as a fallback is one logical piece, not a sequence of one-liners.
- The first non-empty line should start with `[EOD <date>] DRAFT FAILED —`.
- Hard limit: ~1500 chars (Mattermost handles longer but mobile readability collapses).
- Sections to include if noteworthy: (a) what closed today, (b) what's still in flight, (c) tomorrow's top 3 priorities, (d) Friday-only: 1-line weekly takeaway.

If Mattermost is ALSO down, write the full content to `daily/YYYY-MM-DD.md` under a `### EOD content (undelivered)` section. The user can read it there once a channel recovers.

### Step 6.4 — Log delivery status to today's daily log

Append a `### Delivery` block recording: which channels were used (Gmail draft, Mattermost teaser, both), any failures, and whether degradation paths were taken. This makes the EOD self-assessment honest and lets the user see at a glance why a briefing arrived (or didn't).

## 7. Update state.json

After checking all data sources, update `state.json` with source health and metrics. Read the current file, then write it back:

- **Source health**: For each source checked successfully, set `sources.<name>.last_success` to the current ISO timestamp and `consecutive_failures` to `0`. For failures, set `last_failure` and increment `consecutive_failures`.
- **Cached identifiers**: Store any successfully resolved Atlassian `cloudId` / `accountId` in `cached.*`.
- **Metrics**: Update `metrics.today.emails_sent` (increment by 1 for the EOD email), `metrics.today.web_searches`, `metrics.today.suggestions_made`. Also update corresponding `metrics.week.total_*` counters.
- **Metrics date rollover**: If `metrics.today.date` != today, reset daily counters. If `metrics.week.week_start` != current Monday, reset weekly counters.
- **Do not modify** `last_run.*` fields — those are managed by `run.sh`.
