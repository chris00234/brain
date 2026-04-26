"""SLO + trace + manual ingest + canonical index/lint + answer candidates."""

from __future__ import annotations

import hashlib
import json as _json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from api_deps import _safe_http_detail, verify_bearer
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from rate_limit import limiter

from config import BRAIN_DIR

router = APIRouter(dependencies=[Depends(verify_bearer)])


# ── Pydantic models ───────────────────────────────────
class BrainIngestRequest(BaseModel):
    content: str = Field(..., min_length=1, max_length=50000)
    source: str = Field(default="api")
    category: str = Field(default="other")
    tags: list[str] = Field(default_factory=list)


class CanonicalizeRequest(BaseModel):
    query: str
    answer: str
    reason: str | None = None
    agent: str | None = None
    source_route: str = "mcp:brain_canonicalize"


# ── SLO observability ─────────────────────────────────
def _slo_result_to_dict(r: object) -> dict:
    return {
        "name": r.slo.name,
        "description": r.slo.description,
        "target": r.slo.target,
        "actual": r.actual,
        "delta": r.delta,
        "breached": r.breached,
        "severity": r.slo.severity,
        "unit": r.slo.metric_unit,
    }


@router.get("/brain/slos", tags=["observability"])
def get_slos() -> dict:
    """Return current SLO check results without dispatching alerts."""
    try:
        from brain_core.slos import check_all

        results = check_all()
        items = [_slo_result_to_dict(r) for r in results]
        return {
            "checked": len(results),
            "breached": sum(1 for r in results if r.breached),
            "alerts_sent": 0,
            "items": items,
            "results": items,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=_safe_http_detail("internal", e)) from e


@router.post("/brain/slos/check", tags=["observability"])
def trigger_slos_check() -> dict:
    """Manually trigger an SLO check + alert dispatch."""
    try:
        from brain_core.slos import run

        summary = run()
        summary["items"] = summary.get("results", [])
        return summary
    except Exception as e:
        raise HTTPException(status_code=500, detail=_safe_http_detail("internal", e)) from e


# ── Trace / ingest / index / canonical ────────────────
@router.get("/brain/trace/{note_id}", tags=["autonomy"])
def trace_provenance(note_id: str, max_depth: int = 3) -> dict:
    """Trace relation chains from a canonical note."""
    try:
        from brain_core.provenance import trace

        if max_depth < 0 or max_depth > 10:
            raise HTTPException(status_code=400, detail="max_depth must be 0-10")
        return trace(note_id, max_depth=max_depth)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=_safe_http_detail("internal", e)) from e


@router.post("/brain/ingest", tags=["autonomy"])
@limiter.limit("10/minute")
def brain_ingest(request: Request, req: BrainIngestRequest) -> dict:
    """Manual ingest: submit text/URL for LLM extraction and integration."""
    try:
        content = req.content
        source_name = req.source

        try:
            from brain_core import test_gate

            is_test, reason = test_gate.is_test_context(content, source_name)
            if is_test:
                return {"status": "test_skipped", "reason": reason}
        except ImportError:
            pass

        from brain_core.openclaw_dispatch import dispatch

        prompt = (
            f"Extract key facts, decisions, and insights from this content. "
            f"Write a concise summary as a knowledge note.\n\n"
            f"Source: {source_name}\n"
            f"Content:\n{content[:5000]}\n\n"
            f"Return ONLY a JSON object:\n"
            f'{{"title": "...", "summary": "...", "key_facts": ["..."], '
            f'"domain": "decisions|infra|projects|chris"}}'
        )
        result = dispatch(agent="sage", message=prompt, thinking="low", timeout=60)
        if not result.ok:
            return {"status": "dispatch_failed", "error": result.error[:200]}

        try:
            extracted = _json.loads(result.text.strip().strip("`").strip())
        except _json.JSONDecodeError:
            extracted = {
                "title": source_name,
                "summary": result.text[:500],
                "key_facts": [],
                "domain": "decisions",
            }

        inbox_dir = BRAIN_DIR.parent / "knowledge" / "raw" / "inbox"
        inbox_dir.mkdir(parents=True, exist_ok=True)
        slug = hashlib.md5(content[:200].encode()).hexdigest()[:12]  # noqa: S324 — non-crypto slug
        record = {
            "id": f"raw_manual_{slug}",
            "type": "raw",
            "subtype": "manual_ingest",
            "title": extracted.get("title", source_name)[:120],
            "content": extracted.get("summary", ""),
            "key_facts": extracted.get("key_facts", []),
            "domain": extracted.get("domain", "decisions"),
            "source": source_name,
            "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        }
        out_path = inbox_dir / f"manual_{slug}.json"
        out_path.write_text(_json.dumps(record, indent=2, ensure_ascii=False))

        return {
            "status": "ingested",
            "id": record["id"],
            "title": record["title"],
            "path": str(out_path.relative_to(BRAIN_DIR.parent)),
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=_safe_http_detail("internal", e)) from e


# ── Canonical index rebuild ───────────────────────────
_INDEX_SKIP_NAMES = {"index.md", "_index.md", "_identity.md", "_state.md", "_profile.md"}
_INDEX_BOILERPLATE = {
    "review this proposed canonical note.",
    "review this proposed canonical note",
    "## statement",
    "## source summary",
    "## summary",
    "## observations",
}
_INDEX_MANUAL_OPEN = "<!-- manual-edit-above -->"
_INDEX_MANUAL_CLOSE = "<!-- manual-edit-below -->"


def _index_extract_summary(meta: dict, body: str) -> str:
    ps = (meta.get("provenance_summary") or "").strip()
    if ps and len(ps) > 20 and "review this proposed" not in ps.lower():
        return ps[:140]
    for raw in body.splitlines():
        line = raw.strip().lstrip("- ").lstrip("* ").strip()
        if not line or line.startswith("#") or line.startswith("<!--"):
            continue
        if line.lower() in _INDEX_BOILERPLATE:
            continue
        return line[:140]
    return ""


def _index_preserve_manual_block(existing_path: Path) -> str | None:
    if not existing_path.exists():
        return None
    try:
        text = existing_path.read_text()
    except Exception:
        return None
    if _INDEX_MANUAL_OPEN not in text or _INDEX_MANUAL_CLOSE not in text:
        return None
    start = text.index(_INDEX_MANUAL_OPEN)
    end = text.index(_INDEX_MANUAL_CLOSE) + len(_INDEX_MANUAL_CLOSE)
    return text[start:end]


@router.post("/brain/index/rebuild", tags=["autonomy"])
def rebuild_canonical_index() -> dict:
    """Rebuild canonical/index.md + index.json sidecar."""
    try:
        knowledge_dir = BRAIN_DIR.parent / "knowledge"
        canonical_dir = knowledge_dir / "canonical"
        if not canonical_dir.exists():
            return {"status": "no canonical dir"}

        entries = []
        for md_file in sorted(canonical_dir.rglob("*.md")):
            if md_file.name in _INDEX_SKIP_NAMES or md_file.name.endswith(".bak"):
                continue
            if any(part.endswith(".bak") for part in md_file.parts):
                continue
            try:
                text = md_file.read_text()
                lines = text.splitlines()
                if not lines or not lines[0].startswith("---"):
                    continue
                end_idx = None
                for i in range(1, len(lines)):
                    if lines[i].strip() == "---":
                        end_idx = i
                        break
                if end_idx is None:
                    continue
                meta = _json.loads("\n".join(lines[1:end_idx]))
                if meta.get("type") != "canonical":
                    continue
                if meta.get("status") != "active":
                    continue
                body = "\n".join(lines[end_idx + 1 :])
                entries.append(
                    {
                        "id": meta.get("id", ""),
                        "title": meta.get("title", md_file.stem),
                        "domain": meta.get("domain", "") or "other",
                        "subtype": meta.get("subtype", ""),
                        "status": meta.get("status", ""),
                        "confidence": meta.get("confidence", 0),
                        "summary": _index_extract_summary(meta, body),
                        "updated_at": meta.get("updated_at", ""),
                        "path": str(md_file.relative_to(knowledge_dir)),
                    }
                )
            except Exception:  # noqa: S112 — skip malformed canonical files silently
                continue

        by_domain: dict[str, list] = {}
        for e in entries:
            by_domain.setdefault(e["domain"], []).append(e)

        index_path = canonical_dir / "index.md"
        json_path = canonical_dir / "index.json"
        manual_block = _index_preserve_manual_block(index_path)

        header = [
            "# Canonical Knowledge Index",
            f"Generated: {datetime.now(UTC).isoformat(timespec='seconds')}",
            f"Total: {len(entries)} active canonical notes across {len(by_domain)} domains",
            "",
        ]
        if manual_block:
            header.append(manual_block)
            header.append("")
        else:
            header.extend([_INDEX_MANUAL_OPEN, _INDEX_MANUAL_CLOSE, ""])

        body_lines: list[str] = []
        for domain in sorted(by_domain):
            body_lines.append(f"## {domain} ({len(by_domain[domain])})")
            for e in sorted(by_domain[domain], key=lambda x: x["title"].lower()):
                summary = e["summary"] or "_no summary_"
                body_lines.append(f"- **{e['title']}** (`{e['id']}`) — {summary}")
            body_lines.append("")

        index_path.write_text("\n".join(header + body_lines) + "\n")

        json_path.write_text(
            _json.dumps(
                {
                    "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
                    "total": len(entries),
                    "domains": {d: len(es) for d, es in by_domain.items()},
                    "entries": entries,
                },
                indent=2,
                ensure_ascii=False,
            )
            + "\n"
        )

        return {
            "status": "rebuilt",
            "total_notes": len(entries),
            "domains": len(by_domain),
            "path": str(index_path.relative_to(knowledge_dir)),
            "json_path": str(json_path.relative_to(knowledge_dir)),
            "manual_block_preserved": manual_block is not None,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=_safe_http_detail("internal", e)) from e


@router.get("/brain/canonical_lint", tags=["lint"])
def canonical_lint_latest() -> dict:
    """Return the latest canonical_lint report."""
    try:
        report_dir = BRAIN_DIR.parent / "knowledge" / "reports" / "canonical_lint"
        if not report_dir.exists():
            return {"status": "no_report", "reports": []}
        json_reports = sorted(report_dir.glob("*.json"), reverse=True)
        if not json_reports:
            return {"status": "no_report", "reports": []}
        latest = json_reports[0]
        return {
            "status": "ok",
            "latest_path": latest.name,
            "report": _json.loads(latest.read_text()),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=_safe_http_detail("internal", e)) from e


@router.post("/brain/canonicalize", tags=["decide"])
def brain_canonicalize(req: CanonicalizeRequest) -> dict:
    """Mark a query→answer pair as canonical-worthy."""
    try:
        import answer_candidates as _ac

        row_id = _ac.record(
            source_route=req.source_route,
            query=req.query,
            answer=req.answer,
            agent=req.agent,
            reason=req.reason,
        )
        if row_id == 0:
            return {"status": "skipped", "reason": "answer too short or empty"}
        return {"status": "recorded", "candidate_id": row_id}
    except Exception as e:
        raise HTTPException(status_code=500, detail=_safe_http_detail("internal", e)) from e


@router.get("/brain/answer_candidates", tags=["decide"])
def answer_candidates_list(status: str = "pending", limit: int = 20) -> dict:
    """List answer candidates. Default: recent pending."""
    try:
        import answer_candidates as _ac

        if status == "pending":
            items = _ac.list_pending(limit=limit)
        else:
            from brain_core.config import BRAIN_DB as _BDB

            with sqlite3.connect(str(_BDB)) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    "SELECT * FROM answer_candidates WHERE status=? ORDER BY created_at DESC LIMIT ?",
                    (status, limit),
                ).fetchall()
                items = [dict(r) for r in rows]
        return {"status": "ok", "count": len(items), "items": items, "stats": _ac.stats()}
    except Exception as e:
        raise HTTPException(status_code=500, detail=_safe_http_detail("internal", e)) from e
