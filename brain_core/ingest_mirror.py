"""brain_core/ingest_mirror.py — shared atoms-mirror + hygiene classifier pipeline.

CR7 fix (2026-04-14): factors the v3 Brain Hygiene Stack block out of
server.py::create_memory so it can be reused from /memory/batch,
/learn, wm_consolidate, and any future ingest path. Previously only
POST /memory went through the hygiene pipeline — batch, learn, and
wm_consolidate all silently skipped classification + topic supersession,
making them an implicit hygiene bypass for any caller that chose them.

The mirror sequence is:
  1. atoms_gate.enforce       — 30-word discipline + redistill
  2. ingest_classifier.classify — topic/speaker/scope/provisional
  3. atoms_store.upsert_atom   — write the atom row with hygiene fields
  4. llm_backlog.enqueue       — catch-up classify if LLM fell to heuristic
  5. topic-based supersession   — BEGIN IMMEDIATE + latest-wins UPDATE

All steps are best-effort (any sqlite3.Error is logged + returned in the
result dict but never raised). The return shape lets callers log what
happened per-memory without each caller re-implementing the block.
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

log = logging.getLogger("brain.ingest_mirror")


@dataclass
class MirrorResult:
    atom_id: str | None = None
    classifier_source: str = "none"
    classifier_reason: str = ""
    atom_status: str = "none"
    atom_quality: float = 0.5
    superseded_topic: bool = False
    enqueued_classify: bool = False
    error: str = ""
    warnings: list[str] = field(default_factory=list)


def mirror_memory(
    *,
    content: str,
    chroma_id: str,
    category: str = "fact",
    agent: str = "claude",
    source: str = "manual",
    operation: str = "ADD",
    confidence: float = 0.5,
    parent_atom_id: str | None = None,
    now_iso: str,
    allow_redistill: bool = False,
) -> MirrorResult:
    """Write an atom through the full hygiene pipeline.

    Returns a MirrorResult that documents every step's outcome. Callers
    should persist result.atom_id if they need to reference the atom
    later, and check result.error / result.warnings for diagnostics.
    """
    result = MirrorResult()
    try:
        from atoms_gate import enforce as _atoms_enforce
        from atoms_store import upsert_atom
        from ingest_classifier import classify as _ingest_classify
    except ImportError as e:
        result.error = f"import_failed:{e}"
        return result

    # 1. Atoms gate (30-word discipline + optional redistill)
    try:
        atom_text, atom_status, atom_quality = _atoms_enforce(
            content[:2000],
            allow_redistill=allow_redistill,
        )
        result.atom_status = atom_status
        result.atom_quality = atom_quality
    except Exception as e:
        result.error = f"atoms_gate:{e}"
        return result

    # 2. Classify (LLM w/ heuristic fallback)
    classification = None
    try:
        classification = _ingest_classify(
            content[:2000],
            author_agent=agent,
            category=category,
        )
        if classification:
            result.classifier_source = classification.source
            result.classifier_reason = classification.reason
    except Exception as e:
        result.warnings.append(f"classify:{e}")

    # 3. Upsert atom
    try:
        result.atom_id = upsert_atom(
            text=atom_text,
            chroma_id=chroma_id,
            kind=category or "fact",
            confidence=confidence,
            tier="episodic",
            distilled_by="manual",
            collection_hint="semantic_memory",
            quality_score=atom_quality,
            valid_from=now_iso,
            provenance={
                "agent": agent,
                "source": source,
                "operation": operation,
                "atoms_gate_status": atom_status,
                "classifier_source": result.classifier_source,
                "classifier_reason": result.classifier_reason,
            },
            parent_atom_id=parent_atom_id,
            provisional=(classification.provisional if classification else False),
            trust_score=(classification.confidence if classification else 0.5),
            topic_key=(classification.topic_key if classification else None),
            speaker_entity=(classification.speaker_entity if classification else "chris"),
            scope=(classification.scope if classification else "global"),
        )
    except Exception as e:
        result.error = f"upsert_atom:{e}"
        return result

    # 4. llm_backlog catch-up — queue LLM re-classify if we fell to heuristic
    if (
        classification
        and classification.source == "heuristic"
        and result.atom_id
    ):
        try:
            from llm_backlog import enqueue as _backlog_enqueue
            _backlog_enqueue(
                "classify",
                {
                    "atom_id": result.atom_id,
                    "content": content[:2000],
                    "author_agent": agent,
                    "category": category or "fact",
                },
            )
            result.enqueued_classify = True
        except Exception as e:
            result.warnings.append(f"backlog_enqueue:{e}")

    # 5. Topic-based supersession (F5 fix — BEGIN IMMEDIATE + latest-wins)
    if classification and classification.topic_key:
        try:
            from atoms_store import _conn as _atoms_conn
            with _atoms_conn() as _c:
                _c.execute("BEGIN IMMEDIATE")
                cursor = _c.execute(
                    "UPDATE atoms SET valid_until = ?, updated_at = ? "
                    "WHERE topic_key = ? AND speaker_entity = ? "
                    "AND (valid_until IS NULL OR valid_until = '') "
                    "AND id NOT IN ("
                    "  SELECT id FROM atoms "
                    "  WHERE topic_key = ? AND speaker_entity = ? "
                    "  ORDER BY created_at DESC, id DESC LIMIT 1"
                    ")",
                    (
                        now_iso,
                        now_iso,
                        classification.topic_key,
                        classification.speaker_entity,
                        classification.topic_key,
                        classification.speaker_entity,
                    ),
                )
                _c.commit()
                if cursor.rowcount > 0:
                    result.superseded_topic = True
        except Exception as e:
            result.warnings.append(f"supersession:{e}")

    return result
