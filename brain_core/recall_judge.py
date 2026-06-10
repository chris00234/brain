"""brain_core/recall_judge.py — LLM-as-judge for recall quality.

self_eval.py measures retrieval determinism (does the same query consistently
return the same atoms?). It does NOT measure correctness — the system can
"reliably" return the wrong answer and self_eval would say everything is fine.

This module samples recent /recall/v2 calls, sends (query, top-k retrieved
documents) to the LLM judge, and writes a per-call relevance/groundedness
score. Strong scores back-feed into action_audit.outcome so calibration and
LtR see real-quality labels — not just synthetic eval-set passes.

Cost: ~50 sampled queries/day → 50 LLM calls/day on the ChatGPT Pro
subscription. Subscription-bounded, no per-call billing.

Output:
  - recall_judgments table (logs/brain.db) — per-call structured judgment
  - action_audit.outcome — set to 'judged_good' or 'judged_wrong' when the
    judge confidence + relevance score crosses a threshold
"""

from __future__ import annotations

import json
import logging
import random
import sqlite3
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cli_llm import dispatch as _dispatch

from config import BRAIN_DB

log = logging.getLogger("brain.recall_judge")

SAMPLE_SIZE = 100
TIMEOUT_SEC = 30
JUDGE_GOOD_RELEVANCE = 0.7
JUDGE_WRONG_RELEVANCE = 0.3
TOP_K_FOR_JUDGE = 3  # only evaluate the top-3 retrieved docs to keep prompts compact
JUDGED_ACTORS = ("claude", "codex", "mcp", "jenna", "sage", "liz", "ellie", "market", "brain")
JUDGED_ACTOR_PLACEHOLDERS = "?, ?, ?, ?, ?, ?, ?, ?, ?"
# Wall-clock budget. 100 samples x (10s recall + 30s judge) worst-case is
# ~67 minutes, but realistic average is ~10s/sample so the cap rarely
# bites. 2026-05-13: raised 600→1500 along with the SAMPLE_SIZE bump
# (50→100). The previous cap was hitting before the sample size — actual
# 35 judged/day vs 50 cap. Cap stays well under the next scheduled job
# slot so the scheduler thread is never blocked.
MAX_RUN_SECONDS = 1500

_PROMPT = """You are a relevance judge for a personal-memory retrieval system. Given a
USER QUERY and the top retrieved documents, score whether the documents actually
answer the query with CURRENT, useful memory.

USER QUERY: {query}

RETRIEVED DOCUMENTS:
{docs}

Respond with ONE LINE of JSON, no prose:
{{"relevance": 0.0-1.0, "groundedness": 0.0-1.0, "reason": "<=80 chars"}}

- relevance: do the docs address the query topic? (0=off-topic, 1=directly answers)
- groundedness: are the docs concrete factual statements vs vague restatements? (0=vague, 1=specific)
- reason: brief sentence explaining the relevance score

Scoring rules (apply to BOTH scores):
- Stale beats nothing only if marked: a doc that is superseded, archived, or
  contradicted by a more recent doc in the set scores LOW unless the query
  explicitly asks for history.
- Authority: a stated current fact/preference/decision outranks a session log,
  weekly digest, or summary that merely mentions the topic. Quoting the query's
  own words (test fixtures, transcripts echoing the question) is NOT an answer.
- Cross-lingual: the query and a doc may be in different languages (Korean and
  English). Judge by meaning — a doc that answers a Korean query in English (or
  vice versa) is fully relevant; never penalize language mismatch.
"""


def _ensure_table(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS recall_judgments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            action_audit_id INTEGER NOT NULL,
            query_text TEXT,
            relevance REAL,
            groundedness REAL,
            reason TEXT,
            judge_provider TEXT,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_recall_judgments_audit
          ON recall_judgments(action_audit_id);
        CREATE INDEX IF NOT EXISTS idx_recall_judgments_created
          ON recall_judgments(created_at);
        """
    )
    conn.commit()


def _fetch_docs_for_query(query: str) -> dict[str, str]:
    """Live re-recall: ask brain what it would surface NOW for this query.

    The action_audit id formats vary across routes (16-char hex, 32-char hex,
    semantic_memory:HASH) and don't all round-trip back to documents. Rather
    than reverse-engineer per-route id schemes, we re-execute the recall and
    judge the live top-K. Trade-off: the judgment reflects current ranking,
    not the exact ranking the caller saw — which is fine for a quality signal,
    since the brain is supposed to be improving.
    """
    if not query:
        return {}
    try:
        import urllib.parse
        from pathlib import Path

        from http_pool import http_json

        secret_path = Path.home() / ".brain/credentials/.personal_webhook_secret"
        token = secret_path.read_text().strip() if secret_path.exists() else ""
        qs = urllib.parse.urlencode({"q": query[:500], "k": TOP_K_FOR_JUDGE, "actor": "recall_judge"})
        payload = http_json(
            "GET",
            f"http://127.0.0.1:8791/recall/v2?{qs}",
            timeout=10,
            headers={"Authorization": f"Bearer {token}"} if token else None,
        )
    except Exception:
        return {}
    docs: dict[str, str] = {}
    for r in (payload.get("results") or [])[:TOP_K_FOR_JUDGE]:
        rid = str(r.get("id") or r.get("chunk_id") or len(docs))[:24]
        text = r.get("content") or r.get("title") or r.get("document") or ""
        if text:
            docs[rid] = text[:600]
    return docs


def _judge_one(query: str, docs_by_id: dict[str, str]) -> dict | None:
    if not docs_by_id:
        return None
    formatted = "\n\n".join(
        f"[{i + 1}] ({aid[:24]}…): {doc}" for i, (aid, doc) in enumerate(docs_by_id.items())
    )
    prompt = _PROMPT.format(query=query[:500], docs=formatted)
    result = _dispatch(agent="jenna", message=prompt, thinking="low", timeout=TIMEOUT_SEC)
    if not result.ok or not result.text:
        return None
    text = result.text.strip()
    # Strip code fences if the model wrapped JSON
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:].lstrip()
    try:
        parsed = json.loads(text.splitlines()[0] if "\n" in text else text)
    except Exception:
        return None
    if not isinstance(parsed, dict):
        return None
    rel = parsed.get("relevance")
    grd = parsed.get("groundedness")
    if not isinstance(rel, int | float) or not isinstance(grd, int | float):
        return None
    return {
        "relevance": max(0.0, min(1.0, float(rel))),
        "groundedness": max(0.0, min(1.0, float(grd))),
        "reason": str(parsed.get("reason", ""))[:200],
        "provider": result.provider or "unknown",
    }


def run(sample: int = SAMPLE_SIZE, hours: int = 24, dry_run: bool = False) -> dict:
    """Sample N recent recalls, judge them, write back to action_audit + recall_judgments."""
    conn = sqlite3.connect(str(BRAIN_DB))
    conn.row_factory = sqlite3.Row
    _ensure_table(conn)
    counters = {"sampled": 0, "judged": 0, "good": 0, "wrong": 0, "skipped": 0, "budget_exit": 0}
    start_time = time.monotonic()
    try:
        # Filter out empty-retrieval rows (the pretool brain nudge fires a
        # /recall/v2 for every Bash/Grep hook invocation; many of those queries
        # match nothing and have retrieved_atom_ids='[]'). Nothing to judge.
        # Both /recall/v2 and /recall/active are eligible — the judge does a
        # live re-recall against the query_text. Filters:
        #   - actor whitelist: only judge Chris's CLI sessions (claude/codex) and
        #     named Hermes profiles. Excludes 'eval' (deterministic eval runs,
        #     non-representative), 'unknown' (hook-driven Bash command echos),
        #     and 'recall_judge' (self-feedback loop)
        #   - length >= 5 (skip empty/trivial)
        #   - exclude shell-command queries that slip past the actor filter
        #     (defense in depth — pretool_brain_nudge sometimes uses different
        #     actors)
        cutoff = (
            (datetime.now(UTC) - timedelta(hours=max(1, int(hours or 24))))
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z")
        )
        rows = conn.execute(
            "SELECT id, query_text "
            "FROM action_audit "
            "WHERE route IN ('/recall/v2', '/recall/active') "
            "  AND outcome IS NULL "
            "  AND query_text IS NOT NULL "
            "  AND length(query_text) >= 5 "
            "  AND actor IN (" + JUDGED_ACTOR_PLACEHOLDERS + ") "
            "  AND query_text NOT LIKE 'sed %' "
            "  AND query_text NOT LIKE 'grep %' "
            "  AND query_text NOT LIKE 'cat %' "
            "  AND query_text NOT LIKE 'awk %' "
            "  AND query_text NOT LIKE 'find %' "
            "  AND query_text NOT LIKE 'ls %' "
            "  AND query_text NOT LIKE 'echo %' "
            "  AND query_text NOT LIKE 'curl %' "
            "  AND query_text NOT LIKE 'rg %' "
            "  AND query_text NOT LIKE 'python %' "
            "  AND query_text NOT LIKE '/Users/%' "
            "  AND query_text NOT LIKE '%.py %' "
            "  AND query_text NOT LIKE '%.md %' "
            "  AND query_text NOT LIKE '{%' "
            "  AND query_text NOT LIKE '[%' "
            "  AND created_at > ? "
            "ORDER BY created_at DESC "
            "LIMIT ?",
            (*JUDGED_ACTORS, cutoff, sample * 4),  # over-fetch so random.sample has room
        ).fetchall()
        if not rows:
            log.info("recall_judge: no candidates")
            return counters
        chosen = random.sample(list(rows), min(sample, len(rows)))
        for row in chosen:
            if time.monotonic() - start_time > MAX_RUN_SECONDS:
                counters["budget_exit"] += 1
                log.warning(
                    "recall_judge wall-clock budget exhausted at %d/%d samples",
                    counters["sampled"],
                    len(chosen),
                )
                break
            counters["sampled"] += 1
            docs = _fetch_docs_for_query(row["query_text"])
            if not docs:
                counters["skipped"] += 1
                continue
            judgment = _judge_one(row["query_text"], docs)
            if not judgment:
                counters["skipped"] += 1
                continue
            counters["judged"] += 1
            now = datetime.now(UTC).isoformat(timespec="seconds")
            if not dry_run:
                conn.execute(
                    "INSERT INTO recall_judgments "
                    "(action_audit_id, query_text, relevance, groundedness, reason, "
                    " judge_provider, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        row["id"],
                        row["query_text"][:500],
                        judgment["relevance"],
                        judgment["groundedness"],
                        judgment["reason"],
                        judgment["provider"],
                        now,
                    ),
                )
            if judgment["relevance"] >= JUDGE_GOOD_RELEVANCE:
                counters["good"] += 1
                if not dry_run:
                    conn.execute(
                        "UPDATE action_audit SET outcome = ?, outcome_reason = ?, "
                        "resolved_at = ? WHERE id = ? AND outcome IS NULL",
                        (
                            "judged_good",
                            json.dumps(
                                {
                                    "relevance": judgment["relevance"],
                                    "groundedness": judgment["groundedness"],
                                }
                            ),
                            now,
                            row["id"],
                        ),
                    )
            elif judgment["relevance"] <= JUDGE_WRONG_RELEVANCE:
                counters["wrong"] += 1
                if not dry_run:
                    conn.execute(
                        "UPDATE action_audit SET outcome = ?, outcome_reason = ?, "
                        "resolved_at = ? WHERE id = ? AND outcome IS NULL",
                        (
                            "judged_wrong",
                            json.dumps(
                                {
                                    "relevance": judgment["relevance"],
                                    "groundedness": judgment["groundedness"],
                                    "reason": judgment["reason"],
                                }
                            ),
                            now,
                            row["id"],
                        ),
                    )
        if not dry_run:
            conn.commit()
    finally:
        conn.close()
    log.info("recall_judge: %s", counters)
    return counters


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--sample", type=int, default=SAMPLE_SIZE)
    parser.add_argument("--hours", type=int, default=24)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    print(json.dumps(run(sample=args.sample, hours=args.hours, dry_run=args.dry_run), indent=2))
