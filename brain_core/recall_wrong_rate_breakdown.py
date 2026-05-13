"""brain_core/recall_wrong_rate_breakdown.py — slice the wrong-rate signal
by language, route, and actor.

Today /brain/state surfaces a single judge wrong-rate scalar.  "30% wrong"
is a noisy stat by itself — the brain can't tell whether Korean queries
are worse than English, whether `claude` corrections drive most of the
volume, or whether `/recall/active` differs from `/recall/v2`.  This
module groups recall_judgments + recall_structural_judge outcomes into
slices so the worst slice can be targeted.

Deterministic, read-only, no LLM.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import sys
from collections.abc import Callable
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from config import BRAIN_DB
except ImportError:
    BRAIN_DB = Path("/Users/chrischo/server/brain/logs/brain.db")

log = logging.getLogger("brain.recall_wrong_rate_breakdown")


_HANGUL_RE = re.compile(r"[가-힣ㄱ-ㆎ]")


_WRONG_OUTCOMES = ("judged_wrong", "structural_wrong")
_GOOD_OUTCOMES = ("judged_good", "structural_good")


def breakdown(
    *,
    hours: int = 168,
    brain_db_path: Path | str | None = None,
) -> dict:
    """Return per-slice wrong-rate breakdown over the last `hours`.

    Result:
      {
        "window_hours": N,
        "total": int,           # total judged outcomes (good+wrong)
        "wrong": int,
        "wrong_rate": float,    # overall wrong fraction
        "by_language": {ko: {...}, en: {...}},
        "by_route":    {"/recall/v2": {...}, "/recall/active": {...}},
        "by_actor":    {"claude": {...}, ...}
      }
    """
    db = Path(brain_db_path or BRAIN_DB)
    out = {
        "window_hours": max(1, int(hours or 168)),
        "total": 0,
        "wrong": 0,
        "wrong_rate": 0.0,
        "by_language": {},
        "by_route": {},
        "by_actor": {},
        "worst_slice": None,
    }
    if not db.exists():
        out["status"] = "db_missing"
        return out
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT id, query_text, actor, route, outcome "
                "FROM action_audit "
                "WHERE outcome IN (?, ?, ?, ?) "
                "  AND created_at > datetime('now', ? || ' hours') "
                "  AND length(query_text) >= 5 ",
                (*_WRONG_OUTCOMES, *_GOOD_OUTCOMES, f"-{int(hours)}"),
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        out["status"] = f"error:{str(exc)[:120]}"
        return out
    total = len(rows)
    wrong = sum(1 for r in rows if r["outcome"] in _WRONG_OUTCOMES)
    out["total"] = total
    out["wrong"] = wrong
    out["wrong_rate"] = round(wrong / total, 4) if total else 0.0

    out["by_language"] = _group(rows, _classify_language)
    out["by_route"] = _group(rows, lambda r: r["route"] or "(unknown)")
    out["by_actor"] = _group(rows, lambda r: (r["actor"] or "(unknown)").strip() or "(unknown)")

    out["worst_slice"] = _worst_slice(out)
    out["status"] = "ok"
    return out


def _classify_language(row: sqlite3.Row) -> str:
    q = row["query_text"] or ""
    if _HANGUL_RE.search(q):
        return "ko"
    return "en"


def _group(
    rows: list[sqlite3.Row],
    key_fn: Callable[[sqlite3.Row], str],
) -> dict[str, dict]:
    groups: dict[str, dict] = {}
    for r in rows:
        key = key_fn(r)
        g = groups.setdefault(key, {"total": 0, "wrong": 0})
        g["total"] += 1
        if r["outcome"] in _WRONG_OUTCOMES:
            g["wrong"] += 1
    for g in groups.values():
        g["wrong_rate"] = round(g["wrong"] / g["total"], 4) if g["total"] else 0.0
    return groups


def _worst_slice(report: dict) -> dict | None:
    """Pick the single slice with the highest wrong_rate AND enough volume
    (>=5 samples) to be statistically meaningful."""
    best: dict | None = None
    for axis_name, slices in (
        ("language", report["by_language"]),
        ("route", report["by_route"]),
        ("actor", report["by_actor"]),
    ):
        for label, stats in slices.items():
            if stats["total"] < 5:
                continue
            if not best or stats["wrong_rate"] > best["wrong_rate"]:
                best = {
                    "axis": axis_name,
                    "label": label,
                    "wrong_rate": stats["wrong_rate"],
                    "wrong": stats["wrong"],
                    "total": stats["total"],
                }
    return best


if __name__ == "__main__":
    import argparse
    import json

    p = argparse.ArgumentParser()
    p.add_argument("--hours", type=int, default=168)
    args = p.parse_args()
    print(json.dumps(breakdown(hours=args.hours), indent=2, default=str))
