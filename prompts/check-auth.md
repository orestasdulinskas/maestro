# Auth/Source Probe (read-only diagnostic)

You are running a diagnostic probe of every data source Maestro depends on. **Do not write to any file. Do not send any email. Do not modify state.json.** Output a single dashboard at the end and stop.

## Probes to run

For each source below, make the listed cheapest-possible call. If the call succeeds with sensible data, mark it `✓ healthy`. If it fails (auth error, connect URL, permission denied, timeout, empty payload where data was expected), mark it `✗` with a one-line reason.

Tool names are MCP function names. Your runtime wraps them with a prefix (see AGENTS.md → Provider Adapter): Claude Code = `mcp__<server>__<fn>`, Codex = `<server>.<fn>`, opencode = `<server>:<fn>`.

| Source | MCP function | Server | What success looks like |
|---|---|---|---|
| Gmail (read) | `gmail-get-profile` | Pipedream (or claude.ai Gmail connector on Claude Code) | Returns an `emailAddress` field |
| Calendar | `google_calendar-list-calendars` | Pipedream | Returns at least 1 calendar |
| Jira | `getAccessibleAtlassianResources` | Atlassian | Returns at least 1 cloudId |
| Confluence | `getConfluenceSpaces` (limit 1, cloudId from prior call) | Atlassian | Returns at least 1 space |
| Drive | `google_drive-list-files` (pageSize=1) | Pipedream | Returns a file list (not a Connect URL or auth error) |
| Runner | `python3 runner/maestro.py auth` (shell, not MCP) | local | Prints "config.json: present" + state backend status |

For **Gmail (send)**, do NOT call `gmail-send-email` — sending a probe email would spam the user. Instead, read `state.json > sources.gmail_in` and `gmail_sent` and report the timestamp of `last_success` and `last_failure`. Format: `last sent: <ISO timestamp>, last failure: <ISO timestamp>`.

## Output format

Print exactly this format and nothing else (no preamble, no analysis, no suggestions):

```
=== Maestro Auth Check ===
Time: <ISO timestamp>

Gmail (read)       <✓ healthy | ✗ <reason>>
Gmail (send)       last_success=<timestamp or "never">, last_failure=<timestamp or "never">
Calendar           <✓ healthy | ✗ <reason>>
Jira               <✓ healthy | ✗ <reason>>
Confluence         <✓ healthy | ✗ <reason>>
Drive              <✓ healthy | ✗ <reason>>
Runner             <✓ healthy | ✗ <reason>>

Summary: <N>/6 sources healthy
```

(Gmail send is not in the 6-source count — it's informational because we can't probe it without actually sending.)

## Constraints

- Read-only. No Write/Edit. No state.json mutation. No email send.
- If a tool is denied or unavailable, mark `✗ tool unavailable` rather than failing.
- Keep each reason under ~60 chars.
- Total runtime budget: 60 seconds. If a probe hangs, abandon it and mark `✗ timeout`.
