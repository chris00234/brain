from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "privacy_negative_audit",
    ROOT / "cli" / "privacy_negative_audit.py",
)
privacy_negative_audit = importlib.util.module_from_spec(SPEC)
sys.modules["privacy_negative_audit"] = privacy_negative_audit
assert SPEC.loader is not None
SPEC.loader.exec_module(privacy_negative_audit)


def _point(
    *,
    point_id: str = "p1",
    document: str = "normal private note summary",
    payload: dict | None = None,
):
    base = {
        "entry_schema_version": "brain-entry-v2",
        "content_hash": "abc",
        "source_type": "note",
        "source_kind": "named_source",
        "chunk_strategy": "paragraph",
        "tags": ["source:note"],
        "source": "apple-notes://1",
    }
    if payload:
        base.update(payload)
    return SimpleNamespace(id=point_id, document=document, payload=base)


def test_audit_point_blocks_secret_like_content_without_content():
    findings = privacy_negative_audit.audit_point(
        "personal",
        _point(document="password: definitely-secret-value"),
    )

    assert findings[0].severity == "blocking"
    assert findings[0].code == "secret_like_explicit_password"
    assert findings[0].detail == "pattern matched; content suppressed"
    assert "definitely-secret" not in repr(findings[0])


def test_run_writes_report_and_counts_findings(tmp_path, monkeypatch):
    monkeypatch.setattr(
        privacy_negative_audit,
        "_get_points",
        lambda collection, limit: [
            _point(point_id="ok"),
            _point(point_id="bad", payload={"content_hash": ""}),
        ],
    )

    report = privacy_negative_audit.run(report_file=tmp_path / "privacy.json")

    assert report["status"] == "blocked"
    assert report["sampled_points"] == 2
    assert report["blocking_findings"] == 1
    assert report["content_suppressed"] is True
    assert (tmp_path / "privacy.json").exists()


def test_raw_message_oversize_is_warning_only():
    findings = privacy_negative_audit.audit_point(
        "personal",
        _point(
            document="x" * 801,
            payload={"source_type": "message", "chunk_strategy": "atomic"},
        ),
    )

    assert findings[0].severity == "warning"
    assert findings[0].code == "raw_message_oversize"


def test_reindex_redacted_points_upserts_only_redacted_documents(monkeypatch):
    calls = []

    fake_store = SimpleNamespace(
        upsert=lambda collection, ids, vectors, payloads, documents: calls.append(
            {
                "collection": collection,
                "ids": ids,
                "vectors": vectors,
                "payloads": payloads,
                "documents": documents,
            }
        )
    )
    monkeypatch.setitem(
        sys.modules,
        "vector_store",
        SimpleNamespace(get_vector_store=lambda: fake_store),
    )
    monkeypatch.setitem(
        sys.modules,
        "indexer",
        SimpleNamespace(get_embedding=lambda text, prefix: [0.1, 0.2] if prefix == "passage" else []),
    )

    reindexed = privacy_negative_audit._reindex_redacted_points(
        "personal",
        [
            _point(
                point_id="redacted",
                document="safe [REDACTED:github_token]",
                payload={"privacy_redaction_version": "privacy-redaction-v1"},
            ),
            _point(point_id="plain", document="normal private note"),
        ],
    )

    assert reindexed == 1
    assert calls == [
        {
            "collection": "personal",
            "ids": ["redacted"],
            "vectors": [[0.1, 0.2]],
            "payloads": [
                {
                    "entry_schema_version": "brain-entry-v2",
                    "content_hash": "abc",
                    "source_type": "note",
                    "source_kind": "named_source",
                    "chunk_strategy": "paragraph",
                    "tags": ["source:note"],
                    "source": "apple-notes://1",
                    "privacy_redaction_version": "privacy-redaction-v1",
                }
            ],
            "documents": ["safe [REDACTED:github_token]"],
        }
    ]


def test_run_reports_reindexed_points(tmp_path, monkeypatch):
    point = _point(
        point_id="redacted",
        document="safe [REDACTED:github_token]",
        payload={"privacy_redaction_version": "privacy-redaction-v1"},
    )
    monkeypatch.setattr(privacy_negative_audit, "_get_points", lambda collection, limit: [point])
    monkeypatch.setattr(
        privacy_negative_audit,
        "_reindex_redacted_points",
        lambda collection, points: 1,
    )

    report = privacy_negative_audit.run(
        report_file=tmp_path / "privacy.json",
        reindex_redacted=True,
    )

    assert report["status"] == "ok"
    assert report["reindexed_points"] == 1
    assert report["repaired_points"] == 0
