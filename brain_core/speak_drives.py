"""brain_core/speak_drives.py — rule-based signal collectors.

Each drive returns 0..N Observations. Exceptions are swallowed by
collect_observations in speak_composer.py; drives themselves are best-effort
and MUST NOT raise.

Split from speak.py 2026-04-23.
"""

from __future__ import annotations

import logging
import os
import re as _re
import sqlite3
from datetime import UTC, datetime, timedelta

from speak_schema import Observation, autonomy_conn, brain_conn

log = logging.getLogger("brain.speak")


def _synthesis_auto_dispatch_enabled() -> bool:
    """Kill switch for synthesis_drive's autonomous agent dispatch.

    Default OFF — brain emits observations only. Chris flips this on when
    confident the pattern quality is high enough to let brain act on its own.
    """
    return os.environ.get("BRAIN_SYNTHESIS_AUTO_DISPATCH", "0") == "1"


def contradiction_drive() -> list[Observation]:
    """Surface new pending contradictions from attention_queue (last 24h).

    These have already passed the polarity gate upstream in learn.py, so
    what reaches us should be real conflicts worth asking Chris about.
    """
    obs: list[Observation] = []
    cutoff = (datetime.now(UTC) - timedelta(hours=24)).isoformat(timespec="seconds")
    try:
        with brain_conn() as conn:
            rows = conn.execute(
                "SELECT id, summary, detail, created_at, severity "
                "FROM attention_queue "
                "WHERE dismissed=0 AND category='contradiction' AND created_at >= ? "
                "ORDER BY created_at DESC LIMIT 5",
                (cutoff,),
            ).fetchall()
    except sqlite3.Error as exc:
        log.debug("contradiction_drive sql error: %s", exc)
        return obs
    for r in rows:
        short = (r["summary"] or "")[:200].replace("Unresolved contradiction: ", "")
        sev = 7.0 if r["severity"] == "critical" else 5.0
        obs.append(
            Observation(
                drive="contradiction_drive",
                category="contradiction",
                severity=sev,
                message=f"새 contradiction: {short}",
                dedup_key=f"contradiction:{r['id']}",
                payload={"attention_id": r["id"], "severity_raw": r["severity"]},
            )
        )
    return obs


def coding_revert_drive() -> list[Observation]:
    """Flag files with repeated revert/refined chains — pattern of thrash."""
    obs: list[Observation] = []
    try:
        from coding_events import outcome_stats

        stats = outcome_stats(within_hours=24)
    except Exception as exc:
        log.debug("coding_revert_drive stats error: %s", exc)
        return obs
    reverted = stats.get("reverted", 0)
    refined = stats.get("refined", 0)
    total = sum(stats.values()) or 0
    if total < 5:
        return obs
    revert_pct = (reverted / total) * 100 if total else 0
    if revert_pct >= 20:
        sev = 6.0 if revert_pct >= 40 else 4.5
        obs.append(
            Observation(
                drive="coding_revert_drive",
                category="pattern",
                severity=sev,
                message=(
                    f"최근 24h coding_event {total}건 중 revert {reverted}건 ({revert_pct:.0f}%) + "
                    f"refined {refined}건. 어떤 파일에서 thrash 중인지 확인 필요."
                ),
                dedup_key=f"coding_revert:{datetime.now(UTC).date().isoformat()}",
                payload={"stats": stats, "revert_pct": round(revert_pct, 1)},
            )
        )
    try:
        with brain_conn() as conn:
            rows = conn.execute(
                "SELECT re.content FROM raw_events re "
                "JOIN coding_event_outcomes co ON co.event_id = re.id "
                "WHERE re.source_type='coding_event' AND co.outcome='reverted' "
                "  AND re.timestamp >= ?",
                ((datetime.now(UTC) - timedelta(hours=24)).isoformat(timespec="seconds"),),
            ).fetchall()
    except sqlite3.Error:
        rows = []

    file_counts: dict[str, int] = {}
    for r in rows:
        m = _re.search(r"(?:Edit|Write|NotebookEdit) on (\S+)", r["content"] or "")
        if m:
            fp = m.group(1)
            file_counts[fp] = file_counts.get(fp, 0) + 1
    for fp, cnt in file_counts.items():
        if cnt >= 2:
            obs.append(
                Observation(
                    drive="coding_revert_drive",
                    category="pattern",
                    severity=6.0,
                    message=f"{fp} 24h 내 revert {cnt}회 — 뭔가 막히는 중인 듯.",
                    dedup_key=f"revert_file:{fp}:{datetime.now(UTC).date().isoformat()}",
                    payload={"file_path": fp, "revert_count": cnt},
                )
            )
    return obs


def stale_thread_drive() -> list[Observation]:
    """Agent messages pending > 48h across all agents."""
    obs: list[Observation] = []
    cutoff = (datetime.now(UTC) - timedelta(hours=48)).isoformat(timespec="seconds")
    try:
        with autonomy_conn() as conn:
            rows = []
            for table in ("messages", "agent_messages"):
                try:
                    rows = conn.execute(
                        f"SELECT from_agent, to_agent, content, created_at "
                        f"FROM {table} "
                        f"WHERE status='pending' AND created_at <= ? "
                        f"ORDER BY created_at ASC LIMIT 5",
                        (cutoff,),
                    ).fetchall()
                    break
                except sqlite3.Error:
                    continue
    except Exception as exc:
        log.debug("stale_thread_drive error: %s", exc)
        return obs
    for r in rows:
        age_h = 0.0
        try:
            created = datetime.fromisoformat(r["created_at"].replace("Z", "+00:00"))
            if created.tzinfo is None:
                created = created.replace(tzinfo=UTC)
            age_h = (datetime.now(UTC) - created).total_seconds() / 3600
        except (ValueError, KeyError, TypeError):
            pass
        summary = (r["content"] or "")[:120]
        obs.append(
            Observation(
                drive="stale_thread_drive",
                category="thread",
                severity=min(8.0, 3.0 + age_h / 24.0),
                message=(f"{r['from_agent']} → {r['to_agent']} 메시지 " f"{age_h:.0f}h째 pending: {summary}"),
                dedup_key=f"stale_msg:{r['from_agent']}:{r['to_agent']}:{r['created_at']}",
                payload={"from": r["from_agent"], "to": r["to_agent"], "age_h": round(age_h, 1)},
            )
        )
    return obs
