"""brain_core/eval_holdout_promote.py - weekly eval auto-growth pipeline (Phase C1).

Reads candidate proposals from `eval_proposals` table, scores them by novelty
against the existing eval_set.json, drops near-duplicates, and writes the top-N
to a pending file for human audit.

Schedule: Sun 8:45am via JOB_REGISTRY/scheduler (registered separately).

Novelty scoring: 1 - max cosine similarity against existing eval set queries
using the local Ollama embedder. No new API spend (uses existing rail).
"""

from __future__ import annotations

import json
import logging
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


log = logging.getLogger("brain.eval_holdout_promote")

try:
    from eval_proposals import list_candidates, mark_status

    from config import AUTONOMY_DB, BRAIN_DIR
except ImportError:
    AUTONOMY_DB = Path("/Users/chrischo/server/brain/logs/autonomy.db")
    BRAIN_DIR = Path("/Users/chrischo/server/brain")
    list_candidates = None  # type: ignore[assignment]
    mark_status = None  # type: ignore[assignment]


EVAL_SET_PATH = BRAIN_DIR / "cli" / "eval_set.json"
PENDING_PATH = BRAIN_DIR / "cli" / "eval_holdout_pending.json"

NOVELTY_THRESHOLD = 0.30  # below this similarity = novel enough to promote
TOP_N = 5  # max items to promote per weekly run

# Phase N3 — auto-graduation thresholds
AUTO_GRADUATE_MIN_RUNS = 4
AUTO_GRADUATE_PASS_RATIO = 0.75
AUTO_REJECT_MIN_RUNS = 5
AUTO_REJECT_MAX_PASSES = 1
AUTO_GRADUATE_WEEKLY_CAP = 5
TELEGRAM_STUCK_THRESHOLD_DAYS = 14

try:
    from config import BRAIN_DB
except ImportError:
    BRAIN_DB = Path("/Users/chrischo/server/brain/logs/brain.db")


def _embed(text: str) -> list[float] | None:
    """Embed via the local Ollama embedder. Returns None on failure.

    indexer exposes `get_embedding(text)` for single texts and
    `get_embeddings_batch(texts)` for batches. The previous import of
    `_embed_texts` was a stale name and silently broke the M7 self-evolution
    loop (caught by tests/integration/test_self_evolution_e2e.py).
    """
    try:
        from indexer import get_embedding  # type: ignore[attr-defined]

        result = get_embedding(text, prefix="query")
        if result and isinstance(result, list):
            return result
    except Exception as exc:
        log.warning("embed failed: %s", exc)
    return None


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(y * y for y in b) ** 0.5
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def _load_eval_queries() -> list[str]:
    """Load existing eval queries to compare novelty against."""
    if not EVAL_SET_PATH.exists():
        return []
    try:
        data = json.loads(EVAL_SET_PATH.read_text())
        return [item.get("query", "") for item in data if item.get("query")]
    except Exception as exc:
        log.warning("failed to load eval set: %s", exc)
        return []


def _max_similarity_to_existing(query_emb: list[float], existing_embs: list[list[float]]) -> float:
    return max((_cosine_similarity(query_emb, e) for e in existing_embs), default=0.0)


def run() -> dict:
    """Walk candidate proposals, compute novelty, promote top-N to pending file."""
    if list_candidates is None or mark_status is None:
        return {"error": "eval_proposals module unavailable"}

    candidates = list_candidates(status="candidate", limit=200)
    if not candidates:
        return {"checked": 0, "promoted": 0, "rejected": 0, "reason": "no candidates"}

    existing_queries = _load_eval_queries()
    if not existing_queries:
        log.warning("no existing eval queries to compare against — promoting all candidates as novel")
        existing_embs: list[list[float]] = []
    else:
        # Embed existing queries once (could be cached, but rebuild weekly is fine)
        existing_embs = []
        for q in existing_queries:
            emb = _embed(q)
            if emb:
                existing_embs.append(emb)

    scored: list[tuple[float, dict]] = []
    rejected = 0
    for cand in candidates:
        cand_query = cand.get("query") or ""
        if not cand_query:
            continue
        cand_emb = _embed(cand_query)
        if not cand_emb:
            continue
        max_sim = _max_similarity_to_existing(cand_emb, existing_embs)
        novelty = 1.0 - max_sim
        if novelty < NOVELTY_THRESHOLD:
            mark_status(cand["id"], "rejected", novelty_score=novelty)
            rejected += 1
            continue
        scored.append((novelty, cand))

    # Top-N by novelty
    scored.sort(key=lambda x: x[0], reverse=True)
    promoted = scored[:TOP_N]

    # M7-WS7 H1 fix: merge with existing pending file instead of clobbering.
    # Before this fix, every Sunday run rewrote eval_holdout_pending.json from
    # scratch, dropping items still awaiting human review (Telegram digest had
    # already gone out, but Chris hadn't approved/rejected them yet). New
    # behavior: read existing file, dedupe by id, append novel items, persist.
    existing_pending: list[dict] = []
    if PENDING_PATH.exists():
        try:
            existing_pending = json.loads(PENDING_PATH.read_text())
            if not isinstance(existing_pending, list):
                existing_pending = []
        except (json.JSONDecodeError, OSError):
            log.warning("could not read existing pending file; starting fresh")
            existing_pending = []

    existing_ids = {row.get("id") for row in existing_pending if isinstance(row, dict)}

    new_rows: list[dict] = []
    skipped_dupes = 0
    for novelty, cand in promoted:
        if cand["id"] in existing_ids:
            skipped_dupes += 1
            mark_status(cand["id"], "pending", novelty_score=novelty)
            continue
        new_rows.append(
            {
                "id": cand["id"],
                "query": cand["query"],
                "expected": cand["expected"],
                "expected_sources": json.loads(cand.get("expected_sources") or "[]"),
                "novelty": round(novelty, 3),
                "source_event": cand.get("source_event"),
                "promoted_at": _now_iso(),
            }
        )
        mark_status(cand["id"], "pending", novelty_score=novelty)

    pending_payload = existing_pending + new_rows

    PENDING_PATH.parent.mkdir(parents=True, exist_ok=True)
    PENDING_PATH.write_text(json.dumps(pending_payload, indent=2, ensure_ascii=False))

    # Phase N3: seed eval_holdout_lifecycle rows so auto_graduate has something
    # to track nightly. Best-effort — if brain.db is unavailable, the weekly
    # promote still succeeds and we fall back to the legacy Telegram path.
    if new_rows:
        try:
            conn = _lifecycle_conn()
            try:
                conn.execute("BEGIN IMMEDIATE")
                for row in new_rows:
                    _ensure_lifecycle_row(conn, row["id"], row.get("promoted_at"))
                conn.commit()
            finally:
                conn.close()
        except sqlite3.Error as exc:
            log.warning("lifecycle seeding failed: %s", exc)

    return {
        "checked": len(candidates),
        "promoted": len(new_rows),
        "rejected": rejected,
        "pending_file": str(PENDING_PATH),
        "pending_total": len(pending_payload),
        "duplicates_skipped": skipped_dupes,
    }


# ── Phase N3: holdout lifecycle + auto-graduation ──────────────────────


def _lifecycle_conn(db_path: Path | None = None) -> sqlite3.Connection:
    target = db_path or BRAIN_DB
    conn = sqlite3.connect(str(target))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _ensure_lifecycle_row(
    conn: sqlite3.Connection, candidate_id: str, promoted_at: str | None = None
) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO eval_holdout_lifecycle (candidate_id, promoted_at) " "VALUES (?, ?)",
        (candidate_id, promoted_at or _now_iso()),
    )


def record_eval_result(candidate_id: str, pass_bool: bool, db_path: Path | None = None) -> dict | None:
    """Phase N3: record one nightly eval outcome for a pending holdout candidate.

    Called by cli/eval_gate.py after the stable eval run — one pass per
    candidate per night. Increments eval_runs (+ eval_passes when pass_bool).
    Returns the updated row or None on error.
    """
    if not candidate_id:
        return None
    try:
        conn = _lifecycle_conn(db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            _ensure_lifecycle_row(conn, candidate_id)
            conn.execute(
                "UPDATE eval_holdout_lifecycle SET "
                "  eval_runs = eval_runs + 1, "
                "  eval_passes = eval_passes + ? "
                "WHERE candidate_id = ?",
                (1 if pass_bool else 0, candidate_id),
            )
            row = conn.execute(
                "SELECT * FROM eval_holdout_lifecycle WHERE candidate_id = ?",
                (candidate_id,),
            ).fetchone()
            conn.commit()
            return dict(row) if row else None
        finally:
            conn.close()
    except sqlite3.Error as exc:
        log.warning("record_eval_result failed: %s", exc)
        return None


def _write_json_atomic(path: Path, payload: list) -> None:
    """Write payload to path via *.tmp + rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    tmp.replace(path)


def _load_pending_map() -> tuple[list[dict], dict[str, dict]]:
    """Return (raw_list, id→entry dict) of the current pending file."""
    if not PENDING_PATH.exists():
        return [], {}
    try:
        raw = json.loads(PENDING_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return [], {}
    if not isinstance(raw, list):
        return [], {}
    return raw, {e["id"]: e for e in raw if isinstance(e, dict) and e.get("id")}


def auto_graduate(db_path: Path | None = None) -> dict:
    """Phase N3: promote consistently-passing holdout candidates back into the
    frozen eval set. Runs Sun 7:30 (scheduler) right BEFORE the existing
    eval_holdout_promote Sun 8:45 so this week's graduates exit pending before
    new candidates arrive.

    Rules (hard-coded):
      graduate — eval_runs >= 4 AND eval_passes / eval_runs >= 0.75
      reject   — eval_runs >= 5 AND eval_passes <= 1
      weekly graduation cap = 5 (defense against runaway eval-set bloat)

    A per-run backup (eval_set.json.backup) is written before the atomic
    rewrite so rollback is a single file rename.
    """
    summary = {
        "graduated": 0,
        "rejected": 0,
        "still_pending": 0,
        "cap_reached": False,
    }

    raw_pending, pending_by_id = _load_pending_map()
    if not pending_by_id:
        return {**summary, "reason": "no_pending"}

    try:
        conn = _lifecycle_conn(db_path)
    except sqlite3.OperationalError as exc:
        return {**summary, "error": f"lifecycle_db_unavailable: {exc}"}

    graduated_ids: list[str] = []
    rejected_ids: list[dict[str, str]] = []

    try:
        rows = conn.execute(
            "SELECT candidate_id, eval_runs, eval_passes, auto_stable_at, rejected_at "
            "FROM eval_holdout_lifecycle WHERE auto_stable_at IS NULL AND rejected_at IS NULL"
        ).fetchall()
        # Weekly cap — count graduations in the last 7 days
        cutoff = (datetime.now(UTC) - timedelta(days=7)).isoformat(timespec="seconds")
        graduated_7d = conn.execute(
            "SELECT COUNT(*) FROM eval_holdout_lifecycle WHERE auto_stable_at >= ?",
            (cutoff,),
        ).fetchone()[0]
        remaining_cap = max(0, AUTO_GRADUATE_WEEKLY_CAP - graduated_7d)

        conn.execute("BEGIN IMMEDIATE")
        for row in rows:
            cid = row["candidate_id"]
            if cid not in pending_by_id:
                continue
            runs = row["eval_runs"] or 0
            passes = row["eval_passes"] or 0
            if runs >= AUTO_GRADUATE_MIN_RUNS and passes / max(1, runs) >= AUTO_GRADUATE_PASS_RATIO:
                if remaining_cap <= 0:
                    summary["cap_reached"] = True
                    continue
                conn.execute(
                    "UPDATE eval_holdout_lifecycle SET auto_stable_at = ? WHERE candidate_id = ?",
                    (_now_iso(), cid),
                )
                graduated_ids.append(cid)
                remaining_cap -= 1
                continue
            if runs >= AUTO_REJECT_MIN_RUNS and passes <= AUTO_REJECT_MAX_PASSES:
                reason = f"failed {runs} runs with {passes} passes"
                conn.execute(
                    "UPDATE eval_holdout_lifecycle SET rejected_at = ?, reject_reason = ? "
                    "WHERE candidate_id = ?",
                    (_now_iso(), reason, cid),
                )
                rejected_ids.append({"id": cid, "reason": reason})
        conn.commit()
    finally:
        conn.close()

    if graduated_ids:
        try:
            existing = []
            if EVAL_SET_PATH.exists():
                existing = json.loads(EVAL_SET_PATH.read_text())
                if not isinstance(existing, list):
                    existing = []
                # Snapshot backup ONCE before the rewrite
                backup_path = EVAL_SET_PATH.with_suffix(EVAL_SET_PATH.suffix + ".backup")
                backup_path.write_text(json.dumps(existing, indent=2, ensure_ascii=False))

            existing_queries = {e.get("query") for e in existing if isinstance(e, dict)}
            for cid in graduated_ids:
                candidate = pending_by_id.get(cid)
                if not candidate:
                    continue
                q = candidate.get("query")
                if q and q in existing_queries:
                    continue
                existing.append(
                    {
                        "query": candidate.get("query", ""),
                        "expected_source": "",
                        "expected_content": candidate.get("expected", ""),
                        "_graduated_from_holdout": True,
                        "_graduated_at": _now_iso(),
                        "_candidate_id": cid,
                    }
                )
            _write_json_atomic(EVAL_SET_PATH, existing)
        except Exception as exc:
            log.error("eval_set write failed during auto_graduate: %s", exc)
            return {**summary, "error": str(exc)}

    # Remove graduated + rejected ids from the pending file
    resolved_ids = set(graduated_ids) | {r["id"] for r in rejected_ids}
    if resolved_ids:
        new_pending = [e for e in raw_pending if e.get("id") not in resolved_ids]
        _write_json_atomic(PENDING_PATH, new_pending)
        summary["still_pending"] = len(new_pending)
    else:
        summary["still_pending"] = len(raw_pending)

    summary["graduated"] = len(graduated_ids)
    summary["rejected"] = len(rejected_ids)
    summary["graduated_ids"] = graduated_ids
    summary["rejected_ids"] = rejected_ids
    return summary


def stuck_candidates(
    db_path: Path | None = None, threshold_days: int = TELEGRAM_STUCK_THRESHOLD_DAYS
) -> list[dict]:
    """Return candidates whose lifecycle row has been pending >= N days with
    no auto-graduation or rejection. Used by eval_holdout_audit to gate the
    Telegram digest (N3 removes Telegram from the routine path).
    """
    try:
        conn = _lifecycle_conn(db_path)
    except sqlite3.OperationalError:
        return []
    try:
        cutoff = (datetime.now(UTC) - timedelta(days=threshold_days)).isoformat(timespec="seconds")
        rows = conn.execute(
            "SELECT candidate_id, promoted_at, eval_runs, eval_passes FROM eval_holdout_lifecycle "
            "WHERE auto_stable_at IS NULL AND rejected_at IS NULL AND promoted_at <= ?",
            (cutoff,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--graduate",
        action="store_true",
        help="Phase N3: run auto_graduate instead of the weekly promote",
    )
    args = parser.parse_args()
    if args.graduate:
        sys.stdout.write(json.dumps(auto_graduate(), indent=2) + "\n")
    else:
        sys.stdout.write(json.dumps(run(), indent=2) + "\n")
