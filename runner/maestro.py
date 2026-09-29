#!/usr/bin/env python3
"""Maestro runner — provider-agnostic orchestration CLI.

Subcommands the agent invokes from its prompt (works on Claude Code, Codex CLI,
opencode, deep-agents, and Anthropic Remote Routines):

    prepare              Emit the Run Context block for the prompt (state + memory recall).
    finalize             Persist run-complete state and roll daily/weekly counters.
    write <path> <body>  Path-validated write; refuses protected paths.
    mattermost           Deliver an urgent Mattermost line (cap-enforced).
    send-email           Stage an email payload; the runtime delivers it via its
                         gmail-send capability. Recipient is locked to config.json.
    preflight            Check the run window (local time) and the AWS identity.
    start                Record run-start in state.json (pairs with finalize).
    state pull|push      Sync operational-state files to/from the configured backend
                         (S3 via boto3/CLI, or local ~/.maestro/).
    secrets pull         Fetch maestro/* secrets from AWS Secrets Manager into env.
    auth                 Print which subsystems are configured (S3/secrets/Mattermost).

All subcommands are idempotent and exit non-zero on validation failure so that
upstream automation can branch on the exit code without parsing stdout.

The runner never imports cognee at top level; the memory subsystem is shelled out
to lib/memory_cognee.py inside its dedicated venv. The runner never makes the
actual gmail-send HTTP call — the runtime's gmail-send MCP tool does that. The
runner's role is to validate, persist, and gate side effects.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

# ── Paths ────────────────────────────────────────────────────────

ROOT = Path(__file__).resolve().parent.parent
LIB = ROOT / "lib"
STATE_FILE = ROOT / "state.json"
CONFIG_FILE = ROOT / "config.json"
TMP_DIR = ROOT / ".tmp"
OUTGOING_EMAIL = TMP_DIR / "maestro-outgoing-email.json"

# Files / directories the agent may write directly. Anything else routes through
# `runner write` or is refused.
WRITABLE_PREFIXES = ("daily/", "knowledge/", ".tmp/")
WRITABLE_FILES = ("briefing.md", "feedback.md", "state.json")
PROTECTED_PREFIXES = (
    ".claude/", "prompts/", "lib/", ".secrets/", "providers/", "runner/",
    "mcp/", "scheduling/", "fixtures/",
)
PROTECTED_FILES = (
    "AGENTS.md", "CLAUDE.md", "config.json", "config.example.json",
    "run.sh", "mcp-servers.json", "README.md", "ARCHITECTURE.md", "ROADMAP.md",
    "LICENSE", ".gitignore", ".env",
)
PROTECTED_GLOB_PREFIXES = (".env",)

# Sanity cap on Mattermost lines per run. Not the design-level cap (the prompts
# say "no cap; trust the agent's judgment" under the form-factor routing rule);
# this is just a runaway-loop protection. The agent should never approach it
# under normal operation. Override per-run by setting MAESTRO_MATTERMOST_CAP.
MATTERMOST_SANITY_CAP = 100


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Generic helpers ──────────────────────────────────────────────


def load_config() -> dict:
    if not CONFIG_FILE.exists():
        sys.stderr.write(
            f"runner: {CONFIG_FILE} not found. Copy config.example.json to config.json "
            f"and set email.recipient before running.\n"
        )
        sys.exit(2)
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        sys.stderr.write(f"runner: config.json is invalid JSON: {e}\n")
        sys.exit(2)


def relative_to_root(path: Path) -> str | None:
    try:
        return path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return None


def is_writable(rel_path: str) -> tuple[bool, str]:
    """Returns (allowed, reason). Mirrors providers/claude-code/hooks logic."""
    if any(rel_path.startswith(p) for p in WRITABLE_PREFIXES):
        return True, ""
    if rel_path in WRITABLE_FILES:
        return True, ""
    if any(rel_path.startswith(p) for p in PROTECTED_PREFIXES):
        return False, f"protected prefix (one of {PROTECTED_PREFIXES})"
    if rel_path in PROTECTED_FILES:
        return False, "protected file"
    if any(rel_path.startswith(p) for p in PROTECTED_GLOB_PREFIXES):
        return False, "env/secret file"
    # Inside repo root but not explicitly listed: allow (matches existing hook
    # behavior; user can tighten by adding to PROTECTED_*).
    return True, ""


def shell_out(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    """Run a subprocess, capturing output, never raising on non-zero. Caller checks."""
    return subprocess.run(cmd, capture_output=True, text=True, **kwargs)


# ── AWS credentials ─────────────────────────────────────────────
#
# Anthropic cloud sandboxes preset AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY to
# a placeholder ("proxy-injected") and do not pass AWS_* credential variables
# through from the environment config. The routine environment therefore stores
# the maestro-routine key as MAESTRO_AK / MAESTRO_SK, and the runner hands them
# to boto3 / the AWS CLI here, in code. Prompts never handle credentials and
# never need to export or print them.
#
# When MAESTRO_AK / MAESTRO_SK are unset (local runs), the default AWS
# credential chain applies unchanged.


def aws_region() -> str | None:
    # boto3 reads AWS_DEFAULT_REGION, the AWS CLI reads AWS_REGION; accept both.
    return os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")


def _maestro_aws_creds() -> dict | None:
    ak = os.environ.get("MAESTRO_AK")
    sk = os.environ.get("MAESTRO_SK")
    if ak and sk:
        return {"aws_access_key_id": ak, "aws_secret_access_key": sk}
    return None


def aws_client(service: str):
    import boto3
    creds = _maestro_aws_creds() or {}
    return boto3.session.Session(region_name=aws_region(), **creds).client(service)


def aws_cli_env() -> dict:
    """Environment for `aws` CLI subprocesses, with the maestro key applied."""
    env = os.environ.copy()
    creds = _maestro_aws_creds()
    if creds:
        env["AWS_ACCESS_KEY_ID"] = creds["aws_access_key_id"]
        env["AWS_SECRET_ACCESS_KEY"] = creds["aws_secret_access_key"]
        env.pop("AWS_SESSION_TOKEN", None)
        env.pop("AWS_PROFILE", None)
    region = aws_region()
    if region:
        env.setdefault("AWS_DEFAULT_REGION", region)
    return env


# ── Subcommand: prepare ─────────────────────────────────────────


def cmd_prepare(args: argparse.Namespace) -> int:
    """Emit the Run Context block. Wraps lib/state.py inject-context and (optionally)
    runs memory recall via lib/memory_cognee.py.
    """
    state_py = LIB / "state.py"
    if not state_py.exists():
        sys.stderr.write(f"runner: missing {state_py}\n")
        return 2

    cmd = [sys.executable, str(state_py), "inject-context", args.run_type,
           "--interval", str(args.interval)]
    result = shell_out(cmd)
    sys.stdout.write(result.stdout)
    if result.returncode != 0:
        sys.stderr.write(result.stderr)
        return result.returncode

    # Memory recall (cognee) — best-effort. Skip silently if venv or secrets missing.
    if not args.skip_memory:
        memory_py = LIB / "memory_cognee.py"
        venv_py = LIB / "cognee-venv" / "Scripts" / "python.exe"
        if not venv_py.exists():
            venv_py = LIB / "cognee-venv" / "bin" / "python3"
        if memory_py.exists() and venv_py.exists():
            mem = shell_out([str(venv_py), str(memory_py), "recall", "--top-k", "5"])
            if mem.returncode == 0 and mem.stdout.strip():
                sys.stdout.write("\n")
                sys.stdout.write(mem.stdout)
            elif mem.returncode != 0:
                sys.stderr.write(f"runner: memory recall failed (continuing): {mem.stderr[:200]}\n")
    return 0


# ── Subcommand: preflight ───────────────────────────────────────


PREFLIGHT_SKIP = 10  # exit code: outside the run window, stop without doing anything


def cmd_preflight(args: argparse.Namespace) -> int:
    """Gate a scheduled run before it touches anything.

    1. --window HH-HH: skip (exit 10) unless the local hour in --tz is inside
       the inclusive window. Lets a UTC cron cover both DST offsets while the
       run itself keeps a fixed local window.
    2. AWS identity (s3 backend only): STS GetCallerIdentity must succeed and,
       if --expect-identity is given, the ARN must end with it.
    """
    if args.window:
        m = re.fullmatch(r"(\d{1,2})-(\d{1,2})", args.window)
        if not m:
            sys.stderr.write(f"runner preflight: --window must look like 08-18, got '{args.window}'.\n")
            return 2
        start_h, end_h = int(m.group(1)), int(m.group(2))
        from zoneinfo import ZoneInfo
        local_now = datetime.now(ZoneInfo(args.tz))
        if not start_h <= local_now.hour <= end_h:
            sys.stdout.write(
                f"preflight: SKIP - {local_now:%H:%M} {args.tz} is outside the "
                f"{start_h:02d}:00-{end_h:02d}:59 window. Stop the run now.\n"
            )
            return PREFLIGHT_SKIP
        sys.stdout.write(f"preflight: window OK ({local_now:%a %H:%M} {args.tz}).\n")

    if state_backend() == "s3":
        if not have_boto3():
            sys.stderr.write("runner preflight: boto3 is not installed.\n")
            return 2
        try:
            arn = aws_client("sts").get_caller_identity()["Arn"]
        except Exception as e:
            sys.stderr.write(f"runner preflight: AWS identity check failed: {type(e).__name__}: {e}\n")
            return 3
        if args.expect_identity and not arn.endswith(args.expect_identity):
            sys.stderr.write(
                f"runner preflight: AWS identity is {arn}, expected one ending in "
                f"'{args.expect_identity}'.\n"
            )
            return 3
        sys.stdout.write(f"preflight: AWS OK ({arn}).\n")
    return 0


# ── Subcommand: start ───────────────────────────────────────────


def cmd_start(args: argparse.Namespace) -> int:
    """Record run-start (started_at, prompt hash, run counter) in state.json.

    Run after `state pull` so it updates the pulled file, and pair it with
    `finalize` before `state push` so both timestamps reach the backend.
    """
    prompt_file = ROOT / "prompts" / ("end-of-day.md" if args.run_type == "eod" else "heartbeat.md")
    prompt_hash = "none"
    if prompt_file.exists():
        prompt_hash = hashlib.sha256(prompt_file.read_bytes()).hexdigest()[:8]
    result = shell_out([sys.executable, str(LIB / "state.py"), "run-start", args.run_type,
                        "--prompt-hash", prompt_hash])
    if result.returncode != 0:
        sys.stderr.write(result.stderr)
        return result.returncode
    sys.stdout.write(f"start: {args.run_type} run started [prompt:{prompt_hash}]\n")
    return 0


# ── Subcommand: finalize ────────────────────────────────────────


def cmd_finalize(args: argparse.Namespace) -> int:
    """Mark run-complete and persist any deferred metric increments."""
    state_py = LIB / "state.py"
    cmd = [sys.executable, str(state_py), "run-complete", args.run_type,
           "--exit-code", str(args.exit_code)]
    result = shell_out(cmd)
    if result.returncode != 0:
        sys.stderr.write(result.stderr)
        return result.returncode
    sys.stdout.write(f"finalize: {args.run_type} run complete (exit code {args.exit_code}).\n")

    # Re-index memory in the background after a successful heartbeat (skip on EOD
    # since EOD typically follows a heartbeat).
    if not args.skip_memory and args.run_type == "heartbeat" and args.exit_code == 0:
        memory_py = LIB / "memory_cognee.py"
        venv_py = LIB / "cognee-venv" / "Scripts" / "python.exe"
        if not venv_py.exists():
            venv_py = LIB / "cognee-venv" / "bin" / "python3"
        if memory_py.exists() and venv_py.exists():
            subprocess.Popen(
                [str(venv_py), str(memory_py), "index"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )  # fire-and-forget; runner does not wait
    return 0


# ── Subcommand: write ───────────────────────────────────────────


def cmd_write(args: argparse.Namespace) -> int:
    """Path-validated write. Used by runtimes without a write hook."""
    target = Path(args.path)
    if not target.is_absolute():
        target = ROOT / target
    rel = relative_to_root(target)
    if rel is None:
        sys.stderr.write(f"runner write: {args.path} is outside the project root.\n")
        return 2
    allowed, reason = is_writable(rel)
    if not allowed:
        sys.stderr.write(f"runner write: refusing to write '{rel}' — {reason}.\n")
        return 2

    # Body: from --body, --body-file, or stdin
    if args.body is not None:
        body = args.body
    elif args.body_file:
        body = Path(args.body_file).read_text(encoding="utf-8")
    else:
        body = sys.stdin.read()

    target.parent.mkdir(parents=True, exist_ok=True)
    if args.append:
        with open(target, "a", encoding="utf-8") as f:
            f.write(body)
    else:
        # Atomic-ish: write to temp file then replace.
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(body, encoding="utf-8")
        os.replace(tmp, target)
    sys.stderr.write(f"runner write: ok ({rel}, {len(body)} chars).\n")
    return 0


# ── Subcommand: mattermost ──────────────────────────────────────


def cmd_mattermost(args: argparse.Namespace) -> int:
    """Stage and deliver an urgent Mattermost line.

    Default behavior (since 2026-05): the line is appended to
    `.tmp/mattermost_urgent.txt` AND delivered inline via `lib/mattermost.py`.
    On successful delivery the marker file is cleaned up by `send-file`; on
    failure the unsent line is preserved so an external post-run hook (e.g.,
    the legacy `providers/claude-code/run.sh` flow) can retry.

    Pass `--stage-only` (or `MAESTRO_MATTERMOST_STAGE_ONLY=1`) to skip the
    inline delivery — useful only in flows where another orchestrator handles
    the actual API call. The `--deliver` flag is kept as a no-op for backward
    compatibility (delivery is now the default).
    """
    line = (args.urgent or "").strip()
    if not line:
        sys.stderr.write("runner mattermost: --urgent message is empty; refusing.\n")
        return 2
    # Sanity-only hard cap (anti-runaway). The prompts give a soft target of
    # "short and scannable" but allow longer when context matters. Anything
    # over this threshold is a sign the content should be a Gmail draft, not
    # a Mattermost line. Truncation here is the safety net, not the design.
    HARD_CAP_CHARS = 1500
    if len(line) > HARD_CAP_CHARS:
        line = line[:HARD_CAP_CHARS - 3] + "..."

    TMP_DIR.mkdir(exist_ok=True)
    marker = TMP_DIR / "mattermost_urgent.txt"

    # Sanity check only. The prompts handle "what's worth posting" — the runner
    # shouldn't override the agent's judgment. This is a runaway-loop guard.
    cap = int(os.environ.get("MAESTRO_MATTERMOST_CAP", MATTERMOST_SANITY_CAP))
    existing = []
    if marker.exists():
        existing = [l for l in marker.read_text(encoding="utf-8").splitlines() if l.strip()]
    if len(existing) >= cap:
        sys.stderr.write(
            f"runner mattermost: sanity cap reached ({cap} lines this run); refusing additional line. "
            f"If this is legitimate, raise MAESTRO_MATTERMOST_CAP.\n"
        )
        return 3

    # Append to marker file
    with open(marker, "a", encoding="utf-8") as f:
        f.write(line + "\n")
    sys.stderr.write(f"runner mattermost: posting line #{len(existing) + 1}.\n")

    # Deliver inline by default. Skip only if explicitly opted out (rare —
    # for flows where another orchestrator handles delivery post-run).
    stage_only = args.stage_only or os.environ.get("MAESTRO_MATTERMOST_STAGE_ONLY") == "1"
    if stage_only:
        sys.stderr.write("runner mattermost: --stage-only set; skipping inline delivery.\n")
        return 0

    mattermost_py = LIB / "mattermost.py"
    if not mattermost_py.exists():
        sys.stderr.write("runner mattermost: lib/mattermost.py missing; staged only.\n")
        return 0
    result = shell_out([sys.executable, str(mattermost_py), "send-file", str(marker)])
    sys.stdout.write(result.stdout)

    # Always clear the marker after an inline-delivery attempt. On success,
    # send-file already unlinks it; on failure, send-file rewrites it with
    # the unsent lines (designed for the legacy run.sh post-run retry path).
    # For inline-delivery, that preserve-on-failure behavior actively creates
    # duplicates: the agent's retry calls `runner mattermost --urgent "X"`
    # again, which appends "X" to the marker that already has "X" preserved,
    # so the next send-file call posts both. Clear the marker here so each
    # `runner mattermost` invocation is atomic w.r.t. the marker file.
    if marker.exists():
        marker.unlink()

    if result.returncode != 0:
        sys.stderr.write(result.stderr)
        return result.returncode
    return 0


# ── Subcommand: send-email ──────────────────────────────────────


def cmd_send_email(args: argparse.Namespace) -> int:
    """Stage an outgoing email payload with the recipient locked from config.json.

    The runner does NOT make the actual SMTP/HTTP call — that's the runtime's
    gmail-send MCP tool. The runner's value is the recipient guarantee: it reads
    config.json once and writes a JSON payload the agent then passes verbatim to
    the gmail-send call. If the agent tries to pass a different recipient, the
    prompt-level rule (AGENTS.md) and the staged JSON disagree, which surfaces in
    audit logs.

    Output (stdout): JSON {recipient, subject, body} for the agent to consume.
    Side effect: also writes the same JSON to .tmp/maestro-outgoing-email.json
    so it's recoverable if the agent's MCP call fails.
    """
    config = load_config()
    recipient = (config.get("email") or {}).get("recipient")
    if not recipient or "@" not in recipient:
        sys.stderr.write(
            "runner send-email: config.json > email.recipient missing or malformed. "
            "Set it before running.\n"
        )
        return 2

    subject = args.subject or ""
    if args.body is not None:
        body = args.body
    elif args.body_file:
        body = Path(args.body_file).read_text(encoding="utf-8")
    else:
        body = sys.stdin.read()
    if not subject.strip() or not body.strip():
        sys.stderr.write("runner send-email: subject and body must both be non-empty.\n")
        return 2

    payload = {
        "recipient": recipient,
        "subject": subject,
        "body": body,
        "subject_prefix": (config.get("email") or {}).get("subject_prefix", ""),
        "staged_at": now_iso(),
    }

    TMP_DIR.mkdir(exist_ok=True)
    OUTGOING_EMAIL.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    if os.environ.get("MAESTRO_DRY_SEND") == "1":
        sys.stderr.write(
            f"runner send-email: DRY mode (MAESTRO_DRY_SEND=1) — staged to "
            f"{OUTGOING_EMAIL}. The runtime's gmail-send would have sent this.\n"
        )
    else:
        sys.stderr.write(
            f"runner send-email: payload validated and staged to {OUTGOING_EMAIL}. "
            f"Pass these exact values to your runtime's gmail-send capability.\n"
        )

    sys.stdout.write(json.dumps(payload, indent=2))
    sys.stdout.write("\n")
    return 0


# ── Subcommand: state pull/push ─────────────────────────────────


# Files synced to/from the state backend. Mirror the local layout.
# config.json is config (not state) but routine clones don't have it — sync via
# the same mechanism so the cloud agent can read email.recipient. If you edit
# config.json locally, push it to S3 manually or via `runner state push`.
STATE_FILES = (
    "briefing.md",
    "feedback.md",
    "state.json",
    "config.json",
)
STATE_DIRS = (
    "daily",
    "knowledge",
)


def state_backend() -> str:
    return os.environ.get("MAESTRO_STATE_BACKEND", "s3")


def state_bucket() -> str:
    bucket = os.environ.get("MAESTRO_STATE_BUCKET")
    if not bucket:
        sys.stderr.write(
            "runner state: MAESTRO_STATE_BUCKET env var is unset and backend is s3. "
            "Either export it (e.g. maestro-state-yourname) or set "
            "MAESTRO_STATE_BACKEND=local.\n"
        )
        sys.exit(2)
    return bucket


def have_boto3() -> bool:
    try:
        import boto3  # noqa: F401
        return True
    except ImportError:
        return False


def have_aws_cli() -> bool:
    return shutil.which("aws") is not None


def cmd_state(args: argparse.Namespace) -> int:
    """state pull|push — sync operational files to/from S3 (default) or local."""
    backend = state_backend()

    if backend == "local":
        return _state_local(args.action)
    if backend == "s3":
        return _state_s3(args.action, args.daily_days)
    sys.stderr.write(f"runner state: unknown backend '{backend}'. Use 's3' or 'local'.\n")
    return 2


def _state_local(action: str) -> int:
    local_root = Path(os.environ.get("MAESTRO_LOCAL_STATE", str(Path.home() / ".maestro")))
    local_root.mkdir(parents=True, exist_ok=True)
    if action == "pull":
        for f in STATE_FILES:
            src = local_root / f
            if src.exists():
                shutil.copy2(src, ROOT / f)
        for d in STATE_DIRS:
            src = local_root / d
            if src.exists():
                shutil.copytree(src, ROOT / d, dirs_exist_ok=True)
        sys.stderr.write(f"runner state pull (local): copied from {local_root}\n")
    elif action == "push":
        for f in STATE_FILES:
            src = ROOT / f
            if src.exists():
                shutil.copy2(src, local_root / f)
        for d in STATE_DIRS:
            src = ROOT / d
            if src.exists():
                shutil.copytree(src, local_root / d, dirs_exist_ok=True)
        sys.stderr.write(f"runner state push (local): copied to {local_root}\n")
    return 0


def _state_s3(action: str, daily_days: int) -> int:
    bucket = state_bucket()
    if have_boto3():
        return _state_s3_via_boto3(action, bucket, daily_days)
    if have_aws_cli():
        return _state_s3_via_cli(action, bucket)
    sys.stderr.write(
        "runner state: backend=s3 but neither `boto3` nor the `aws` CLI is available. "
        "Install one, or set MAESTRO_STATE_BACKEND=local.\n"
    )
    return 2


def _state_s3_via_cli(action: str, bucket: str) -> int:
    """Fallback full sync. `aws s3 sync` already skips unchanged files."""
    env = aws_cli_env()
    failed = False
    if action == "pull":
        for f in STATE_FILES:
            shell_out(["aws", "s3", "cp", f"s3://{bucket}/{f}", str(ROOT / f)], env=env)
        for d in STATE_DIRS:
            r = shell_out(["aws", "s3", "sync", f"s3://{bucket}/{d}/", str(ROOT / d / "")], env=env)
            failed |= r.returncode != 0
    elif action == "push":
        for f in STATE_FILES:
            if (ROOT / f).exists():
                r = shell_out(["aws", "s3", "cp", str(ROOT / f), f"s3://{bucket}/{f}"], env=env)
                failed |= r.returncode != 0
        for d in STATE_DIRS:
            if (ROOT / d).is_dir():
                r = shell_out(["aws", "s3", "sync", str(ROOT / d / ""), f"s3://{bucket}/{d}/"], env=env)
                failed |= r.returncode != 0
    sys.stderr.write(f"runner state {action} (s3 via CLI): bucket={bucket}{' FAILED' if failed else ''}\n")
    return 1 if failed else 0


# Records the MD5 of every file as pulled, so push uploads only what changed.
STATE_MANIFEST = TMP_DIR / "state-manifest.json"
DAILY_LOG_RE = re.compile(r"^daily/(\d{4}-\d{2}-\d{2})\.md$")


def _md5(path: Path) -> str:
    return hashlib.md5(path.read_bytes()).hexdigest()


def _is_old_daily_log(key: str, cutoff: date | None) -> bool:
    if cutoff is None:
        return False
    m = DAILY_LOG_RE.match(key)
    return bool(m) and date.fromisoformat(m.group(1)) < cutoff


def _state_s3_via_boto3(action: str, bucket: str, daily_days: int) -> int:
    from botocore.exceptions import ClientError
    s3 = aws_client("s3")

    if action == "pull":
        # daily/YYYY-MM-DD.md older than `daily_days` stays in S3 (0 = pull all).
        cutoff = date.today() - timedelta(days=daily_days) if daily_days > 0 else None
        manifest: dict[str, str] = {}
        keys: list[str] = []
        for f in STATE_FILES:
            keys.append(f)
        paginator = s3.get_paginator("list_objects_v2")
        skipped = 0
        for d in STATE_DIRS:
            for page in paginator.paginate(Bucket=bucket, Prefix=f"{d}/"):
                for obj in page.get("Contents", []) or []:
                    if _is_old_daily_log(obj["Key"], cutoff):
                        skipped += 1
                    else:
                        keys.append(obj["Key"])
        for key in keys:
            dest = ROOT / key
            dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                s3.download_file(bucket, key, str(dest))
            except ClientError as e:
                if e.response.get("Error", {}).get("Code") in ("404", "NoSuchKey"):
                    continue  # top-level file not created yet (first run)
                sys.stderr.write(f"runner state pull: failed to download {key}: {e}\n")
                return 1
            manifest[key] = _md5(dest)
        TMP_DIR.mkdir(exist_ok=True)
        STATE_MANIFEST.write_text(json.dumps(manifest), encoding="utf-8")
        sys.stderr.write(
            f"runner state pull (s3 via boto3): bucket={bucket} files={len(manifest)} "
            f"older_daily_logs_left_in_s3={skipped}\n"
        )
        return 0

    # push: upload files that are new or differ from what was pulled. Without a
    # manifest (no pull in this working tree) everything is uploaded.
    manifest = {}
    if STATE_MANIFEST.exists():
        manifest = json.loads(STATE_MANIFEST.read_text(encoding="utf-8"))
    candidates = [ROOT / f for f in STATE_FILES if (ROOT / f).exists()]
    for d in STATE_DIRS:
        if (ROOT / d).is_dir():
            candidates += [p for p in (ROOT / d).rglob("*") if p.is_file()]
    uploaded = []
    for p in candidates:
        key = p.relative_to(ROOT).as_posix()
        if manifest.get(key) == _md5(p):
            continue
        s3.upload_file(str(p), bucket, key)
        uploaded.append(key)
    sys.stderr.write(
        f"runner state push (s3 via boto3): bucket={bucket} uploaded={len(uploaded)}"
        f"{' (' + ', '.join(uploaded[:10]) + (' ...' if len(uploaded) > 10 else '') + ')' if uploaded else ''}\n"
    )
    return 0


# ── Subcommand: secrets pull ────────────────────────────────────


def cmd_secrets(args: argparse.Namespace) -> int:
    """Fetch maestro/* secrets from AWS Secrets Manager. Print KEY=VALUE lines
    on stdout so the caller can `eval $(runner secrets pull --shell)`.

    Skipped silently if MAESTRO_STATE_BACKEND=local — local runs use .env.
    """
    if state_backend() == "local":
        sys.stderr.write("runner secrets: backend=local; skipping AWS fetch. Use .env instead.\n")
        return 0

    prefix = os.environ.get("MAESTRO_SECRETS_PREFIX", "maestro/")
    names = args.names or [f"{prefix}mattermost"]

    if have_boto3():
        return _secrets_via_boto3(names, args.shell)
    if have_aws_cli():
        return _secrets_via_cli(names, args.shell)
    sys.stderr.write("runner secrets: neither AWS CLI nor boto3 available.\n")
    return 2


def _secrets_via_cli(names: list[str], shell_format: bool) -> int:
    for name in names:
        r = shell_out(["aws", "secretsmanager", "get-secret-value",
                       "--secret-id", name, "--query", "SecretString",
                       "--output", "text"], env=aws_cli_env())
        if r.returncode != 0:
            sys.stderr.write(f"runner secrets: failed to fetch {name}: {r.stderr.strip()}\n")
            continue
        _emit_secret(name, r.stdout.strip(), shell_format)
    return 0


def _secrets_via_boto3(names: list[str], shell_format: bool) -> int:
    sm = aws_client("secretsmanager")
    for name in names:
        try:
            resp = sm.get_secret_value(SecretId=name)
        except Exception as e:
            sys.stderr.write(f"runner secrets: failed to fetch {name}: {e}\n")
            continue
        _emit_secret(name, resp.get("SecretString", ""), shell_format)
    return 0


def _emit_secret(name: str, raw: str, shell_format: bool) -> None:
    """A secret can be a JSON blob (multiple KEY=VALUE) or a flat string."""
    try:
        as_json = json.loads(raw)
        if isinstance(as_json, dict):
            for k, v in as_json.items():
                if shell_format:
                    sys.stdout.write(f'export {k}={json.dumps(str(v))}\n')
                else:
                    sys.stdout.write(f"{k}={v}\n")
            return
    except json.JSONDecodeError:
        pass
    # Treat as a single flat value, key derived from the secret name's last segment.
    key = name.rsplit("/", 1)[-1].upper()
    if shell_format:
        sys.stdout.write(f'export {key}={json.dumps(raw)}\n')
    else:
        sys.stdout.write(f"{key}={raw}\n")


# ── Subcommand: auth ────────────────────────────────────────────


def cmd_auth(args: argparse.Namespace) -> int:
    """Print which subsystems are configured. Used by check-auth.md prompt."""
    out = ["=== Maestro Runner Auth Probe ===", f"Time: {now_iso()}", ""]
    out.append(f"Working tree:       {ROOT}")
    out.append(f"State backend:      {state_backend()}")
    if state_backend() == "s3":
        out.append(f"State bucket:       {os.environ.get('MAESTRO_STATE_BUCKET', 'NOT SET')}")
        out.append(f"boto3 available:    {'yes' if have_boto3() else 'no'}")
        out.append(f"aws CLI available:  {'yes' if have_aws_cli() else 'no'}")
        out.append(f"AWS credentials:    {'MAESTRO_AK/MAESTRO_SK' if _maestro_aws_creds() else 'default chain'}")
    out.append(f"config.json:        {'present' if CONFIG_FILE.exists() else 'MISSING'}")
    if CONFIG_FILE.exists():
        c = load_config()
        recip = (c.get("email") or {}).get("recipient", "")
        masked = recip[:3] + "***@" + recip.split("@")[-1] if "@" in recip else "MALFORMED"
        out.append(f"email.recipient:    {masked}")
    out.append(f"state.json:         {'present' if STATE_FILE.exists() else 'absent (first run)'}")
    cognee_venv = LIB / "cognee-venv"
    out.append(f"Cognee venv:        {'present' if cognee_venv.is_dir() else 'absent (memory disabled)'}")
    mattermost_env = "MATTERMOST_BOT_TOKEN" in os.environ
    out.append(f"Mattermost env:     {'configured' if mattermost_env else 'absent (mattermost disabled)'}")
    out.append("")
    sys.stdout.write("\n".join(out))
    sys.stdout.write("\n")
    return 0


# ── argparse wiring ─────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="runner/maestro.py", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("prepare", help="Emit Run Context block for the prompt")
    sp.add_argument("run_type", choices=["heartbeat", "eod"])
    sp.add_argument("--interval", type=int, default=60, help="Minutes between runs (catch-up detection)")
    sp.add_argument("--skip-memory", action="store_true")

    sp = sub.add_parser("preflight", help="Check run window and AWS identity before a scheduled run")
    sp.add_argument("--window", help="Inclusive local-hour window, e.g. 08-18; exit 10 outside it")
    sp.add_argument("--tz", default="Europe/Vilnius", help="Time zone for --window")
    sp.add_argument("--expect-identity", help="Required suffix of the AWS caller ARN, e.g. :user/maestro-routine")

    sp = sub.add_parser("start", help="Record run-start in state.json")
    sp.add_argument("run_type", choices=["heartbeat", "eod"])

    sp = sub.add_parser("finalize", help="Mark run-complete and update metrics")
    sp.add_argument("run_type", choices=["heartbeat", "eod"])
    sp.add_argument("--exit-code", type=int, default=0)
    sp.add_argument("--skip-memory", action="store_true")

    sp = sub.add_parser("write", help="Path-validated write of operational state")
    sp.add_argument("path")
    sp.add_argument("--body")
    sp.add_argument("--body-file")
    sp.add_argument("--append", action="store_true")

    sp = sub.add_parser("mattermost", help="Deliver an urgent Mattermost line (inline by default)")
    sp.add_argument("--urgent", required=True, help="Mattermost message (sanity cap ~1500 chars)")
    sp.add_argument("--stage-only", action="store_true",
                    help="Stage to .tmp/ only; skip inline delivery (legacy flow)")
    sp.add_argument("--deliver", action="store_true",
                    help="(Deprecated; inline delivery is now the default) Kept for backward compatibility.")

    sp = sub.add_parser("send-email", help="Stage outgoing email with recipient locked from config.json")
    sp.add_argument("--subject", required=True)
    sp.add_argument("--body")
    sp.add_argument("--body-file")

    sp = sub.add_parser("state", help="Sync operational state to/from backend (s3 or local)")
    sp.add_argument("action", choices=["pull", "push"])
    sp.add_argument("--daily-days", type=int, default=int(os.environ.get("MAESTRO_DAILY_DAYS", "14")),
                    help="pull: only daily/YYYY-MM-DD.md from the last N days (0 = all). Default 14.")

    sp = sub.add_parser("secrets", help="Fetch maestro/* secrets from AWS Secrets Manager")
    sub_secrets = sp.add_subparsers(dest="secrets_cmd", required=True)
    sp_pull = sub_secrets.add_parser("pull")
    sp_pull.add_argument("--names", nargs="+", help="Secret IDs (default: maestro/mattermost)")
    sp_pull.add_argument("--shell", action="store_true",
                         help="Emit `export KEY=VALUE` lines for shell eval")

    sub.add_parser("auth", help="Print which subsystems are configured")
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    dispatch = {
        "prepare": cmd_prepare,
        "preflight": cmd_preflight,
        "start": cmd_start,
        "finalize": cmd_finalize,
        "write": cmd_write,
        "mattermost": cmd_mattermost,
        "send-email": cmd_send_email,
        "state": cmd_state,
        "auth": cmd_auth,
    }
    if args.cmd == "secrets":
        # Two-level subparser: secrets pull
        return cmd_secrets(args)
    fn = dispatch.get(args.cmd)
    if fn is None:
        parser.print_help()
        return 1
    return fn(args)


if __name__ == "__main__":
    sys.exit(main())
