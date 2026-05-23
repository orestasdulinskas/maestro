#!/usr/bin/env python3
"""PreToolUse hook for Write/Edit: enforce the agent's write surface.

Reads the tool-call JSON from stdin (as Claude Code passes to PreToolUse hooks),
extracts the target file_path, and decides whether the write is allowed.

Decision tree:
  1. No file_path in input  → allow (not our concern).
  2. Path inside one of WRITABLE_PATHS (daily/, knowledge/, briefing.md,
     state.json, .tmp/)  → allow.
  3. Path matches one of PROTECTED_PATHS (CLAUDE.md, config.json, prompts/,
     run.sh, lib/, .claude/, .env*, .secrets/, mcp-servers.json)  → block.
  4. Path elsewhere inside the project root  → allow.
  5. Path outside the project root  → block.

Exit codes (Claude Code hook contract):
  0  Allowed; the tool call proceeds.
  2  Blocked; a message on stderr is shown to the agent and the call is denied.

Project root is resolved from $CLAUDE_PROJECT_DIR (Claude Code sets this) with a
fallback to the script's grandparent directory so this works during local
unit-style testing too.

This replaces the previous inline-Bash hook which depended on `jq` (often
missing) and on `realpath -m` path styles matching Git Bash MSYS style (they
don't, on Windows). Doing it in Python sidesteps both issues.
"""
import json
import os
import sys
from pathlib import Path


# Paths the agent is allowed to write to. Relative to project root. Use forward
# slashes; pathlib handles platform-specific separators.
WRITABLE_PREFIXES = (
    "daily/",
    "knowledge/",
    ".tmp/",
)
WRITABLE_FILES = (
    "briefing.md",
    "feedback.md",
    "state.json",
)
# Paths the agent is explicitly NOT allowed to write to. These would otherwise be
# allowed by "elsewhere inside project root" rule.
PROTECTED_PREFIXES = (
    ".claude/",
    "prompts/",
    "lib/",
    "providers/",
    "runner/",
    "mcp/",
    "scheduling/",
    "fixtures/",
    ".secrets/",
)
PROTECTED_FILES = (
    "AGENTS.md",
    "CLAUDE.md",
    "config.json",
    "config.example.json",
    "README.md",
    "ARCHITECTURE.md",
    "ROADMAP.md",
    "LICENSE",
    ".gitignore",
    # Legacy / quarantined; protect even though they've moved or are no longer
    # at the root, to refuse any rogue agent that tries to recreate them.
    "run.sh",
    "mcp-servers.json",
)
# .env, .env.local, .env.production, etc.
PROTECTED_GLOB_PREFIXES = (".env",)


def project_root() -> Path:
    env_root = os.environ.get("CLAUDE_PROJECT_DIR")
    if env_root:
        return Path(env_root).resolve()
    # Fallback: this script lives at <root>/providers/claude-code/hooks/check_write_path.py
    return Path(__file__).resolve().parents[3]


def relative_to_root(path: Path, root: Path) -> str | None:
    """Return path relative to root with forward slashes, or None if outside."""
    try:
        rel = path.resolve().relative_to(root)
    except ValueError:
        return None
    return rel.as_posix()


def decide(filepath: str, root: Path) -> tuple[int, str]:
    """Return (exit_code, message). exit_code 0 = allow, 2 = block."""
    if not filepath:
        return 0, ""

    abs_path = Path(filepath).resolve()
    rel = relative_to_root(abs_path, root)
    if rel is None:
        return 2, (
            f"BLOCKED: write to '{filepath}' is outside the project directory "
            f"({root}). Only files within are writable."
        )

    # Explicitly writable surface (highest precedence).
    if any(rel.startswith(p) for p in WRITABLE_PREFIXES):
        return 0, ""
    if rel in WRITABLE_FILES:
        return 0, ""

    # Explicitly protected — block.
    if any(rel.startswith(p) for p in PROTECTED_PREFIXES):
        return 2, (
            f"BLOCKED: write to protected path '{rel}' is not allowed. "
            f"Writable: daily/, knowledge/, briefing.md, feedback.md, state.json, .tmp/."
        )
    if rel in PROTECTED_FILES:
        return 2, (
            f"BLOCKED: write to protected file '{rel}' is not allowed. "
            f"Writable: daily/, knowledge/, briefing.md, feedback.md, state.json, .tmp/."
        )
    if any(rel.startswith(p) for p in PROTECTED_GLOB_PREFIXES):
        return 2, (
            f"BLOCKED: write to '{rel}' is not allowed (env/secret file). "
            f"Writable: daily/, knowledge/, briefing.md, feedback.md, state.json, .tmp/."
        )

    # Inside project root but not explicitly listed: allow. This is the implicit
    # "anything else under maestro/" rule from the original hook.
    return 0, ""


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except json.JSONDecodeError:
        # Malformed input — allow rather than block, matching prior hook behavior.
        return 0

    filepath = (
        (data.get("tool_input") or {}).get("file_path")
        or ""
    )
    code, msg = decide(filepath, project_root())
    if msg:
        print(msg, file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main())
