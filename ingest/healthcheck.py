#!/opt/homebrew/bin/python3
"""Daily healthcheck for the brain's capture pipeline.

Verifies:
  1. The 4 personal ChromaDB collections (notes, messages, calendar, tasks)
     grew since yesterday — or alerts via Telegram if any are stuck.
  2. The personal-ingest-failures.jsonl log has no entries from the last 24h
     — or surfaces recent failures.
  3. The most recent MinIO chroma backup is < 36 hours old (Phase 0c).

Runs daily at 9 AM via launchd. Posts a single Telegram DM only if there's
something wrong; stays silent on green.

Usage:
  healthcheck_capture.py [--force]    # --force always sends a status DM
"""

import argparse
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ── Config ──────────────────────────────────────────────
STATE_FILE = Path("/Users/chrischo/server/brain/logs/.healthcheck_state.json")
FAILURE_LOG = Path("/Users/chrischo/server/brain/logs/personal-ingest-failures.jsonl")
MONITORED_COLLECTIONS = {
    "personal": {"min_docs": 10, "source": "personal_ingest"},
    "knowledge": {"min_docs": 50, "source": "reindex"},
    "experience": {"min_docs": 20, "source": "reindex"},
    "canonical": {"min_docs": 5, "source": "reindex"},
    "semantic_memory": {"min_docs": 5, "source": "learn"},
    "obsidian": {"min_docs": 10, "source": "obsidian_sync"},
}

OPENCLAW_BIN = "/Users/chrischo/.local/bin/openclaw"
TELEGRAM_CHAT_ID = "8484060831"
TELEGRAM_ACCOUNT = "jenna-bot"

MC_BIN = "/opt/homebrew/bin/mc"
MINIO_ALIAS = "local"
MINIO_BUCKET = "rag-backups"
BACKUP_STALENESS_HOURS = 36


# ── ChromaDB collection counts ──────────────────────────
try:
    import sys as _sys
    _sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "brain_core"))
    from config import CHROMA_URL
except ImportError:
    CHROMA_URL = "http://127.0.0.1:8000"
CHROMA_API = f"{CHROMA_URL}/api/v2/tenants/default_tenant/databases/default_database/collections"

def get_collection_counts() -> dict[str, int]:
    """Live counts via ChromaDB HTTP API on localhost (no docker exec)."""
    import urllib.request
    try:
        resp = urllib.request.urlopen(CHROMA_API, timeout=10)
        cols = json.loads(resp.read())
    except Exception as e:
        return {"_error": str(e)}

    counts: dict[str, int] = {}
    for c in cols:
        cid = c.get("id")
        name = c.get("name")
        if not cid or not name:
            continue
        try:
            resp = urllib.request.urlopen(f"{CHROMA_API}/{cid}/count", timeout=10)
            counts[name] = int(resp.read().strip())
        except Exception:
            counts[name] = -1
    return counts


# ── State (uses safe_state for file locking) ──────────────
try:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "brain_core"))
    from safe_state import load_state as _safe_load, save_state as _safe_save
    def load_state() -> dict:
        return _safe_load(STATE_FILE)
    def save_state(state: dict) -> None:
        state["last_check"] = datetime.now().isoformat()
        _safe_save(STATE_FILE, state)
except ImportError:
    def load_state() -> dict:
        if STATE_FILE.exists():
            try:
                return json.loads(STATE_FILE.read_text())
            except Exception:
                return {}
        return {}
    def save_state(state: dict) -> None:
        state["last_check"] = datetime.now().isoformat()
        STATE_FILE.write_text(json.dumps(state, indent=2))


# ── Failure log inspection ──────────────────────────────
def recent_failures(hours: int = 24) -> list[dict]:
    """Return failure entries newer than `hours` ago.

    Normalizes every parsed timestamp to UTC-aware before comparison so that
    older naive-timestamped rows mixed with newer aware-timestamped rows
    don't raise TypeError (which `except Exception: continue` would swallow,
    silently dropping all failures).
    """
    if not FAILURE_LOG.exists():
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    out = []
    try:
        for line in FAILURE_LOG.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                ts_raw = entry.get("timestamp", "")
                if not ts_raw:
                    continue
                ts = datetime.fromisoformat(ts_raw.rstrip("Zz"))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                if ts >= cutoff:
                    out.append(entry)
            except Exception:
                continue
    except Exception:
        pass
    return out


# ── MinIO backup freshness ──────────────────────────────
def latest_backup_age_hours() -> float | None:
    """Return age of newest chroma-backup-*.tar.gz in MinIO, in hours. None on error."""
    try:
        result = subprocess.run(
            [MC_BIN, "ls", f"{MINIO_ALIAS}/{MINIO_BUCKET}/"],
            capture_output=True,
            text=True,
            timeout=20,
        )
        if result.returncode != 0:
            return None
        newest: datetime | None = None
        for line in result.stdout.splitlines():
            parts = line.strip().split()
            if not parts:
                continue
            fname = parts[-1]
            if not fname.startswith("chroma-backup-") or not fname.endswith(".tar.gz"):
                continue
            try:
                date_str = fname.replace("chroma-backup-", "").replace(".tar.gz", "")
                fdate = datetime.strptime(date_str, "%Y-%m-%d")
                if newest is None or fdate > newest:
                    newest = fdate
            except ValueError:
                continue
        if newest is None:
            return None
        return (datetime.now() - newest).total_seconds() / 3600
    except Exception:
        return None


# ── Telegram ────────────────────────────────────────────
def send_telegram(text: str) -> bool:
    try:
        result = subprocess.run(
            [
                OPENCLAW_BIN, "message", "send",
                "--channel", "telegram",
                "--target", TELEGRAM_CHAT_ID,
                "--account", TELEGRAM_ACCOUNT,
                "--message", text,
            ],
            capture_output=True,
            text=True,
            timeout=20,
        )
        return result.returncode == 0
    except Exception:
        return False


# ── Main check ──────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(description="Daily capture pipeline healthcheck")
    parser.add_argument("--force", action="store_true", help="Always send a status DM")
    args = parser.parse_args()

    issues: list[str] = []

    # 1. Collection growth
    counts = get_collection_counts()
    state = load_state()
    yesterday_counts = state.get("counts", {})

    if "_error" in counts:
        issues.append(f"❌ ChromaDB unreachable: {counts['_error']}")
    else:
        for name, spec in MONITORED_COLLECTIONS.items():
            today = counts.get(name, -1)
            yesterday = yesterday_counts.get(name, -1)
            if today < 0:
                issues.append(f"❌ Collection `{name}` count unreadable")
                continue
            if today == 0:
                issues.append(f"❌ Collection `{name}` is EMPTY (source: {spec['source']})")
                continue
            if yesterday < 0:
                # First run — record baseline, don't alert on growth/min_docs
                continue
            if today < spec["min_docs"]:
                issues.append(f"⚠️ Collection `{name}` has only {today} docs (min: {spec['min_docs']}, source: {spec['source']})")
            if today < yesterday:
                issues.append(f"⚠️ Collection `{name}` shrank ({yesterday} → {today})")

    # 2. Recent failures
    failures = recent_failures(hours=24)
    if failures:
        adapters = sorted({f.get("adapter", "?") for f in failures})
        issues.append(f"⚠️ {len(failures)} ingest failures in last 24h (adapters: {', '.join(adapters)})")

    # 3. Adapter watermark staleness
    ADAPTER_STATES = {
        # adapter_name: (state_file_path, max_staleness_hours)
        "git_activity": (Path("/Users/chrischo/server/brain/logs/git-activity-state.json"), 48),
        "screen_time":  (Path("/Users/chrischo/server/brain/logs/screen-time-state.json"), 192),
        "gmail":        (Path("/Users/chrischo/.openclaw/workspace-jenna/.gmail_ingest_state.json"), 48),
        "browser":      (Path("/Users/chrischo/.openclaw/workspace-sage/.brain_state/browser_ingest_state.json"), 48),
    }
    for adapter, (state_path, max_hours) in ADAPTER_STATES.items():
        try:
            if not state_path.exists():
                continue
            state_data = json.loads(state_path.read_text())
            last_run = state_data.get("last_run", "")
            if last_run:
                last_dt = datetime.fromisoformat(last_run.replace("Z", "+00:00"))
                if last_dt.tzinfo is None:
                    last_dt = last_dt.replace(tzinfo=timezone.utc)
                age_hours = (datetime.now(timezone.utc) - last_dt).total_seconds() / 3600
                if age_hours > max_hours:
                    issues.append(f"\u26a0\ufe0f Adapter `{adapter}` stale \u2014 last run {age_hours:.0f}h ago (max: {max_hours}h)")
        except Exception:
            pass

    # 4. Backup freshness
    age = latest_backup_age_hours()
    if age is None:
        issues.append("⚠️ Cannot read MinIO backup listing")
    elif age > BACKUP_STALENESS_HOURS:
        issues.append(f"⚠️ Latest MinIO backup is {age:.1f}h old (threshold {BACKUP_STALENESS_HOURS}h)")

    # Persist baseline for tomorrow
    save_state({"counts": counts})

    # Decide whether to send a DM
    print(f"Healthcheck — {datetime.now().isoformat()}")
    print(f"Counts: {counts}")
    print(f"Issues: {len(issues)}")
    for i in issues:
        print(f"  {i}")

    if issues:
        msg = (
            "🧠 Brain capture healthcheck — issues detected:\n\n"
            + "\n".join(issues)
            + "\n\nFull log: ~/server/brain/logs/personal-ingest-failures.jsonl"
        )
        send_telegram(msg)
    elif args.force:
        msg = (
            "🧠 Brain capture healthcheck — all green ✅\n\n"
            + "\n".join(f"  {k}: {v}" for k, v in counts.items() if not k.startswith("_"))
        )
        send_telegram(msg)


if __name__ == "__main__":
    main()
