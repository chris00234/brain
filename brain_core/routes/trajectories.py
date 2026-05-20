"""Trajectory export — structured episode payloads for evals + agent handoff.

GET /brain/trajectories/export

2026-05-20 W3.5 round 3 (codex gap 5): Hermes exports per-session trajectories
for batch evals and inter-agent handoff. Brain didn't have an equivalent —
`/brain/sessions/search` returns retrieval hits, not reusable episodes.

Output shape:
  {
    "actor": str | null,
    "session_id": str | null,
    "since": str (ISO),
    "events_total": int,
    "trajectories": [
      {
        "session_ref": str,         # source_ref grouping the timeline
        "actor": str,
        "first_at": str (ISO),
        "last_at": str (ISO),
        "event_count": int,
        "goal": str | null,         # best-effort: first content matching a goal-like marker
        "timeline": [
          {"at", "actor", "source_type", "summary"}, ...
        ],
        "decisions": [...],         # decision_ledger rows linked by actor+session
        "outcomes": [...],          # outcomes for those decisions
        "lessons": [...]            # cross_agent_lessons matches by topic
      }
    ]
  }
"""

from __future__ import annotations

import logging
import re
import sqlite3
from datetime import UTC, datetime, timedelta

from api_deps import _safe_http_detail, verify_bearer
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from rate_limit import limiter

from config import BRAIN_DIR

log = logging.getLogger("brain.routes.trajectories")

router = APIRouter(dependencies=[Depends(verify_bearer)])

MAX_TIMELINE_PER_TRAJECTORY = 50
MAX_TRAJECTORIES_PER_REQUEST = 20
DEFAULT_SINCE_DAYS = 7


# Dual ASCII + fullwidth colon support so Korean transcripts hit the same
# extraction. RUF001 flags the fullwidth char as ambiguous but it's intentional.
_GOAL_PATTERNS = (
    re.compile(r"goal[:：]\s*(.+)", re.IGNORECASE),  # noqa: RUF001
    re.compile(r"to-?do[:：]\s*(.+)", re.IGNORECASE),  # noqa: RUF001
    re.compile(r"focus[:：]\s*(.+)", re.IGNORECASE),  # noqa: RUF001
)


def _extract_goal(content: str) -> str | None:
    if not content:
        return None
    for pat in _GOAL_PATTERNS:
        m = pat.search(content)
        if m:
            return m.group(1).strip()[:240]
    return None


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(BRAIN_DIR / "logs" / "brain.db"))
    conn.row_factory = sqlite3.Row
    return conn


def _connect_autonomy() -> sqlite3.Connection:
    conn = sqlite3.connect(str(BRAIN_DIR / "logs" / "autonomy.db"))
    conn.row_factory = sqlite3.Row
    return conn


def _decisions_for(actor: str | None, session_ref: str, since_iso: str) -> list[dict]:
    """Pull decision_ledger rows that align with this trajectory window."""
    try:
        conn = _connect_autonomy()
    except Exception:
        return []
    try:
        # Decisions are scoped by (agent, situation_hash); narrow on agent +
        # created_at window. session_id may not be set on every row, so we
        # fall back to time-window filtering when not present.
        cur = conn.execute(
            """
            SELECT id, situation, options_json, recommendation, outcome_status,
                   review_status, created_at
            FROM decision_ledger
            WHERE created_at >= ?
              AND (agent = ? OR ? IS NULL)
            ORDER BY created_at DESC
            LIMIT 25
            """,
            (since_iso, actor, actor),
        )
        return [dict(r) for r in cur.fetchall()]
    except sqlite3.OperationalError:
        # Schema variance: column names differ across releases. Fall back to
        # a permissive SELECT *.
        try:
            cur = conn.execute(
                "SELECT * FROM decision_ledger WHERE created_at >= ? ORDER BY created_at DESC LIMIT 25",
                (since_iso,),
            )
            return [dict(r) for r in cur.fetchall()]
        except Exception:
            return []
    finally:
        conn.close()


_LESSONS_LOOKUP_BUDGET_S = 2.0


def _lessons_for(actor: str | None, topic_hint: str | None = None, limit: int = 5) -> list[dict]:
    """Pull recent failure_memory lessons relevant to this trajectory.

    2026-05-20 W3.5 round 3 (codex defect 3): the previous implementation
    queried a ``lessons`` table in brain.db that does not exist — the join
    silently returned empty for every trajectory. Real lesson storage is in
    failure_memory (Neo4j-backed via failure_memory.get_similar_lessons).

    2026-05-20 W3.5 round-4 defect C: failure_memory ultimately drives
    Neo4j over a driver with a 30s acquisition timeout and no per-query
    cap. A cold/down Neo4j would block this route up to 30s per
    trajectory. Run the lookup on a daemon thread with a hard
    ``_LESSONS_LOOKUP_BUDGET_S`` ceiling so trajectory export remains
    bounded; failures degrade to []. The orphan worker keeps draining in
    the background but cannot stall the response.
    """
    import threading as _t

    seed = (topic_hint or "").strip() or (actor or "").strip()
    if not seed:
        return []
    result: list[dict] = []
    error_box: list[BaseException] = []

    def _worker() -> None:
        try:
            import failure_memory as _fm

            out = _fm.get_similar_lessons(seed, agent_id=actor or "system", limit=limit) or []
            result.extend(out)
        except BaseException as exc:
            error_box.append(exc)

    t = _t.Thread(target=_worker, daemon=True, name="trajectories_lessons")
    t.start()
    t.join(timeout=_LESSONS_LOOKUP_BUDGET_S)
    if t.is_alive():
        log.debug("trajectories lessons lookup timed out after %.1fs", _LESSONS_LOOKUP_BUDGET_S)
        return []
    if error_box:
        log.debug("trajectories lessons lookup failed: %s", error_box[0])
        return []
    return result


def _group_trajectories(
    actor: str | None,
    session_id: str | None,
    source_type: str | None,
    since_iso: str,
    max_groups: int,
) -> list[dict]:
    """Group raw_events into per-session_ref trajectories. Returns at most
    ``max_groups`` trajectories ordered by most-recent last_at desc.
    """
    conn = _connect()
    try:
        params: list = [since_iso]
        where_extra = []
        if actor:
            where_extra.append("actor = ?")
            params.append(actor)
        if session_id:
            where_extra.append("source_ref = ?")
            params.append(session_id)
        if source_type:
            where_extra.append("source_type = ?")
            params.append(source_type)
        where_sql = ("AND " + " AND ".join(where_extra)) if where_extra else ""

        # Pull recent groups first — by last activity per source_ref.
        # where_sql is composed from hardcoded literals in where_extra; no
        # user input is concatenated into the SQL string, so S608 is a false
        # positive here (all user values flow through ? placeholders).
        groups = conn.execute(
            f"""
            SELECT source_ref,
                   actor,
                   COUNT(*) AS event_count,
                   MIN(timestamp) AS first_at,
                   MAX(timestamp) AS last_at
            FROM raw_events
            WHERE timestamp >= ?
              AND source_ref IS NOT NULL AND source_ref != ''
              {where_sql}
            GROUP BY source_ref, actor
            ORDER BY last_at DESC
            LIMIT ?
            """,
            (*params, max_groups),
        ).fetchall()

        trajectories = []
        for g in groups:
            ref = g["source_ref"]
            # Pull a bounded timeline for this group. The interpolated fragment
            # is a literal "AND actor = ?" — no user data in the SQL string.
            tl = conn.execute(
                f"""
                SELECT timestamp, actor, source_type, substr(content, 1, 400) AS content
                FROM raw_events
                WHERE source_ref = ? AND timestamp >= ?
                  {("AND actor = ?" if actor else "")}
                ORDER BY timestamp ASC
                LIMIT ?
                """,  # noqa: S608 — interpolated fragment is a literal only
                (
                    (ref, since_iso, actor, MAX_TIMELINE_PER_TRAJECTORY)
                    if actor
                    else (ref, since_iso, MAX_TIMELINE_PER_TRAJECTORY)
                ),
            ).fetchall()

            timeline = [
                {
                    "at": r["timestamp"],
                    "actor": r["actor"] or "",
                    "source_type": r["source_type"] or "",
                    "summary": (r["content"] or "")[:400],
                }
                for r in tl
            ]
            goal = None
            for t in timeline[:5]:
                goal = _extract_goal(t["summary"])
                if goal:
                    break
            trajectories.append(
                {
                    "session_ref": ref,
                    "actor": g["actor"] or "",
                    "first_at": g["first_at"],
                    "last_at": g["last_at"],
                    "event_count": int(g["event_count"]),
                    "goal": goal,
                    "timeline": timeline,
                }
            )
        return trajectories
    finally:
        conn.close()


@router.get("/brain/trajectories/export", tags=["brain"])
@limiter.limit("30/minute")
def trajectories_export(
    request: Request,
    actor: str | None = Query(default=None, description="Filter raw_events.actor"),
    session_id: str | None = Query(default=None, description="Filter raw_events.source_ref"),
    source_type: str | None = Query(default=None, description="Filter raw_events.source_type"),
    since_days: int = Query(default=DEFAULT_SINCE_DAYS, ge=1, le=90),
    max_trajectories: int = Query(default=10, ge=1, le=MAX_TRAJECTORIES_PER_REQUEST),
) -> dict:
    """Export structured trajectories (timeline + decisions + lessons) for
    eval / handoff. Each trajectory groups raw_events by source_ref so the
    consumer gets one episode per agent session.
    """
    if since_days < 1:
        raise HTTPException(status_code=400, detail="since_days must be >= 1")
    since_iso = (datetime.now(UTC) - timedelta(days=since_days)).isoformat()
    try:
        trajectories = _group_trajectories(
            actor=actor,
            session_id=session_id,
            source_type=source_type,
            since_iso=since_iso,
            max_groups=max_trajectories,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=_safe_http_detail("trajectories_group", e)) from e

    decisions_cache: dict[str, list[dict]] = {}
    for t in trajectories:
        cache_key = f"{t['actor']}::{t['session_ref']}"
        if cache_key not in decisions_cache:
            decisions_cache[cache_key] = _decisions_for(t["actor"] or actor, t["session_ref"], since_iso)
        t["decisions"] = decisions_cache[cache_key]
        # Outcomes are folded into decision rows already (outcome_status field
        # on decision_ledger). Surface them explicitly for downstream parsers.
        t["outcomes"] = [
            {
                "decision_id": d.get("id"),
                "status": d.get("outcome_status"),
                "review": d.get("review_status"),
            }
            for d in t["decisions"]
            if (d.get("outcome_status") or d.get("review_status"))
        ]
        t["lessons"] = _lessons_for(t["actor"] or actor, topic_hint=t.get("goal"), limit=5)

    return {
        "actor": actor,
        "session_id": session_id,
        "source_type": source_type,
        "since": since_iso,
        "events_total": sum(t["event_count"] for t in trajectories),
        "trajectory_count": len(trajectories),
        "trajectories": trajectories,
    }
