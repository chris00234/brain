#!/usr/bin/env python3
"""Privacy-negative sample audit for high-value personal-source vectors.

This is a bounded, read-only guardrail. It does not print private content; it
reports point ids, source metadata, and violation codes only.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "brain_core"))

from source_policy import (  # noqa: E402
    PRIVACY_REDACTION_VERSION,
    redact_sensitive_text,
    sensitive_text_findings,
)

DEFAULT_REPORT = ROOT / "logs" / "privacy-negative-audit.json"
DEFAULT_COLLECTIONS = ("personal",)
REQUIRED_CONTRACT_FIELDS = (
    "entry_schema_version",
    "content_hash",
    "source_type",
    "source_kind",
    "chunk_strategy",
    "tags",
)


@dataclass(frozen=True)
class Finding:
    severity: str
    code: str
    collection: str
    point_id: str
    source_type: str
    source: str
    detail: str


def _payload_text(payload: dict[str, Any], document: str | None) -> str:
    fields: list[str] = []
    if document:
        fields.append(document)
    for key in ("title", "summary", "snippet", "document_title", "source_name"):
        value = payload.get(key)
        if isinstance(value, str):
            fields.append(value)
    return "\n".join(fields)


def _source_ref(payload: dict[str, Any]) -> str:
    return str(payload.get("source") or payload.get("source_path") or payload.get("source_name") or "")[:160]


def audit_point(collection: str, point: Any) -> list[Finding]:
    payload = dict(getattr(point, "payload", {}) or {})
    point_id = str(getattr(point, "id", "") or payload.get("vector_point_id") or "")
    source_type = str(payload.get("source_type") or payload.get("type") or "")
    source = _source_ref(payload)
    document = getattr(point, "document", None)
    findings: list[Finding] = []

    for field in REQUIRED_CONTRACT_FIELDS:
        if payload.get(field) in (None, "", [], {}):
            findings.append(
                Finding(
                    severity="blocking",
                    code="missing_contract_field",
                    collection=collection,
                    point_id=point_id,
                    source_type=source_type,
                    source=source,
                    detail=field,
                )
            )

    text = _payload_text(payload, document)
    for code in sensitive_text_findings(text):
        findings.append(
            Finding(
                severity="blocking",
                code=f"secret_like_{code}",
                collection=collection,
                point_id=point_id,
                source_type=source_type,
                source=source,
                detail="pattern matched; content suppressed",
            )
        )

    if source_type in {"message", "imessage", "sms"} and len(document or "") > 800:
        findings.append(
            Finding(
                severity="warning",
                code="raw_message_oversize",
                collection=collection,
                point_id=point_id,
                source_type=source_type,
                source=source,
                detail=f"document_chars={len(document or '')}",
            )
        )
    return findings


def _get_points(collection: str, limit: int) -> list[Any]:
    from vector_store import get_vector_store

    return get_vector_store().get(
        collection,
        limit=limit,
        with_payload=True,
        with_vectors=False,
        with_documents=True,
    )


def _repair_redactions(collection: str, points: list[Any]) -> int:
    from vector_store import get_vector_store

    store = get_vector_store()
    repaired = 0
    for point in points:
        patch: dict[str, Any] = {}
        codes_all: list[str] = []
        document = getattr(point, "document", None) or ""
        redacted, codes = redact_sensitive_text(document)
        if codes and redacted != document:
            patch["_document"] = redacted
            codes_all.extend(codes)
        payload = dict(getattr(point, "payload", {}) or {})
        for key in ("title", "summary", "snippet", "document_title", "source_name"):
            value = payload.get(key)
            if not isinstance(value, str):
                continue
            redacted_value, value_codes = redact_sensitive_text(value)
            if value_codes and redacted_value != value:
                patch[key] = redacted_value
                codes_all.extend(value_codes)
        if not patch:
            continue
        codes_unique = sorted(set(codes_all))
        ok = store.update_payload(
            collection,
            [str(getattr(point, "id", ""))],
            {
                "privacy_redaction_version": PRIVACY_REDACTION_VERSION,
                "privacy_redaction_count": len(codes_unique),
                "privacy_redaction_codes": codes_unique,
                "privacy_redacted_at": datetime.now(UTC).isoformat(timespec="seconds"),
                **patch,
            },
        )
        if ok:
            repaired += 1
    return repaired


def _reindex_redacted_points(collection: str, points: list[Any]) -> int:
    """Re-upsert redacted points so dense/sparse vectors use redacted text."""

    from indexer import get_embedding
    from vector_store import get_vector_store

    store = get_vector_store()
    reindexed = 0
    for point in points:
        payload = dict(getattr(point, "payload", {}) or {})
        if not payload.get("privacy_redaction_version") and not payload.get("privacy_redaction_count"):
            continue
        document = getattr(point, "document", None) or ""
        if not document:
            continue
        vector = get_embedding(document[:8000], prefix="passage")
        store.upsert(
            collection,
            [str(getattr(point, "id", ""))],
            [vector],
            [payload],
            [document],
        )
        reindexed += 1
    return reindexed


def run(
    *,
    collections: Iterable[str] = DEFAULT_COLLECTIONS,
    limit_per_collection: int = 200,
    report_file: Path = DEFAULT_REPORT,
    repair_redact: bool = False,
    reindex_redacted: bool = False,
) -> dict[str, Any]:
    collection_list = tuple(collections)
    findings: list[Finding] = []
    collection_counts: dict[str, int] = {}
    errors: dict[str, str] = {}
    repaired_points = 0
    reindexed_points = 0
    for collection in collection_list:
        try:
            points = _get_points(collection, limit_per_collection)
        except Exception as exc:
            collection_counts[collection] = 0
            errors[collection] = str(exc)[:200]
            findings.append(
                Finding(
                    severity="blocking",
                    code="collection_scan_failed",
                    collection=collection,
                    point_id="",
                    source_type="",
                    source="",
                    detail=str(exc)[:200],
                )
            )
            continue
        collection_counts[collection] = len(points)
        for point in points:
            findings.extend(audit_point(collection, point))
        if repair_redact:
            repaired_points += _repair_redactions(collection, points)
        if reindex_redacted:
            reindexed_points += _reindex_redacted_points(collection, points)

    if (repair_redact and repaired_points) or (reindex_redacted and reindexed_points):
        # Re-scan after repair/reindex so the persisted report reflects current state.
        report = run(
            collections=collection_list,
            limit_per_collection=limit_per_collection,
            report_file=report_file,
            repair_redact=False,
            reindex_redacted=False,
        )
        report["repaired_points"] = repaired_points
        report["reindexed_points"] = reindexed_points
        report_file.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return report

    blocking = [f for f in findings if f.severity == "blocking"]
    warnings = [f for f in findings if f.severity == "warning"]
    report = {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "status": "blocked" if blocking else "ok",
        "collections": list(collection_list),
        "collection_counts": collection_counts,
        "sampled_points": sum(collection_counts.values()),
        "blocking_findings": len(blocking),
        "warnings": len(warnings),
        "errors": errors,
        "findings": [asdict(f) for f in findings[:200]],
        "content_suppressed": True,
        "repaired_points": repaired_points,
        "reindexed_points": reindexed_points,
    }
    report_file.parent.mkdir(parents=True, exist_ok=True)
    report_file.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument("--collections", default=",".join(DEFAULT_COLLECTIONS))
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--repair-redact", action="store_true")
    parser.add_argument("--reindex-redacted", action="store_true")
    args = parser.parse_args()
    collections = tuple(c.strip() for c in args.collections.split(",") if c.strip())
    report = run(
        collections=collections,
        limit_per_collection=max(1, int(args.limit)),
        report_file=args.report,
        repair_redact=args.repair_redact,
        reindex_redacted=args.reindex_redacted,
    )
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if report["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
