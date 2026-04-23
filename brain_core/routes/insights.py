"""/agent/heartbeat + /brain/proactive + /brain/insights — low-latency surfaces."""

from __future__ import annotations

import json
import os
import time
from datetime import datetime as _dt
from datetime import timedelta as _td

from api_deps import SERVER_START, _safe_http_detail, verify_bearer
from config import DISTILLED_DAILY
from fastapi import APIRouter, Depends, HTTPException, Query

router = APIRouter()


# ── Heartbeat (unauthenticated) ───────────────────────
@router.get("/agent/heartbeat", tags=["liveness"])
def agent_heartbeat() -> dict:
    """Ultra-cheap unauthenticated heartbeat agents can poll."""
    try:
        from brain_core import config as _cfg

        flags = {
            "atoms_read": getattr(_cfg, "BRAIN_ATOMS_READ", False),
            "self_rag": os.environ.get("BRAIN_SELF_RAG_ENABLED", "false").lower()
            in ("1", "true", "yes", "on"),
            "autopilot_killed": os.environ.get("BRAIN_AUTOPILOT_DISABLED", "").strip().lower()
            in ("1", "true", "yes", "on"),
        }
    except Exception:
        flags = {}
    return {
        "status": "ok",
        "uptime_sec": int(time.time() - SERVER_START),
        "features": flags,
    }


# ── Proactive (auth required) ─────────────────────────
@router.get("/brain/proactive", tags=["decide"], dependencies=[Depends(verify_bearer)])
def brain_proactive(severity: str | None = None, max_age_hours: int = 24) -> dict:
    """Returns current proactive insights/alerts."""
    try:
        from brain_core.proactive import get_current_insights

        insights = get_current_insights(max_age_hours=max_age_hours, severity=severity)
        return {
            "insights": [vars(i) if hasattr(i, "__dict__") else i for i in insights],
            "total": len(insights),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=_safe_http_detail("internal", e)) from e


@router.post("/brain/proactive/{insight_id}/dismiss", tags=["decide"], dependencies=[Depends(verify_bearer)])
def dismiss_proactive(insight_id: str) -> dict:
    """Mark a proactive insight as acknowledged."""
    try:
        from brain_core.proactive import dismiss_insight

        ok = dismiss_insight(insight_id)
        return {"status": "dismissed" if ok else "not_found", "id": insight_id}
    except Exception as e:
        raise HTTPException(status_code=500, detail=_safe_http_detail("internal", e)) from e


@router.get("/brain/insights", tags=["decide"], dependencies=[Depends(verify_bearer)])
def brain_insights(days: int = Query(default=7, ge=1, le=30)) -> dict:
    """Return recent daily insights produced by proactive_linker."""
    insights_dir = DISTILLED_DAILY.parent / "insights"
    if not insights_dir.exists():
        return {"days": days, "files": 0, "results": []}

    out: list[dict] = []
    today = _dt.now().date()
    for offset in range(days):
        d = today - _td(days=offset)
        f = insights_dir / f"{d.isoformat()}.md"
        if not f.exists():
            continue
        try:
            text = f.read_text()
        except Exception:  # noqa: S112 — unreadable file skipped
            continue

        meta: dict = {}
        body = text
        if text.startswith("---json"):
            try:
                _, rest = text.split("---json\n", 1)
                meta_json, body = rest.split("\n---\n", 1)
                meta = json.loads(meta_json)
            except Exception:  # noqa: S110 — keep body if frontmatter malformed
                pass
        elif text.startswith("---\n"):
            try:
                _, rest = text.split("---\n", 1)
                meta_block, body = rest.split("\n---\n", 1)
                meta = json.loads(meta_block) if meta_block.strip().startswith("{") else {}
            except Exception:  # noqa: S110 — keep body if frontmatter malformed
                pass

        sections: list[dict] = []
        current_title: str | None = None
        current_desc_lines: list[str] = []
        for line in body.splitlines():
            if line.startswith("## "):
                if current_title is not None:
                    sections.append(
                        {
                            "title": current_title,
                            "description": "\n".join(current_desc_lines).strip()[:600],
                        }
                    )
                t = line[3:].strip()
                if t and t[0].isdigit():
                    parts = t.split(". ", 1)
                    if len(parts) == 2:
                        t = parts[1]
                current_title = t
                current_desc_lines = []
            elif current_title is not None:
                current_desc_lines.append(line)
        if current_title is not None:
            sections.append(
                {
                    "title": current_title,
                    "description": "\n".join(current_desc_lines).strip()[:600],
                }
            )

        out.append(
            {
                "date": d.isoformat(),
                "title": meta.get("title", f"Daily Insights — {d.isoformat()}"),
                "entities": meta.get("entities", []),
                "confidence": meta.get("confidence", 0.0),
                "insights": sections,
            }
        )

    return {"days": days, "files": len(out), "results": out}
