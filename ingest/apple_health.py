#!/Users/chrischo/server/brain/.venv/bin/python
"""Apple Health ingest — daily focus/recovery signal from HealthKit.

Pipeline (one-time setup):
  iPhone → Shortcuts app → "Export Health Summary to iCloud"
    Data points: Sleep (hours + score), Active Energy (kcal), Resting HR,
                 HRV SDNN, Step count, Exercise minutes, Stand hours.
    Schedule: automation, daily at 06:00, writes JSON to
      ~/Library/Mobile Documents/com~apple~CloudDocs/brain/health/<YYYY-MM-DD>.json
  Mac syncs via iCloud Drive → this script ingests new files.

Why: Chris's focus state correlates with sleep + recovery. With this in
brain, /recall queries like "what was my energy pattern on bad coding
days?" become answerable.

JSON format expected (produced by the Shortcut):
  {
    "date": "2026-04-22",
    "sleep_hours": 7.2,
    "sleep_quality": 0.82,      # 0..1 from Shortcuts "Sleep Analysis"
    "active_kcal": 412,
    "resting_hr_bpm": 54,
    "hrv_sdnn_ms": 48.2,
    "steps": 6240,
    "exercise_min": 28,
    "stand_hours": 10
  }

Usage:
  apple_health.py [--dry-run] [--force <path>]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

INBOX_DIR = Path("/Users/chrischo/server/knowledge/raw/inbox")
STATE_FILE = Path("/Users/chrischo/server/brain/logs/apple-health-state.json")
FAILURE_LOG = Path("/Users/chrischo/server/brain/logs/apple-health-failures.jsonl")

# iCloud Drive folder where the Shortcut writes daily JSON.
HEALTH_EXPORT_DIR = Path(
    "/Users/chrischo/Library/Mobile Documents/com~apple~CloudDocs/brain/health"
)

# Retention for iCloud Drive source files. After ingest succeeds, files older
# than this are deleted to prevent unbounded accumulation on iCloud.
SOURCE_RETENTION_DAYS = 30
MIN_AGE_BEFORE_DELETE_DAYS = 7  # safety: never delete files younger than this


def _load_state() -> dict:
    if not STATE_FILE.exists():
        return {"ingested_dates": [], "last_ingested": None}
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return {"ingested_dates": [], "last_ingested": None}


def _save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


def _prune_old_source_files(ingested_dates: set[str]) -> int:
    """Delete iCloud source JSONs older than SOURCE_RETENTION_DAYS.

    Safety guards:
      - Only deletes files whose date stem is in ingested_dates (proven processed).
      - Never deletes files younger than MIN_AGE_BEFORE_DELETE_DAYS.
      - Fails silently per file so one bad path doesn't block cleanup.
    """
    if not HEALTH_EXPORT_DIR.exists():
        return 0
    from datetime import timedelta

    now = datetime.now(UTC)
    cutoff = now - timedelta(days=SOURCE_RETENTION_DAYS)
    min_age_cutoff = now - timedelta(days=MIN_AGE_BEFORE_DELETE_DAYS)
    deleted = 0
    for path in HEALTH_EXPORT_DIR.glob("*.json"):
        date_stem = path.stem  # YYYY-MM-DD
        if date_stem not in ingested_dates:
            continue
        try:
            mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
        except OSError:
            continue
        if mtime > min_age_cutoff:  # too young, skip
            continue
        if mtime > cutoff:
            continue
        try:
            path.unlink()
            deleted += 1
        except OSError as exc:
            _log_failure("prune_unlink_failed", path=str(path), error=str(exc))
    return deleted


def _log_failure(reason: str, **meta) -> None:
    FAILURE_LOG.parent.mkdir(parents=True, exist_ok=True)
    with FAILURE_LOG.open("a") as f:
        f.write(json.dumps({"ts": datetime.now(UTC).isoformat(), "reason": reason, **meta}) + "\n")


def _format_summary(day: dict) -> str:
    """Convert a daily health JSON to a short, searchable markdown."""
    date = day.get("date", "unknown")
    sleep_h = day.get("sleep_hours")
    sleep_q = day.get("sleep_quality")
    kcal = day.get("active_kcal")
    rhr = day.get("resting_hr_bpm")
    hrv = day.get("hrv_sdnn_ms")
    steps = day.get("steps")
    ex_min = day.get("exercise_min")
    stand = day.get("stand_hours")

    def fmt(v, suffix=""):
        return f"{v}{suffix}" if v is not None else "—"

    # Simple recovery call-outs so the canonical promoter has features to
    # group on later (e.g. "Chris's low-recovery days").
    tags = []
    if isinstance(sleep_h, (int, float)):
        if sleep_h < 6:
            tags.append("short-sleep")
        elif sleep_h >= 8:
            tags.append("full-sleep")
    if isinstance(hrv, (int, float)) and hrv < 35:
        tags.append("low-hrv")
    if isinstance(rhr, (int, float)) and rhr > 65:
        tags.append("elevated-rhr")
    if isinstance(ex_min, (int, float)) and ex_min >= 30:
        tags.append("trained")
    tags_line = " ".join(f"`{t}`" for t in tags) or "—"

    return "\n".join(
        [
            f"# Apple Health — {date}",
            "",
            f"- Date: {date}",
            f"- Tags: {tags_line}",
            "",
            "## Metrics",
            "",
            f"- Sleep: {fmt(sleep_h, 'h')} (quality {fmt(sleep_q)})",
            f"- Resting HR: {fmt(rhr, ' bpm')} · HRV SDNN: {fmt(hrv, ' ms')}",
            f"- Active energy: {fmt(kcal, ' kcal')} · Exercise: {fmt(ex_min, ' min')} · Stand: {fmt(stand, ' hr')}",
            f"- Steps: {fmt(steps)}",
            "",
        ]
    ) + "\n"


def _stable_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def ingest_one(path: Path, dry_run: bool = False) -> dict:
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        _log_failure("parse_failed", path=str(path), error=str(exc))
        return {"status": "error", "path": str(path), "reason": "parse_failed"}

    date = data.get("date")
    if not date:
        _log_failure("missing_date", path=str(path))
        return {"status": "error", "path": str(path), "reason": "missing_date"}

    # Build markdown body for distillation.
    md = _format_summary(data)
    content_hash = _stable_hash(md)

    # Wrap in the raw_*.json envelope canonical_pipeline's batch_distill.py
    # expects — filename MUST match glob `raw_*.json` or distillation skips it.
    date_slug = date.replace("-", "_")  # 2026-04-22 -> 2026_04_22
    raw_id = f"raw_{date_slug}_{content_hash[:8]}"
    envelope = {
        "id": raw_id,
        "timestamp": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "source_type": "apple_health",
        "source_ref": f"apple_health:{path.name}",
        "actor": "ios-shortcut",
        "visibility": "private",
        "scrub_status": "scrubbed",
        "content": md,
        "attachments": [],
        "entities": ["Chris"],
        "hash": f"sha256:{content_hash}",
    }

    dest = INBOX_DIR / f"{raw_id}.json"
    if dry_run:
        return {"status": "dry_run", "date": date, "dest": str(dest)}
    if dest.exists():
        return {"status": "skip", "date": date, "reason": "already_ingested"}
    INBOX_DIR.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(envelope, indent=2, ensure_ascii=False), encoding="utf-8")
    return {"status": "ok", "date": date, "dest": str(dest)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", help="ingest a single JSON file (bypasses state)")
    args = parser.parse_args()

    state = _load_state()
    ingested = set(state.get("ingested_dates") or [])
    results = []

    if args.force:
        results.append(ingest_one(Path(args.force), dry_run=args.dry_run))
    else:
        if not HEALTH_EXPORT_DIR.exists():
            msg = {
                "status": "skip",
                "reason": "export_dir_missing",
                "expected": str(HEALTH_EXPORT_DIR),
                "hint": "Install the iOS Shortcut that writes daily JSON to this path.",
            }
            print(json.dumps(msg, indent=2))
            return 0
        for path in sorted(HEALTH_EXPORT_DIR.glob("*.json")):
            # Date inferred from filename (YYYY-MM-DD.json) as first cheap filter
            date_from_name = path.stem
            if date_from_name in ingested:
                continue
            res = ingest_one(path, dry_run=args.dry_run)
            if res.get("status") == "ok":
                ingested.add(res["date"])
            results.append(res)

    pruned = 0
    if not args.dry_run:
        state["ingested_dates"] = sorted(ingested)[-365:]  # keep last year
        state["last_ingested"] = datetime.now(UTC).isoformat()
        # Prune old iCloud source files to prevent unbounded growth.
        pruned = _prune_old_source_files(ingested)
        state["last_prune_deleted"] = pruned
        _save_state(state)

    summary = {
        "processed": len(results),
        "written": sum(1 for r in results if r.get("status") == "ok"),
        "skipped": sum(1 for r in results if r.get("status") == "skip"),
        "errors": sum(1 for r in results if r.get("status") == "error"),
        "pruned_source_files": pruned,
    }
    print(json.dumps({"summary": summary, "details": results[:10]}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
