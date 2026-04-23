"""Read synthesis outputs (daily/weekly/monthly markdown)."""

from __future__ import annotations

import re
from datetime import datetime

from api_deps import verify_bearer
from config import DISTILLED_DAILY, MONTHLY_DIR, WEEKLY_DIR
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import PlainTextResponse

router = APIRouter(dependencies=[Depends(verify_bearer)])

# Validation patterns — prevent path traversal via the target param.
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_WEEK_RE = re.compile(r"^\d{4}-W\d{2}$")
_MONTH_RE = re.compile(r"^\d{4}-\d{2}$")


@router.get("/synthesis/daily", response_class=PlainTextResponse, tags=["synthesis"])
def synthesis_daily(date: str | None = None) -> str:
    target = date or datetime.now().strftime("%Y-%m-%d")
    if not _DATE_RE.match(target):
        raise HTTPException(status_code=400, detail="date must be YYYY-MM-DD")
    f = DISTILLED_DAILY / f"{target}.md"
    if not f.exists():
        raise HTTPException(status_code=404, detail=f"no daily synthesis for {target}")
    return f.read_text()


@router.get("/synthesis/weekly", response_class=PlainTextResponse, tags=["synthesis"])
def synthesis_weekly(week: str | None = None) -> str:
    target = week or datetime.now().strftime("%G-W%V")
    if not _WEEK_RE.match(target):
        raise HTTPException(status_code=400, detail="week must be YYYY-Www")
    f = WEEKLY_DIR / f"{target}.md"
    if not f.exists():
        raise HTTPException(status_code=404, detail=f"no weekly arc for {target}")
    return f.read_text()


@router.get("/synthesis/monthly", response_class=PlainTextResponse, tags=["synthesis"])
def synthesis_monthly(month: str | None = None) -> str:
    target = month or datetime.now().strftime("%Y-%m")
    if not _MONTH_RE.match(target):
        raise HTTPException(status_code=400, detail="month must be YYYY-MM")
    f = MONTHLY_DIR / f"{target}.md"
    if not f.exists():
        raise HTTPException(status_code=404, detail=f"no monthly arc for {target}")
    return f.read_text()
