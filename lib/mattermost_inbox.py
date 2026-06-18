#!/usr/bin/env python3
"""Fetch recent Mattermost messages across all DMs and channels the user belongs to.

Authenticates with a personal access token read from the MATTERMOST_TOKEN
environment variable. Uses only the Python standard library (no pip deps, no
binaries) so it runs cleanly under restrictive antivirus policies.

Usage:
    python fetch_messages.py [--since 1d] [--server URL] [--json] [--include-self]
                             [--channel-type all|dm|channel] [--max-per-channel N]

Examples:
    python fetch_messages.py --since 6h
    python fetch_messages.py --since 1w --channel-type dm
    python fetch_messages.py --since 1d --json > inbox.json
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

DEFAULT_SERVER = "https://chat.exacaster.com"


# --------------------------------------------------------------------------- #
# HTTP helpers
# --------------------------------------------------------------------------- #
def api_request(server, token, path, method="GET", body=None):
    """Make an authenticated request to the Mattermost API v4 and return parsed JSON."""
    url = f"{server.rstrip('/')}/api/v4{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    # Cloudflare (which fronts chat.exacaster.com) returns HTTP 403 error 1010
    # for the default "Python-urllib" agent. Present a browser-like signature.
    req.add_header(
        "User-Agent",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    )
    req.add_header("Accept", "application/json")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")
        raise SystemExit(
            f"ERROR {e.code} on {method} {path}: {detail}\n"
            "Check that MATTERMOST_TOKEN is valid and that personal access "
            "tokens are enabled on the server."
        )
    except urllib.error.URLError as e:
        raise SystemExit(f"ERROR connecting to {url}: {e.reason}")


# --------------------------------------------------------------------------- #
# Time window parsing
# --------------------------------------------------------------------------- #
def parse_since(spec):
    """Convert a window spec like '6h', '1d', '2w', '30m' into epoch milliseconds cutoff."""
    m = re.fullmatch(r"(\d+)\s*([mhdw])", spec.strip().lower())
    if not m:
        raise SystemExit(
            f"Invalid --since value '{spec}'. Use forms like 30m, 6h, 1d, 2w."
        )
    qty, unit = int(m.group(1)), m.group(2)
    seconds = {"m": 60, "h": 3600, "d": 86400, "w": 604800}[unit] * qty
    return int((time.time() - seconds) * 1000)


def fmt_ts(ms):
    """Format epoch milliseconds as a local, human-readable timestamp."""
    return datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M")


# --------------------------------------------------------------------------- #
# User name resolution (batched + cached)
# --------------------------------------------------------------------------- #
class UserCache:
    def __init__(self, server, token):
        self.server, self.token = server, token
        self.cache = {}

    def resolve(self, user_ids):
        """Resolve a set of user ids to display names, batching unknowns in one call."""
        missing = [uid for uid in user_ids if uid and uid not in self.cache]
        for i in range(0, len(missing), 100):
            batch = missing[i : i + 100]
            users = api_request(
                self.server, self.token, "/users/ids", method="POST", body=batch
            ) or []
            for u in users:
                self.cache[u["id"]] = _display_name(u)

    def name(self, user_id):
        return self.cache.get(user_id, user_id or "system")


def _display_name(user):
    """Build a friendly display name from a Mattermost user object."""
    first, last = user.get("first_name", ""), user.get("last_name", "")
    full = f"{first} {last}".strip()
    return full if full else user.get("username", user.get("id", "unknown"))


# --------------------------------------------------------------------------- #
# Channel discovery + naming
# --------------------------------------------------------------------------- #
def get_all_channels(server, token, my_id):
    """Return deduped channels across every team the user is a member of."""
    teams = api_request(server, token, "/users/me/teams") or []
    seen, channels = set(), []
    for team in teams:
        team_channels = (
            api_request(server, token, f"/users/me/teams/{team['id']}/channels") or []
        )
        for ch in team_channels:
            if ch["id"] not in seen:
                seen.add(ch["id"])
                channels.append(ch)
    return channels


def channel_label(ch, users, my_id):
    """Produce a readable label for a channel, resolving DM/group member names."""
    ctype = ch.get("type")
    if ctype == "D":  # direct message: name is "<idA>__<idB>"
        parts = ch.get("name", "").split("__")
        other = next((p for p in parts if p != my_id), my_id)
        return f"DM with {users.name(other)}"
    if ctype == "G":  # group message
        dn = ch.get("display_name", "").strip()
        return f"Group: {dn}" if dn else "Group DM"
    # public (O) / private (P) channel
    return ch.get("display_name") or ch.get("name") or ch["id"]


def dm_member_ids(ch, my_id):
    """Return the other participant ids for a direct/group channel (for name resolution)."""
    if ch.get("type") == "D":
        return [p for p in ch.get("name", "").split("__")]
    return []


# --------------------------------------------------------------------------- #
# Post fetching
# --------------------------------------------------------------------------- #
def get_recent_posts(server, token, channel_id, cutoff_ms, max_posts):
    """Page through a channel's posts (newest first) collecting those newer than cutoff."""
    collected, page, per_page = [], 0, 100
    while len(collected) < max_posts:
        resp = api_request(
            server,
            token,
            f"/channels/{channel_id}/posts?page={page}&per_page={per_page}",
        )
        if not resp:
            break
        order = resp.get("order", [])
        posts = resp.get("posts", {})
        if not order:
            break
        hit_old = False
        for pid in order:  # order is newest -> oldest
            post = posts.get(pid, {})
            if post.get("create_at", 0) < cutoff_ms:
                hit_old = True
                break
            # skip join/leave/system messages
            if post.get("type"):
                continue
            collected.append(post)
        if hit_old or len(order) < per_page:
            break
        page += 1
    return collected


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description="Fetch recent Mattermost messages.")
    ap.add_argument("--since", default="1d", help="Time window: 30m, 6h, 1d, 2w (default 1d)")
    ap.add_argument("--server", default=os.environ.get("MATTERMOST_SERVER", DEFAULT_SERVER))
    ap.add_argument("--channel-type", choices=["all", "dm", "channel"], default="all")
    ap.add_argument("--max-per-channel", type=int, default=200)
    ap.add_argument("--include-self", action="store_true", help="Include your own posts")
    ap.add_argument(
        "--include-maestro",
        action="store_true",
        help="Include the MAESTRO bot channel (excluded by default as it is bot-update noise)",
    )
    ap.add_argument("--json", action="store_true", help="Emit JSON instead of text")
    args = ap.parse_args()

    token = os.environ.get("MATTERMOST_TOKEN")
    if not token:
        raise SystemExit(
            "MATTERMOST_TOKEN environment variable is not set.\n"
            "PowerShell:  $env:MATTERMOST_TOKEN = '<your-token>'\n"
            "Persist it:  setx MATTERMOST_TOKEN \"<your-token>\""
        )

    cutoff = parse_since(args.since)
    me = api_request(args.server, token, "/users/me")
    my_id = me["id"]
    users = UserCache(args.server, token)
    users.cache[my_id] = _display_name(me)

    channels = get_all_channels(args.server, token, my_id)
    if args.channel_type == "dm":
        channels = [c for c in channels if c.get("type") in ("D", "G")]
    elif args.channel_type == "channel":
        channels = [c for c in channels if c.get("type") in ("O", "P")]

    # The MAESTRO channel is a bot that posts updates to the user — useful at times,
    # noisy at others. Excluded unless explicitly requested.
    if not args.include_maestro:
        def _is_maestro(c):
            label = (c.get("display_name") or c.get("name") or "").strip().lower()
            return label == "maestro"
        channels = [c for c in channels if not _is_maestro(c)]

    # Pre-resolve DM participant names so channel labels read nicely.
    dm_ids = {uid for c in channels for uid in dm_member_ids(c, my_id)}
    users.resolve(dm_ids)

    results = []
    for ch in channels:
        posts = get_recent_posts(args.server, token, ch["id"], cutoff, args.max_per_channel)
        if not args.include_self:
            posts = [p for p in posts if p.get("user_id") != my_id]
        if not posts:
            continue
        users.resolve({p.get("user_id") for p in posts})
        posts.sort(key=lambda p: p.get("create_at", 0))  # oldest -> newest
        results.append((ch, posts))

    # Most recently active channel first.
    results.sort(key=lambda r: r[1][-1].get("create_at", 0), reverse=True)

    if args.json:
        out = []
        for ch, posts in results:
            out.append({
                "channel": channel_label(ch, users, my_id),
                "channel_id": ch["id"],
                "type": ch.get("type"),
                "messages": [
                    {
                        "time": fmt_ts(p["create_at"]),
                        "create_at": p["create_at"],
                        "from": users.name(p.get("user_id")),
                        "message": p.get("message", ""),
                    }
                    for p in posts
                ],
            })
        print(json.dumps(out, indent=2, ensure_ascii=False))
        return

    total = sum(len(p) for _, p in results)
    print(f"Recent Mattermost messages (last {args.since}) — {total} message(s) "
          f"across {len(results)} conversation(s)\n")
    for ch, posts in results:
        print(f"=== {channel_label(ch, users, my_id)} ({len(posts)}) ===")
        for p in posts:
            msg = p.get("message", "").replace("\n", "\n    ")
            print(f"  [{fmt_ts(p['create_at'])}] {users.name(p.get('user_id'))}: {msg}")
        print()


if __name__ == "__main__":
    main()
