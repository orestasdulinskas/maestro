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
    state pull|push      Sync operational-state files to/from the configured backend
                         (S3 via AWS MCP/boto3/CLI, or local ~/.maestro/).
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
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
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
        return _state_s3(args.action)
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


def _state_s3(action: str) -> int:
    bucket = state_bucket()
    if have_aws_cli():
        return _state_s3_via_cli(action, bucket)
    if have_boto3():
        return _state_s3_via_boto3(action, bucket)
    sys.stderr.write(
        "runner state: backend=s3 but neither `aws` CLI nor `boto3` is available. "
        "Install one, or set MAESTRO_STATE_BACKEND=local.\n"
    )
    return 2


def _state_s3_via_cli(action: str, bucket: str) -> int:
    if action == "pull":
        for f in STATE_FILES:
            shell_out(["aws", "s3", "cp", f"s3://{bucket}/{f}", str(ROOT / f)])
        for d in STATE_DIRS:
            shell_out(["aws", "s3", "sync", f"s3://{bucket}/{d}/", str(ROOT / d / "")])
    elif action == "push":
        for f in STATE_FILES:
            if (ROOT / f).exists():
                shell_out(["aws", "s3", "cp", str(ROOT / f), f"s3://{bucket}/{f}"])
        for d in STATE_DIRS:
            if (ROOT / d).is_dir():
                shell_out(["aws", "s3", "sync", str(ROOT / d / ""), f"s3://{bucket}/{d}/"])
    sys.stderr.write(f"runner state {action} (s3 via CLI): bucket={bucket}\n")
    return 0


def _state_s3_via_boto3(action: str, bucket: str) -> int:
    import boto3
    # boto3's default region resolution checks AWS_DEFAULT_REGION, not AWS_REGION
    # (which is what the AWS CLI uses). Pass it explicitly so the runner works
    # regardless of which env-var convention the runtime sets.
    region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
    s3 = boto3.client("s3", region_name=region)
    if action == "pull":
        for f in STATE_FILES:
            try:
                s3.download_file(bucket, f, str(ROOT / f))
            except Exception:
                pass  # missing object is OK on first run
        for d in STATE_DIRS:
            _s3_sync_down_boto3(s3, bucket, d, ROOT / d)
    elif action == "push":
        for f in STATE_FILES:
            if (ROOT / f).exists():
                s3.upload_file(str(ROOT / f), bucket, f)
        for d in STATE_DIRS:
            if (ROOT / d).is_dir():
                _s3_sync_up_boto3(s3, bucket, d, ROOT / d)
    sys.stderr.write(f"runner state {action} (s3 via boto3): bucket={bucket}\n")
    return 0


def _s3_sync_down_boto3(s3, bucket: str, prefix: str, local_dir: Path) -> None:
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=f"{prefix}/"):
        for obj in page.get("Contents", []) or []:
            key = obj["Key"]
            dest = ROOT / key
            dest.parent.mkdir(parents=True, exist_ok=True)
            s3.download_file(bucket, key, str(dest))


def _s3_sync_up_boto3(s3, bucket: str, prefix: str, local_dir: Path) -> None:
    for p in local_dir.rglob("*"):
        if p.is_file():
            rel = p.relative_to(ROOT).as_posix()
            s3.upload_file(str(p), bucket, rel)


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

    if have_aws_cli():
        return _secrets_via_cli(names, args.shell)
    if have_boto3():
        return _secrets_via_boto3(names, args.shell)
    sys.stderr.write("runner secrets: neither AWS CLI nor boto3 available.\n")
    return 2


def _secrets_via_cli(names: list[str], shell_format: bool) -> int:
    for name in names:
        r = shell_out(["aws", "secretsmanager", "get-secret-value",
                       "--secret-id", name, "--query", "SecretString",
                       "--output", "text"])
        if r.returncode != 0:
            sys.stderr.write(f"runner secrets: failed to fetch {name}: {r.stderr.strip()}\n")
            continue
        _emit_secret(name, r.stdout.strip(), shell_format)
    return 0


def _secrets_via_boto3(names: list[str], shell_format: bool) -> int:
    import boto3
    region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
    sm = boto3.client("secretsmanager", region_name=region)
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
