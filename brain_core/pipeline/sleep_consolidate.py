#!/Users/chrischo/server/brain/.venv/bin/python3
"""brain_core/pipeline/sleep_consolidate.py — Phase N4 sleep consolidation.

CLS (Complementary Learning Systems, McClelland et al) applied to the brain:
during the daily 3:55am idle window, replay the last 48h of retrievals,
build co-activation edges between atoms that fired together, promote
frequently-accessed episodic atoms to semantic, and grow the A-MEM
Zettelkasten via k-NN linking.

Algorithm (from the plan file):
  1. Pull action_audit rows from last 48h, group by session_id (fallback:
     30-min sliding window on created_at). Cap top-8 per session by rank.
  2. UPSERT atom_coactivation(n_events += 1, last_seen_at=now).
  3. Compute f_atom = 30d access frequency per atom.
  4. Episodic → semantic promotion: tier='episodic' AND f>=5 AND co_high>=2
     AND confidence>=0.6. Fire update_atom_confidence reinforce +0.3.
  5. A-MEM auto-linking: for every atom retrieved in window, query k=5
     semantic_memory neighbors within cosine 0.22, INSERT OR IGNORE
     provenance(relation='related'). Cap 3 new edges per atom per run.
  6. Predictive-error weighting: atoms with contradict events in last 7d
     get consolidation weight × 0.5 (high-surprise demotion).
  7. Sage summary via openclaw_dispatch if replay_count >= 20. Only LLM call.
  8. Log one sleep_cycles row.

Schedule: daily 3:55am, after memory_consolidation (3:45) which it depends on.

Wall-clock budget: < 60s on ~500 atoms, ~2400 raw_events baseline.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

BRAIN_CORE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BRAIN_CORE))

from atoms_store import (  # noqa: E402
    BRAIN_ATOMS_ENABLED,
    BRAIN_DB,
    _now,
    update_atom_confidence,
)

SLEEP_WINDOW_HOURS = 48
TOP_PAIRS_PER_SESSION = 8
# 2026-04-16 Tier 3 #2: Complementary Learning Systems interleaved replay
# (McClelland et al. 1995). The hippocampal replay that drives cortical
# consolidation in biology interleaves old AND new memories — replaying
# only new observations causes catastrophic forgetting of old traces.
# Each sleep cycle now samples K existing high-confidence semantic atoms
# alongside the 48h action_audit window and re-runs coactivation across
# the combined set. Old traces maintain their neighborhood connectivity;
# new traces get woven into existing schema rather than floating alone.
INTERLEAVE_OLD_SAMPLES = 32
INTERLEAVE_MIN_CONFIDENCE = 0.6
INTERLEAVE_MIN_TIER = "semantic"  # episodic/semantic/core — skip episodic
AMEM_K = 5
AMEM_DIST_THRESHOLD = 0.22
AMEM_EDGES_PER_ATOM = 3
FREQUENCY_LOOKBACK_DAYS = 30
PROMOTION_FREQ_THRESHOLD = 5
PROMOTION_COHIGH_THRESHOLD = 2
PROMOTION_CONFIDENCE_FLOOR = 0.6
PROMOTION_REINFORCE_WEIGHT = 0.3
PREDICTIVE_ERROR_DEMOTION = 0.5
SUMMARY_TRIGGER_REPLAY_COUNT = 20
SUMMARY_TIMEOUT_SEC = 60
MAX_COACTIVATION_ROWS = 100_000  # emergency cap — alert + skip upsert if exceeded


def _now_utc() -> datetime:
    return datetime.now(UTC)


def _start_cycle(conn: sqlite3.Connection) -> int:
    cur = conn.execute(
        "INSERT INTO sleep_cycles (started_at, replay_count, edges_added, consolidated) "
        "VALUES (?, 0, 0, 0)",
        (_now(),),
    )
    conn.commit()
    return int(cur.lastrowid)


def _finish_cycle(
    conn: sqlite3.Connection,
    cycle_id: int,
    *,
    replay_count: int,
    edges_added: int,
    consolidated: int,
    summary: dict,
) -> None:
    conn.execute(
        "UPDATE sleep_cycles SET ended_at=?, replay_count=?, edges_added=?, "
        "consolidated=?, summary_json=? WHERE id=?",
        (_now(), replay_count, edges_added, consolidated, json.dumps(summary), cycle_id),
    )
    conn.commit()


def _sample_old_high_confidence_atoms(conn: sqlite3.Connection) -> list[str]:
    """2026-04-16 Tier 3 #2: sample N old high-confidence atoms to
    interleave with the 48h replay window. Prevents catastrophic
    forgetting of stable long-tail semantic knowledge.

    Pulls atoms that are:
      - tier in (semantic, core) — already consolidated, worth rehearsing
      - confidence >= INTERLEAVE_MIN_CONFIDENCE — trusted enough to matter
      - updated_at NOT in the last 7 days — actually OLD, not redundantly
        picked up via the recent window

    Random sample keeps the interleave broad rather than biased toward
    one cluster. Chroma_ids returned so they merge cleanly into the
    session-grouping pipeline as pseudo-retrievals.
    """
    recent_cutoff = (_now_utc() - timedelta(days=7)).isoformat(timespec="seconds")
    try:
        rows = conn.execute(
            "SELECT chroma_id FROM atoms "
            "WHERE tier IN ('semantic', 'core') "
            "AND confidence >= ? "
            "AND updated_at < ? "
            "ORDER BY RANDOM() "
            "LIMIT ?",
            (INTERLEAVE_MIN_CONFIDENCE, recent_cutoff, INTERLEAVE_OLD_SAMPLES),
        ).fetchall()
    except sqlite3.Error:
        return []
    return [r["chroma_id"] for r in rows if r["chroma_id"]]


def _fetch_window(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    cutoff = (_now_utc() - timedelta(hours=SLEEP_WINDOW_HOURS)).isoformat(timespec="seconds")
    return conn.execute(
        "SELECT id, route, tool, actor, session_id, retrieved_chroma_ids, created_at "
        "FROM action_audit WHERE created_at >= ? AND retrieved_chroma_ids IS NOT NULL "
        "ORDER BY created_at ASC",
        (cutoff,),
    ).fetchall()


def _group_sessions(rows: list[sqlite3.Row]) -> list[list[str]]:
    """Group retrieved atom ids by session_id (fallback: 30-min sliding window)."""
    groups: dict[str, list[str]] = defaultdict(list)
    fallback_window = timedelta(minutes=30)
    last_ts: datetime | None = None
    fallback_bucket: str = "w_0"
    fallback_counter = 0
    for row in rows:
        try:
            chroma_ids = json.loads(row["retrieved_chroma_ids"] or "[]")
        except json.JSONDecodeError:
            continue
        if not isinstance(chroma_ids, list) or not chroma_ids:
            continue
        session_key = row["session_id"]
        if not session_key:
            try:
                ts = datetime.fromisoformat(row["created_at"].rstrip("Z")).replace(tzinfo=UTC)
            except Exception:
                continue
            if last_ts is None or (ts - last_ts) > fallback_window:
                fallback_counter += 1
                fallback_bucket = f"w_{fallback_counter}"
            last_ts = ts
            session_key = fallback_bucket
        # top-8 cap is on the full session, not per row — trim once at the end
        groups[session_key].extend(str(cid) for cid in chroma_ids if cid)
    return [ids[:TOP_PAIRS_PER_SESSION] for ids in groups.values() if len(ids) >= 2]


def _resolve_atom_ids(conn: sqlite3.Connection, chroma_ids: list[str]) -> dict[str, str]:
    """Map chroma_id → atoms.id for the given list. Missing entries are omitted."""
    if not chroma_ids:
        return {}
    placeholders = ",".join("?" * len(chroma_ids))
    rows = conn.execute(
        f"SELECT chroma_id, id FROM atoms WHERE chroma_id IN ({placeholders})",
        tuple(chroma_ids),
    ).fetchall()
    return {r["chroma_id"]: r["id"] for r in rows}


def _upsert_pair(conn: sqlite3.Connection, atom_a: str, atom_b: str) -> bool:
    """Upsert one coactivation pair. Returns True if inserted (new edge)."""
    a, b = (atom_a, atom_b) if atom_a < atom_b else (atom_b, atom_a)
    cur = conn.execute(
        "INSERT INTO atom_coactivation (atom_a_id, atom_b_id, n_events, last_seen_at) "
        "VALUES (?, ?, 1, ?) "
        "ON CONFLICT(atom_a_id, atom_b_id) DO UPDATE SET "
        "  n_events = n_events + 1, "
        "  last_seen_at = excluded.last_seen_at",
        (a, b, _now()),
    )
    return cur.rowcount > 0


def _update_coactivation(conn: sqlite3.Connection, sessions: list[list[str]]) -> tuple[int, set[str]]:
    """Upsert all session pairs; returns (edges_added, set of atom ids touched)."""
    edges_added = 0
    touched: set[str] = set()
    total_rows = conn.execute("SELECT COUNT(*) FROM atom_coactivation").fetchone()[0]
    if total_rows >= MAX_COACTIVATION_ROWS:
        print(
            f"[sleep_consolidate] atom_coactivation at cap {total_rows} — skipping upsert",
            file=sys.stderr,
        )
        return 0, touched
    for session_chroma_ids in sessions:
        unique = list(dict.fromkeys(session_chroma_ids))
        mapping = _resolve_atom_ids(conn, unique)
        atom_ids = [mapping[c] for c in unique if c in mapping]
        if len(atom_ids) < 2:
            continue
        touched.update(atom_ids)
        for i, a in enumerate(atom_ids):
            for b in atom_ids[i + 1 :]:
                if a == b:
                    continue
                if _upsert_pair(conn, a, b):
                    edges_added += 1
    conn.commit()
    return edges_added, touched


def _access_frequency(conn: sqlite3.Connection) -> Counter[str]:
    """30-day access count per atom (from action_audit retrieved_chroma_ids)."""
    cutoff = (_now_utc() - timedelta(days=FREQUENCY_LOOKBACK_DAYS)).isoformat(timespec="seconds")
    freq: Counter[str] = Counter()
    for row in conn.execute(
        "SELECT retrieved_chroma_ids FROM action_audit "
        "WHERE created_at >= ? AND retrieved_chroma_ids IS NOT NULL",
        (cutoff,),
    ).fetchall():
        try:
            ids = json.loads(row[0] or "[]")
        except json.JSONDecodeError:
            continue
        if isinstance(ids, list):
            freq.update(str(x) for x in ids if x)
    return freq


def _predictive_error_atoms(conn: sqlite3.Connection) -> set[str]:
    cutoff = (_now_utc() - timedelta(days=7)).isoformat(timespec="seconds")
    rows = conn.execute(
        "SELECT DISTINCT atom_id FROM atom_evidence " "WHERE event_type='contradict' AND created_at >= ?",
        (cutoff,),
    ).fetchall()
    return {r["atom_id"] for r in rows}


def _promote_episodic_to_semantic(
    conn: sqlite3.Connection,
    freq_by_chroma: Counter[str],
    demotion_set: set[str],
) -> int:
    """Apply the CLS promotion rule. Returns #promoted atoms.

    Two-phase: collect candidates + apply tier flips in one transaction,
    commit, THEN fire update_atom_confidence for each. The reinforce calls
    open their own conn via atoms_store._conn — if we kept the first conn
    open with uncommitted writes, the inner conn would hit SQLITE_BUSY on
    BEGIN IMMEDIATE and silently skip the ledger row.
    """
    rows = conn.execute(
        "SELECT id, chroma_id, confidence FROM atoms " "WHERE tier='episodic' AND confidence >= ?",
        (PROMOTION_CONFIDENCE_FLOOR,),
    ).fetchall()
    promoted_ids: list[str] = []
    for row in rows:
        atom_id = row["id"]
        chroma_id = row["chroma_id"]
        f = freq_by_chroma.get(chroma_id, 0)
        if f < PROMOTION_FREQ_THRESHOLD:
            continue
        co_high = conn.execute(
            "SELECT COUNT(*) FROM atom_coactivation "
            "WHERE (atom_a_id = ? OR atom_b_id = ?) AND n_events >= 3",
            (atom_id, atom_id),
        ).fetchone()[0]
        if co_high < PROMOTION_COHIGH_THRESHOLD:
            continue
        conn.execute(
            "UPDATE atoms SET tier='semantic', updated_at=? WHERE id=?",
            (_now(), atom_id),
        )
        promoted_ids.append(atom_id)
    conn.commit()

    for atom_id in promoted_ids:
        weight = PROMOTION_REINFORCE_WEIGHT
        if atom_id in demotion_set:
            weight *= PREDICTIVE_ERROR_DEMOTION
        try:
            update_atom_confidence(
                atom_id=atom_id,
                event_type="reinforce",
                weight=weight,
                evidence_ref="sleep_consolidate",
                cluster_size=1,
            )
        except Exception:
            pass
    return len(promoted_ids)


def _amem_link_neighbors(
    conn: sqlite3.Connection,
    touched_atom_ids: set[str],
) -> int:
    """For each touched atom, query k=5 neighbors in semantic_memory and
    INSERT OR IGNORE related provenance + atom_entity rows. Cap 3/atom.
    """
    if not touched_atom_ids:
        return 0
    try:
        from indexer import get_embedding
        from vector_store import get_vector_store
    except ImportError:
        return 0

    store = get_vector_store()
    # Matching similarity floor: a cosine-distance cap of AMEM_DIST_THRESHOLD
    # maps to similarity >= 1 - AMEM_DIST_THRESHOLD.
    sim_floor = 1.0 - AMEM_DIST_THRESHOLD

    edges_added = 0
    # Only link atoms with known text and chroma_id
    placeholders = ",".join("?" * len(touched_atom_ids))
    atom_rows = conn.execute(
        f"SELECT id, chroma_id, text FROM atoms WHERE id IN ({placeholders})",
        tuple(touched_atom_ids),
    ).fetchall()
    for row in atom_rows:
        atom_id = row["id"]
        text = row["text"] or ""
        if not text:
            continue
        try:
            emb = get_embedding(text[:1000], prefix="passage")
        except Exception:
            continue
        if not emb:
            continue
        try:
            hits = store.query(
                "semantic_memory",
                vector=emb,
                k=AMEM_K + 1,
                with_payload=False,
            )
        except Exception:
            continue
        new_edges_this_atom = 0
        for h in hits:
            if new_edges_this_atom >= AMEM_EDGES_PER_ATOM:
                break
            if h.score < sim_floor:
                continue
            if h.id == row["chroma_id"]:
                continue
            neighbor_row = conn.execute("SELECT id FROM atoms WHERE chroma_id = ?", (h.id,)).fetchone()
            if not neighbor_row or neighbor_row["id"] == atom_id:
                continue
            neighbor_atom_id = neighbor_row["id"]
            # Avoid re-inserting existing provenance edges
            existing = conn.execute(
                "SELECT 1 FROM provenance WHERE parent_kind='atom' AND parent_id=? "
                "AND child_kind='atom' AND child_id=? AND relation='related' LIMIT 1",
                (atom_id, neighbor_atom_id),
            ).fetchone()
            if existing:
                continue
            try:
                conn.execute(
                    "INSERT OR IGNORE INTO provenance "
                    "(parent_kind, parent_id, child_kind, child_id, relation, confidence, created_at) "
                    "VALUES ('atom', ?, 'atom', ?, 'related', ?, ?)",
                    (atom_id, neighbor_atom_id, round(h.score, 4), _now()),
                )
                edges_added += 1
                new_edges_this_atom += 1
            except sqlite3.Error:
                continue
        conn.commit()
    return edges_added


def _summarize_via_sage(cycle_stats: dict, touched_atom_ids: set[str], conn: sqlite3.Connection) -> dict:
    """Dispatch one Sage summary call if the cycle was active enough to be
    worth summarizing. 200 tokens max; stored as JSON on sleep_cycles.summary.
    Best-effort — never fails the pipeline.
    """
    if cycle_stats.get("replay_count", 0) < SUMMARY_TRIGGER_REPLAY_COUNT:
        return {"skipped": "below_trigger"}
    try:
        from cli_llm import dispatch
    except ImportError:
        return {"skipped": "dispatch_unavailable"}
    sample_rows = (
        conn.execute(
            "SELECT text FROM atoms WHERE id IN ({}) LIMIT 10".format(
                ",".join("?" * min(10, len(touched_atom_ids)))
            ),
            tuple(list(touched_atom_ids)[:10]),
        ).fetchall()
        if touched_atom_ids
        else []
    )
    sample = "\n".join(f"- {r['text'][:160]}" for r in sample_rows)
    prompt = (
        "You are summarizing the top 3 patterns from a brain 'sleep cycle' — "
        "the system just consolidated the last 48 hours of memory access. "
        "Given this sample of replayed memories, name the top 3 recurring "
        f"themes in <= 100 words total.\n\nMemories:\n{sample}"
    )
    try:
        result = dispatch(
            agent="sage",
            message=prompt,
            thinking="off",
            timeout=SUMMARY_TIMEOUT_SEC,
            backlog_kind="reflect",
            backlog_payload={
                "agent": "sage",
                "prompt": prompt,
                "thinking": "off",
                "timeout": SUMMARY_TIMEOUT_SEC,
                "source": "sleep_consolidate",
            },
        )
        if result and getattr(result, "ok", False):
            return {"ok": True, "text": (result.text or "")[:2000]}
        return {"ok": False, "error": str(getattr(result, "error", "unknown"))}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:200]}


def run() -> dict:
    if not BRAIN_ATOMS_ENABLED:
        return {"ok": False, "reason": "atoms_disabled"}
    start = time.time()
    conn = sqlite3.connect(str(BRAIN_DB))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        cycle_id = _start_cycle(conn)
        rows = _fetch_window(conn)
        sessions = _group_sessions(rows)
        # 2026-04-16 Tier 3 #2: interleave old high-confidence atoms into
        # the replay set so CLS-style consolidation actually rehearses
        # long-tail semantic knowledge, not just the last 48 hours. The
        # interleave forms a pseudo-session: the old samples jointly
        # co-activate with themselves AND with any overlapping new traces
        # via the upsert step, keeping cortical neighborhoods intact.
        old_samples = _sample_old_high_confidence_atoms(conn)
        if old_samples:
            sessions.append(old_samples)
        replay_count = sum(len(s) for s in sessions)
        interleaved_count = len(old_samples)
        edges_added, touched = _update_coactivation(conn, sessions)
        freq_by_chroma = _access_frequency(conn)
        demotion_set = _predictive_error_atoms(conn)
        promoted = _promote_episodic_to_semantic(conn, freq_by_chroma, demotion_set)
        amem_edges = _amem_link_neighbors(conn, touched)
        summary_payload = _summarize_via_sage({"replay_count": replay_count}, touched, conn)
        summary_stats = {
            "sessions": len(sessions),
            "replay_count": replay_count,
            "interleaved_old_samples": interleaved_count,
            "coactivation_edges_added": edges_added,
            "amem_edges_added": amem_edges,
            "promoted_episodic_to_semantic": promoted,
            "touched_atoms": len(touched),
            "predictive_error_atoms": len(demotion_set),
            "duration_sec": round(time.time() - start, 2),
            "sage_summary": summary_payload,
        }
        _finish_cycle(
            conn,
            cycle_id,
            replay_count=replay_count,
            edges_added=edges_added + amem_edges,
            consolidated=promoted,
            summary=summary_stats,
        )
        return {"ok": True, "cycle_id": cycle_id, **summary_stats}
    finally:
        conn.close()


if __name__ == "__main__":
    result = run()
    print(json.dumps(result, indent=2, default=str))
    sys.exit(0 if result.get("ok") else 1)
