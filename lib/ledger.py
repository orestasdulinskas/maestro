#!/usr/bin/env python3
"""Maestro open-items ledger + the one-post-per-run renderer.

The ledger (`knowledge/ledger.json`, synced with the rest of knowledge/) is the
single memory of what the user still has to do ("needs you") and what Maestro is
watching on their behalf ("waiting on others"). Every Mattermost post is rendered
from it by `render`, in one fixed shape, so the agent never formats a post by hand.

Usage (normally via `python3 runner/maestro.py ledger ...` / `... post`):

  ledger.py list [--json]                          open items, for the agent to read at run start
  ledger.py add ID --title T [--link URL] [--kind needs_you|waiting]
                  [--waiting-on WHO] [--sig S] [--bump]  create, or update in place (--bump = re-surface now)
  ledger.py done ID                                user did it
  ledger.py snooze ID 2d|6h|1w                     hide until then
  ledger.py touch ID --sig S [--note "what changed"]   compare a waiting item's signature; prints changed|unchanged
  ledger.py changed "line" [--id ID] [--link URL]  record a change for this run's post
  ledger.py acks                                   read the user's "done|snooze|add" replies in the channel, apply them
  ledger.py render [--mode auto|morning|hourly|eod] [--now ISO]   print the post (exit 10 = nothing to post)

Sanitising rules live in `clean_line()`: no bold, no emoji, no inline clock
times, one id per line at the end as a link, 110 chars max per line.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LIB = ROOT / "lib"
LEDGER_FILE = ROOT / "knowledge" / "ledger.json"
STATE_FILE = ROOT / "state.json"
CONFIG_FILE = ROOT / "config.json"
TZ_NAME = os.environ.get("MAESTRO_TZ", "Europe/Vilnius")

SCHEMA = 1
NO_POST = 10          # exit code of `render` when there is nothing to say
LINE_MAX = 160        # safety net; the agent is told to write under 100
HOURLY_NEEDS_MAX = 3   # 1 + 3 + 1 + 3 = 8 lines at most
HOURLY_CHANGED_MAX = 3
FULL_NEEDS_MAX = 10
FULL_CHANGED_MAX = 4
WAITING_MAX = 4
ACK_RE = re.compile(r"^\s*(done|snooze|add)\s+(\S+)(?:\s+(\d+)\s*([hdw]))?(?:\s+(.*))?$", re.I)
JIRA_KEY_RE = re.compile(r"^[A-Z][A-Z0-9]+-\d+$")
# "at 14:22", "(14:22)", "(13:19-13:53)" are noise; a meeting time next to a date stays.
TIME_RE = re.compile(r"\s+at\s+\d{1,2}:\d{2}\b|\s*\(\s*(?:\w+\s+)?\d{1,2}:\d{2}(?:\s*[-–]\s*\d{1,2}:\d{2})?\s*\)")

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")


# ── time ─────────────────────────────────────────────────────────

def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def local_now(now: datetime | None = None) -> datetime:
    from zoneinfo import ZoneInfo
    return (now or now_utc()).astimezone(ZoneInfo(TZ_NAME))


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def short_date(s: str | None) -> str:
    d = parse_iso(s)
    return f"{d.day} {d:%b}" if d else "?"


# ── storage ──────────────────────────────────────────────────────

def load() -> dict:
    if LEDGER_FILE.exists():
        try:
            data = json.loads(LEDGER_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict) and "items" in data:
                data.setdefault("changes", [])
                data.setdefault("meta", {})
                return data
        except json.JSONDecodeError as e:
            sys.stderr.write(f"ledger: {LEDGER_FILE} is corrupt ({e}); starting empty.\n")
    return {"schema_version": SCHEMA, "items": {}, "changes": [], "meta": {}}


def save(data: dict) -> None:
    LEDGER_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = LEDGER_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, LEDGER_FILE)


def load_config_links() -> dict:
    try:
        return (json.loads(CONFIG_FILE.read_text(encoding="utf-8")).get("links") or {})
    except (OSError, json.JSONDecodeError):
        return {}


def norm_id(raw: str, data: dict | None = None) -> str:
    """Ids keep the case they were added with; lookups are case-insensitive so a
    reply like `done gn-2027` still matches GN-2027."""
    raw = raw.strip().strip(",.;:")
    if data:
        for existing in data["items"]:
            if existing.lower() == raw.lower():
                return existing
    return raw.upper() if JIRA_KEY_RE.match(raw.upper()) else raw   # new Jira-shaped id: canonical case


def default_link(item_id: str) -> str:
    links = load_config_links()
    base = links.get("jira_base") or os.environ.get("MAESTRO_JIRA_BASE", "")
    if base and JIRA_KEY_RE.match(item_id.upper()):
        return base.rstrip("/") + "/" + item_id.upper()
    return ""


# ── text hygiene ─────────────────────────────────────────────────

def clean_line(text: str, item_id: str | None = None) -> str:
    """Plain language only: no markdown emphasis, no emoji, no clock times, no repeated id."""
    t = text.strip()
    t = re.sub(r"\*\*|__|`", "", t)
    t = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", t)          # markdown links -> text
    t = "".join(c for c in t if ord(c) < 128 or unicodedata.category(c)[0] in "LNPZ")
    t = TIME_RE.sub("", t)
    if item_id:  # drop the id only when it leads or trails the line; mid-sentence mentions stay
        esc = re.escape(item_id)
        t = re.sub(r"^\s*[\[(]?" + esc + r"[\])]?:?\s*", "", t, flags=re.I)
        t = re.sub(r"\s*[-,(\[]?\s*" + esc + r"[\])]?\s*$", "", t, flags=re.I)
    t = re.sub(r"\s+", " ", t).strip(" -:,")
    if len(t) > LINE_MAX:
        t = t[:LINE_MAX - 3].rstrip() + "..."
    return t


def id_suffix(item_id: str | None, link: str | None) -> str:
    if not item_id:
        return ""
    return f" [{item_id}]({link})" if link else f" [{item_id}]"


# ── item ops ─────────────────────────────────────────────────────

def open_items(data: dict, now: datetime) -> list[dict]:
    out = []
    for item in data["items"].values():
        if item.get("state") == "done":
            continue
        if item.get("state") == "snoozed":
            until = parse_iso(item.get("snooze_until"))
            if until and until > now:
                continue
            item["state"] = "open"           # snooze expired: back on the list, flagged as changed
            item["dirty"] = True
        out.append(item)
    out.sort(key=lambda i: i.get("first_seen") or "")
    return out


def record_change(data: dict, line: str, item_id: str | None, link: str | None, now: datetime) -> None:
    data["changes"].append({"ts": iso(now), "line": line, "id": item_id, "link": link or ""})


def cmd_list(args) -> int:
    data = load()
    now = now_utc()
    items = open_items(data, now)
    if args.json:
        print(json.dumps({"items": items, "meta": data["meta"]}, indent=2, ensure_ascii=False))
        return 0
    if not items:
        print("ledger: no open items")
    for it in items:
        tag = "waiting" if it.get("kind") == "waiting" else "needs_you"
        who = f" waiting on {it['waiting_on']}" if it.get("waiting_on") else ""
        print(f"{it['id']} | {tag}{who} | since {short_date(it.get('first_seen'))} | "
              f"nudged {short_date(it.get('last_nudged'))} | {it.get('title') or '(no title yet)'}"
              f"{'' if it.get('link') else ' | LINK MISSING'}")
    return 0


def cmd_add(args) -> int:
    data = load()
    now = now_utc()
    item_id = norm_id(args.id, data)
    item = data["items"].get(item_id)
    created = item is None
    if created:
        item = {"id": item_id, "first_seen": iso(now), "last_nudged": None, "state": "open",
                "kind": "needs_you", "title": "", "link": "", "waiting_on": "", "sig": "", "dirty": True}
        data["items"][item_id] = item
    if args.title:
        item["title"] = clean_line(args.title, item_id)
    if args.link:
        item["link"] = args.link.strip()
    elif not item.get("link"):
        item["link"] = default_link(item_id)
    if args.kind:
        item["kind"] = args.kind
    if args.waiting_on:
        item["waiting_on"] = args.waiting_on.strip()
        item["kind"] = "waiting"
    if args.sig is not None:
        item["sig"] = args.sig
    if args.bump or item.get("state") == "done":
        item["dirty"] = True
    item["state"] = "open"
    item.pop("snooze_until", None)
    save(data)
    print(f"{'added' if created else 'updated'} {item_id}")
    return 0


def cmd_done(args) -> int:
    data = load()
    now = now_utc()
    item_id = norm_id(args.id, data)
    item = data["items"].get(item_id)
    if not item:
        sys.stderr.write(f"ledger done: {item_id} is not in the ledger.\n")
        return 1
    item["state"] = "done"
    item["done_at"] = iso(now)
    item["dirty"] = False
    if not args.quiet:
        record_change(data, "done: " + (item.get("title") or item_id), item_id, item.get("link"), now)
    save(data)
    print(f"done {item_id}")
    return 0


def parse_duration(num: str | None, unit: str | None) -> timedelta:
    n = int(num) if num else 1
    return {"h": timedelta(hours=n), "d": timedelta(days=n), "w": timedelta(weeks=n)}[(unit or "d").lower()]


def cmd_snooze(args) -> int:
    data = load()
    now = now_utc()
    item_id = norm_id(args.id, data)
    item = data["items"].get(item_id)
    if not item:
        sys.stderr.write(f"ledger snooze: {item_id} is not in the ledger.\n")
        return 1
    m = re.fullmatch(r"(\d+)\s*([hdw])", args.duration.strip(), re.I)
    if not m:
        sys.stderr.write("ledger snooze: duration must look like 2d, 6h or 1w.\n")
        return 2
    until = now + parse_duration(m.group(1), m.group(2))
    item["state"] = "snoozed"
    item["snooze_until"] = iso(until)
    item["dirty"] = False
    if not args.quiet:
        record_change(data, f"snoozed until {short_date(iso(until))}: " + (item.get("title") or item_id),
                      item_id, item.get("link"), now)
    save(data)
    print(f"snoozed {item_id} until {iso(until)}")
    return 0


def cmd_touch(args) -> int:
    """Waiting-on items: post only when the signature (last comment id, MR state,
    job status, newest mail id ...) differs from the one stored."""
    data = load()
    now = now_utc()
    item_id = norm_id(args.id, data)
    item = data["items"].get(item_id)
    if not item:
        sys.stderr.write(f"ledger touch: {item_id} is not in the ledger; add it first.\n")
        return 1
    item["last_checked"] = iso(now)
    if item.get("sig") == args.sig:
        save(data)
        print("unchanged")
        return 0
    first_sig = not item.get("sig")
    item["sig"] = args.sig
    item["last_change"] = iso(now)
    if not first_sig:
        line = args.note or ("update on " + (item.get("title") or item_id))
        record_change(data, line, item_id, item.get("link"), now)
    save(data)
    print("baseline" if first_sig else "changed")
    return 0


def cmd_changed(args) -> int:
    data = load()
    item_id = norm_id(args.id, data) if args.id else None
    link = args.link or (data["items"].get(item_id, {}).get("link") if item_id else "") or default_link(item_id or "")
    record_change(data, args.line, item_id, link, now_utc())
    save(data)
    print("recorded")
    return 0


# ── acks: the user's replies in the channel ──────────────────────

def _mm():
    sys.path.insert(0, str(LIB))
    import mattermost  # noqa: WPS433 (same package, stdlib client)
    mattermost.load_env()
    return mattermost


def _state_cache_get(key: str):
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8")).get("cached", {}).get(key)
    except (OSError, json.JSONDecodeError):
        return None


def _state_cache_set(key: str, value) -> None:
    import subprocess
    state_py = LIB / "state.py"
    if state_py.exists():
        subprocess.run([sys.executable, str(state_py), "cache", key, str(value)], check=False, timeout=10)


def apply_ack(data: dict, verb: str, raw_id: str, num: str | None, unit: str | None, rest: str | None,
              now: datetime) -> str | None:
    """Apply one ack. Returns the change line to show, or None if the id is unknown/ignored."""
    item_id = norm_id(raw_id, data)
    verb = verb.lower()
    if verb == "add":
        item = data["items"].get(item_id)
        title = clean_line(rest, item_id) if rest and not rest.startswith("http") else ""
        link = rest.strip() if rest and rest.startswith("http") else ""
        if item is None:
            data["items"][item_id] = {"id": item_id, "first_seen": iso(now), "last_nudged": None,
                                      "state": "open", "kind": "needs_you", "title": title,
                                      "link": link or default_link(item_id), "waiting_on": "", "sig": "",
                                      "dirty": True}
        else:
            item.update(state="open", dirty=True)
            item.pop("snooze_until", None)
            if title:
                item["title"] = title
            if link:
                item["link"] = link
        return "you added: " + (title or item_id)
    item = data["items"].get(item_id)
    if item is None:
        return None
    if verb == "done":
        item.update(state="done", done_at=iso(now), dirty=False)
        return "you marked done: " + (item.get("title") or item_id)
    until = now + parse_duration(num, unit)
    item.update(state="snoozed", snooze_until=iso(until), dirty=False)
    return f"you snoozed until {short_date(iso(until))}: " + (item.get("title") or item_id)


def cmd_acks(args) -> int:
    """Read the user's posts in the Maestro channel since the ack watermark and apply
    the three verbs. Anything else in a post is data for the feedback step, not for us."""
    mm = _mm()
    data = load()
    now = now_utc()
    since = _state_cache_get("ledger_ack_ts")
    since_ms = int(since) if since else int((now - timedelta(days=1)).timestamp() * 1000)
    cid = mm.resolve_channel_id()
    bot_id = os.environ.get("MATTERMOST_BOT_USER_ID", "")
    try:
        resp = mm.http("GET", f"/api/v4/channels/{cid}/posts?since={since_ms}")
    except Exception as e:  # HttpError / TransportError
        sys.stderr.write(f"ledger acks: fetch failed: {e}\n")
        return 3
    posts = resp.get("posts", {}) or {}
    order = resp.get("order", []) or []
    applied, ignored = [], 0
    max_ts = since_ms
    for pid in reversed(order):
        p = posts.get(pid) or {}
        ts = p.get("create_at", 0)
        max_ts = max(max_ts, ts)
        if p.get("user_id") == bot_id or p.get("type"):
            continue
        first_line = (p.get("message") or "").strip().splitlines()[:1]
        m = ACK_RE.match(first_line[0]) if first_line else None
        if not m:
            ignored += 1
            continue
        line = apply_ack(data, *m.groups(), now=now)
        if line is None:
            applied.append(f"ignored ack for unknown id {m.group(2)}")
            continue
        iid = norm_id(m.group(2), data)
        record_change(data, line, iid, data["items"].get(iid, {}).get("link"), now)
        applied.append(line)
    save(data)
    _state_cache_set("ledger_ack_ts", max(max_ts, since_ms))
    for line in applied:
        print("ack: " + line)
    if not applied:
        print(f"ledger acks: none (ignored {ignored} non-ack post(s))")
    return 0


# ── render ───────────────────────────────────────────────────────

def pick_mode(now_local: datetime, requested: str) -> str:
    if requested != "auto":
        return requested
    if now_local.hour <= 8:
        return "morning"
    if now_local.hour >= 18:
        return "eod"
    return "hourly"


def render(data: dict, mode: str, now: datetime) -> str | None:
    """Return the post text, or None when an hourly run has nothing new."""
    full = mode in ("morning", "eod")
    items = open_items(data, now)
    needs = [i for i in items if i.get("kind") != "waiting"]
    waiting = [i for i in items if i.get("kind") == "waiting"]
    if not full:
        needs = [i for i in needs if i.get("dirty") or not i.get("last_nudged")]

    last_post = parse_iso(data["meta"].get("last_post_ts"))
    last_full = parse_iso(data["meta"].get("last_full_post_ts"))
    if mode == "eod":
        day = local_now(now).date()
        changes = [c for c in data["changes"] if local_now(parse_iso(c["ts"])).date() == day]
        changed_header = "Changed today"
    elif mode == "morning":
        changes = [c for c in data["changes"] if not last_full or parse_iso(c["ts"]) > last_full]
        changed_header = "Changed since yesterday"
    else:
        changes = [c for c in data["changes"] if not last_post or parse_iso(c["ts"]) > last_post]
        changed_header = "Changed since last hour"

    needs_max = FULL_NEEDS_MAX if full else HOURLY_NEEDS_MAX
    changed_max = FULL_CHANGED_MAX if full else HOURLY_CHANGED_MAX
    if not full and not needs and not changes:
        return None

    lines = []
    total_needs = len([i for i in items if i.get("kind") != "waiting"])
    header = f"Needs you ({total_needs})" if full else f"Needs you ({len(needs)} new)"
    if full or needs:
        lines.append(header)
        for n, it in enumerate(needs[:needs_max], 1):
            lines.append(f"{n}. {clean_line(it.get('title') or '(no title yet)', it['id'])}"
                         f"{id_suffix(it['id'], it.get('link'))}")
        if len(needs) > needs_max:
            lines.append(f"   and {len(needs) - needs_max} more, see the next morning post")
    if changes:
        lines.append(changed_header)
        for c in changes[-changed_max:]:
            lines.append(f"- {clean_line(c['line'], c.get('id'))}{id_suffix(c.get('id'), c.get('link'))}")
    if full and waiting:
        lines.append(f"Waiting on others ({len(waiting)})")
        for it in waiting[:WAITING_MAX]:
            who = f"{it['waiting_on']}, " if it.get("waiting_on") else ""
            lines.append(f"- {clean_line(it.get('title') or it['id'], it['id'])} ({who}since "
                         f"{short_date(it.get('first_seen'))}){id_suffix(it['id'], it.get('link'))}")
    # bookkeeping for the next run
    for it in needs[:needs_max]:
        it["dirty"] = False
        it["last_nudged"] = iso(now)
    return "\n".join(lines)


def cmd_render(args) -> int:
    data = load()
    now = parse_iso(args.now) or now_utc()
    mode = pick_mode(local_now(now), args.mode)
    text = render(data, mode, now)
    if text is None:
        sys.stderr.write(f"ledger render: {mode} run, nothing new - no post.\n")
        return NO_POST
    if not args.dry:
        data["meta"]["last_post_ts"] = iso(now)
        data["meta"]["last_post_mode"] = mode
        if mode in ("morning", "eod"):
            data["meta"]["last_full_post_ts"] = iso(now)
        cutoff = now - timedelta(days=3)
        data["changes"] = [c for c in data["changes"] if (parse_iso(c["ts"]) or now) > cutoff]
        data["items"] = {k: v for k, v in data["items"].items()
                         if v.get("state") != "done" or (parse_iso(v.get("done_at")) or now) > now - timedelta(days=7)}
        save(data)
    sys.stderr.write(f"ledger render: mode={mode}\n")
    print(text)
    return 0


# ── cli ──────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="lib/ledger.py", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("list"); sp.add_argument("--json", action="store_true")
    sp = sub.add_parser("add")
    sp.add_argument("id"); sp.add_argument("--title"); sp.add_argument("--link")
    sp.add_argument("--kind", choices=["needs_you", "waiting"]); sp.add_argument("--waiting-on")
    sp.add_argument("--sig"); sp.add_argument("--bump", action="store_true", help="re-surface in the next hourly post")
    sp = sub.add_parser("done"); sp.add_argument("id"); sp.add_argument("--quiet", action="store_true")
    sp = sub.add_parser("snooze"); sp.add_argument("id"); sp.add_argument("duration"); sp.add_argument("--quiet", action="store_true")
    sp = sub.add_parser("touch"); sp.add_argument("id"); sp.add_argument("--sig", required=True); sp.add_argument("--note")
    sp = sub.add_parser("changed"); sp.add_argument("line"); sp.add_argument("--id"); sp.add_argument("--link")
    sub.add_parser("acks")
    sp = sub.add_parser("render")
    sp.add_argument("--mode", default="auto", choices=["auto", "morning", "hourly", "eod"])
    sp.add_argument("--now", help="ISO timestamp override (tests)")
    sp.add_argument("--dry", action="store_true", help="print without touching bookkeeping")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return {"list": cmd_list, "add": cmd_add, "done": cmd_done, "snooze": cmd_snooze, "touch": cmd_touch,
            "changed": cmd_changed, "acks": cmd_acks, "render": cmd_render}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
