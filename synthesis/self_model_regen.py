#!/opt/homebrew/bin/python3
"""Nightly self-model atom regeneration - DMN-like unified identity anchor.

Biology: the Default Mode Network (Raichle 2001, Northoff 2006) maintains an
ongoing self-model - medial PFC evaluates "is this relevant to me?" and
posterior midline handles autobiographical context. Every incoming signal is
scored against this self-model for personal relevance.

This job compiles the brain's scattered self-data into one canonical atom
that boot_context can pin as the default retrieval anchor:

  sources:
    - canonical/chris/_identity.md   (immutable core)
    - canonical/chris/_state.md      (mutable - projects/tools/focus)
    - top-positive valence atoms     (what Chris consistently liked)
    - most-accessed canonical atoms  (what the brain uses most)

  output:
    - canonical/chris/_self_model.md (one file, ingested into canonical)

Super-human: never deletes any component, never down-weights. Just
re-assembles the freshest unified view each night. Hypertext links back to
source atoms so the narrative self remains introspectable.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "brain_core"))

try:
    from config import BRAIN_DB, KNOWLEDGE_DIR
except ImportError:
    KNOWLEDGE_DIR = Path("/Users/chrischo/server/knowledge")
    BRAIN_DB = Path("/Users/chrischo/server/brain/logs/brain.db")

CHRIS_DIR = KNOWLEDGE_DIR / "canonical" / "chris"
IDENTITY_PATH = CHRIS_DIR / "_identity.md"
STATE_PATH = CHRIS_DIR / "_state.md"
SELF_MODEL_PATH = CHRIS_DIR / "_self_model.md"

TOP_VALENCE_N = 10
TOP_ACCESSED_N = 10


def _read(path: Path) -> str:
    try:
        return path.read_text().strip()
    except FileNotFoundError:
        return ""


def _top_positive_valence(n: int) -> list[dict]:
    try:
        conn = sqlite3.connect(str(BRAIN_DB))
        rows = conn.execute(
            "SELECT atom_id, valence, event_count, last_reason "
            "FROM atom_valence WHERE valence > 0 ORDER BY valence DESC LIMIT ?",
            (n,),
        ).fetchall()
        conn.close()
        return [{"atom_id": r[0], "valence": r[1], "events": r[2], "reason": r[3] or ""} for r in rows]
    except sqlite3.Error:
        return []


def _top_reinforced_atoms(n: int) -> list[dict]:
    try:
        conn = sqlite3.connect(str(BRAIN_DB))
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, chroma_id, text, reinforcement_count, kind, tier "
            "FROM atoms WHERE reinforcement_count > 0 "
            "AND tier = 'canonical' "
            "ORDER BY reinforcement_count DESC LIMIT ?",
            (n,),
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except sqlite3.Error:
        return []


def compile_self_model() -> str:
    identity = _read(IDENTITY_PATH)
    state = _read(STATE_PATH)
    valence = _top_positive_valence(TOP_VALENCE_N)
    reinforced = _top_reinforced_atoms(TOP_ACCESSED_N)
    now = datetime.now(UTC).isoformat(timespec="seconds")

    # Canonical JSON frontmatter so rebuild_canonical_index + canonical
    # ChromaDB ingest pick this up as a first-class atom (without this the
    # file sits on disk but never becomes retrievable).
    meta = {
        "id": "chris_self_model",
        "type": "canonical",
        "domain": "chris",
        "subtype": "self_model",
        "title": "Chris Cho - unified self-model (DMN anchor)",
        "status": "active",
        "visibility": "private",
        "confidence": 0.95,
        "updated_at": now,
        "last_reviewed_at": now,
        "owner": "chris",
        "scope": "global",
        "valid_from": now,
        "valid_to": None,
        "sources": [
            "canonical/chris/_identity.md",
            "canonical/chris/_state.md",
            "brain.db::atom_valence (top positive)",
            "brain.db::atoms (top reinforcement_count)",
        ],
        "provenance_summary": (
            "Nightly DMN-like compile: identity + state + top-valence atoms + "
            "top-reinforced canonical atoms. Regenerated from signals, not "
            "hand-edited. Raichle 2001 / Northoff 2006."
        ),
    }

    lines: list[str] = []
    lines.append("---json")
    lines.append(json.dumps(meta, indent=2, ensure_ascii=False))
    lines.append("---")
    lines.append("")
    lines.append(f"# Chris - unified self-model (regenerated {now})")
    lines.append("")
    lines.append(
        "> Auto-compiled nightly from identity + state + reinforcement signals. "
        "This atom is the brain's DMN anchor - the default answer to "
        "'who is this for' and 'what matters'."
    )
    lines.append("")

    if identity:
        lines.append("## Identity (immutable core)")
        lines.append(identity)
        lines.append("")

    if state:
        lines.append("## Current state")
        lines.append(state)
        lines.append("")

    if valence:
        lines.append("## Top positive signals (amygdala: what Chris values)")
        for v in valence:
            lines.append(
                f"- `{v['atom_id']}` (valence {v['valence']:+.2f}, n={v['events']}): " f"{v['reason'][:160]}"
            )
        lines.append("")

    if reinforced:
        lines.append("## Most-reinforced canonical atoms (what the brain uses)")
        for r in reinforced:
            preview = (r.get("text") or "")[:160].replace("\n", " ")
            lines.append(f"- `{r['id']}` ({r['kind']}, n={r['reinforcement_count']}): {preview}")
        lines.append("")

    return "\n".join(lines)


def run(dry_run: bool = False) -> dict:
    body = compile_self_model()
    if dry_run:
        return {"status": "dry_run", "chars": len(body)}
    CHRIS_DIR.mkdir(parents=True, exist_ok=True)
    # Atomic write — bare write_text leaves a truncated file on crash, and
    # _self_model.md is the DMN anchor boot_context loads at every cold start.
    from safe_state import atomic_write_text

    atomic_write_text(SELF_MODEL_PATH, body + "\n")
    return {
        "status": "ok",
        "path": str(SELF_MODEL_PATH),
        "chars": len(body),
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }


if __name__ == "__main__":
    import argparse
    import json

    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    print(json.dumps(run(dry_run=args.dry_run), indent=2, ensure_ascii=False))
