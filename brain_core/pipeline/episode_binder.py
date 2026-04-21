#!/Users/chrischo/server/brain/.venv/bin/python3
"""brain_core/pipeline/episode_binder.py — Round 10 Wave 2 (B2).

CoALA-style episodic memory: cluster the day's new memories into time-windowed
"episodes" so retrieval can cross-promote peer memories from the same moment.
Pure local job, zero LLM cost — does NOT duplicate brain_reflect's nightly
reflection. Instead, complements it by adding the structural primitive that
recall-time episode binding needs.

What this does:
  1. Fetch new semantic_memory + experience entries from the last 24h
  2. Cluster by 30-min sliding window over created_at (greedy, no DBSCAN —
     timestamps are 1D so a sort + linear sweep is exact and sub-millisecond)
  3. Write each cluster ≥2 members to autonomy.db `episodes` table with member
     IDs + entity tags (theme = top-3 most-common entities)
  4. Hebbian boost: for every entity pair co-occurring in the same episode,
     bump RELATES_TO weight in Neo4j by 5%. Complements graph_consolidation's
     retrieval-co-occurrence Hebbian (different signal, same edge weights).

What this does NOT do:
  - No LLM dispatch (brain_reflect already handles narrative reflection)
  - No replacement for graph_consolidation (different Hebbian signal source)
  - No memory deletion / pruning

Schedule: daily 3:10am, between entity_resolution (3:05) and code_index_refresh
(3:25). No collisions.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from vector_store import get_vector_store

try:
    from config import AUTONOMY_DB
except ImportError:
    AUTONOMY_DB = Path("/Users/chrischo/server/brain/logs/autonomy.db")

EPISODE_WINDOW_MINUTES = 30
LOOKBACK_HOURS = 24
MIN_EPISODE_SIZE = 2
MAX_EPISODE_MEMORIES = 50  # cap so a runaway day doesn't make a single mega-episode
HEBBIAN_BOOST = 0.05  # 5% reinforcement on RELATES_TO weight per episode co-occurrence


def _ensure_table() -> None:
    AUTONOMY_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(AUTONOMY_DB))
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS episodes (
                episode_id TEXT PRIMARY KEY,
                start_ts TEXT NOT NULL,
                end_ts TEXT NOT NULL,
                member_count INTEGER NOT NULL,
                member_ids TEXT NOT NULL,
                theme_entities TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_episodes_window ON episodes(start_ts, end_ts)")
        # Reverse map: memory_id → episode_id, for fast O(1) lookup at recall time
        conn.execute("""
            CREATE TABLE IF NOT EXISTS episode_membership (
                memory_id TEXT PRIMARY KEY,
                episode_id TEXT NOT NULL,
                FOREIGN KEY (episode_id) REFERENCES episodes(episode_id)
            )
        """)
        conn.commit()
    finally:
        conn.close()


def _parse_ts(raw: str) -> datetime | None:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.rstrip("Zz"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt
    except Exception:
        return None


def _fetch_recent_memories() -> list[dict]:
    """Pull memories created in the last LOOKBACK_HOURS from semantic_memory + experience."""
    cutoff = datetime.now(UTC) - timedelta(hours=LOOKBACK_HOURS)
    store = get_vector_store()
    out: list[dict] = []
    for col_name in ("semantic_memory", "experience"):
        try:
            points = store.get(
                col_name,
                limit=1_000_000,
                with_payload=True,
                with_documents=False,
            )
        except Exception as e:
            print(f"  warning: {col_name} fetch failed: {e}", file=sys.stderr)
            continue
        for p in points:
            mid = p.id
            meta = p.payload or {}
            ts = _parse_ts(meta.get("created_at", "") or meta.get("updated_at", ""))
            if ts is None or ts < cutoff:
                continue
            # Skip obsolete and superseded — we don't want them anchoring an episode
            if (meta.get("memory_class") or "") == "obsolete":
                continue
            if meta.get("superseded_by"):
                continue
            # Extract entities — may be a list, JSON string, or comma-separated
            entities = meta.get("entities") or []
            if isinstance(entities, str):
                s = entities.strip()
                if s.startswith("["):
                    try:
                        entities = json.loads(s)
                    except Exception:
                        entities = [e.strip() for e in s.split(",") if e.strip()]
                else:
                    entities = [e.strip() for e in s.split(",") if e.strip()]
            if not isinstance(entities, list):
                entities = []
            out.append(
                {
                    "id": mid,
                    "created_at": ts,
                    "collection": col_name,
                    "entities": [e for e in entities if isinstance(e, str) and e],
                }
            )
    return out


def _cluster_by_window(memories: list[dict]) -> list[list[dict]]:
    """Greedy 30-min window clustering. Sort by timestamp, sweep linearly,
    start a new cluster whenever the gap to the previous member exceeds the
    window. Each cluster is the maximal contiguous span of close-in-time entries.
    """
    if not memories:
        return []
    sorted_mems = sorted(memories, key=lambda m: m["created_at"])
    clusters: list[list[dict]] = []
    current: list[dict] = [sorted_mems[0]]
    window = timedelta(minutes=EPISODE_WINDOW_MINUTES)
    for mem in sorted_mems[1:]:
        gap = mem["created_at"] - current[-1]["created_at"]
        if gap <= window:
            if len(current) < MAX_EPISODE_MEMORIES:
                current.append(mem)
            else:
                # Cap reached on a still-close-in-time stream — flush the
                # full cluster as an episode and start a fresh one with the
                # current member as the first element. The previous code
                # silently dropped the full cluster on cap-hit.
                if len(current) >= MIN_EPISODE_SIZE:
                    clusters.append(current)
                current = [mem]
        else:
            if len(current) >= MIN_EPISODE_SIZE:
                clusters.append(current)
            current = [mem]
    if len(current) >= MIN_EPISODE_SIZE:
        clusters.append(current)
    return clusters


def _theme_from_cluster(cluster: list[dict], top_k: int = 3) -> list[str]:
    """Top-K most common entities across the cluster's memories."""
    counter: Counter[str] = Counter()
    for mem in cluster:
        for ent in mem["entities"]:
            counter[ent] += 1
    return [name for name, _ in counter.most_common(top_k)]


def _persist_episodes(clusters: list[list[dict]]) -> tuple[int, int]:
    """Write clusters to autonomy.db. Returns (episodes_written, members_written).

    Idempotent: re-running on the same day overwrites prior episodes for that
    window — episode_id is derived from start_ts so re-runs replace cleanly.
    """
    if not clusters:
        return (0, 0)
    now_iso = datetime.now(UTC).isoformat()
    conn = sqlite3.connect(str(AUTONOMY_DB))
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        eps_written = 0
        members_written = 0
        for cluster in clusters:
            start = cluster[0]["created_at"]
            end = cluster[-1]["created_at"]
            episode_id = f"ep_{start.strftime('%Y%m%dT%H%M%S')}"
            theme = _theme_from_cluster(cluster)
            member_ids = [m["id"] for m in cluster]
            # Upsert episode row
            conn.execute(
                "INSERT OR REPLACE INTO episodes "
                "(episode_id, start_ts, end_ts, member_count, member_ids, theme_entities, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    episode_id,
                    start.isoformat(),
                    end.isoformat(),
                    len(cluster),
                    json.dumps(member_ids),
                    json.dumps(theme),
                    now_iso,
                ),
            )
            eps_written += 1
            # Clear any prior membership rows for these memories (re-bind)
            placeholders = ",".join("?" * len(member_ids))
            conn.execute(
                f"DELETE FROM episode_membership WHERE memory_id IN ({placeholders})",
                member_ids,
            )
            for mid in member_ids:
                conn.execute(
                    "INSERT OR REPLACE INTO episode_membership (memory_id, episode_id) VALUES (?, ?)",
                    (mid, episode_id),
                )
                members_written += 1
        conn.commit()
        return (eps_written, members_written)
    finally:
        conn.close()


def _hebbian_boost_episode_pairs(clusters: list[list[dict]]) -> int:
    """For each entity pair co-occurring in the same episode, bump
    RELATES_TO weight in Neo4j by HEBBIAN_BOOST. Returns # of edges touched.

    Best-effort: silently no-ops if Neo4j is unreachable. The retrieval path
    works fine without this — it's pure reinforcement.
    """
    try:
        from neo4j_client import run_write
    except Exception:
        return 0
    pairs_seen: set[tuple[str, str]] = set()
    for cluster in clusters:
        # Collect distinct entities in this episode
        entities: set[str] = set()
        for mem in cluster:
            for ent in mem["entities"]:
                if ent and len(ent) >= 2:
                    entities.add(ent)
        if len(entities) < 2:
            continue
        ent_list = sorted(entities)
        for i in range(len(ent_list)):
            for j in range(i + 1, len(ent_list)):
                a, b = ent_list[i], ent_list[j]
                # Canonical order so (a,b) and (b,a) dedupe
                pair = (a, b)
                if pair in pairs_seen:
                    continue
                pairs_seen.add(pair)
    if not pairs_seen:
        return 0
    boosted = 0
    for a, b in pairs_seen:
        try:
            # Saturate at 1.0; only boost if the edge already exists. We don't
            # CREATE edges from temporal co-occurrence — that's brain_reflect /
            # entity_resolution's job. We only REINFORCE existing relationships.
            run_write(
                "MATCH (x:Entity {name: $a})-[r:RELATES_TO]-(y:Entity {name: $b}) "
                "SET r.weight = CASE WHEN r.weight + $boost > 1.0 THEN 1.0 "
                "ELSE r.weight + $boost END, "
                "r.last_episode_boost = $now",
                {"a": a, "b": b, "boost": HEBBIAN_BOOST, "now": datetime.now(UTC).isoformat()},
            )
            boosted += 1
        except Exception:
            continue
    return boosted


def main() -> int:
    # Hard wall-clock cap so a hung Qdrant scroll or Neo4j write can't
    # grow this process to multi-GB RSS while launchd's misfire_grace
    # is ignored. Triggered once during the 2026-04-21 Qdrant
    # rebuild: binder scheduled at 03:18 PDT while the sparse reindex
    # was mid-delete/recreate of collections, left running 3h46min
    # holding ~7GB RSS before manual kill.
    import signal

    def _timeout(signum, frame):  # noqa: ARG001 - signal signature
        print("[episode_binder] FATAL: exceeded 300s wall-clock, aborting", flush=True)
        sys.exit(124)

    signal.signal(signal.SIGALRM, _timeout)
    signal.alarm(300)

    print(f"[episode_binder] starting at {datetime.now(UTC).isoformat()}", flush=True)
    _ensure_table()
    memories = _fetch_recent_memories()
    print(f"[episode_binder] fetched {len(memories)} memories from last {LOOKBACK_HOURS}h", flush=True)
    if not memories:
        print(json.dumps({"status": "ok", "episodes": 0, "members": 0, "hebbian_boosts": 0}))
        return 0

    clusters = _cluster_by_window(memories)
    print(
        f"[episode_binder] clustered into {len(clusters)} episodes "
        f"(window={EPISODE_WINDOW_MINUTES}min, min_size={MIN_EPISODE_SIZE})",
        flush=True,
    )

    eps, members = _persist_episodes(clusters)
    print(f"[episode_binder] wrote {eps} episodes / {members} membership rows", flush=True)

    hebbian = _hebbian_boost_episode_pairs(clusters)
    print(f"[episode_binder] hebbian boost applied to {hebbian} edges", flush=True)

    largest = max((len(c) for c in clusters), default=0)
    print(
        json.dumps(
            {
                "status": "ok",
                "memories_scanned": len(memories),
                "episodes": eps,
                "members": members,
                "hebbian_boosts": hebbian,
                "largest_episode": largest,
            }
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
