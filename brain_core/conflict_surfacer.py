"""brain_core/conflict_surfacer.py — surface candidate atom-pair conflicts.

Walks non-superseded atoms grouped by `topic_key` and flags pairs whose
texts overlap heavily (likely talking about the same thing) but include
divergence signals (negation flip, opposing numbers, different proper
nouns). Each surviving pair becomes a review task — the dispatcher
decides whether to supersede.

Pure SQLite + python token sets. No LLM, no embeddings.

The job is meant to run nightly:
  - cap output at MAX_TASKS_PER_RUN so we don't flood task_queue
  - dedupe against open review tasks via (atom_a, atom_b) signature
  - skip pairs with text length ratio outside [0.5, 2.0] (different scope)
"""

from __future__ import annotations

import logging
import re
import sqlite3
from pathlib import Path
from typing import Any

log = logging.getLogger("brain.conflict_surfacer")

MIN_TOPIC_CLUSTER_SIZE = 2
# Pairs are already in the same topic_key, so the bar is "enough lexical
# overlap that they are talking about the same thing." A negation flip
# inside a topic cluster is real signal even with modest token overlap.
MIN_TOKEN_OVERLAP = 0.30
MAX_TASKS_PER_RUN = 5
TOKEN_MIN_LEN = 3
MAX_TEXT_LEN_RATIO = 2.0

_TOKEN_RE = re.compile(r"[A-Za-z0-9가-힣]+")
_STOPWORDS = frozenset(
    {
        "the",
        "and",
        "for",
        "with",
        "from",
        "this",
        "that",
        "into",
        "after",
        "before",
        "but",
        "are",
        "was",
        "were",
        "has",
        "had",
        "have",
        "you",
        "they",
        "them",
        "his",
        "her",
        "its",
        "any",
        "all",
        "out",
        "via",
        "per",
        "off",
        "still",
        "also",
        "some",
        "every",
    }
)
_NEGATION_TOKENS = frozenset(
    {
        "no",
        "not",
        "never",
        "without",
        "disabled",
        "deprecated",
        "removed",
        "stopped",
        "retired",
        "broken",
        "false",
    }
)
_AFFIRM_TOKENS = frozenset(
    {
        "yes",
        "enabled",
        "active",
        "working",
        "live",
        "running",
        "true",
        "current",
        "primary",
    }
)


def _tokens(text: str) -> set[str]:
    return {
        tok
        for tok in (t.lower() for t in _TOKEN_RE.findall(text or ""))
        if len(tok) >= TOKEN_MIN_LEN and tok not in _STOPWORDS
    }


def _polarity(tokens: set[str]) -> str:
    neg = bool(tokens & _NEGATION_TOKENS)
    pos = bool(tokens & _AFFIRM_TOKENS)
    if neg and not pos:
        return "negative"
    if pos and not neg:
        return "positive"
    return "neutral"


def _numeric_signature(text: str) -> tuple[str, ...]:
    return tuple(re.findall(r"\d+(?:\.\d+)?", text or ""))


def _signature(atom_a: str, atom_b: str) -> str:
    lo, hi = sorted([atom_a, atom_b])
    return f"conflict::{lo}::{hi}"


def _is_divergent(text_a: str, text_b: str, toks_a: set[str], toks_b: set[str]) -> bool:
    pol_a = _polarity(toks_a)
    pol_b = _polarity(toks_b)
    if pol_a != pol_b and "neutral" not in (pol_a, pol_b):
        return True
    nums_a = _numeric_signature(text_a)
    nums_b = _numeric_signature(text_b)
    return bool(nums_a and nums_b and set(nums_a) != set(nums_b))


def find_conflicts(
    *,
    brain_db_path: Path | str,
    limit_clusters: int = 200,
    min_overlap: float = MIN_TOKEN_OVERLAP,
) -> list[dict]:
    """Return candidate conflict pairs ranked by token overlap.

    Each entry has the pair's atom_ids, topic_key, overlap score, and
    a short reason string so callers know why it surfaced.
    """
    db_path = Path(brain_db_path)
    if not db_path.exists():
        return []
    pairs: list[dict] = []
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            topics = conn.execute(
                """
                SELECT topic_key, COUNT(*) AS n
                FROM atoms
                WHERE topic_key IS NOT NULL AND topic_key != ''
                  AND superseded_by IS NULL
                  AND tier != 'obsolete'
                GROUP BY topic_key
                HAVING COUNT(*) >= ?
                ORDER BY n DESC
                LIMIT ?
                """,
                (MIN_TOPIC_CLUSTER_SIZE, limit_clusters),
            ).fetchall()
            for topic in topics:
                rows = conn.execute(
                    """
                    SELECT id, text, tier, confidence, trust_score, created_at
                    FROM atoms
                    WHERE topic_key = ?
                      AND superseded_by IS NULL
                      AND tier != 'obsolete'
                      AND length(text) > 20
                    ORDER BY created_at DESC
                    LIMIT 12
                    """,
                    (topic["topic_key"],),
                ).fetchall()
                atoms = [dict(r) for r in rows]
                for i, atom_a in enumerate(atoms):
                    toks_a = _tokens(atom_a["text"])
                    if not toks_a:
                        continue
                    for atom_b in atoms[i + 1 :]:
                        toks_b = _tokens(atom_b["text"])
                        if not toks_b:
                            continue
                        text_a, text_b = atom_a["text"] or "", atom_b["text"] or ""
                        len_ratio = max(len(text_a), len(text_b)) / max(1, min(len(text_a), len(text_b)))
                        if len_ratio > MAX_TEXT_LEN_RATIO:
                            continue
                        overlap = len(toks_a & toks_b) / max(1, len(toks_a | toks_b))
                        if overlap < min_overlap:
                            continue
                        if not _is_divergent(text_a, text_b, toks_a, toks_b):
                            continue
                        pairs.append(
                            {
                                "atom_a": atom_a["id"],
                                "atom_b": atom_b["id"],
                                "topic_key": topic["topic_key"],
                                "overlap": round(overlap, 3),
                                "reason": _explain(text_a, text_b, toks_a, toks_b),
                                "preview_a": text_a[:160],
                                "preview_b": text_b[:160],
                                "tier_a": atom_a["tier"],
                                "tier_b": atom_b["tier"],
                            }
                        )
        finally:
            conn.close()
    except sqlite3.Error as exc:
        log.warning("conflict scan failed: %s", exc)
        return []
    pairs.sort(key=lambda p: p["overlap"], reverse=True)
    return pairs


def _explain(text_a: str, text_b: str, toks_a: set[str], toks_b: set[str]) -> str:
    pol_a = _polarity(toks_a)
    pol_b = _polarity(toks_b)
    if pol_a != pol_b and "neutral" not in (pol_a, pol_b):
        return f"polarity flip ({pol_a} vs {pol_b})"
    nums_a = _numeric_signature(text_a)
    nums_b = _numeric_signature(text_b)
    if nums_a and nums_b and set(nums_a) != set(nums_b):
        only_a = sorted(set(nums_a) - set(nums_b))[:3]
        only_b = sorted(set(nums_b) - set(nums_a))[:3]
        return f"numeric divergence ({only_a} vs {only_b})"
    return "diverging tokens in same topic cluster"


def materialize_review_tasks(
    *,
    brain_db_path: Path | str,
    max_tasks: int = MAX_TASKS_PER_RUN,
    task_queue_obj: Any | None = None,
) -> dict:
    """Find conflicts and turn the top-N into bounded review tasks.

    Dedupes against existing open tasks by signature. Mirrors
    outcome_feedback.create_override_review_tasks contract:
    - no policy mutation
    - cli_llm dispatch for the investigation
    - returns counts + skipped reasons so the scheduler can log
    """
    pairs = find_conflicts(brain_db_path=brain_db_path)
    if not pairs:
        return {"created": [], "skipped": [], "found": 0}

    tq = task_queue_obj or _default_task_queue()
    if tq is None:
        return {
            "created": [],
            "skipped": [{"reason": "task_queue_unavailable"}],
            "found": len(pairs),
        }
    open_signatures = _open_signatures(tq)
    created: list[dict] = []
    skipped: list[dict] = []
    for pair in pairs[: max(1, int(max_tasks))]:
        sig = _signature(pair["atom_a"], pair["atom_b"])
        if sig in open_signatures:
            skipped.append({"signature": sig, "reason": "open_task_exists"})
            continue
        title = f"Resolve atom conflict in {pair['topic_key']}"
        description = (
            f"Two non-superseded atoms in the same topic_key may disagree.\n"
            f"Reason: {pair['reason']} (token overlap {pair['overlap']}).\n\n"
            f"A ({pair['atom_a']}, tier={pair['tier_a']}): {pair['preview_a']}\n"
            f"B ({pair['atom_b']}, tier={pair['tier_b']}): {pair['preview_b']}\n\n"
            "Decide: do these actually conflict? If yes, supersede the wrong one "
            "via brain_correct(replaces=[...]) and explain why."
        )
        task = tq.create_task(
            title=title,
            description=description,
            assigned_agent="brain_cli",
            priority=4,
            confidence=0.6,
            confidence_reasoning="heuristic conflict surfacer — human/agent must verify",
            created_by="conflict_surfacer",
            metadata={
                "domain": "brain-system",
                "source": "conflict_surfacer",
                "conflict_signature": sig,
                "atom_a": pair["atom_a"],
                "atom_b": pair["atom_b"],
                "topic_key": pair["topic_key"],
                "overlap": pair["overlap"],
                "reason": pair["reason"],
                "mutates_policy": False,
                "uses_llm": True,
                "llm_dispatch": "cli_llm",
            },
        )
        created.append({"signature": sig, "task_id": task.get("id"), "title": title})
        open_signatures.add(sig)
    return {"created": created, "skipped": skipped, "found": len(pairs)}


def _default_task_queue() -> Any | None:
    try:
        from task_queue import task_queue

        return task_queue
    except Exception as exc:
        log.debug("conflict_surfacer: task_queue unavailable: %s", exc)
        return None


def _open_signatures(task_queue_obj: Any) -> set[str]:
    sigs: set[str] = set()
    try:
        statuses = ("pending", "approved", "running")
        for status in statuses:
            for task in task_queue_obj.list_tasks(status=status) or []:
                md = task.get("metadata") or {}
                sig = md.get("conflict_signature")
                if sig:
                    sigs.add(sig)
    except Exception as exc:
        log.debug("conflict_surfacer: open-task scan failed: %s", exc)
    return sigs


def run_default(brain_db_path: Path | str | None = None) -> dict:
    """Entry point for the scheduled job. Resolves BRAIN_DB lazily so the
    function can be imported in environments where config is mocked."""
    if brain_db_path is None:
        from config import BRAIN_DB

        brain_db_path = BRAIN_DB
    return materialize_review_tasks(brain_db_path=brain_db_path)
