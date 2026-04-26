"""/brain/command/* — brain originates work items to OpenClaw agents."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from api_deps import _safe_http_detail, log, verify_bearer
from fastapi import APIRouter, Depends, HTTPException
from fastapi import Path as PathParam
from pydantic import BaseModel, Field

from config import BRAIN_DIR

router = APIRouter(dependencies=[Depends(verify_bearer)])


class BrainCommandRequest(BaseModel):
    to_agent: str = Field(..., description="Target agent: jenna | liz | ellie | sage | market")
    content: str = Field(..., description="The instruction/work item body")
    message_type: str = Field("task", description="info | task | question | alert")
    priority: int = Field(5, ge=1, le=10, description="1=urgent, 10=background")
    reason: str | None = Field(None, description="Why brain decided this — for audit trail")


class BrainCommandAck(BaseModel):
    status: str = Field(..., description="received | in_progress | done | rejected")
    note: str | None = Field(None, description="Agent's note / outcome summary")
    agent: str = Field(..., description="Acking agent — must match to_agent on the message")


_BRAIN_COMMAND_AGENTS = {"jenna", "liz", "ellie", "sage", "market"}


@router.post("/brain/command", tags=["agency"])
def brain_command(payload: BrainCommandRequest) -> dict:
    """Brain issues a work item to an OpenClaw agent."""
    if payload.to_agent not in _BRAIN_COMMAND_AGENTS:
        raise HTTPException(
            status_code=400,
            detail=f"to_agent must be one of {sorted(_BRAIN_COMMAND_AGENTS)}",
        )
    if not payload.content.strip():
        raise HTTPException(status_code=400, detail="content required")

    try:
        from agent_messenger import send_message

        body = payload.content.strip()
        if payload.reason:
            body += f"\n\n[brain reasoning]: {payload.reason}"
        msg = send_message(
            from_agent="brain",
            to_agent=payload.to_agent,
            content=body,
            message_type=payload.message_type,
            priority=payload.priority,
            metadata={"origin": "brain/command", "reason": payload.reason or ""},
        )
        try:
            from atoms_store import insert_raw_event as _insert_raw_event

            _insert_raw_event(
                event_id=f"brain_command_{msg['id']}",
                content=f"brain -> {payload.to_agent}: {body[:400]}",
                timestamp=msg["created_at"],
                source_type="brain_command",
                source_ref=f"brain:{msg['id']}",
                actor="brain",
                visibility="private",
                scrub_status="scrubbed",
            )
        except Exception as exc:
            log.debug("brain_command atom write failed: %s", exc)

        return {"ok": True, "message_id": msg["id"], "action": msg.get("_action", "stored")}
    except HTTPException:
        raise
    except Exception as exc:
        log.warning("brain_command failed: %s", exc)
        raise HTTPException(status_code=500, detail=_safe_http_detail("internal", exc)) from exc


@router.get("/brain/command/history", tags=["agency"])
def brain_command_history(limit: int = 30) -> dict:
    """Recent brain-originated commands."""
    try:
        import sqlite3 as _sqlite3

        with _sqlite3.connect(str(BRAIN_DIR / "logs" / "autonomy.db")) as conn:
            conn.row_factory = _sqlite3.Row
            rows = conn.execute(
                "SELECT id, to_agent, content, message_type, priority, status, created_at, delivered_at "
                "FROM messages WHERE from_agent='brain' "
                "ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return {"items": [dict(r) for r in rows]}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=_safe_http_detail("internal", exc)) from exc


@router.get("/brain/command/pending", tags=["agency"])
def brain_command_pending(to_agent: str | None = None) -> dict:
    """Unfinished brain-originated commands."""
    try:
        import sqlite3 as _sqlite3

        with _sqlite3.connect(str(BRAIN_DIR / "logs" / "autonomy.db")) as conn:
            conn.row_factory = _sqlite3.Row
            if to_agent:
                rows = conn.execute(
                    "SELECT id, to_agent, content, message_type, priority, status, created_at "
                    "FROM messages WHERE from_agent='brain' AND to_agent=? "
                    "  AND status IN ('pending', 'in_progress') "
                    "ORDER BY priority ASC, created_at ASC",
                    (to_agent,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT id, to_agent, content, message_type, priority, status, created_at "
                    "FROM messages WHERE from_agent='brain' "
                    "  AND status IN ('pending', 'in_progress') "
                    "ORDER BY priority ASC, created_at ASC"
                ).fetchall()
        return {"count": len(rows), "items": [dict(r) for r in rows]}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=_safe_http_detail("internal", exc)) from exc


@router.post("/brain/command/{message_id}/ack", tags=["agency"])
def brain_command_ack(
    message_id: Annotated[str, PathParam()],
    payload: BrainCommandAck,
) -> dict:
    """Agent acks a brain-originated command."""
    if payload.status not in ("received", "in_progress", "done", "rejected"):
        raise HTTPException(
            status_code=400,
            detail="status must be one of: received | in_progress | done | rejected",
        )
    try:
        import sqlite3 as _sqlite3

        with _sqlite3.connect(str(BRAIN_DIR / "logs" / "autonomy.db")) as conn:
            conn.row_factory = _sqlite3.Row
            row = conn.execute(
                "SELECT from_agent, to_agent FROM messages WHERE id=?",
                (message_id,),
            ).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="message not found")
            if row["from_agent"] != "brain":
                raise HTTPException(status_code=400, detail="not a brain-originated command")
            if row["to_agent"] != payload.agent:
                raise HTTPException(
                    status_code=403,
                    detail=f"only {row['to_agent']} can ack this message",
                )
            now_iso = datetime.now(UTC).isoformat(timespec="seconds")
            new_status = {
                "received": "delivered",
                "in_progress": "in_progress",
                "done": "completed",
                "rejected": "rejected",
            }[payload.status]
            conn.execute(
                "UPDATE messages SET status = ?, delivered_at = ? WHERE id = ?",
                (new_status, now_iso, message_id),
            )
            conn.commit()

        try:
            from atoms_store import insert_raw_event as _insert_raw_event

            note = f" note: {payload.note}" if payload.note else ""
            _insert_raw_event(
                event_id=f"brain_command_ack_{message_id}_{payload.status}",
                content=f"{payload.agent} acked brain cmd {message_id}: {payload.status}{note}",
                timestamp=now_iso,
                source_type="brain_command_ack",
                source_ref=f"brain:{message_id}",
                actor=payload.agent,
                visibility="private",
                scrub_status="scrubbed",
            )
        except Exception as exc:
            log.debug("brain_command_ack audit failed: %s", exc)

        return {"ok": True, "message_id": message_id, "new_status": new_status}
    except HTTPException:
        raise
    except Exception as exc:
        log.warning("brain_command_ack failed: %s", exc)
        raise HTTPException(status_code=500, detail=_safe_http_detail("internal", exc)) from exc
