#!/usr/bin/env python3
"""Mattermost outbound DM client for Maestro.

Usage:
  python3 lib/mattermost.py send "message text"      Post a single message to channel
  python3 lib/mattermost.py send -                   Post stdin as one message
  python3 lib/mattermost.py send-file <path>         Post each non-empty line as a separate message
  python3 lib/mattermost.py fetch-recent [--since <ms-epoch|ISO>]  Fetch user-side posts in the channel since timestamp
                                                                  (default: state.json > cached.last_seen_mattermost_message_ts,
                                                                  or 24h ago on first run). Prints Markdown-formatted block
                                                                  to stdout; advances the cached watermark on success.

Reads creds from .env in the maestro/ directory. Posts to the channel specified
by MATTERMOST_CHANNEL_ID. If that env var is unset, falls back to creating (or
re-using) a 1:1 DM channel with MATTERMOST_USER_ID and caching it under
state.json > cached.mattermost_channel_id (cache write delegated to lib/state.py
so it shares the same atomic-rename pattern the harness already uses).

Exit codes:
  0    All requested sends succeeded.
  2    Missing required env var.
  3    HTTP error from Mattermost (4xx/5xx); for send-file, the marker file is
       rewritten with the still-unsent lines so the next run can retry.
  4    Network/transport error (DNS, timeout); same retry semantics as 3.
  5    Refused to send an empty message.

For send-file: any successful sends are committed; failures truncate the marker
to the unsent remainder. A fully-successful send removes the marker file.
"""
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env"
STATE_FILE = ROOT / "state.json"
STATE_PY = ROOT / "lib" / "state.py"


def load_env():
    """Load .env into os.environ if present. Simple KEY=VALUE parser, no quoting."""
    if not ENV_FILE.exists():
        return
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


def cfg(key):
    v = os.environ.get(key)
    if not v:
        sys.stderr.write(f"mattermost: missing env var {key}\n")
        sys.exit(2)
    return v


class HttpError(Exception):
    """Raised on HTTP-level (4xx/5xx) failures. Wraps status + body."""

    def __init__(self, code, body):
        super().__init__(f"HTTP {code}: {body}")
        self.code = code
        self.body = body


class TransportError(Exception):
    """Raised on network/transport-level failures (timeouts, DNS, etc.)."""


def http(method, path, body=None):
    """Make a request. Raises HttpError / TransportError on failure (callers decide exit code)."""
    url = cfg("MATTERMOST_BASE_URL").rstrip("/") + path
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": "Bearer " + cfg("MATTERMOST_BOT_TOKEN"),
            "Content-Type": "application/json; charset=utf-8",
            # Many enterprise Mattermost deployments sit behind Cloudflare bot-management,
            # which rejects the default `Python-urllib/3.x` User-Agent with HTTP 403
            # (CF error 1010). A non-default UA passes the rule. Stable identifier so the
            # request is recognizable in logs as Maestro, not a generic bot.
            "User-Agent": "maestro-bot/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise HttpError(e.code, e.read().decode("utf-8", "replace")) from e
    except Exception as e:
        raise TransportError(f"{type(e).__name__}: {e}") from e


def _cache_channel_id(cid):
    """Persist the resolved DM-fallback channel id via state.py's atomic writer.

    Only called in the DM-fallback path (MATTERMOST_CHANNEL_ID unset). When the
    operator provides MATTERMOST_CHANNEL_ID, the env var is the source of truth
    and no caching happens.

    Falls back to a direct (non-atomic) state.json write only if state.py can't be
    invoked. Concurrent runs are unlikely given hourly cron + lock.
    """
    if STATE_PY.exists():
        try:
            subprocess.run(
                [sys.executable, str(STATE_PY), "cache", "mattermost_channel_id", cid],
                check=True,
                timeout=10,
            )
            return
        except Exception as e:
            sys.stderr.write(f"mattermost: state.py cache delegation failed ({e}); falling back to direct write\n")
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8")) if STATE_FILE.exists() else {}
    except Exception:
        state = {}
    state.setdefault("cached", {})["mattermost_channel_id"] = cid
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def resolve_channel_id():
    """Return the Mattermost channel ID to post to.

    Resolution order:
      1. MATTERMOST_CHANNEL_ID env var (preferred — explicit operator config).
      2. state.json > cached.mattermost_channel_id (cached from a prior DM-fallback run).
      3. state.json > cached.mattermost_dm_channel_id (legacy field name; kept for backward compat).
      4. Create a 1:1 DM channel between MATTERMOST_BOT_USER_ID and MATTERMOST_USER_ID,
         then cache the result under (2).

    Path (1) is the supported configuration. Paths (2)–(4) keep older setups working
    without forcing a migration; remove once you're sure no installation still
    relies on the legacy DM-discovery flow.
    """
    env_cid = os.environ.get("MATTERMOST_CHANNEL_ID")
    if env_cid:
        return env_cid

    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        state = {}
    cached = state.get("cached", {})
    cid = cached.get("mattermost_channel_id") or cached.get("mattermost_dm_channel_id")
    if cid:
        return cid

    bot_id = cfg("MATTERMOST_BOT_USER_ID")
    user_id = cfg("MATTERMOST_USER_ID")
    try:
        ch = http("POST", "/api/v4/channels/direct", [bot_id, user_id])
    except HttpError as e:
        sys.stderr.write(f"mattermost: failed to resolve DM channel: {e}\n")
        sys.exit(3)
    except TransportError as e:
        sys.stderr.write(f"mattermost: transport error resolving DM channel: {e}\n")
        sys.exit(4)

    cid = ch["id"]
    _cache_channel_id(cid)
    return cid


def _post_one(message):
    """Send one message to the configured Mattermost channel. Raises on failure."""
    cid = resolve_channel_id()
    post = http("POST", "/api/v4/posts", {"channel_id": cid, "message": message})
    return post["id"]


def cmd_send(message):
    if not message.strip():
        sys.stderr.write("mattermost: refusing to send empty message\n")
        sys.exit(5)
    try:
        post_id = _post_one(message)
    except HttpError as e:
        sys.stderr.write(f"mattermost: send failed: {e}\n")
        sys.exit(3)
    except TransportError as e:
        sys.stderr.write(f"mattermost: send failed: {e}\n")
        sys.exit(4)
    print(post_id)


def cmd_send_file(path):
    """Send each non-empty line as a separate DM.

    Per-line failure-handling contract:
      - On any line failure, rewrite the marker file with the still-unsent lines
        (the failing line plus all lines after it) and exit non-zero. The next
        run.sh post-heartbeat sweep will retry.
      - On full success, remove the marker file (caller in run.sh also removes
        it, but we do it here too so a direct `python3 lib/mattermost.py send-file`
        invocation behaves consistently).
    """
    if not path.exists():
        sys.stderr.write(f"mattermost: file not found: {path}\n")
        sys.exit(1)

    all_lines = path.read_text(encoding="utf-8").splitlines()
    # Preserve original lines (with whitespace) but only attempt non-empty ones.
    sent = 0
    exit_code = 0
    error_msg = None
    remaining = []  # lines to keep in the marker on failure (raw, including blanks for fidelity)

    failed = False
    for raw in all_lines:
        if failed:
            remaining.append(raw)
            continue
        line = raw.strip()
        if not line:
            # Skip blank lines silently; don't preserve them in the rewritten marker
            # (they have no information value).
            continue
        try:
            post_id = _post_one(line)
            # Emit post id on stdout so callers (and the runner's subprocess
            # forwarder) can confirm exactly which lines actually landed in
            # Mattermost. One id per delivered line. This was a verification
            # gap surfaced 2026-05-24: cmd_send_file used to silently swallow
            # the post id (only cmd_send printed it), making the runner's
            # success path indistinguishable from staging on stdout alone.
            print(post_id)
            sent += 1
        except HttpError as e:
            failed = True
            exit_code = 3
            error_msg = str(e)
            remaining.append(raw)
        except TransportError as e:
            failed = True
            exit_code = 4
            error_msg = str(e)
            remaining.append(raw)

    if failed:
        # Rewrite the marker with what's left so run.sh's next pass retries the rest.
        path.write_text("\n".join(remaining) + ("\n" if remaining else ""), encoding="utf-8")
        sys.stderr.write(
            f"mattermost: sent {sent} message(s), then failed: {error_msg}. "
            f"Marker rewritten with {len(remaining)} unsent line(s).\n"
        )
        sys.exit(exit_code)

    # All-success: clean up marker.
    try:
        path.unlink()
    except OSError:
        pass
    sys.stderr.write(f"sent {sent} message(s)\n")


def _ms_now() -> int:
    import time
    return int(time.time() * 1000)


def _parse_since(arg: str | None) -> int:
    """Accept either a ms epoch int, an ISO 8601 string, or None.

    Returns ms-epoch. On None or parse failure, reads
    state.json > cached.last_seen_mattermost_message_ts; if absent, defaults to 24h ago.
    """
    if arg:
        # Try int first (ms epoch)
        if arg.isdigit():
            return int(arg)
        # Then ISO 8601
        try:
            from datetime import datetime
            return int(datetime.fromisoformat(arg.replace("Z", "+00:00")).timestamp() * 1000)
        except Exception:
            sys.stderr.write(f"mattermost: could not parse --since={arg!r}; falling back to state.json cache\n")

    # Fall back to state cache
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        cached_ts = state.get("cached", {}).get("last_seen_mattermost_message_ts")
        if cached_ts:
            return int(cached_ts)
    except Exception:
        pass

    # Default: 24h ago
    return _ms_now() - 24 * 3600 * 1000


def _ms_to_iso(ms: int) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


def cmd_fetch_recent(since_ms: int):
    """Print user-side posts in the configured channel since `since_ms` to stdout.

    Output is a Markdown block consumable by the heartbeat prompt as
    `## Recent Mattermost Feedback`. Caller (run.sh) is expected to prepend it
    to the prompt's Run Context.

    Posts authored by the bot itself are filtered out (we don't want the agent
    to "hear" its own outbound messages as feedback). System messages are also
    skipped. The watermark (state.json > cached.last_seen_mattermost_message_ts)
    is advanced to the most recent post's `create_at` on successful fetch, so
    repeated calls don't re-surface the same messages.
    """
    cid = resolve_channel_id()
    bot_user_id = os.environ.get("MATTERMOST_BOT_USER_ID", "")

    try:
        # /channels/{id}/posts?since=ms — returns up to 1000 posts since that ms epoch.
        data = http("GET", f"/api/v4/channels/{cid}/posts?since={since_ms}")
    except HttpError as e:
        sys.stderr.write(f"mattermost: fetch failed: {e}\n")
        sys.exit(3)
    except TransportError as e:
        sys.stderr.write(f"mattermost: fetch failed: {e}\n")
        sys.exit(4)

    order = data.get("order", []) or []
    posts = data.get("posts", {}) or {}

    # `order` is newest-first per Mattermost docs. Render oldest-first for the prompt.
    rendered = []
    max_create_at = since_ms
    for pid in reversed(order):
        post = posts.get(pid) or {}
        if post.get("user_id") == bot_user_id:
            continue  # ignore our own outbound
        if post.get("type"):  # non-empty type = system message (joins, header changes, etc.)
            continue
        create_at = post.get("create_at", 0)
        if create_at > max_create_at:
            max_create_at = create_at
        msg = (post.get("message") or "").strip()
        if not msg:
            continue
        rendered.append((create_at, post.get("user_id", "?"), msg))

    if not rendered:
        # Nothing to surface; still advance watermark to "now" so next call doesn't
        # re-pull this empty window forever.
        if STATE_PY.exists():
            try:
                subprocess.run(
                    [sys.executable, str(STATE_PY), "cache", "last_seen_mattermost_message_ts", str(_ms_now())],
                    check=True,
                    timeout=10,
                )
            except Exception:
                pass
        sys.stderr.write(f"mattermost: no new user-side posts since {_ms_to_iso(since_ms)}\n")
        return

    # Print the Markdown block.
    print("## Recent Mattermost Feedback")
    print("")
    print(f"_{len(rendered)} new post(s) from the user in the agent's channel since {_ms_to_iso(since_ms)}._")
    print("")
    for ts, uid, msg in rendered:
        # Truncate very long messages defensively; full content always available in Mattermost.
        if len(msg) > 600:
            msg = msg[:600] + "… [truncated]"
        print(f"- **{_ms_to_iso(ts)}** (user `{uid}`):")
        for line in msg.splitlines():
            print(f"  > {line}")
        print("")

    # Advance watermark
    if STATE_PY.exists():
        try:
            subprocess.run(
                [sys.executable, str(STATE_PY), "cache", "last_seen_mattermost_message_ts", str(max_create_at)],
                check=True,
                timeout=10,
            )
        except Exception as e:
            sys.stderr.write(f"mattermost: watermark advance failed: {e}\n")


def main():
    load_env()
    if len(sys.argv) < 2:
        sys.stderr.write(__doc__)
        sys.exit(1)
    cmd = sys.argv[1]

    if cmd == "send":
        if len(sys.argv) < 3:
            sys.stderr.write("usage: send <message>|-\n")
            sys.exit(1)
        msg = sys.stdin.read() if sys.argv[2] == "-" else sys.argv[2]
        cmd_send(msg)
    elif cmd == "send-file":
        if len(sys.argv) < 3:
            sys.stderr.write("usage: send-file <path>\n")
            sys.exit(1)
        cmd_send_file(Path(sys.argv[2]))
    elif cmd == "fetch-recent":
        # Optional --since <ms-or-iso>
        since_arg = None
        if len(sys.argv) >= 4 and sys.argv[2] == "--since":
            since_arg = sys.argv[3]
        cmd_fetch_recent(_parse_since(since_arg))
    else:
        sys.stderr.write(f"unknown command: {cmd}\n")
        sys.stderr.write(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
