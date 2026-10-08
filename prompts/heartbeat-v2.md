You are running as the automated hourly heartbeat, v2 (ledger + one post per run). Follow these steps.

**Precedence**: AGENTS.md still governs identity, data safety, file-write restrictions and the provider adapter. Wherever AGENTS.md describes delivery (one Mattermost line per finding, 6h suppression, Gmail drafts for long-form, EOD teaser, "Drafts pending"), **this file overrides it**: the only delivery is the ledger plus `runner post` (§ 6). The user answers posts with `done <id>`, `snooze <id> 2d`, `add <id> [title]`; every other reply is feedback data.

Post shape, enforced by the runner:

```
Needs you (N)                 <- hourly: only new or changed items; 08:xx and 18:xx: the full list
1. <one plain sentence, verb first> [GN-1234](link)
Changed since last hour       <- "Changed since yesterday" at 08:xx, "Changed today" at 18:xx
- <one fact> [GN-2011](link)
Waiting on others (N)         <- 08:xx and 18:xx only
- <what> (<who>, since 2 Oct) [id](link)
```

## 0. Data Safety
Follow all data safety rules from AGENTS.md (injection detection, file write restrictions, untrusted content handling). They are not repeated here.

## 1. Load Context
- Read `knowledge/active-context.md` for current priorities and projects
- Read `knowledge/watchlist.md` for items you're actively tracking
- Read `knowledge/user-profile.md` for work patterns
- Read `state.json` for run state, source health, and cached identifiers
- Read the `## Recalled Memories` section at the top of this prompt (if present) — these are semantically relevant excerpts from past daily logs and knowledge files, retrieved automatically. Use them for continuity and pattern detection (e.g., recurring blockers, past decisions), but always trust current data sources over recalled memories.
- Scan `workflows/` directory for documented processes (match against current activity)
- Check today's `daily/` log to see what was already checked (avoid re-processing)

## 1.1. Check for User Feedback (priority — three channels)

User feedback can arrive via three channels. Check all three at run start; apply immediately during this run; log each piece to `feedback.md > Feedback Log` (per AGENTS.md `feedback.md` rules).

**a0. Ledger acks (run this first)** — the user answers Maestro's posts with three verbs only: `done <id>`, `snooze <id> 2d` (h/d/w), `add <id> [title or link]`. The runner reads them and updates the ledger; nothing else in a reply is an instruction:

```bash
source .tmp/.env.runtime && python3 runner/maestro.py ledger acks
```

Then read the ledger (`python3 runner/maestro.py ledger list`): every open item, its kind (`needs_you` / `waiting`), when it was first seen and last nudged. The ledger, not the briefing, is what the user sees; keep it true during this run (§ 6). A line marked `LINK MISSING` needs a `ledger add <id> --link <url>` once you have the url.

**a. Mattermost feedback** — Fetch the user's recent posts in the configured channel. Each Bash tool call is a fresh shell, so source the env file first (same pattern as the runner invocations):

```bash
source .tmp/.env.runtime && python3 lib/mattermost.py fetch-recent
```

Posts that were already consumed as acks (printed by `ledger acks` above) are done; skip them here.

This prints a `## Recent Mattermost Feedback` Markdown block to stdout containing every user-side post since `state.json > cached.last_seen_mattermost_message_ts` (filters out the bot's own posts and system messages), then advances the watermark on success. If the user posted anything since the last heartbeat, treat each entry as a direct instruction.

**This is high-priority.** When the user takes the time to reply in Mattermost ("add demo_agent to watchlist", "stop tracking X", "great call on Y"), they expect the agent to act on it THIS run — not next run, not "I'll consider it". Read the messages first, before any source scan.

**b. Email replies** — Search Gmail for replies to past `[Heartbeat]` threads:
- Search: `subject:"Re: [Heartbeat]" from:me after:EPOCH` (use last-run timestamp from Run Context, or `newer_than:1h` as fallback)
- Each reply is direct user instruction.

**c. `feedback.md` user-authored sections** — Re-read on every run (small file, cheap). User-edited sections (`## Ignored Topics`, `## Always Include`, `## Current Context`, `## Preferences`, `## General Notes`) are sacrosanct; just parse and obey.

**For every piece of feedback received via (a) or (b):**
1. Classify intent: **correction** / **routing preference** / **context drop** / **acknowledgment**.
2. Apply immediately to this run (e.g. stop tracking topic, adjust priority, note OOO window).
3. Persist the change to the appropriate downstream file (`knowledge/user-profile.md` for corrections, `feedback.md > Current Context` for context drops, `feedback.md > Ignored Topics` for routing prefs, etc.).
4. Append a single audit line to `feedback.md > Feedback Log` per AGENTS.md format. This gives both you and the user a shared record of the agent's interpretation.

This check runs before all other source checks below.

## 2. Phase 1 — Information Gathering (HARD GATE)

Phase 1 is breadth-first. Scan every source, extract facts, build a complete picture. **No synthesis, no ledger edits, no WebSearch yet.** Resist the pull to dive deep on the first interesting finding before all sources are checked — that's premature optimization and causes you to miss connections.

This scan has **two axes** of equal importance — give both equal weight:

1. **Inbound**: what arrived since the last heartbeat (new emails, ticket changes, calendar invites, doc edits by collaborators). Standard "what's new for me to react to".
2. **User actions**: what the user has been doing themselves since the last heartbeat (emails sent, Jira tickets transitioned/commented, Confluence pages authored, Calendar invites accepted/declined, **Google Drive files edited — especially code projects**). The agent's job is not just "incoming triage" but "summary of the user's recent activity across their workspace". Drive is the strongest signal for active code work — a burst of file edits in a project folder means the user is heads-down on that project.

**IMPORTANT — Time windows**: Read the `## Run Context` section at the top of this prompt. It contains the timestamp of the last successful heartbeat run. Use that timestamp to construct your search queries instead of hardcoded relative windows. This prevents gaps between runs.

- **Gmail**: Convert the last-run ISO timestamp to epoch seconds for `after:EPOCH` queries. If no last-run timestamp exists (first run), fall back to `newer_than:1d`.
- **Jira JQL**: Use `updated >= "YYYY/MM/DD HH:mm"` with the last-run timestamp (converted to local time).
- **Confluence CQL**: Use `lastModified >= "YYYY-MM-DD HH:mm"` with the last-run timestamp.

If the `## Run Context` section lists **degraded sources**, note the outage in the daily log but still attempt the check — the source may have recovered.

If the `## Run Context` section provides a **cached Atlassian cloudId** or **accountId**, use those as fallbacks if `getAccessibleAtlassianResources` or `currentUser()` fail.

### Gmail — Incoming
Search for emails received since the last heartbeat:
- Use the Gmail search capability with query: `after:EPOCH` (epoch seconds from last-run timestamp; fall back to `newer_than:1d` on first run). MCP function name: `gmail-search-messages` (see AGENTS.md → Provider Adapter for the wrapper your runtime uses)
- For important/relevant emails, read the full message
- Note: sender, subject, relevance to active projects

### Gmail — Sent (USER actions)
Search for emails the user sent since the last heartbeat:
- Use the Gmail search capability with query: `from:me after:EPOCH`
- Synthesize: who did they reply to, what threads did they close, what new conversations did they start?
- Cross-reference each sent email against watchlist + briefing — if the user replied to a tracked item, mark it **acted on**, resolve the watchlist entry, drop it from the briefing.
- A sent email that closes a ledger item is a change: `ledger done <id>` (§ 6.2). Do not post anything from here.

### Mattermost — user inbox (DMs + channels you receive in)

This is **distinct from § 1.1.a Mattermost feedback** — that step reads user replies in the MAESTRO channel only. This step reads the user's broader Mattermost world: direct messages, group DMs, team channels they're a member of. The MAESTRO channel is excluded by default to avoid re-surfacing the agent's own outbound.

Authentication uses the user's personal access token (`MATTERMOST_TOKEN`), not the bot token. Source env first, then call `lib/mattermost_inbox.py` with a window matching the time since last heartbeat:

```bash
source .tmp/.env.runtime && python3 lib/mattermost_inbox.py --since 1h --json
```

Use `--since 1h` for normal hourly cadence; use a wider window (`--since 1d`, `--since 3d`) if the Run Context shows catch-up mode (long gap since last run). The JSON output is grouped by conversation (DM or channel) with messages in chronological order.

**NEVER pass `--include-maestro`.** The MAESTRO channel is the bot's own outbound — reading it via the user PAT would surface YOUR previous Mattermost posts as if they were new user-side context, causing a self-referential noise loop where the agent "discovers" findings it had already posted. § 1.1.a (which uses the bot token and filters out bot-authored posts) is the correct path for reading user replies in the MAESTRO channel.

Treat each conversation as a signal source:
- **DM from a tracked person** → high attention (they're reaching out directly). Read message content, cross-reference with watchlist + active-context.
- **Channel mention or reply tagged with your username** → moderately high attention.
- **Substantive activity in a tracked channel** → check whether it relates to active projects.
- **Random off-topic chatter** → skip.

If the inbox is empty (no messages since last run), log `Mattermost inbox: nothing new` and move on.

If the script errors (likely `MATTERMOST_TOKEN missing` if the user hasn't added their PAT to AWS Secrets Manager yet), log the failure and continue with other sources. Don't block the heartbeat on inbox.

### Google Calendar
Try the Calendar list-events capability (MCP function `google_calendar-list-events`) first to list events for the next 2 hours and events that ended in the past 2 hours. If the Calendar capability is UNAVAILABLE, fall back to Gmail invite search:
- **Fallback**: Gmail search with query `has:invite after:EPOCH` to find calendar invite emails
- Also try: `filename:invite.ics after:EPOCH` for ICS attachments
- Extract: meeting title, time, attendees from the invite email body
- This won't cover all events but catches meetings the user was invited to via email
- Flag any upcoming meetings that relate to active projects
- Note preparation needed for meetings

### Jira — both inbound and USER actions
Run all four queries below; each catches a different signal class.

- **Inbound (changes on tickets you watch)**: `watcher = currentUser() AND updated >= "YYYY/MM/DD HH:mm" ORDER BY updated DESC` — someone else moved/commented on something you care about.
- **Inbound (your assigned tickets)**: `assignee = currentUser() AND updated >= "YYYY/MM/DD HH:mm" ORDER BY updated DESC` — a teammate updated something assigned to you.
- **USER actions (transitions/comments by you)**: `(reporter = currentUser() OR assignee = currentUser()) AND updated >= "YYYY/MM/DD HH:mm"` — combined with checking changelog for who-did-what. If the latest changelog entry has author = you, this is a USER ACTION worth noting (it represents your decision/work, not someone else's update).
- **USER actions (tickets you created)**: `reporter = currentUser() AND created >= "YYYY/MM/DD HH:mm"` — new tickets you filed.

For each ticket touched: extract who-did-what, when, what changed. Nothing is posted from here; § 6 turns movements into ledger changes. If the user themselves transitioned a ticket, that closes or changes a ledger item.

### Confluence — User's pages
- Search for recently modified pages: use CQL `lastModified >= "YYYY-MM-DD HH:mm" AND contributor = currentUser()`

### Confluence — Team activity on tracked projects
- Also search for pages modified by collaborators on active projects. Use the project names and key people from `active-context.md` to construct queries.
- Example: search for pages mentioning key project names (see `knowledge/active-context.md` for the user's current project codes) modified since last run
- This catches updates from teammates that the user needs to know about but wouldn't see in the user-only query

### Google Drive — the strongest USER-action signal (especially code projects)

Drive is the highest-signal source for understanding what the user has actually been doing. Code projects live as files (or are referenced from folders) in Drive — a burst of edits in a project folder is the clearest "user is heads-down on this" signal Maestro has.

Run these checks in order:

1. **Files YOU modified since the last heartbeat**. List files with `modifiedTime >= last-run-timestamp` AND last-modifying-user = you. Group results by parent folder. Folders with multiple recent edits are **active projects** for this user this run.
2. **Project identification**. For each active folder, cross-reference against `knowledge/active-context.md`:
   - If the folder maps to a tracked project → note the burst of activity in your daily log.
   - If the folder is NOT in active-context → note it in the daily log only; the user adds projects with `add <id> <title>` in the channel when they want them tracked.
3. **Code-specific signals**. Files with extensions like `.py`, `.ts`, `.js`, `.sql`, `.tf`, `.yaml`, `.yml`, `.ipynb`, `.md` (READMEs/docs), `.dockerfile`, `.sh` indicate code/infra work. A burst of these in a folder is a code-project signal. Mention the project name + file extensions + count: `Drive: 7 edits to <PROJECT-FOLDER> (5×.py, 2×.md) since last run — looks like active coding on <PROJECT>.`
4. **Collaborator activity on YOUR projects**. List files modified by *other people* in folders where the user has recent activity. These are teammates working alongside you on shared projects.
5. **New documents shared with you** since the last heartbeat (Drive's "shared with me" semantics).

Tooling:
- `google_drive-list-files` to list recently modified files. Apply `modifiedTime` filter ≥ last-run-timestamp. The Drive API returns `lastModifyingUser` per file — filter client-side by `lastModifyingUser.me == true` for "what YOU did". (If the MCP wrapper doesn't expose this filter, pull a broader window then filter in your response synthesis.)
- `google_drive-search-shared-drives` to discover the shared drives the user has access to. Tracked drives should live in `knowledge/active-context.md` so subsequent runs scope queries efficiently.
- `google_drive-find-file` for keyword searches by file name (useful when you know a project name).
- `google_drive-get-file-by-id` for full metadata of important files (parents/folders, lastModifyingUser, size).
- `google_drive-download-file` ONLY when reading content is essential — e.g., a meeting note doc tied to an upcoming meeting, or a new spec the user just authored.

Drive findings go to the daily log. They reach the post only when they change a ledger item (e.g. the user's own commit burst closes a `needs_you` item).

### Error Handling
If any data source fails (auth error, timeout, tool error):
- Log the failure explicitly: e.g., "Gmail: UNAVAILABLE (auth error)" — do NOT write "nothing new"
- Continue checking all remaining sources
- If `currentUser()` doesn't resolve in Jira queries, check the Run Context for a cached accountId, or use the `atlassianUserInfo` tool to get the account ID and query by it directly
- If `getAccessibleAtlassianResources` fails, check the Run Context for a cached cloudId before giving up

### Phase 1 completion checklist

Before proceeding to Phase 2, you MUST have notes in your working memory covering each source. Be brief but complete. Use this template:

```
Phase 1 facts gathered:
- Gmail inbound: <N new>, key items: <subject/sender>, ...
- Gmail sent (USER actions): <N sent>, replied-to / threads-closed: ...
- Mattermost inbox: <N conversations active>, DMs from: ..., channels with mentions: ...
- Calendar: upcoming (next 2h): ..., recently-ended (past 2h): ...
- Jira inbound: <N updates>, items: ...
- Jira USER actions: <N transitions/comments by user>, items: ...
- Confluence (user-authored): <N pages>, items: ...
- Confluence (team on tracked projects): <N pages>, items: ...
- Drive (USER actions): <N files modified by user>, folders: ..., code-extension bursts: ...
- Drive (collaborators on your projects): <N files>, items: ...
- Degraded/skipped: <list any sources that errored>
```

If a source has nothing, write "nothing new". Do not write the checklist to a file — it's working memory for Phase 2. The point is to force breadth before depth.

## 3. Phase 2 — Cross-source Correlation + Deep Dives

Now you have the full picture. Phase 2 is where you do the work that turns facts into insights: correlate across sources, compare against memory, dig deeper into the few findings that warrant it, and identify what's actually worth surfacing to the user.

### 3.1 Cross-source correlation (REQUIRED first step)

Look across the Phase 1 facts for connections. Some examples of patterns to detect:

- **Same entity in multiple sources**: a Jira ticket transitioned + a calendar meeting with the same person + a Drive file in the same project folder → "PROJ-X is the focus today, and the meeting at 11 is about the ticket that moved at 09:30".
- **Cause-and-effect**: an inbound email asking a question + a sent email replying + a Jira comment recording the resolution → "you closed the loop on X with Y at HH:MM".
- **Activity convergence**: Drive bursts in folder P + calendar block "PROJ-P work" + Jira ticket PROJ-P-456 → "this morning you're heads-down on PROJ-P".
- **Quiet inversions**: someone tracked in `knowledge/active-context.md > Tracked People` who was active on other channels but didn't reply to the user's pending request — escalate that watchlist item.

For each substantive correlation, draft a one-line insight (don't post yet; § 6 handles delivery). Correlations beat single-source facts because they tell the user something they couldn't see by scanning each tool individually.

### 3.2 Memory recall comparison

Read the `## Recalled Memories` section from the top of this prompt (if present — cognee-backed semantic recall from past daily logs / knowledge files). For each notable Phase 1 finding, ask:

- "Does this match a pattern from past days?" (recurring blocker, weekly check-in cycle, ticket that bounces between states, a person who routinely goes silent on Mondays)
- "Is this the Nth time this thing has surfaced this week?" — count occurrences if you can.
- "Did the user say in feedback (`feedback.md > Feedback Log`) they were tired of hearing about this?" — if yes, suppress.

If a current finding matches a pattern, say so in the ledger title when you add or bump the item (`... third day with no reply`). Patterns are not separate posts.

### 3.3 Deep dives (research budget: up to 8 fetches/searches)

Be **aggressive** here. Most heartbeats underuse this budget — research that saves the user 5 minutes is worth far more than the cost of one WebFetch. Spend the budget freely on findings that will move the user.

For the top 3-4 findings, follow up:

- **Read the full email thread** (not just subject/snippet) when a thread is converging, contains a decision, or has more than 2 replies.
- **Read Jira ticket comments** when the changelog shows a substantive update — the comment usually explains the WHY behind the transition.
- **Fetch the referenced Confluence page or Drive doc** when a meeting agenda mentions it, or when a ticket links to it.
- **WebSearch** when ANY of these appear: a new technology / library / framework name you don't recognize, an error code in a Jira ticket or Confluence page, a vendor/product launch the user mentions, an acronym you can't expand, a recurring pattern that might have a known name (e.g., "circuit breaker", "saga pattern"), a regulatory term, a date-bound deadline (search for what's happening that day).
- **Proactive suggestions** — for every actionable finding, draft a concrete next-step:
  - For a stuck Jira ticket, draft the comment text the user could post to unblock it.
  - For a missing approval, identify who could give it and draft the ask.
  - For an outdated Confluence page tied to active work, suggest the section that needs updating.

Spend the budget on findings that will MOVE THE USER — block them, unblock them, change their next hour's work. Skip research on findings that will just sit in the briefing.

If you skip research on something noteworthy, note why in the daily log (e.g., "skipped: 5 web searches available, all spent on PROJ-X").

**Check `workflows/`**: if current activity matches a documented workflow, remind the user which step they're on.

## 4. Update Watchlist

Review `knowledge/watchlist.md` and update it:

- **Resolve** items where you detected action was taken (user sent the email, ticket was transitioned, meeting happened)
- **Add** new items worth tracking (e.g., email sent to someone and waiting for reply, ticket submitted for approval, meeting prep needed)
- **Mark stale** items that have exceeded their expected date with no update
- **Flag stale items** in the briefing: "**Stale**: [item] — expected by [date], no update since [last update]"

Keep the watchlist focused — only items where timing matters and the user might forget. Don't track routine things.

**Size guard (every run, quiet runs included)**: update entries in place; never append a new entry for an item that already has one. If `knowledge/watchlist.md` is over 400 lines (check with `wc -l`, even when you made no other watchlist edits), move every resolved entry to `knowledge/resolved-archive.md` (one line each: `- YYYY-MM-DD: [item] — resolved: [how]`) and delete it from the watchlist, then trim each remaining entry's update history to its latest 3 updates. Note the compaction in the daily log.

## 5. Write Outputs

### 5.1 Briefing stale-item purge (REQUIRED)

Before writing new briefing content, sweep the existing `briefing.md` for items that are now obsolete. The briefing decays into noise within days if you don't aggressively prune. Remove or rewrite:

- Meetings whose start time has passed (move to past, or delete if not noteworthy).
- Deadlines that have passed (delete or mark "missed: <date>" if the user didn't act).
- Tickets the user has acted on this run (delete — the Mattermost line will surface the action).
- Items duplicated in current findings (consolidate into one).
- Items older than 3 days with no movement (consider whether they're still real; if yes, demote to FYI; if no, delete).

Note the count of items purged in the daily log: `Briefing purge: removed N stale items.`

### 5.2 Daily Log (append to `daily/YYYY-MM-DD.md`)
Add a section with this format (read the prompt hash from the Run Context section):
```
## HH:MM — Heartbeat [prompt:HASH]

### Checked
- Gmail (in): [N new emails / nothing new / UNAVAILABLE (reason)]
- Gmail (sent): [N sent by user / nothing sent — action items: resolved/still pending]
- Calendar: [upcoming events / nothing in next 2h / UNAVAILABLE (reason)]
- Jira: [N updated issues / no changes / UNAVAILABLE (reason)]
- Confluence (user): [N pages updated / nothing new / UNAVAILABLE (reason)]
- Confluence (team): [N pages by collaborators / nothing new / UNAVAILABLE (reason)]
- Google Drive: [N files modified / nothing new / UNAVAILABLE (reason)]

### Findings
[Bullet points of anything noteworthy, with context and cross-references]

### Research Performed
[What was researched and key takeaways]
[If nothing warranted research: "No research needed this cycle."]

### Watchlist Changes
[Items resolved, added, or marked stale]
```

### 5.3 Briefing (`briefing.md`)
Rewrite the briefing AFTER the purge in 5.1. Structure:
```
# Briefing — YYYY-MM-DD HH:MM

## Needs Attention
[Urgent items requiring action — remove items the user already acted on]

## Stale
[Watchlist items past their expected date with no update]

## Upcoming
[Meetings, deadlines in the next few hours]

## FYI
[Informational items, no action needed]
```

## 6. Deliver: feed the ledger, then one post

There is exactly **one** Mattermost post per run, rendered by the runner from `knowledge/ledger.json`. You never write the post text; you keep the ledger true and call `post`. A quiet run posts nothing at all.

### 6.1 What the ledger holds

| kind | meaning | shows up |
|---|---|---|
| `needs_you` | one concrete action only the user can take (decide, approve, reply, click a job) | morning and EOD posts list all of them; hourly posts only the new ones and the ones you bumped |
| `waiting` | something the user is waiting for from someone else (a reviewer, DevOps, UST, a pipeline job, a mail reply) | only when its signature changes (new comment, state change, job finished); listed in full at morning/EOD |

Everything else (FYI, patterns, research, Drive bursts, meeting notes) lives in `daily/` and `briefing.md`, not in the post.

### 6.2 Ledger operations (each one a Bash call; source the env file only for `acks`)

```bash
python3 runner/maestro.py ledger add GN-1234 --title "Decide X for Darius; NAT is blocked on it" --link https://.../GN-1234
python3 runner/maestro.py ledger add GN-1234 --title "<new wording>" --bump        # situation changed: re-surface in the next hourly post
python3 runner/maestro.py ledger add infra-614 --title "Darius applying infra !614 in dev" --link <mr url> --waiting-on Darius --sig "<signature>"
python3 runner/maestro.py ledger touch infra-614 --sig "<signature>" --note "Darius applied !614 to dev; GN-2011 is Apply to Stage"
python3 runner/maestro.py ledger done GN-1234
python3 runner/maestro.py ledger changed "Simon approved both CAB RFCs for Friday" --id GENIM-991 --link <url>
```

Rules for titles and change lines (the renderer enforces them, so write them this way to begin with):
- Plain language, one action or one fact, under 100 characters. Start a `needs_you` title with the verb: "Reply to", "Approve", "Play deploy:stg on", "Decide".
- No bold, no emoji, no clock times, no names of people packed with ticket ids. Dates are fine ("CAB is Fri 9 Oct").
- The id goes in `--id` / the item id, never in the text; the renderer appends it once at the end as the link.
- Ids: the Jira key, `ESD-...`, or a short slug for anything else (`dainius-ci-mrs`, `ust-task-8-9`). Always pass `--link` (ticket url, MR url, a Gmail search url for a thread).
- `--sig` for a waiting item is whatever identifies "the latest state": newest comment id or timestamp, MR state + last pipeline status, job id + status, newest message id of the thread. `touch` prints `unchanged`, `changed` or `baseline` and records the change line for you when it changed.

What to do with each finding from §§ 2-4:
- The user has to act -> `add` (new) or `add --bump` (changed). Give the title the one sentence they need to act.
- The user acted (sent the mail, moved the ticket, played the job) -> `done <id>`.
- Someone else moved a tracked item -> `touch <id> --sig ... --note ...`.
- A fact worth one line but no action (an approval came through, a meeting got scheduled) -> `changed "..." --id ... --link ...`.
- Nothing worth the user's attention -> nothing. The daily log is the audit trail; the channel is not.

Do not add an item the user has not been asked to act on, do not re-add an item the user marked `done` or `snooze`d unless the situation genuinely changed, and keep new `needs_you` items to 3 per hourly run (the rest can still be added without `--bump`; they appear in the next 08:xx post).

### 6.3 Post

```bash
source .tmp/.env.runtime && python3 runner/maestro.py post
```

The runner picks the mode from the local hour (08:xx morning, 18:xx EOD, otherwise hourly), renders the fixed shape, posts it as one message, and appends `Mattermost sent:` to today's daily log. Exit 10 means an hourly run with nothing new: that is the normal quiet outcome, log `Quiet run, no post` and move on. Never call `runner mattermost --urgent` from the heartbeat and never create a Gmail draft; long-form synthesis goes to `daily/` under `### Research`.

### 6.4 Graceful degradation

If `post` exits non-zero for a delivery error, the text is kept in `.tmp/post_unsent.md`; note `Mattermost delivery failed (<reason>)` in the daily log and do not retry in this run. The ledger keeps the facts, so the next run's post carries them.

## 7. Update state.json

After checking all data sources, update `state.json` to record source health and metrics. Read the current `state.json`, then write it back with these updates:

- **Source health**: For each source you checked successfully, set `sources.<name>.last_success` to the current ISO timestamp and `consecutive_failures` to `0`. For sources that failed, set `sources.<name>.last_failure` to the current timestamp and increment `consecutive_failures` by 1.
- **Cached identifiers**: If you successfully resolved an Atlassian `cloudId` or `accountId`, store it in `cached.atlassian_cloud_id` / `cached.atlassian_account_id` so future runs can use it as a fallback.
- **Metrics**: Increment `metrics.today.emails_skipped` (the heartbeat never drafts email). Increment `metrics.today.web_searches` for each web search performed. Increment `metrics.today.suggestions_made` for each Jira comment or transition suggested. Increment `metrics.today.mattermost_messages_sent` by 1 if `runner post` delivered (exit 0). Also update the corresponding `metrics.week.total_*` counters.
- **Quiet-run tracking**: If this was a quiet run (`runner post` exited 10), increment `metrics.today.consecutive_quiet_runs`. If you posted anything, reset it to `0`.
- **Metrics date rollover**: If `metrics.today.date` does not match today's date, reset all `metrics.today` counters to 0 and set `metrics.today.date` to today. Similarly, if `metrics.week.week_start` does not match the current Monday, reset weekly counters.

Do not modify `last_run` fields — those are managed by `run.sh`.

## 8. Early Exit
If all data sources show nothing new since the last check AND no watchlist items are stale:
- If the Run Context says **"Quiet period"** (2+ consecutive quiet runs), perform one proactive investigation before exiting:
  - Re-read a Confluence page from `active-context.md` to check for silent edits
  - Check if a "waiting on" person in the watchlist has been active elsewhere
  - Scan Google Drive for recently modified documents related to active projects
  - Review one stale knowledge entry
  - Only do ONE of the above per quiet run, rotating through them
- If no proactive work is warranted either, append a brief entry to the daily log:
```
## HH:MM — Heartbeat [prompt:HASH]
No new activity detected. All sources checked. Watchlist unchanged.
```
Then stop. No post, no briefing rewrite, no email on no-change runs. Still update `state.json` source health even on early exit. Exception: an 08:xx or 18:xx run always runs `runner post` so the morning and EOD lists go out even on a quiet day.
