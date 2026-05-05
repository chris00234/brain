#!/Users/chrischo/server/brain/.venv/bin/python
"""Kuma incident ingest — Ellie distills service uptime events.

Pulls monitor heartbeats from Uptime Kuma (v1.x socket.io API), keeps
only state-change events (UP→DOWN or DOWN→UP), groups by monitor+day,
and writes to raw/inbox for the canonical pipeline to promote.

Pipeline: Kuma heartbeats → state-change filter → group by day → raw/inbox

Credentials:
  KUMA_URL  — default http://127.0.0.1:3001
  KUMA_USER — admin username
  KUMA_PASS — admin password  (or KUMA_CRED_FILE path to one-line file)

Usage:
  kuma_heartbeats.py [--dry-run] [--hours 24]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

INBOX_DIR = Path("/Users/chrischo/server/knowledge/raw/inbox")
STATE_FILE = Path("/Users/chrischo/server/brain/logs/kuma-ingest-state.json")
FAILURE_LOG = Path("/Users/chrischo/server/brain/logs/kuma-ingest-failures.jsonl")

DEFAULT_URL = "http://127.0.0.1:3001"
DEFAULT_WINDOW_HOURS = 24

# Status codes from Uptime Kuma v1.23 (monitor.js)
STATUS_DOWN = 0
STATUS_UP = 1
STATUS_PENDING = 2
STATUS_MAINTENANCE = 3

STATUS_LABEL = {
    STATUS_DOWN: "DOWN",
    STATUS_UP: "UP",
    STATUS_PENDING: "PENDING",
    STATUS_MAINTENANCE: "MAINTENANCE",
}


def _read_creds() -> tuple[str, str, str]:
    url = os.environ.get("KUMA_URL", DEFAULT_URL)
    user = os.environ.get("KUMA_USER")
    password = os.environ.get("KUMA_PASS")
    cred_file = os.environ.get("KUMA_CRED_FILE")
    if cred_file and Path(cred_file).exists():
        raw = Path(cred_file).read_text().strip()
        if raw.startswith("{"):
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                data = {}
            if isinstance(data, dict):
                url = str(data.get("url") or url)
                user = str(data.get("username") or data.get("user") or user or "")
                password = str(data.get("password") or data.get("pass") or password or "")
        elif not password:
            password = raw
    if not user or not password:
        print("KUMA_USER and KUMA_PASS (or KUMA_CRED_FILE) required", file=sys.stderr)
        sys.exit(2)
    return url, user, password


def _load_state() -> dict:
    if not STATE_FILE.exists():
        return {"last_ingested": None, "last_event_ts": {}}
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return {"last_ingested": None, "last_event_ts": {}}


def _save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


def _log_failure(reason: str, **meta) -> None:
    FAILURE_LOG.parent.mkdir(parents=True, exist_ok=True)
    with FAILURE_LOG.open("a") as f:
        f.write(json.dumps({"ts": datetime.now(UTC).isoformat(), "reason": reason, **meta}) + "\n")


def fetch_state_changes(url: str, user: str, password: str, hours: int) -> list[dict]:
    """Fetch important heartbeats (state changes) across all monitors."""
    try:
        from uptime_kuma_api import UptimeKumaApi
    except ImportError:
        print("pip install uptime-kuma-api", file=sys.stderr)
        sys.exit(2)

    api = UptimeKumaApi(url)
    try:
        api.login(user, password)
        monitors = api.get_monitors()
        state_changes: list[dict] = []
        for mon in monitors:
            mon_id = mon["id"]
            name = mon["name"]
            try:
                # important=True returns only state transitions
                beats = api.get_important_heartbeats(mon_id)
            except Exception as exc:
                _log_failure("get_important_heartbeats failed", monitor=name, error=str(exc))
                continue
            cutoff = datetime.now(UTC) - timedelta(hours=hours)
            for b in beats or []:
                # time is ISO, status int
                try:
                    ts = datetime.fromisoformat(b["time"].replace("Z", "+00:00"))
                except (KeyError, ValueError):
                    continue
                if ts < cutoff:
                    continue
                state_changes.append(
                    {
                        "monitor": name,
                        "monitor_id": mon_id,
                        "time": ts.isoformat(),
                        "status": b.get("status"),
                        "status_label": STATUS_LABEL.get(b.get("status"), "UNKNOWN"),
                        "msg": (b.get("msg") or "")[:500],
                        "ping": b.get("ping"),
                    }
                )
        return state_changes
    finally:
        try:
            api.disconnect()
        except Exception:
            pass


def _stable_hash(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def write_incidents(events: list[dict], dry_run: bool = False) -> dict:
    """Group by (monitor, date), emit one raw_*.json envelope per bucket.

    Filename convention `raw_YYYY_MM_DD_{hash}.json` matches what
    canonical_pipeline's batch_distill.py scans for (`raw_*.json` glob).
    Emitting .md here would be silently ignored by distillation.
    """
    if not events:
        return {"status": "ok", "written": 0, "events": 0}

    from collections import defaultdict

    grouped: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for ev in events:
        date = ev["time"][:10]
        grouped[(ev["monitor"], date)].append(ev)

    written = 0
    INBOX_DIR.mkdir(parents=True, exist_ok=True)
    for (monitor, date), bucket in grouped.items():
        bucket.sort(key=lambda x: x["time"])
        down_count = sum(1 for e in bucket if e["status"] == STATUS_DOWN)
        up_count = sum(1 for e in bucket if e["status"] == STATUS_UP)
        lines = [
            f"# Kuma incident log — {monitor} ({date})",
            "",
            f"- Monitor: `{monitor}`",
            f"- Date: {date}",
            f"- Transitions: {len(bucket)} (DOWN events: {down_count}, recoveries: {up_count})",
            "",
            "## Timeline",
            "",
        ]
        for e in bucket:
            ping_s = f" (ping {e['ping']}ms)" if e.get("ping") else ""
            msg = e.get("msg", "").strip().replace("\n", " ")
            lines.append(f"- `{e['time'][11:19]}` — **{e['status_label']}**{ping_s} · {msg}")
        content = "\n".join(lines) + "\n"

        content_hash = _stable_hash(content)
        date_slug = date.replace("-", "_")
        monitor_slug = monitor.lower().replace(" ", "-").replace("/", "-")[:32]
        raw_id = f"raw_{date_slug}_kuma_{monitor_slug}_{content_hash[:8]}"
        envelope = {
            "id": raw_id,
            "timestamp": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "source_type": "kuma_incident",
            "source_ref": f"kuma:{monitor}:{date}",
            "actor": "ellie",
            "visibility": "private",
            "scrub_status": "scrubbed",
            "content": content,
            "attachments": [],
            "entities": ["Chris", monitor],
            "hash": f"sha256:{content_hash}",
        }
        dest = INBOX_DIR / f"{raw_id}.json"
        if dry_run:
            print(f"[DRY] would write {dest.name} ({len(bucket)} events)")
            continue
        if dest.exists():
            continue
        dest.write_text(json.dumps(envelope, indent=2, ensure_ascii=False), encoding="utf-8")
        written += 1

    return {"status": "ok", "written": written, "events": len(events), "buckets": len(grouped)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--hours", type=int, default=DEFAULT_WINDOW_HOURS)
    args = parser.parse_args()

    url, user, password = _read_creds()
    events = fetch_state_changes(url, user, password, args.hours)
    result = write_incidents(events, dry_run=args.dry_run)
    result["window_hours"] = args.hours

    if not args.dry_run:
        state = _load_state()
        state["last_ingested"] = datetime.now(UTC).isoformat()
        state["last_window_hours"] = args.hours
        state["last_result"] = result
        _save_state(state)

    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
