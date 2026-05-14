"""/brain/deboost — outcome-aware atom weight surfacer + manual override."""

from __future__ import annotations

import sqlite3

from api_deps import verify_bearer
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

router = APIRouter(dependencies=[Depends(verify_bearer)])


class DeboostSet(BaseModel):
    atom_id: str
    weight: float = Field(ge=0.0, le=1.0)
    reason: str = ""


@router.get("/brain/deboost", tags=["brain"])
def list_deboost(
    floor: float = Query(0.20, ge=0.0, le=1.0),
    limit: int = Query(50, ge=1, le=500),
) -> dict:
    from brain_core import atom_deboost
    from config import BRAIN_DB

    weights = atom_deboost.load_weight_map(brain_db_path=BRAIN_DB, floor=floor)
    items = sorted(
        ({"atom_id": k, "weight": v} for k, v in weights.items()),
        key=lambda r: r["weight"],
    )[:limit]
    return {"floor": floor, "count": len(weights), "items": items}


@router.post("/brain/deboost/scan", tags=["brain"])
def trigger_scan() -> dict:
    """Run the deboost update pass now and return the summary."""
    from brain_core import atom_deboost
    from config import BRAIN_DB

    return atom_deboost.update_weights(brain_db_path=BRAIN_DB)


@router.post("/brain/deboost/set", tags=["brain"])
def set_deboost(body: DeboostSet) -> dict:
    """Manually set a deboost weight (operator override)."""
    import json
    from datetime import UTC, datetime

    from config import BRAIN_DB

    now = datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    try:
        conn = sqlite3.connect(str(BRAIN_DB), timeout=5)
        try:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS atom_deboost (
                    atom_id TEXT PRIMARY KEY,
                    weight REAL NOT NULL DEFAULT 1.0,
                    evidence_json TEXT NOT NULL DEFAULT '{}',
                    reason TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                INSERT INTO atom_deboost (atom_id, weight, evidence_json, reason, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(atom_id) DO UPDATE SET
                    weight = excluded.weight,
                    evidence_json = excluded.evidence_json,
                    reason = excluded.reason,
                    updated_at = excluded.updated_at
                """,
                (
                    body.atom_id,
                    body.weight,
                    json.dumps({"manual": True}),
                    body.reason or "operator override",
                    now,
                ),
            )
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        raise HTTPException(status_code=500, detail=f"db error: {exc}") from exc
    return {"status": "ok", "atom_id": body.atom_id, "weight": body.weight}
