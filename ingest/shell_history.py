#!/opt/homebrew/bin/python3
"""Shell history ingest — Ellie owns infrastructure command capture.

Reads ~/.zsh_history incrementally (tracks last-seen byte offset), aggressively
redacts secret-bearing lines, groups surviving commands into 5-minute work
buckets, writes one schema-compliant raw record per bucket to raw/inbox/.

LLM-free. No Ellie agent dispatch needed for ingestion — that happens later
in the canonical pipeline if there's signal worth promoting.

Schema: ~/server/knowledge/schemas/raw.schema.json

Usage:
  ingest_shell_history.py [--dry-run] [--max-buckets 100]
"""

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

# ── Config ──────────────────────────────────────────────
HISTORY_FILE = Path.home() / ".zsh_history"
STATE_FILE = Path("/Users/chrischo/.openclaw/workspace-ellie/.brain_state/shell_history_offset.json")
INBOX_DIR = Path("/Users/chrischo/server/knowledge/raw/inbox")
FAILURE_LOG = Path("/Users/chrischo/.openclaw/workspace-ellie/logs/shell-ingest-failures.jsonl")
BUCKET_SECONDS = 5 * 60  # 5-minute work buckets

# Drop entire line on these patterns — secret-bearing
SECRET_DROP_PATTERNS = [
    re.compile(r"\b(password|passwd|pwd|secret|api[_-]?key|api[_-]?token|bearer|authorization)\b", re.I),
    re.compile(r"\bsk-[a-zA-Z0-9_-]{20,}"),                       # API keys
    re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{36,}"),                 # GitHub tokens
    re.compile(r"\bxox[bp]-[A-Za-z0-9-]{10,}"),                   # Slack tokens
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),                          # AWS keys
    re.compile(r"\b[A-Za-z0-9+/]{40,}={0,2}\b"),                  # Long base64 (catches most embedded tokens)
    re.compile(r"@chris980113|chris980113@", re.I),               # Personal password leak guard
    re.compile(r"\bexport\s+\w+="),                                # env var assignments often have secrets
    re.compile(r"\b(set|setenv)\s+\w+="),                          # ditto
]

# Drop low-signal noise (interactive shell mistakes, ls/cd/clear)
NOISE_PATTERNS = [
    re.compile(r"^\s*(ls|cd|pwd|clear|exit|history|echo|cat|less|more|man|which|type)\b"),
    re.compile(r"^\s*\.\.+\s*$"),                                  # ..  ...
    re.compile(r"^\s*$"),                                          # blank
    re.compile(r"^\s*#"),                                          # comments
]


def load_offset() -> int:
    if not STATE_FILE.exists():
        return 0
    try:
        return int(json.loads(STATE_FILE.read_text()).get("offset", 0))
    except Exception:
        return 0


def save_offset(offset: int) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps({"offset": offset, "last_run": datetime.now().isoformat()}))


def log_failure(reason: str) -> None:
    try:
        FAILURE_LOG.parent.mkdir(parents=True, exist_ok=True)
        with FAILURE_LOG.open("a") as f:
            f.write(json.dumps({"timestamp": datetime.now().isoformat(), "reason": reason[:300]}) + "\n")
    except Exception:
        pass


def is_secret(line: str) -> bool:
    return any(p.search(line) for p in SECRET_DROP_PATTERNS)


def is_noise(line: str) -> bool:
    return any(p.match(line) for p in NOISE_PATTERNS)


def parse_zsh_extended(raw: bytes) -> list[tuple[int, str]]:
    """Parse zsh extended history format: `: <epoch>:<duration>;<command>`.

    Returns list of (epoch, command) tuples. Falls back to plain-text mode for
    lines that don't match the extended format. Handles multi-line commands
    joined by a trailing backslash (zsh's line-continuation syntax) — without
    this, `git commit -m \\\n"message"` gets split into two records.
    """
    out: list[tuple[int, str]] = []
    text = raw.decode("utf-8", errors="replace")
    pat = re.compile(r"^: (\d+):\d+;(.*)$")
    # First pass: join continuation lines. zsh writes the backslash verbatim.
    raw_lines = text.splitlines()
    joined_lines: list[str] = []
    buf: list[str] = []
    for line in raw_lines:
        if buf:
            # Continuation of a previous line — strip the final backslash
            # from the buffer tail and append.
            buf.append(line)
            if not line.endswith("\\"):
                joined_lines.append("\n".join(buf).replace("\\\n", "\n"))
                buf = []
        elif line.endswith("\\"):
            buf.append(line)
        else:
            joined_lines.append(line)
    if buf:
        joined_lines.append("\n".join(buf).replace("\\\n", "\n"))
    for line in joined_lines:
        m = pat.match(line)
        if m:
            try:
                out.append((int(m.group(1)), m.group(2).strip()))
            except ValueError:
                continue
        elif line.strip():
            out.append((int(datetime.now().timestamp()), line.strip()))
    return out


def filter_commands(commands: list[tuple[int, str]]) -> list[tuple[int, str]]:
    out = []
    for ts, cmd in commands:
        if is_secret(cmd):
            continue
        if is_noise(cmd):
            continue
        if len(cmd) < 4:
            continue
        out.append((ts, cmd))
    return out


def bucket_commands(commands: list[tuple[int, str]]) -> list[dict]:
    """Group commands into 5-minute work buckets, return summary records."""
    if not commands:
        return []
    buckets: dict[int, list[tuple[int, str]]] = {}
    for ts, cmd in commands:
        key = ts - (ts % BUCKET_SECONDS)
        buckets.setdefault(key, []).append((ts, cmd))

    out = []
    for bucket_start in sorted(buckets.keys()):
        cmds = buckets[bucket_start]
        start_dt = datetime.fromtimestamp(bucket_start, tz=timezone.utc)
        end_ts = max(t for t, _ in cmds)
        end_dt = datetime.fromtimestamp(end_ts, tz=timezone.utc)
        cmd_lines = [cmd for _, cmd in cmds]
        # Detect dominant context: which directory/repo did Chris work in?
        cwd_hint = ""
        for cmd in cmd_lines:
            if cmd.startswith("cd "):
                cwd_hint = cmd[3:].strip().strip('"').strip("'")
                break

        summary = (
            f"Shell session {start_dt.strftime('%Y-%m-%d %H:%M')}–{end_dt.strftime('%H:%M')} "
            f"UTC ({len(cmd_lines)} commands"
            + (f", in {cwd_hint}" if cwd_hint else "")
            + "):\n"
            + "\n".join(f"  $ {c}" for c in cmd_lines[:30])
            + ("\n  ...truncated..." if len(cmd_lines) > 30 else "")
        )
        out.append({
            "bucket_start_ts": bucket_start,
            "start_iso": start_dt.isoformat().replace("+00:00", "Z"),
            "end_iso": end_dt.isoformat().replace("+00:00", "Z"),
            "command_count": len(cmd_lines),
            "cwd_hint": cwd_hint,
            "summary": summary,
        })
    return out


def build_raw_record(bucket: dict) -> dict:
    digest = hashlib.sha256(bucket["summary"].encode()).hexdigest()
    date_part = bucket["start_iso"][:10].replace("-", "_")
    return {
        "id": f"raw_shell_{date_part}_{digest[:8]}",
        "timestamp": bucket["start_iso"],
        "source_type": "shell",
        "source_ref": f"shell:{bucket['bucket_start_ts']}",
        "actor": "chris",
        "visibility": "private",
        "scrub_status": "scrubbed",
        "content": bucket["summary"],
        "attachments": [],
        "entities": ["Chris", "shell"] + ([bucket["cwd_hint"]] if bucket["cwd_hint"] else []),
        "hash": f"sha256:{digest}",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Ingest zsh history into raw inbox")
    parser.add_argument("--dry-run", action="store_true", help="Print what would be ingested without writing")
    parser.add_argument("--max-buckets", type=int, default=200, help="Cap on buckets per run (newest first)")
    args = parser.parse_args()

    if not HISTORY_FILE.exists():
        log_failure(f"history file missing: {HISTORY_FILE}")
        sys.exit(1)

    offset = load_offset()
    file_size = HISTORY_FILE.stat().st_size

    if file_size < offset:
        # File rotated/truncated — start over
        offset = 0

    if file_size == offset:
        print(f"No new shell history (offset={offset})")
        return

    with HISTORY_FILE.open("rb") as f:
        f.seek(offset)
        new_bytes = f.read()
        new_offset = f.tell()

    commands = parse_zsh_extended(new_bytes)
    print(f"Parsed {len(commands)} new history entries since offset {offset}")

    surviving = filter_commands(commands)
    print(f"After redaction + noise filter: {len(surviving)} commands")

    buckets = bucket_commands(surviving)
    print(f"Grouped into {len(buckets)} 5-minute buckets")

    if args.max_buckets and len(buckets) > args.max_buckets:
        # Keep newest
        buckets = buckets[-args.max_buckets:]
        print(f"Capped to newest {args.max_buckets} buckets")

    if args.dry_run:
        print("\n[DRY RUN] sample bucket:")
        if buckets:
            print(buckets[0]["summary"])
        print(f"\n[DRY RUN] would write {len(buckets)} records to {INBOX_DIR}")
        return

    INBOX_DIR.mkdir(parents=True, exist_ok=True)
    written = 0
    for bucket in buckets:
        record = build_raw_record(bucket)
        out = INBOX_DIR / f"{record['id']}.json"
        if out.exists():
            continue  # idempotent — same hash = same content
        out.write_text(json.dumps(record, ensure_ascii=False, indent=2))
        written += 1

    save_offset(new_offset)
    print(f"Wrote {written} new records to {INBOX_DIR}")
    print(f"State updated: offset={new_offset}")

    if written == 0 and len(buckets) > 0:
        sys.stderr.write(f"WARN adapter=shell_history buckets={len(buckets)} written=0 — possible duplicate or write failure\n")


if __name__ == "__main__":
    main()
