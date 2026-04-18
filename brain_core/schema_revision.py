"""brain_core/schema_revision.py — Friston free-energy schema revision.

2026-04-16 Tier 3 #5: the existing contradiction system treats each
prediction error as an isolated event — demote the specific atom,
move on. But Friston's free-energy principle (2010) says the right
response to repeated prediction errors is not atom-level punishment,
it's to update the GENERATIVE MODEL that produced the wrong prediction.

Operationalization:
  1. Every prediction_error event lands in atom_evidence with
     event_type='prediction_error' (learn.py already fires these).
  2. This job clusters recent prediction_error rows by the atom's
     topic_key (or simhash fallback) — topic buckets where the brain
     has been wrong multiple times in a short window indicate a
     schema-level issue, not just one bad atom.
  3. For each qualifying cluster (>= MIN_CLUSTER_SIZE errors in window),
     enqueue a schema-revision proposal into raw/inbox: Sage is asked
     (via the existing reflect pipeline) to rewrite the relevant
     canonical knowledge to accommodate the pattern of errors.
  4. Proposals go through normal score → promote flow with a strong
     provenance tag so Chris can see they arose from systematic drift,
     not a single correction.

Runs weekly (not daily) because schema-level drift takes time to
accumulate signal.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from atoms_store import BRAIN_DB
    from config import INBOX_DIR
except ImportError:
    BRAIN_DB = Path("/Users/chrischo/server/brain/logs/brain.db")
    INBOX_DIR = Path("/Users/chrischo/server/knowledge/raw/inbox")


MIN_CLUSTER_SIZE = 3  # minimum prediction errors to qualify as systematic
LOOKBACK_DAYS = 14  # recency window — older errors are out-of-scope
MAX_PROPOSALS_PER_RUN = 3  # bound Sage dispatch + inbox pressure


def _fetch_error_clusters() -> list[dict]:
    """Group prediction_error events by atoms.topic_key over the window."""
    cutoff = (datetime.now(UTC) - timedelta(days=LOOKBACK_DAYS)).isoformat(timespec="seconds")
    if not BRAIN_DB.exists():
        return []
    try:
        conn = sqlite3.connect(str(BRAIN_DB))
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT ae.atom_id, ae.weight, ae.created_at,
                   a.topic_key, a.text, a.kind, a.confidence
            FROM atom_evidence ae
            JOIN atoms a ON a.id = ae.atom_id
            WHERE ae.event_type = 'prediction_error'
              AND ae.created_at >= ?
              AND a.tier != 'obsolete'
            """,
            (cutoff,),
        ).fetchall()
        conn.close()
    except sqlite3.Error:
        return []
    buckets: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        key = r["topic_key"] or f"nokey:{(r['text'] or '')[:40]}"
        buckets[key].append(
            {
                "atom_id": r["atom_id"],
                "weight": float(r["weight"] or 0),
                "created_at": r["created_at"],
                "text": r["text"],
                "kind": r["kind"],
                "confidence": float(r["confidence"] or 0),
            }
        )
    clusters = [
        {"topic_key": key, "events": evs, "n": len(evs)}
        for key, evs in buckets.items()
        if len(evs) >= MIN_CLUSTER_SIZE
    ]
    clusters.sort(key=lambda c: c["n"], reverse=True)
    return clusters[:MAX_PROPOSALS_PER_RUN]


def _build_schema_proposal(cluster: dict) -> dict:
    """Synthesize a raw/inbox record describing the schema-level drift."""
    events = cluster["events"]
    atom_ids = [e["atom_id"] for e in events]
    sample_texts = "\n".join(f"- {e['text'][:160]}" for e in events[:5])
    now = datetime.now(UTC).isoformat(timespec="seconds")
    content = (
        f"Systematic prediction-error cluster (Friston schema-revision signal).\n\n"
        f"Topic key: {cluster['topic_key']}\n"
        f"{cluster['n']} prediction errors in the last {LOOKBACK_DAYS} days. "
        f"Suggests the brain's generative model for this topic is miscalibrated.\n\n"
        f"Representative atoms (ids affected): {', '.join(atom_ids[:5])}\n\n"
        f"Sample evidence texts:\n{sample_texts}\n\n"
        f"Recommendation: Sage/Chris should review the canonical note(s) covering "
        f"'{cluster['topic_key']}' and either rewrite them to match current state or "
        f"add a **Conflict:** block noting the drift."
    )
    return {
        "id": f"raw_schema_revision_{cluster['topic_key'][:30]}_{now[:10]}".replace(" ", "_"),
        "type": "raw",
        "subtype": "schema_revision_signal",
        "title": f"Schema drift: {cluster['topic_key'][:60]}",
        "content": content,
        "entities": ["Chris"],
        "domain": "decisions",
        "source": "brain-schema-revision:friston",
        "source_type": "schema_revision",
        "source_ref": f"friston:{cluster['topic_key'][:80]}",
        "created_at": now,
        "visibility": "private",
    }


def run() -> dict:
    """Weekly free-energy schema revision signal emitter."""
    clusters = _fetch_error_clusters()
    if not clusters:
        return {"status": "ok", "clusters": 0, "note": "no systematic drift detected"}
    INBOX_DIR.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    for cluster in clusters:
        record = _build_schema_proposal(cluster)
        out = INBOX_DIR / f"{record['id']}.json"
        try:
            out.write_text(json.dumps(record, indent=2, ensure_ascii=False))
            written.append(record["id"])
        except OSError:
            continue
    return {
        "status": "ok",
        "clusters_detected": len(clusters),
        "proposals_written": len(written),
        "proposal_ids": written,
    }


if __name__ == "__main__":
    print(json.dumps(run(), indent=2, ensure_ascii=False))
