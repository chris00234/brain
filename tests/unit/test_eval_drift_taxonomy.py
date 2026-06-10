"""Drift taxonomy for trend-track eval failures (Contract 10).

The taxonomy tool classifies failing per-test rows from an
eval_compare --include-per-test report into durable drift classes, checking
expected sources/phrases against the live knowledge tree, so fixture debt,
superseded truth, and genuine ranking misses stop being one undifferentiated
failure count.
"""

from __future__ import annotations

import json

from eval_drift_taxonomy import classify_case, classify_report


def _knowledge(tmp_path):
    root = tmp_path / "knowledge"
    (root / "canonical" / "chris").mkdir(parents=True)
    (root / "canonical" / "archived").mkdir(parents=True)
    (root / "distilled" / "infra").mkdir(parents=True)
    (root / "obsolete").mkdir(parents=True)
    (root / "canonical" / "chris" / "_state.md").write_text("primary active sprint: brain hardening")
    (root / "distilled" / "infra" / "dist_gateway.md").write_text("gateway listens on loopback")
    (root / "canonical" / "archived" / "old-rule.md").write_text("the legacy approval flow is mandatory")
    (root / "obsolete" / "dead.md").write_text("retired fact only here")
    return root


def _case(**kw):
    base = {
        "query": "q",
        "expected_source": "",
        "expected_content": "",
        "hit_source": False,
        "hit_content": False,
        "hit_content_loose": False,
        "top_sources": ["something"],
    }
    base.update(kw)
    return base


def test_zero_results_class(tmp_path):
    c = classify_case(_case(top_sources=[]), knowledge_root=_knowledge(tmp_path))
    assert c["primary_class"] == "zero_results"


def test_loose_only_paraphrase(tmp_path):
    c = classify_case(
        _case(hit_source=True, hit_content_loose=True, expected_content="primary active sprint"),
        knowledge_root=_knowledge(tmp_path),
    )
    assert c["primary_class"] == "loose_only_paraphrase"


def test_phrase_live_ranking_miss(tmp_path):
    c = classify_case(
        _case(expected_content="primary active sprint", expected_source="canonical/chris/_state.md"),
        knowledge_root=_knowledge(tmp_path),
    )
    assert c["primary_class"] == "phrase_live_ranking_miss"
    assert any("_state.md" in e for e in c["evidence"])


def test_stale_expected_phrase(tmp_path):
    c = classify_case(
        _case(expected_content="phrase that exists nowhere at all"),
        knowledge_root=_knowledge(tmp_path),
    )
    assert c["primary_class"] == "stale_expected_phrase"


def test_phrase_only_in_archive(tmp_path):
    root = _knowledge(tmp_path)
    c = classify_case(_case(expected_content="legacy approval flow is mandatory"), knowledge_root=root)
    assert c["primary_class"] == "phrase_only_in_archive"
    c2 = classify_case(_case(expected_content="retired fact only here"), knowledge_root=root)
    assert c2["primary_class"] == "phrase_only_in_archive"


def test_source_only_miss_when_no_expected_content(tmp_path):
    c = classify_case(
        _case(expected_source="calendar", hit_content=True, hit_content_loose=True),
        knowledge_root=_knowledge(tmp_path),
    )
    assert c["primary_class"] == "source_only_miss"


def test_vanished_expected_source_flag(tmp_path):
    c = classify_case(
        _case(
            expected_source="canonical/chris/deleted-note.md",
            expected_content="primary active sprint",
        ),
        knowledge_root=_knowledge(tmp_path),
    )
    assert "vanished_expected_source" in c["flags"]


def test_collection_token_source_is_not_vanished(tmp_path):
    c = classify_case(
        _case(expected_source="distilled", expected_content="gateway listens on loopback"),
        knowledge_root=_knowledge(tmp_path),
    )
    assert "vanished_expected_source" not in c["flags"]


def test_classify_report_aggregates(tmp_path):
    root = _knowledge(tmp_path)
    report = {
        "v2": {
            "per_test": [
                _case(hit_source=True, hit_content=True, hit_content_loose=True),  # pass: excluded
                _case(top_sources=[]),
                _case(expected_content="phrase that exists nowhere at all"),
            ]
        }
    }
    path = tmp_path / "report.json"
    path.write_text(json.dumps(report))
    out = classify_report(path, track="default", knowledge_root=root)
    assert out["track"] == "default"
    assert out["total"] == 3
    assert out["failures"] == 2
    assert out["classes"]["zero_results"] == 1
    assert out["classes"]["stale_expected_phrase"] == 1
