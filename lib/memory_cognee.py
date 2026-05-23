#!/usr/bin/env python3
"""Maestro memory — cognee-backed knowledge graph + vector recall.

Replaces the SQLite/FTS5/sqlite-vec stack in memory.py with cognee 1.0.

Design constraints (must match memory.py CLI so run.sh stays unchanged):
  index   — re-process the corpus into cognee's knowledge graph + vector store
  recall  — emit a "## Recalled Memories" markdown section for prompt injection
  search  — ad-hoc query, prints top results
  stats   — index health summary

Isolation: this script is meant to run inside lib/cognee-venv only. The main
agent runtime never imports cognee — it shells out to this script. Keeps the
litellm dependency surface confined to one subprocess.

Storage roots are pinned to maestro/.cognee_data/ via env vars set in main()
so cognee never touches ~/.cognee or the site-packages tree.

Required env at runtime:
  LLM_API_KEY                    — OpenAI key (read from lib/.cognee_secrets if set)
  MAESTRO_COGNEE_MODEL           — overrides the default model string

Run via: lib/cognee-venv/Scripts/python.exe lib/memory_cognee.py <cmd>
"""
from __future__ import annotations

import asyncio
import io
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

# Force UTF-8 stdio on Windows so structlog + emoji don't crash the pipe.
if sys.stdout.encoding != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
if sys.stderr.encoding != "utf-8":
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

SCRIPT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = SCRIPT_DIR / ".cognee_data"
SECRETS_FILE = SCRIPT_DIR / "lib" / ".cognee_secrets"

CORPUS_DIRS = {
    "daily": SCRIPT_DIR / "daily",
    "knowledge": SCRIPT_DIR / "knowledge",
}
EXTRA_FILES = [SCRIPT_DIR / "briefing.md"]

DEFAULT_MODEL = "gpt-4o-mini"   # OpenAI chat model used for cognee's graph operations; override via OPENAI_MODEL env var
DEFAULT_EMBEDDING_MODEL = "text-embedding-3-small"
DEFAULT_TOP_K = 5


def _bootstrap_env() -> None:
    """Pin cognee storage to .cognee_data/ and load OpenAI key from secrets file.

    Called before `import cognee` so the config picks up our paths.
    """
    DATA_DIR.mkdir(exist_ok=True)
    os.environ.setdefault("DATA_ROOT_DIRECTORY", str(DATA_DIR / "data"))
    os.environ.setdefault("SYSTEM_ROOT_DIRECTORY", str(DATA_DIR / "system"))
    os.environ.setdefault("CACHE_ROOT_DIRECTORY", str(DATA_DIR / "cache"))
    os.environ.setdefault("LOGS_ROOT_DIRECTORY", str(DATA_DIR / "logs"))
    os.environ.setdefault("ENABLE_BACKEND_ACCESS_CONTROL", "false")
    os.environ.setdefault("CACHING", "false")

    os.environ.setdefault("LLM_PROVIDER", "openai")
    os.environ.setdefault("LLM_MODEL", os.environ.get("MAESTRO_COGNEE_MODEL", DEFAULT_MODEL))
    os.environ.setdefault("EMBEDDING_PROVIDER", "openai")
    os.environ.setdefault("EMBEDDING_MODEL", DEFAULT_EMBEDDING_MODEL)
    os.environ.setdefault("EMBEDDING_DIMENSIONS", "1536")  # text-embedding-3-small max

    if SECRETS_FILE.is_file():
        for line in SECRETS_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

    # Convention: a single OPENAI_API_KEY supplies both LLM and embedding auth.
    openai_key = os.environ.get("OPENAI_API_KEY")
    if openai_key:
        os.environ.setdefault("LLM_API_KEY", openai_key)
        os.environ.setdefault("EMBEDDING_API_KEY", openai_key)


# ── File discovery ────────────────────────────────────────────────

def discover_files() -> list[tuple[Path, str]]:
    """Find every markdown file in the corpus. Returns (path, source) pairs."""
    files: list[tuple[Path, str]] = []
    for source, dirpath in CORPUS_DIRS.items():
        if dirpath.is_dir():
            for md in sorted(dirpath.rglob("*.md")):
                files.append((md, source))
    for extra in EXTRA_FILES:
        if extra.is_file():
            files.append((extra, "root"))
    return files


def _daily_log_date(path: Path) -> datetime | None:
    """Parse the YYYY-MM-DD prefix from a daily log filename, else None."""
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", path.stem)
    if not m:
        return None
    try:
        return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def select_files_for_indexing(all_files: list[tuple[Path, str]]) -> list[tuple[Path, str]]:
    """Apply the rolling-window policy to decide what to send to cognee.

    TODO(you): Implement the rolling-window cutoff. This is where your
    domain knowledge matters — the agent has to remember enough to be useful
    but not so much that the graph bloats and indexing cost spirals.

    Inputs:
        all_files — every markdown file under daily/, knowledge/, briefing.md

    Decisions you need to make:
        1. How many days of `daily/YYYY-MM-DD.md` logs to include?
           (e.g. last 30 days hot, older skipped — relying on daily summaries
           in knowledge/active-context.md to carry forward older context)
        2. Always include all of `knowledge/` regardless of age? (Probably yes —
           those files are curated, not append-only, and small enough.)
        3. Always include `briefing.md`? (Probably yes — it's the live focus.)
        4. What about resolved-archive.md / decay-log.md — do those add signal
           or just noise to the graph?

    Return only the (path, source) tuples that should go to cognee.add().
    Reference impl below — replace with your policy.
    """
    cutoff = datetime.now() - timedelta(days=30)
    skip_names = {"resolved-archive.md", "decay-log.md"}
    selected: list[tuple[Path, str]] = []
    for path, source in all_files:
        if path.name in skip_names:
            continue
        if source == "daily":
            d = _daily_log_date(path)
            if d is None or d < cutoff:
                continue
        selected.append((path, source))
    return selected


# ── Recall orchestration ──────────────────────────────────────────

async def orchestrate_recall(query: str, top_k: int = DEFAULT_TOP_K) -> str:
    """Given a query, run the right cognee SearchType(s) and return markdown.

    TODO(you): Decide the recall strategy. This is the "what do you think"
    you punted to me — here's my recommendation, but it's your call.

    Available SearchTypes (from cognee 1.0.3):
        CHUNKS              — vector retrieval over raw text. No LLM at query
                              time. Cheapest. Drop-in for current behavior.
        CHUNKS_LEXICAL      — BM25/keyword retrieval over chunks. Pair with
                              CHUNKS for hybrid (closest to current memory.py).
        SUMMARIES           — pre-computed cluster summaries.
        GRAPH_COMPLETION    — LLM synthesizes an answer using graph traversal.
                              The new value cognee adds. Costs 1 LLM call/query.
        TRIPLET_COMPLETION  — like GRAPH_COMPLETION but returns (subj,pred,obj).
        TEMPORAL            — time-aware traversal — interesting for daily logs.
        FEELING_LUCKY       — cognee picks the best mode. Opaque.

    My recommendation:
        - Always call CHUNKS (cheap, deterministic, drop-in).
        - Add ONE graph-aware call (GRAPH_COMPLETION or TEMPORAL). Pick TEMPORAL
          if you care about "what was happening last week vs now"; GRAPH_COMPLETION
          if you care about "what entities link these threads".
        - Skip GRAPH_COMPLETION_DECOMPOSITION / _COT — those make multiple LLM
          calls per query, which competes with the maestro agent's own reasoning.

    Render as a "## Recalled Memories" markdown section. Each result block:
        **path** (lines X-Y, source): preview text...

    Example skeleton (replace with your choice):
        import cognee
        from cognee.api.v1.search import SearchType
        chunks = await cognee.search(query, query_type=SearchType.CHUNKS, top_k=top_k)
        graph  = await cognee.search(query, query_type=SearchType.GRAPH_COMPLETION)
        return _render(chunks, graph)
    """
    import cognee
    from cognee.api.v1.search import SearchType

    chunks: list = []
    temporal: list = []
    try:
        chunks = await cognee.search(query, query_type=SearchType.CHUNKS, top_k=top_k) or []
    except Exception as e:
        print(f"WARN: CHUNKS search failed: {e}", file=sys.stderr)
    try:
        temporal = await cognee.search(query, query_type=SearchType.TEMPORAL) or []
    except Exception as e:
        print(f"WARN: TEMPORAL search failed: {e}", file=sys.stderr)

    out: list[str] = ["## Recalled Memories\n"]
    if not chunks and not temporal:
        out.append("_No relevant memories found in the cognee index._\n")
        return "\n".join(out)

    out.append(
        "Excerpts from past daily logs and knowledge files. "
        "Use for continuity; trust current data sources over stale memories.\n"
    )

    if chunks:
        out.append("### Relevant excerpts")
        for r in chunks[:top_k]:
            text = r.get("text") if isinstance(r, dict) else str(r)
            path = r.get("path") if isinstance(r, dict) else None
            preview = (text or "").strip().replace("\n", " ")[:500]
            header = f"**{path}**: " if path else "- "
            out.append(f"{header}{preview}")
        out.append("")

    if temporal:
        out.append("### Time-aware synthesis")
        for r in temporal[:3]:
            answer = r.get("answer") if isinstance(r, dict) else str(r)
            if answer:
                out.append(f"> {str(answer).strip()[:800]}")
        out.append("")

    return "\n".join(out)


# ── Commands ──────────────────────────────────────────────────────

async def cmd_index(args: list[str]) -> None:
    import cognee

    files = discover_files()
    selected = select_files_for_indexing(files)

    if not selected:
        print("No files matched the rolling-window policy.")
        return

    texts = []
    for path, source in selected:
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            print(f"WARN: could not read {path}: {e}", file=sys.stderr)
            continue
        rel = path.relative_to(SCRIPT_DIR).as_posix()
        texts.append(f"[source={source} path={rel}]\n{content}")

    await cognee.add(texts)
    await cognee.cognify()
    print(f"Indexed {len(texts)} files into cognee ({DATA_DIR}).")


async def cmd_recall(args: list[str]) -> None:
    """Build keyword queries from briefing+watchlist (same heuristic as memory.py),
    then call orchestrate_recall and emit the markdown block."""
    keywords: list[str] = []

    briefing_path = SCRIPT_DIR / "briefing.md"
    if briefing_path.is_file():
        for line in briefing_path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line.startswith(("- ", "* ")):
                tokens = re.findall(r"[A-Z]+-\d+|[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*|\b\w{4,}\b", line)
                keywords.extend(tokens[:3])

    watchlist_path = SCRIPT_DIR / "knowledge" / "watchlist.md"
    if watchlist_path.is_file():
        for line in watchlist_path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line.startswith("- ") and "watching" in line.lower():
                tokens = re.findall(r"[A-Z]+-\d+|[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*", line)
                keywords.extend(tokens[:2])

    seen: set[str] = set()
    unique: list[str] = []
    for kw in keywords:
        if kw.lower() not in seen and len(kw) > 2:
            seen.add(kw.lower())
            unique.append(kw)
    if not unique:
        unique = ["blockers", "upcoming", "deadline"]

    query = " ".join(unique[:8])
    print(await orchestrate_recall(query))


async def cmd_search(args: list[str]) -> None:
    if not args:
        print("Usage: memory_cognee.py search <query>", file=sys.stderr)
        sys.exit(1)
    print(await orchestrate_recall(args[0]))


async def cmd_stats(args: list[str]) -> None:
    import cognee  # noqa: F401  (importing forces config load)
    files = discover_files()
    print(f"Cognee data dir: {DATA_DIR}")
    print(f"Files in corpus: {len(files)}")
    print(f"  daily:     {sum(1 for _, s in files if s == 'daily')}")
    print(f"  knowledge: {sum(1 for _, s in files if s == 'knowledge')}")
    print(f"  root:      {sum(1 for _, s in files if s == 'root')}")
    print(f"LLM model:       {os.environ.get('LLM_MODEL')}")
    print(f"Embedding model: {os.environ.get('EMBEDDING_MODEL')}")


# ── Entrypoint ────────────────────────────────────────────────────

def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: memory_cognee.py <index|recall|search|stats> [args]", file=sys.stderr)
        sys.exit(1)

    _bootstrap_env()

    cmd = sys.argv[1]
    args = sys.argv[2:]
    handlers = {
        "index": cmd_index,
        "recall": cmd_recall,
        "search": cmd_search,
        "stats": cmd_stats,
    }
    if cmd not in handlers:
        print(f"Unknown command: {cmd}", file=sys.stderr)
        sys.exit(1)

    asyncio.run(handlers[cmd](args))


if __name__ == "__main__":
    main()
