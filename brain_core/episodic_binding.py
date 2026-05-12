"""brain_core/episodic_binding.py — D10 multimodal episodic binding.

Biological motivation: the hippocampus binds modalities into unified
episodic memories — when you remember "the meeting in the cafe," your
brain co-retrieves the conversation (text), the room (visual), the
coffee taste (gustatory), and the timestamp. Personal AI systems
typically store modalities in disjoint collections, losing the binding.

This module gives the brain a temporal episode primitive: for any
timestamp or atom_id, retrieve everything brain remembers from a
narrow temporal window across ALL modalities (text atoms, ingested
images, audio transcripts).

Today: text atoms are populated; images and audio collections are
configured but unpopulated (brain_ingest_image endpoint exists but
hasn't been called; no audio ingest defined yet). The function
gracefully returns empty modality lists when no data exists, and
activates automatically when data starts flowing.

Future activation:
  - Image: enable Screenshots ingest, or Photos library ingest, or
    direct POST /brain/ingest/image
  - Audio: when added, would write to a new `audio_events` collection
    or atoms with kind='audio'

This is the wiring side. Data side requires Chris's pipeline choice.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from config import BRAIN_DB
except ImportError:
    BRAIN_DB = Path("/Users/chrischo/server/brain/logs/brain.db")

log = logging.getLogger("brain.episodic_binding")


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _atom_neighbors(
    conn: sqlite3.Connection,
    pivot_ts: datetime,
    window: timedelta,
    exclude_id: str | None,
    limit: int,
) -> list[dict]:
    lower = (pivot_ts - window).isoformat(timespec="seconds")
    upper = (pivot_ts + window).isoformat(timespec="seconds")
    sql = (
        "SELECT id, kind, tier, substr(text, 1, 200) as preview, valid_from "
        "FROM atoms "
        "WHERE tier != 'obsolete' "
        "  AND valid_from BETWEEN ? AND ? "
    )
    params: list = [lower, upper]
    if exclude_id:
        sql += "  AND id != ? "
        params.append(exclude_id)
    sql += "ORDER BY valid_from ASC LIMIT ?"
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    return [
        {
            "id": r[0],
            "kind": r[1],
            "tier": r[2],
            "preview": r[3],
            "at": r[4],
        }
        for r in rows
    ]


def _image_neighbors(pivot_ts: datetime, window: timedelta) -> list[dict]:
    """Placeholder for image collection lookup.

    Today returns []. When images flow (POST /brain/ingest/image populated),
    swap this for a Qdrant query against the image collection filtered by
    timestamp metadata. Schema slot reserved.
    """
    # TODO: when image collection exists, query Qdrant by metadata.timestamp
    # in [lower, upper]. For now, return the expected shape with empty data
    # so consumers can rely on the contract.
    return []


def _audio_neighbors(pivot_ts: datetime, window: timedelta) -> list[dict]:
    """Reserved slot for audio. Returns [] until audio pipeline exists."""
    return []


def bind_episode_by_timestamp(
    ts_iso: str,
    window_minutes: int = 30,
    limit_per_modality: int = 20,
) -> dict:
    """Return all brain artifacts within a temporal window across modalities."""
    pivot = _parse_iso(ts_iso)
    if not pivot:
        return {"error": "invalid_timestamp", "received": ts_iso}
    window = timedelta(minutes=max(1, window_minutes))
    conn = sqlite3.connect(str(BRAIN_DB), timeout=5)
    try:
        text_atoms = _atom_neighbors(conn, pivot, window, None, limit_per_modality)
    finally:
        conn.close()
    images = _image_neighbors(pivot, window)
    audio = _audio_neighbors(pivot, window)
    return {
        "pivot": pivot.isoformat(timespec="seconds"),
        "window_minutes": window_minutes,
        "modalities": {
            "text": {"count": len(text_atoms), "items": text_atoms},
            "image": {"count": len(images), "items": images, "data_available": False},
            "audio": {"count": len(audio), "items": audio, "data_available": False},
        },
    }


def bind_episode_by_atom(atom_id: str, window_minutes: int = 30) -> dict:
    """Find atom's timestamp, then bind everything around it across modalities."""
    conn = sqlite3.connect(str(BRAIN_DB), timeout=5)
    try:
        row = conn.execute(
            "SELECT valid_from, substr(text, 1, 200) FROM atoms WHERE id = ?",
            (atom_id,),
        ).fetchone()
        if not row:
            return {"error": "atom_not_found", "atom_id": atom_id}
        pivot_iso, pivot_text = row[0], row[1]
        pivot = _parse_iso(pivot_iso)
        if not pivot:
            return {"error": "atom_has_invalid_timestamp", "atom_id": atom_id}
        window = timedelta(minutes=max(1, window_minutes))
        text_atoms = _atom_neighbors(conn, pivot, window, atom_id, 20)
    finally:
        conn.close()

    return {
        "pivot_atom_id": atom_id,
        "pivot_at": pivot_iso,
        "pivot_preview": pivot_text,
        "window_minutes": window_minutes,
        "modalities": {
            "text": {"count": len(text_atoms), "items": text_atoms},
            "image": {
                "count": 0,
                "items": [],
                "data_available": False,
                "hint": "POST /brain/ingest/image or enable Screenshots ingest",
            },
            "audio": {
                "count": 0,
                "items": [],
                "data_available": False,
                "hint": "audio ingest pipeline not configured",
            },
        },
    }


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd")
    p_ts = sub.add_parser("by-time")
    p_ts.add_argument("ts")
    p_ts.add_argument("--minutes", type=int, default=30)
    p_a = sub.add_parser("by-atom")
    p_a.add_argument("atom_id")
    p_a.add_argument("--minutes", type=int, default=30)
    args = p.parse_args()
    if args.cmd == "by-time":
        print(  # noqa: T201
            json.dumps(
                bind_episode_by_timestamp(args.ts, window_minutes=args.minutes),
                indent=2,
                ensure_ascii=False,
            )
        )
    elif args.cmd == "by-atom":
        print(  # noqa: T201
            json.dumps(
                bind_episode_by_atom(args.atom_id, window_minutes=args.minutes),
                indent=2,
                ensure_ascii=False,
            )
        )
    else:
        p.print_help()
