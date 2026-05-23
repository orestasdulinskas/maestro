# Maestro Fixtures

Regression-test scenarios for the Maestro agent. Run with:

```
./run.sh --replay <scenario-name>
```

Each fixture is a frozen input snapshot + a set of structural assertions that
codify what "correct" output looks like for that scenario. They run the actual
agent in a sandboxed REPLAY MODE that forbids real tool calls — the agent
reasons over pre-recorded "tool responses" supplied in the fixture.

## Layout

```
fixtures/
  <scenario-name>/
    INPUT/                       # Frozen inputs the agent sees
      description.md             # Human-readable scenario description (NOT shown to agent as task)
      state.json                 # state.json snapshot
      briefing.md                # The previous briefing
      knowledge/                 # Knowledge files (active-context, watchlist, etc.)
      mocked-tools.md            # Pre-recorded tool responses
    EXPECTED/
      assertions.py              # Structural checks (functions named `assert_*`)
    RUNS/                        # Auto-created. Outputs from each replay invocation.
      YYYYMMDDTHHMMSSZ/
        prompt.txt               # The full prompt sent to claude
        claude-output.json       # Raw `claude --print` output
        claude-stderr.log
        parsed-output.json       # Agent's JSON response (the thing assertions test)
```

## Adding fixtures

### Synthetic (fast, low-fidelity)

1. `cp -r fixtures/scenario-01-quiet-friday fixtures/scenario-NN-<short-name>`
2. Edit `INPUT/description.md` to describe the scenario.
3. Edit `INPUT/state.json` and `INPUT/mocked-tools.md` to define the inputs.
4. Replace `EXPECTED/assertions.py` with checks that match what "good" looks like.
5. Run `./run.sh --replay scenario-NN-<short-name>` and iterate.

Use synthetic fixtures for: edge cases you've seen but want to regression-test,
hypothetical bug scenarios, scenarios you cannot capture from production safely.

### Real-anonymized (high-fidelity)

Use these for the "spine" of the test suite — they catch real-world weirdness
that you would never invent. The recipe:

1. **Pick a day.** Look at recent `daily/YYYY-MM-DD.md` files for an interesting day —
   a quiet day, a busy day, a day with a prompt-injection attempt, a day with a
   meeting that triggered post-meeting synthesis.
2. **Capture inputs.** Copy the *previous* day's EOD-time `state.json`, `briefing.md`,
   `knowledge/*.md` into `INPUT/`. (You want the state as it was AT THE START of the
   replayed day.)
3. **Reconstruct mocked tool responses.** Read the daily log for that day to figure out
   what each MCP tool returned. Write them into `mocked-tools.md` in the same shape as
   the synthetic fixture's mocks.
4. **Anonymize.** Replace:
   - **Person names** → keep first names but generic surnames, or use 1-letter codes
     (e.g. "Marius" → "Marius J." or "M.")
   - **Ticket IDs** → keep prefix but replace numbers (e.g. `GENIM-456` → `PROJ-456`)
   - **Email addresses** → use `<role>@example.com` (e.g. `pm@example.com`)
   - **Confluence/Jira URLs** → strip cloudId and use placeholder host
   - **Company-specific project names** → generic substitutes (e.g. "BQMQ" → "Project Q")
   - **API keys, tokens, account IDs** → `FIXTURE-XXX` placeholders
5. **Sanity check.** `git grep` your fixture for: company name, real email domain,
   real Atlassian cloud ID. If anything matches, scrub it.
6. **Write assertions.** Capture what the agent *actually did well* on that day as
   passing assertions. Capture what it did *poorly* as failing assertions you want
   the next code change to fix.

### Tip: capture-once, replay-forever

When you find a scenario the agent handled well, anonymize it and freeze it as a
fixture. Now any future change has to keep handling it well. When the agent fails
on something, capture *that* day too — the failing assertions become a regression
target.

## Writing assertions

See `scenario-01-quiet-friday/EXPECTED/assertions.py` for examples.

Principles:
- **Assert structural facts, not exact strings.** Models phrase things differently
  on different runs. Check that "drive" and "re-auth" appear, not specific wording.
- **Assert presence AND absence.** "X must mention Y" is half the picture; "X must NOT
  mention Z" (e.g. "agent should not retry the disabled Drive source") is equally
  important.
- **One assertion per fact.** Many small assertions surface what specifically broke
  better than one mega-assertion.
- **Keep assertions independent.** Each assertion should pass or fail on its own.

Each assertion is a function `assert_<name>(out: dict) -> tuple[bool, str]` where
`out` has keys `email`, `email_skip_reason`, `daily_log`, `watchlist`, `briefing`,
`suggestions`, `alerts`. See `lib/dryrun.py` for the exact contract.

## Iteration loop

When developing assertions or scenarios:

```
./run.sh --replay <name>           # First run: invokes claude (~1-3 min)
./run.sh --replay <name> --no-run  # Subsequent runs: re-uses cached output, just
                                   # re-runs assertions. Fast iteration on assertion
                                   # logic without burning model tokens.
```

## What replay mode does NOT test

- **Real MCP integration** — replay mocks all tool responses. If a Pipedream tool's
  response shape changes, replay won't catch it. (Liveness ping + production runs do.)
- **Hooks and write restrictions** — replay disables file writes; the
  `gmail-send-email`-recipient hook isn't exercised.
- **Scheduling, locking, retries** — `run.sh`'s wrapper logic is bypassed.
- **Memory indexing churn** — replay does not invoke the memory indexer.

These are integration concerns. Replay tests the *agent's reasoning*, which is
where most prompt + state.py + memory.py changes can introduce regressions.
