"""Capture routes (POST) — generic inbox-writer for iOS Shortcuts, hooks, etc."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal

from api_deps import log, verify_bearer
from config import INBOX_DIR
from fastapi import APIRouter, Depends, HTTPException
from fastapi import Path as PathParam
from pydantic import BaseModel, Field

router = APIRouter(dependencies=[Depends(verify_bearer)])


class CaptureRequest(BaseModel):
    """Generic capture payload — wrapped into a schema-compliant raw record on write."""

    event: str | None = None
    place: str | None = None
    lat: float | None = None
    lon: float | None = None
    accuracy: float | None = None
    battery: float | None = None
    sleep_hrs: float | None = None
    sleep_quality: str | None = None
    steps: int | None = None
    hrv_avg: float | None = None
    rest_hr: float | None = None
    workouts_count: int | None = None
    extra: dict[str, Any] = Field(default_factory=dict)

    model_config = {"extra": "allow"}


class CaptureResponse(BaseModel):
    status: Literal["ok"] = "ok"
    stored: str
    kind: str


def _build_raw_record(source_type: str, payload: dict) -> dict:
    now = datetime.now(UTC).replace(microsecond=0)
    iso = now.isoformat().replace("+00:00", "Z")
    content_str = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    digest = hashlib.sha256(content_str.encode()).hexdigest()
    date_part = iso[:10].replace("-", "_")
    rec_id = f"raw_{source_type}_{date_part}_{digest[:8]}"

    entities = ["Chris"]
    if isinstance(payload.get("place"), str):
        entities.append(payload["place"])

    return {
        "id": rec_id,
        "timestamp": iso,
        "source_type": source_type,
        "source_ref": f"brain-api:{payload.get('event', source_type)}",
        "actor": "chris",
        "visibility": "private",
        "scrub_status": "scrubbed",
        "content": content_str,
        "attachments": [],
        "entities": entities,
        "hash": f"sha256:{digest}",
    }


def _write_inbox(source_type: str, payload: dict) -> Path:
    INBOX_DIR.mkdir(parents=True, exist_ok=True)
    record = _build_raw_record(source_type, payload)
    out = INBOX_DIR / f"{record['id']}.json"
    out.write_text(json.dumps(record, ensure_ascii=False, indent=2))
    return out


@router.post("/location/ingest", response_model=CaptureResponse, tags=["capture"])
@router.post("/location", response_model=CaptureResponse, tags=["capture"], include_in_schema=False)
def capture_location(payload: CaptureRequest) -> CaptureResponse:
    data = payload.model_dump(exclude_none=True)
    data["_received_at"] = datetime.now(UTC).isoformat()
    out = _write_inbox("location", data)
    return CaptureResponse(stored=out.name, kind="location")


@router.post("/health/ingest", response_model=CaptureResponse, tags=["capture"])
@router.post("/health", response_model=CaptureResponse, tags=["capture"], include_in_schema=False)
def capture_health(payload: CaptureRequest) -> CaptureResponse:
    data = payload.model_dump(exclude_none=True)
    data["_received_at"] = datetime.now(UTC).isoformat()
    out = _write_inbox("health", data)
    return CaptureResponse(stored=out.name, kind="health")


@router.post("/capture/{source_type}", response_model=CaptureResponse, tags=["capture"])
def capture_generic(source_type: Annotated[str, PathParam()], payload: CaptureRequest) -> CaptureResponse:
    if not source_type or not re.fullmatch(r"[a-z0-9_\-]{1,32}", source_type):
        raise HTTPException(status_code=400, detail="source_type must be 1-32 chars of [a-z0-9_-]")
    data = payload.model_dump(exclude_none=True)
    data["_received_at"] = datetime.now(UTC).isoformat()
    out = _write_inbox(source_type, data)

    if source_type == "coding_event":
        try:
            from atoms_store import insert_raw_event as _insert_raw_event

            tool = str(data.get("tool", ""))
            fp = str(data.get("file_path", ""))
            old_p = str(data.get("old_preview", ""))[:200]
            new_p = str(data.get("new_preview", ""))[:200]
            success = "ok" if data.get("success", True) else "failed"
            fts_text = " ".join(
                filter(
                    None,
                    [
                        f"{tool} on {fp}",
                        f"session={data.get('session_id', '')}",
                        f"cwd={data.get('cwd', '')}",
                        f"status={success}",
                        f"old:{old_p}" if old_p else "",
                        f"new:{new_p}" if new_p else "",
                    ],
                )
            )
            _insert_raw_event(
                event_id=out.stem,
                content=fts_text,
                timestamp=data.get("ts", datetime.now(UTC).isoformat()),
                source_type="coding_event",
                source_ref=f"claude:{tool}",
                actor="claude",
                visibility="private",
                scrub_status="scrubbed",
                json_path=str(out),
            )
            try:
                from coding_events import classify_on_new_event

                classify_on_new_event(
                    {
                        "id": out.stem,
                        "file_path": fp,
                        "tool": tool,
                        "old": old_p,
                        "new": new_p,
                        "timestamp": data.get("ts"),
                    }
                )
            except Exception as exc:
                log.debug("coding_event outcome classify failed: %s", exc)
        except Exception as exc:
            log.debug("coding_event raw_events insert failed: %s", exc)

    return CaptureResponse(stored=out.name, kind=source_type)
