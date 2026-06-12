"""Open-loop / commitment detection for personal Brain replacement readiness.

The goal is not task automation. It is a deterministic surfacing layer for
commitments and waiting-on items that are likely still unresolved, while
rejecting planning chatter ("maybe", "could", brainstorms).
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

OpenLoopKind = Literal["commitment", "follow_up", "waiting_on", "deadline", "task"]
OpenLoopSource = Literal["atom", "task_queue"]

_DUE_RE = re.compile(
    r"\b(?:by|before|due|deadline(?: is)?|on)\s+"
    r"((?:\d{4}-\d{2}-\d{2})|(?:today|tomorrow)|(?:mon|tue|wed|thu|fri|sat|sun)(?:day)?|(?:next\s+\w+))",
    re.IGNORECASE,
)
_DURABLE_PATTERNS: tuple[tuple[OpenLoopKind, re.Pattern[str]], ...] = (
    ("waiting_on", re.compile(r"\b(waiting on|blocked by|need(?:s)? .+ from|until .+ responds)\b", re.I)),
    (
        "follow_up",
        re.compile(r"\b(follow up|follow-up|circle back|check back|remind me|ping .+ about)\b", re.I),
    ),
    ("deadline", re.compile(r"\b(due|deadline|by \d{4}-\d{2}-\d{2}|before \d{4}-\d{2}-\d{2})\b", re.I)),
    (
        "commitment",
        re.compile(r"\b(i(?:'ll| will)|we(?:'ll| will)|i promise|committed to|must|need to|has to)\b", re.I),
    ),
    ("commitment", re.compile(r"(해야 한다|할게|하겠습니다|기억해|리마인드|팔로우업|마감|기한)")),
)
_CHATTER_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"\b(maybe|might|could|would be nice|thinking about|brainstorm|idea:|option:|what if|consider)\b",
        re.I,
    ),
    re.compile(r"\b(can you|could you|would you|should we)\b", re.I),
    re.compile(r"(어쩌면|고민|아이디어|할까|해볼까|가능하면)"),
)
_RESOLVED_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"\b(done|completed|resolved|closed|shipped|merged|cancelled|canceled|no longer needed)\b", re.I
    ),
    re.compile(r"(완료|해결|취소|끝남|머지됨)"),
)


@dataclass(frozen=True)
class OpenLoopCandidate:
    id: str
    source: OpenLoopSource
    kind: OpenLoopKind
    text: str
    reason: str
    created_at: str
    updated_at: str | None = None
    due_hint: str | None = None
    stale: bool = False
    age_days: int | None = None
    confidence: float = 0.7
    status: str = "open"

    def to_dict(self) -> dict:
        return asdict(self)


def _parse_ts(value: str | None, *, now: datetime) -> datetime:
    if not value:
        return now
    normalized = value.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        return now
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _due_hint(text: str) -> str | None:
    match = _DUE_RE.search(text or "")
    return match.group(1).strip() if match else None


def classify_open_loop_text(
    text: str,
    *,
    source_id: str,
    source: OpenLoopSource = "atom",
    created_at: str | None = None,
    updated_at: str | None = None,
    now: datetime | None = None,
    stale_days: int = 14,
) -> OpenLoopCandidate | None:
    """Return an open-loop candidate for durable commitments, else None.

    Rejects ephemeral planning/question chatter before accepting durable
    markers. Resolved/completed phrasing is also rejected so stale archives do
    not create false alarms.
    """
    clean = " ".join((text or "").split())
    if len(clean) < 12:
        return None
    if any(pattern.search(clean) for pattern in _RESOLVED_PATTERNS):
        return None
    if any(pattern.search(clean) for pattern in _CHATTER_PATTERNS):
        return None

    matched_kind: OpenLoopKind | None = None
    matched_reason = ""
    for kind, pattern in _DURABLE_PATTERNS:
        match = pattern.search(clean)
        if match:
            matched_kind = kind
            matched_reason = f"durable_marker:{match.group(0).lower()}"
            break
    if matched_kind is None:
        return None

    ref_now = (now or datetime.now(UTC)).astimezone(UTC)
    created_dt = _parse_ts(created_at, now=ref_now)
    age_days = max(0, (ref_now - created_dt).days)
    hint = _due_hint(clean)
    confidence = 0.82 if matched_kind in {"waiting_on", "follow_up", "deadline"} else 0.72
    if hint:
        confidence = min(0.95, confidence + 0.08)

    return OpenLoopCandidate(
        id=source_id,
        source=source,
        kind=matched_kind,
        text=clean[:500],
        reason=matched_reason,
        created_at=created_at or ref_now.isoformat().replace("+00:00", "Z"),
        updated_at=updated_at,
        due_hint=hint,
        stale=age_days >= stale_days,
        age_days=age_days,
        confidence=round(confidence, 2),
    )


def scan_atom_open_loops(
    *,
    brain_db_path: Path | str,
    limit: int = 20,
    stale_days: int = 14,
    now: datetime | None = None,
) -> list[dict]:
    """Scan active atoms for unresolved commitment/follow-up candidates."""
    db_path = Path(brain_db_path)
    if not db_path.exists():
        return []
    ref_now = (now or datetime.now(UTC)).astimezone(UTC)
    candidates: list[OpenLoopCandidate] = []
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                """
                SELECT id, text, kind, confidence, tier, valid_from, created_at, updated_at
                FROM atoms
                WHERE tier != 'obsolete'
                  AND superseded_by IS NULL
                  AND length(text) >= 12
                ORDER BY created_at DESC
                LIMIT 1000
                """
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return []

    for row in rows:
        candidate = classify_open_loop_text(
            row["text"] or "",
            source_id=row["id"],
            source="atom",
            created_at=row["created_at"] or row["valid_from"],
            updated_at=row["updated_at"],
            now=ref_now,
            stale_days=stale_days,
        )
        if candidate is not None:
            candidates.append(candidate)

    candidates.sort(key=lambda c: (not c.stale, -(c.age_days or 0), c.created_at))
    return [c.to_dict() for c in candidates[:limit]]


def scan_task_open_loops(
    *,
    autonomy_db_path: Path | str,
    limit: int = 20,
    stale_days: int = 14,
    now: datetime | None = None,
) -> list[dict]:
    """Surface stale non-terminal autonomous tasks as open loops."""
    db_path = Path(autonomy_db_path)
    if not db_path.exists():
        return []
    ref_now = (now or datetime.now(UTC)).astimezone(UTC)
    stale_cutoff = ref_now - timedelta(days=stale_days)
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                """
                SELECT id, title, description, status, assigned_agent, created_at, updated_at
                FROM tasks
                WHERE status NOT IN ('completed', 'failed')
                ORDER BY updated_at ASC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return []

    out: list[OpenLoopCandidate] = []
    for row in rows:
        updated = _parse_ts(row["updated_at"] or row["created_at"], now=ref_now)
        age_days = max(0, (ref_now - updated).days)
        if updated > stale_cutoff and row["status"] not in {
            "pending",
            "approved",
            "assigned",
            "running",
            "paused",
        }:
            continue
        text = f"{row['title']} — {row['description'] or ''}".strip(" —")
        out.append(
            OpenLoopCandidate(
                id=row["id"],
                source="task_queue",
                kind="task",
                text=text[:500],
                reason=f"task_status:{row['status']}",
                created_at=row["created_at"],
                updated_at=row["updated_at"],
                stale=age_days >= stale_days,
                age_days=age_days,
                confidence=0.9,
                status=row["status"],
            )
        )
    return [c.to_dict() for c in out[:limit]]


def open_loop_snapshot(
    *,
    brain_db_path: Path | str,
    autonomy_db_path: Path | str | None = None,
    limit: int = 20,
    stale_days: int = 14,
    now: datetime | None = None,
) -> dict:
    """Combined personal open-loop surface for /brain/doubt and direct routes."""
    atom_items = scan_atom_open_loops(
        brain_db_path=brain_db_path,
        limit=limit,
        stale_days=stale_days,
        now=now,
    )
    task_items = (
        scan_task_open_loops(
            autonomy_db_path=autonomy_db_path,
            limit=limit,
            stale_days=stale_days,
            now=now,
        )
        if autonomy_db_path
        else []
    )
    items = [*atom_items, *task_items]
    items.sort(key=lambda i: (not bool(i.get("stale")), -(i.get("age_days") or 0), i.get("created_at") or ""))
    items = items[:limit]
    return {
        "items": items,
        "total": len(items),
        "stale_count": sum(1 for item in items if item.get("stale")),
        "detector": {
            "mode": "deterministic_read_only",
            "stale_days": stale_days,
            "rejects_session_chatter": True,
            "sources": ["atoms", "task_queue"] if autonomy_db_path else ["atoms"],
        },
    }
