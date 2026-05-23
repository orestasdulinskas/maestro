#!/usr/bin/env python3
"""Maestro fixture replay harness.

Loads a fixture from `fixtures/<name>/INPUT/`, builds a self-contained test prompt
that explicitly forbids real tool calls, runs `claude --print` against it, parses
the agent's structured JSON output, then runs `assertions.py` from the fixture's
EXPECTED/ directory.

Usage:
    python3 lib/dryrun.py <fixture_name> [--no-run]

Modes:
    Default: invokes claude, captures output to fixtures/<name>/RUNS/<timestamp>/,
             runs assertions, prints PASS/FAIL summary.
    --no-run: skips claude invocation, runs assertions against the most recent
              captured output (useful for iterating on assertions).
"""

import importlib.util
import io
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Force UTF-8 stdout/stderr (Windows safety)
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
if sys.stderr.encoding and sys.stderr.encoding.lower() != "utf-8":
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

SCRIPT_DIR = Path(__file__).resolve().parent.parent
FIXTURES_DIR = SCRIPT_DIR / "fixtures"


def build_test_prompt(fixture_input: Path) -> str:
    """Compose a self-contained replay prompt from fixture inputs.

    Why a custom prompt and not heartbeat.md verbatim: the production prompt tells
    the agent to call MCP tools. In replay mode, we forbid tool calls and provide
    pre-recorded responses. So we wrap the inputs and instructions explicitly.
    """
    state = (fixture_input / "state.json").read_text(encoding="utf-8")
    briefing = (fixture_input / "briefing.md").read_text(encoding="utf-8")
    mocked = (fixture_input / "mocked-tools.md").read_text(encoding="utf-8")
    description = (fixture_input / "description.md").read_text(encoding="utf-8")

    knowledge_dir = fixture_input / "knowledge"
    knowledge_blob = ""
    if knowledge_dir.is_dir():
        for kf in sorted(knowledge_dir.glob("*.md")):
            knowledge_blob += f"\n### `knowledge/{kf.name}`\n\n```markdown\n{kf.read_text(encoding='utf-8')}\n```\n"

    prompt = f"""# REPLAY MODE — Maestro Heartbeat Regression Test

You are running in **REPLAY MODE** for an automated regression test.

## Hard rules for this run

1. **Do not call any external tools.** No MCP, no Bash, no WebSearch, no WebFetch. The data you need is already provided below.
2. **Do not write any files.** Produce your output as JSON in your final response, nothing else.
3. **Use the mocked tool responses below as if they were real.** They are authoritative for this run.
4. **Honor the disabled-source rules.** Sources marked disabled in state.json must NOT appear in your output as if you checked them.
5. Output a single JSON object as your final response, with the exact keys specified in "Required Output" below.

## Fixture description (for your context, not for output)

{description}

## state.json (the agent's persistent state at run start)

```json
{state}
```

## briefing.md (the previous briefing the user has been reading)

```markdown
{briefing}
```

## Knowledge files
{knowledge_blob}

## Mocked Tool Responses (treat as real source data)

{mocked}

## Your task

Following the normal Maestro heartbeat reasoning (gather → analyze → synthesize → record & deliver), produce the output you WOULD have produced in this scenario. Apply all the rules from CLAUDE.md (degraded sources, status-change notifications, watchlist escalation, briefing under 60 lines, etc.) — but emit the result as a JSON object instead of taking actions.

## Required Output

Respond with ONLY a JSON object (no prose, no markdown fences) with these keys:

```
{{
  "email": "<full email body you would send to the user, or null if you would skip>",
  "email_skip_reason": "<short reason if email is null, else null>",
  "daily_log": "<the entry you would append to today's daily log>",
  "watchlist": "<the full new contents of knowledge/watchlist.md after your updates>",
  "briefing": "<the full new contents of briefing.md after your updates>",
  "suggestions": [
    {{"type": "jira_comment|jira_transition|meeting_notes|decision|research", "summary": "..."}}
  ],
  "alerts": [
    {{"source": "drive|calendar|...", "kind": "reauth|recovery|other", "summary": "..."}}
  ]
}}
```

`alerts` must include any source-status-change notifications you would emit.
`suggestions` is a list (possibly empty) of structured Jira/decision/research suggestions.
"""
    return prompt


def find_claude_bin():
    """Locate the claude CLI."""
    for candidate in ["claude", os.path.expanduser("~/.local/bin/claude"), os.path.expanduser("~/bin/claude")]:
        result = subprocess.run(["which", candidate] if os.name != "nt" else ["where", candidate],
                                capture_output=True, text=True)
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip().splitlines()[0]
    return None


def run_claude_replay(prompt: str, run_dir: Path, model: str = "") -> tuple[int, str, str]:
    """Invoke claude --print with the test prompt. Returns (exit_code, stdout, stderr)."""
    claude_bin = find_claude_bin()
    if not claude_bin:
        return 127, "", "claude binary not found in PATH"

    prompt_file = run_dir / "prompt.txt"
    prompt_file.write_text(prompt, encoding="utf-8")

    out_file = run_dir / "claude-output.json"

    cmd = [claude_bin, "--print", "--output-format", "json"]
    if model:
        cmd += ["--model", model]
    # No --mcp-config: replay mode forbids MCP tools, so we deliberately don't load any.

    env = os.environ.copy()
    env.pop("CLAUDECODE", None)
    env["PYTHONIOENCODING"] = "utf-8"

    with open(prompt_file, "r", encoding="utf-8") as stdin_f:
        proc = subprocess.run(cmd, stdin=stdin_f, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", env=env, cwd=str(SCRIPT_DIR))

    out_file.write_text(proc.stdout, encoding="utf-8")
    (run_dir / "claude-stderr.log").write_text(proc.stderr or "", encoding="utf-8")
    return proc.returncode, proc.stdout, proc.stderr


def parse_agent_output(claude_json_str: str) -> dict:
    """Extract the agent's JSON response from claude --print's wrapper JSON."""
    try:
        wrapper = json.loads(claude_json_str)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"claude wrapper JSON parse failed: {e}")

    # claude --print --output-format json wraps the result; the agent's text is in `result`
    body = wrapper.get("result") or wrapper.get("content") or ""
    if not body:
        raise RuntimeError(f"no result field in claude output. Keys: {list(wrapper.keys())}")

    # Strip code fences if the model wrapped its JSON
    body = body.strip()
    if body.startswith("```"):
        # remove first line and last ``` line
        lines = body.splitlines()
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        body = "\n".join(lines)

    try:
        return json.loads(body)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"agent output is not valid JSON: {e}\nFirst 500 chars: {body[:500]}")


def load_assertions_module(fixture_dir: Path):
    """Dynamically import EXPECTED/assertions.py from a fixture."""
    path = fixture_dir / "EXPECTED" / "assertions.py"
    if not path.is_file():
        raise FileNotFoundError(f"no assertions.py at {path}")
    spec = importlib.util.spec_from_file_location("fixture_assertions", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run_assertions(mod, agent_output: dict) -> tuple[int, int, list[str]]:
    """Run all `assert_*` callables. Returns (passed, total, messages)."""
    passed = 0
    messages = []
    names = [n for n in dir(mod) if n.startswith("assert_") and callable(getattr(mod, n))]
    for name in names:
        fn = getattr(mod, name)
        try:
            ok, msg = fn(agent_output)
            symbol = "PASS" if ok else "FAIL"
            if ok:
                passed += 1
            messages.append(f"  [{symbol}] {name}: {msg}")
        except NotImplementedError:
            messages.append(f"  [SKIP] {name}: not yet implemented")
        except Exception as e:
            messages.append(f"  [ERROR] {name}: {type(e).__name__}: {e}")
    return passed, len(names), messages


def latest_run_dir(fixture_dir: Path) -> Path | None:
    runs = fixture_dir / "RUNS"
    if not runs.is_dir():
        return None
    candidates = sorted([p for p in runs.iterdir() if p.is_dir()], reverse=True)
    return candidates[0] if candidates else None


def main(argv):
    if len(argv) < 2:
        print("Usage: python3 lib/dryrun.py <fixture_name> [--no-run]", file=sys.stderr)
        sys.exit(2)

    fixture_name = argv[1]
    no_run = "--no-run" in argv[2:]

    fixture_dir = FIXTURES_DIR / fixture_name
    fixture_input = fixture_dir / "INPUT"
    if not fixture_input.is_dir():
        print(f"ERROR: fixture not found: {fixture_input}", file=sys.stderr)
        sys.exit(2)

    print(f"=== Replay: {fixture_name} ===")

    if no_run:
        run_dir = latest_run_dir(fixture_dir)
        if not run_dir:
            print("ERROR: --no-run specified but no previous runs found.", file=sys.stderr)
            sys.exit(2)
        print(f"Using cached run: {run_dir.name}")
        claude_stdout = (run_dir / "claude-output.json").read_text(encoding="utf-8")
    else:
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        run_dir = fixture_dir / "RUNS" / ts
        run_dir.mkdir(parents=True, exist_ok=True)
        prompt = build_test_prompt(fixture_input)
        print(f"Invoking claude (this can take 1-3 minutes)...")
        t0 = time.time()
        rc, stdout, stderr = run_claude_replay(prompt, run_dir)
        elapsed = time.time() - t0
        print(f"claude exit={rc} in {elapsed:.0f}s. Output saved to {run_dir.relative_to(SCRIPT_DIR)}")
        if rc != 0:
            print(f"claude failed. stderr (first 500): {stderr[:500]}", file=sys.stderr)
            sys.exit(rc)
        claude_stdout = stdout

    try:
        agent_output = parse_agent_output(claude_stdout)
    except RuntimeError as e:
        print(f"FAIL: could not parse agent output: {e}", file=sys.stderr)
        sys.exit(3)

    (run_dir / "parsed-output.json").write_text(json.dumps(agent_output, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"\nRunning assertions from {fixture_dir.relative_to(SCRIPT_DIR)}/EXPECTED/assertions.py:")
    mod = load_assertions_module(fixture_dir)
    passed, total, messages = run_assertions(mod, agent_output)
    for m in messages:
        print(m)

    print(f"\n=== {passed}/{total} assertions passed ===")
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main(sys.argv)
