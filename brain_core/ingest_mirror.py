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

# 2026-04-26: semantic supersession thresholds.
# Topic-key + speaker matching alone is too blunt — paraphrases of the same
# fact were marking older atoms expired even though the fact itself stayed
# true. We now compute cosine similarity between the new atom's embedding
# and each existing-same-topic atom; only meaningful contradictions get
# valid_until set.
#
#   sim ≥ SUPERSEDE_REINFORCE_FLOOR  → restatement; older stays unchanged
#   SUPERSEDE_EXPIRE_CEILING ≤ sim < SUPERSEDE_REINFORCE_FLOOR
#                                     → orthogonal/partial; older stays
#   sim < SUPERSEDE_EXPIRE_CEILING   → real contradiction; older expires
#
# Conservative bias: when in doubt, keep both. time_decay handles ranking.
SUPERSEDE_REINFORCE_FLOOR = 0.85
SUPERSEDE_EXPIRE_CEILING = 0.70
# Cap candidates per ingest to bound the embedding cost.
SUPERSEDE_CANDIDATES_LIMIT = 20


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
    if classification and classification.source == "heuristic" and result.atom_id:
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

    # 5. Topic-based supersession with semantic similarity gate.
    # Replaces the prior blunt "expire all older same-topic atoms" UPDATE.
    # Each candidate is compared by cosine similarity against the new content;
    # only real contradictions (sim < SUPERSEDE_EXPIRE_CEILING) get
    # valid_until set. Restatements / paraphrases stay valid because the
    # fact itself is unchanged.
    if classification and classification.topic_key:
        _run_semantic_supersession(
            content=content,
            chroma_id=chroma_id,
            topic_key=classification.topic_key,
            speaker_entity=classification.speaker_entity,
            now_iso=now_iso,
            result=result,
        )

    return result


def _run_semantic_supersession(
    *,
    content: str,
    chroma_id: str,
    topic_key: str,
    speaker_entity: str,
    now_iso: str,
    result: MirrorResult,
) -> None:
    """Per-candidate semantic supersession.

    Pulls every same-topic same-speaker atom (except the just-written one),
    embeds the new content + each candidate, and only sets valid_until on
    candidates whose meaning genuinely diverges. Logged decisions land in
    `brain.ingest_mirror` so the reasoning is traceable.

    Fail-open: any error (embedding service down, sqlite contention) leaves
    every candidate untouched. Better to keep stale atoms than wrongly
    expire a fact that's still true.
    """
    try:
        from atoms_store import _conn as _atoms_conn
        from indexer import get_embedding
    except Exception as e:
        result.warnings.append(f"supersession_import:{e}")
        return

    try:
        with _atoms_conn() as _c:
            rows = _c.execute(
                "SELECT id, chroma_id, text FROM atoms "
                "WHERE topic_key = ? AND speaker_entity = ? "
                "AND (valid_until IS NULL OR valid_until = '') "
                "AND tier != 'obsolete' "
                "AND chroma_id != ? "
                "ORDER BY created_at DESC LIMIT ?",
                (topic_key, speaker_entity, chroma_id, SUPERSEDE_CANDIDATES_LIMIT),
            ).fetchall()
    except Exception as e:
        result.warnings.append(f"supersession_select:{e}")
        return

    if not rows:
        return

    try:
        new_emb = get_embedding(content[:2000], use_cache=True, prefix="passage")
    except Exception as e:
        result.warnings.append(f"supersession_embed_new:{e}")
        return
    if not new_emb:
        return

    # Lazy import — late_interaction owns the numpy-accelerated cosine.
    try:
        from late_interaction import _cosine
    except Exception as e:
        result.warnings.append(f"supersession_cosine:{e}")
        return

    expired_ids: list[str] = []
    reinforced = 0
    coexist = 0
    for row in rows:
        candidate_id = row["id"]
        candidate_text = row["text"] or ""
        try:
            cand_emb = get_embedding(candidate_text[:2000], use_cache=True, prefix="passage")
        except Exception as e:
            log.debug("supersession candidate embed failed id=%s: %s", candidate_id, e)
            continue
        if not cand_emb:
            continue
        sim = _cosine(new_emb, cand_emb)
        if sim >= SUPERSEDE_REINFORCE_FLOOR:
            reinforced += 1
            log.info(
                "supersession: reinforce sim=%.3f topic=%s candidate=%s",
                sim,
                topic_key,
                candidate_id,
            )
        elif sim < SUPERSEDE_EXPIRE_CEILING:
            expired_ids.append(candidate_id)
            log.info(
                "supersession: expire sim=%.3f topic=%s candidate=%s",
                sim,
                topic_key,
                candidate_id,
            )
        else:
            coexist += 1
            log.debug(
                "supersession: coexist sim=%.3f topic=%s candidate=%s",
                sim,
                topic_key,
                candidate_id,
            )

    if not expired_ids:
        log.debug(
            "supersession: no expirations topic=%s candidates=%d reinforced=%d coexist=%d",
            topic_key,
            len(rows),
            reinforced,
            coexist,
        )
        return

    try:
        with _atoms_conn() as _c:
            _c.execute("BEGIN IMMEDIATE")
            # Placeholders are constant `?` strings, not user input — S608 false positive.
            placeholders = ",".join("?" for _ in expired_ids)
            _c.execute(
                f"UPDATE atoms SET valid_until = ?, updated_at = ? WHERE id IN ({placeholders})",  # noqa: S608
                [now_iso, now_iso, *expired_ids],
            )
            _c.commit()
            result.superseded_topic = True
            log.info(
                "supersession: expired %d/%d candidates topic=%s reinforced=%d coexist=%d",
                len(expired_ids),
                len(rows),
                topic_key,
                reinforced,
                coexist,
            )
    except Exception as e:
        result.warnings.append(f"supersession_update:{e}")
