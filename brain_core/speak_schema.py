"""brain_core/speak_schema.py — shared DDL, dataclasses, db helpers for speak.

Split from speak.py 2026-04-23 to keep each module under the <300-line bar.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from config import BRAIN_DB, BRAIN_LOGS_DIR
except ImportError:
    BRAIN_DB = Path("/Users/chrischo/server/brain/logs/brain.db")
    BRAIN_LOGS_DIR = Path("/Users/chrischo/server/brain/logs")

AUTONOMY_DB = BRAIN_LOGS_DIR / "autonomy.db"
log = logging.getLogger("brain.speak")

DEDUP_WINDOW_H = 72
DIGEST_MAX_BULLETS = 3


_SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS brain_speak_log (
  id             TEXT PRIMARY KEY,
  ts             TEXT NOT NULL,
  drive          TEXT NOT NULL,
  category       TEXT NOT NULL,
  severity       REAL NOT NULL,
  message        TEXT NOT NULL,
  dedup_key      TEXT NOT NULL,
  sent_via       TEXT,
  ack            TEXT,
  ack_ts         TEXT,
  payload_json   TEXT
);
CREATE INDEX IF NOT EXISTS idx_speak_dedup ON brain_speak_log(dedup_key, ts);
CREATE INDEX IF NOT EXISTS idx_speak_ts    ON brain_speak_log(ts);
"""


@dataclass
class Observation:
    drive: str
    category: str
    severity: float  # 0-10; 10 = interrupt right now, 3 = FYI
    message: str
    dedup_key: str
    payload: dict = field(default_factory=dict)


def brain_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(BRAIN_DB), timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def autonomy_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(AUTONOMY_DB), timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def ensure_schema() -> None:
    with brain_conn() as conn:
        conn.executescript(_SCHEMA_DDL)
        conn.commit()


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def was_sent_recently(dedup_key: str, within_h: int = DEDUP_WINDOW_H) -> bool:
    cutoff = (datetime.now(UTC) - timedelta(hours=within_h)).isoformat(timespec="seconds")
    with brain_conn() as conn:
        row = conn.execute(
            "SELECT id FROM brain_speak_log WHERE dedup_key = ? AND ts >= ? LIMIT 1",
            (dedup_key, cutoff),
        ).fetchone()
    return row is not None


def log_emit(obs: Observation, sent_via: str | None) -> str:
    entry_id = hashlib.sha256(f"{now_iso()}|{obs.drive}|{obs.dedup_key}".encode()).hexdigest()[:16]
    with brain_conn() as conn:
        conn.execute(
            "INSERT INTO brain_speak_log "
            "(id, ts, drive, category, severity, message, dedup_key, sent_via, payload_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                entry_id,
                now_iso(),
                obs.drive,
                obs.category,
                obs.severity,
                obs.message,
                obs.dedup_key,
                sent_via,
                json.dumps(obs.payload, ensure_ascii=False) if obs.payload else None,
            ),
        )
        conn.commit()
    return entry_id
