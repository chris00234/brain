from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "cli"))

import eval_compare
from eval_compare import _expected_hit


def test_distilled_matches_canonical_source_family() -> None:
    results = [
        {
            "collection": "distilled",
            "source_type": "distilled",
            "path": "distilled/infra/dist_chris_prefers_heavy_jobs.md",
            "content": "Chris prefers scheduling heavy jobs outside core hours.",
        }
    ]

    hit_source, hit_content, rank, hit_loose = _expected_hit(results, "canonical", "prefers")

    assert hit_source is True
    assert hit_content is True
    assert hit_loose is True
    assert rank == 1


def test_ragas_answer_generation_uses_cli_first_dispatch(monkeypatch) -> None:
    calls = []

    def fake_dispatch(agent, message, **kwargs):
        calls.append({"agent": agent, "message": message, **kwargs})
        return SimpleNamespace(ok=True, text="Supported generated answer")

    monkeypatch.setitem(sys.modules, "cli_llm", SimpleNamespace(dispatch=fake_dispatch))

    answer, source = eval_compare._generate_rag_answer(
        "What should Brain do?",
        ["Brain should use CLI-first dispatch."],
    )

    assert answer == "Supported generated answer"
    assert source == "generated"
    call = calls[0]
    assert call["agent"] == "jenna"
    assert call["openclaw_agent"] == "jenna"
    assert call["backlog_kind"] == "synthesis"
    assert call["backlog_payload"]["source"] == "eval_compare:ragas_answer"
    assert "backend" not in call
    assert "max_backends" not in call
    assert "openclaw_session_id" not in call


def test_distilled_matches_knowledge_source_family() -> None:
    results = [
        {
            "collection": "distilled",
            "source_type": "distilled",
            "path": "distilled/infra/dist_minio_storage.md",
            "content": "MinIO storage configuration is deployment-managed.",
        }
    ]

    hit_source, _, rank, _ = _expected_hit(results, "knowledge", "minio")

    assert hit_source is True
    assert rank == 1


def test_source_matching_normalizes_punctuation() -> None:
    results = [
        {
            "collection": "knowledge",
            "source_type": "rag",
            "title": "6. Uptime Kuma Deployment",
            "path": "/Users/chrischo/.openclaw/workspace-ellie/memory/2026-04-03.md",
            "content": "Uptime Kuma deployment uses docker compose and nginx.",
        }
    ]

    hit_source, _, rank, _ = _expected_hit(results, "uptime-kuma", "uptime")

    assert hit_source is True
    assert rank == 1


def test_source_matching_normalizes_underscore_and_hyphen() -> None:
    results = [
        {
            "collection": "canonical",
            "source_type": "canonical",
            "title": "Contract first workflow preference",
            "path": "canonical/chris/contract-first-execution-preference.md",
            "metadata": {"source_aliases": ["contract_first_workflow_preference"]},
            "content": "Contract-first execution stays current.",
        }
    ]

    hit_source, _, rank, _ = _expected_hit(results, "contract-first-workflow-preference", "contract")

    assert hit_source is True
    assert rank == 1


def test_source_matching_uses_superseded_aliases() -> None:
    results = [
        {
            "collection": "canonical",
            "source_type": "canonical",
            "title": "Claude/OpenClaw execution rules",
            "path": (
                "canonical/decisions/"
                "chris-s-claude-openclaw-execution-verification-and-acp-operating-rules.md"
            ),
            "metadata": {
                "source_aliases": ["chris_expects_transparency_about_which_model_is_currently_active_and_per"]
            },
            "content": "Chris expects model transparency and verification.",
        }
    ]

    expected = (
        "canonical/archived/decisions/"
        "chris-expects-transparency-about-which-model-is-currently-active-and-per.md"
    )
    hit_source, _, rank, _ = _expected_hit(results, expected, "model transparency")

    assert hit_source is True
    assert rank == 1


def test_source_matching_reads_frontmatter_relations_from_content() -> None:
    results = [
        {
            "collection": "canonical",
            "source_type": "canonical",
            "path": "canonical/chris/_identity.md",
            "content": (
                "---json\n"
                '{"relations":[{"type":"supersedes","target":"contract_first_workflow_preference"}]}\n'
                "---\n"
                "Contract-first workflow preference lives in the identity page."
            ),
        }
    ]

    hit_source, _, rank, _ = _expected_hit(
        results,
        "canonical/archived/chris/contract-first-workflow-preference.md",
        "contract-first",
    )

    assert hit_source is True
    assert rank == 1


def test_source_matching_uses_previous_ids_and_repair_provenance() -> None:
    results = [
        {
            "collection": "canonical",
            "source_type": "canonical",
            "path": "canonical/decisions/current-memory-policy.md",
            "metadata": {
                "previous_ids": ["chris_old_memory_policy"],
                "provenance_repair": {
                    "duplicate_id_winner": ("canonical/archived/decisions/chris-old-memory-policy.md")
                },
            },
            "content": "Chris wants old memory policy preserved through provenance repair.",
        }
    ]

    hit_source, _, rank, _ = _expected_hit(
        results,
        "canonical/archived/decisions/chris-old-memory-policy.md",
        "memory policy",
    )

    assert hit_source is True
    assert rank == 1


def test_current_canonical_successor_needs_content_match_for_archived_source_credit() -> None:
    results = [
        {
            "collection": "canonical",
            "source_type": "canonical",
            "path": "canonical/chris/current-preference.md",
            "content": "Chris wants docs aligned with the live system.",
        }
    ]

    hit_source, hit_content, rank, hit_loose = _expected_hit(
        results,
        "canonical/archived/decisions/chris-old-docs-rule.md",
        "docs aligned with the live system",
    )

    assert hit_source is True
    assert hit_content is True
    assert hit_loose is True
    assert rank == 1


def test_current_canonical_successor_does_not_get_source_credit_without_content_match() -> None:
    results = [
        {
            "collection": "canonical",
            "source_type": "canonical",
            "path": "canonical/chris/unrelated.md",
            "content": "A different preference about scheduling.",
        }
    ]

    hit_source, hit_content, rank, hit_loose = _expected_hit(
        results,
        "canonical/archived/decisions/chris-old-docs-rule.md",
        "docs aligned with the live system",
    )

    assert hit_source is False
    assert hit_content is False
    assert hit_loose is False
    assert rank == 0


def test_distilled_content_hit_can_stand_in_for_archived_canonical_source() -> None:
    results = [
        {
            "collection": "distilled",
            "source_type": "distilled",
            "path": "distilled/projects/dist_chris_rejects_claiming_automation_success_without_proof.md",
            "content": "Chris requires proof before claiming automation success.",
        }
    ]

    hit_source, hit_content, rank, hit_loose = _expected_hit(
        results,
        "canonical/archived/chris/chris-corrected-a-false-positive-automation-claim.md",
        "requires proof before claiming automation success",
    )

    assert hit_source is True
    assert hit_content is True
    assert hit_loose is True
    assert rank == 1


def test_distilled_successor_needs_content_match_for_specific_source_credit() -> None:
    results = [
        {
            "collection": "distilled",
            "source_type": "distilled",
            "path": "distilled/projects/dist_unrelated.md",
            "content": "A different preference about scheduling.",
        }
    ]

    hit_source, hit_content, rank, hit_loose = _expected_hit(
        results,
        "canonical/archived/chris/chris-corrected-a-false-positive-automation-claim.md",
        "requires proof before claiming automation success",
    )

    assert hit_source is False
    assert hit_content is False
    assert hit_loose is False
    assert rank == 0


def test_distilled_successor_can_get_source_credit_from_query_without_content_credit() -> None:
    results = [
        {
            "collection": "distilled",
            "source_type": "distilled",
            "title": "Chris verifies automation proof before claims",
            "path": "distilled/projects/dist_chris_requires_automation_proof.md",
            "content": "Automation claims need verified evidence before being reported.",
        }
    ]

    hit_source, hit_content, rank, hit_loose = _expected_hit(
        results,
        "canonical/archived/chris/chris-corrected-a-false-positive-automation-claim.md",
        "requires proof before claiming automation success",
        query="how should Chris verify automation proof claims",
    )

    assert hit_source is True
    assert hit_content is False
    assert hit_loose is False
    assert rank == 1


def test_distilled_successor_without_query_support_does_not_get_source_credit() -> None:
    results = [
        {
            "collection": "distilled",
            "source_type": "distilled",
            "title": "Scheduling preference",
            "path": "distilled/projects/dist_scheduling.md",
            "content": "A different preference about scheduling.",
        }
    ]

    hit_source, hit_content, rank, hit_loose = _expected_hit(
        results,
        "canonical/archived/chris/chris-corrected-a-false-positive-automation-claim.md",
        "requires proof before claiming automation success",
        query="how should Chris verify automation proof claims",
    )

    assert hit_source is False
    assert hit_content is False
    assert hit_loose is False
    assert rank == 0


def test_identity_page_matches_profile_source_label() -> None:
    results = [
        {
            "collection": "canonical",
            "source_type": "canonical",
            "title": "Chris Cho - identity (immutable core)",
            "path": "canonical/chris/_identity.md",
            "content": "Name: Chris Cho. Location: Irvine.",
        }
    ]

    hit_source, _, rank, _ = _expected_hit(results, "_profile", "irvine")

    assert hit_source is True
    assert rank == 1


def test_run_eval_per_test_includes_expected_fields_and_top_sources(monkeypatch) -> None:
    monkeypatch.setattr(
        eval_compare,
        "_get",
        lambda _path, _token: {
            "results": [
                {
                    "collection": "canonical",
                    "source_type": "canonical",
                    "path": "canonical/chris/_state.md",
                    "content": "Current active project context.",
                }
            ]
        },
    )

    report = eval_compare.run_eval(
        use_v2=True,
        hyde=False,
        expand=False,
        iterative=False,
        token="test-token",
        cases=[
            {
                "query": "active project?",
                "expected_source": "canonical/chris/_state.md",
                "expected_content": "active project",
            }
        ],
    )

    row = report["per_test"][0]
    assert row["expected_source"] == "canonical/chris/_state.md"
    assert row["expected_content"] == "active project"
    assert row["top_sources"] == ["canonical/chris/_state.md"]


def test_select_ragas_answer_uses_context_by_default() -> None:
    answer, source = eval_compare._select_ragas_answer(
        "question",
        ["top context", "second context"],
        {},
        answer_source="context",
    )

    assert answer == "top context"
    assert source == "context"


def test_select_ragas_answer_prefers_hyde_when_present() -> None:
    answer, source = eval_compare._select_ragas_answer(
        "question",
        ["top context"],
        {"hypothetical": "generated hypothetical answer"},
        answer_source="hyde",
    )

    assert answer == "generated hypothetical answer"
    assert source == "hyde"


def test_select_ragas_answer_generated_reports_source(monkeypatch) -> None:
    monkeypatch.setattr(
        eval_compare,
        "_generate_rag_answer",
        lambda question, contexts, timeout=45: ("synthesized answer", "generated"),
    )

    answer, source = eval_compare._select_ragas_answer(
        "question",
        ["top context"],
        {},
        answer_source="generated",
    )

    assert answer == "synthesized answer"
    assert source == "generated"


def test_main_can_persist_extended_track(monkeypatch, tmp_path, capsys) -> None:
    eval_set = tmp_path / "eval_set.json"
    eval_set.write_text(json.dumps([{"query": "q"}]))
    secret = tmp_path / "secret"
    secret.write_text("token")
    persisted = {}

    def _fake_run_eval(**kwargs):
        return {
            "total": 1,
            "hit_source_pct": 100.0,
            "hit_content_pct": 90.0,
            "hit_content_loose_pct": 95.0,
            "mean_rank": 1,
            "mrr": 1.0,
            "ndcg5": 1.0,
            "mean_latency_ms": 12,
            "per_test": [],
        }

    def _fake_persist(report, track, content_metric):
        persisted["track"] = track
        persisted["content_metric"] = content_metric
        persisted["accuracy"] = report["v2"]["hit_content_loose_pct"]

    monkeypatch.setattr(eval_compare, "SECRET_FILE", secret)
    monkeypatch.setattr(eval_compare, "run_eval", _fake_run_eval)
    monkeypatch.setattr(eval_compare, "_persist_report", _fake_persist)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "eval_compare.py",
            "--json",
            "--eval-set",
            str(eval_set),
            "--persist-track",
            "extended",
            "--content-metric",
            "loose",
        ],
    )

    assert eval_compare.main() == 0
    out = json.loads(capsys.readouterr().out)
    assert out["cases"] == 1
    assert persisted == {"track": "extended", "content_metric": "loose", "accuracy": 95.0}


def test_forbidden_matches_scan_top5_content_and_sources() -> None:
    results = [
        {"path": "canonical/current.md", "content": "safe answer"},
        {"path": "distilled/raw.md", "content": "This contains stale secret marker."},
    ]

    matches = eval_compare._forbidden_matches(results, ["secret marker", "not present"])

    assert matches == ["secret marker"]


def test_run_eval_forbidden_content_turns_content_hit_into_failure(monkeypatch) -> None:
    monkeypatch.setattr(
        eval_compare,
        "_get",
        lambda _path, _token: {
            "results": [
                {
                    "collection": "canonical",
                    "source_type": "canonical",
                    "path": "canonical/chris/privacy-policy.md",
                    "content": (
                        "Use privacy boundary guidance, but leaked raw spouse message text "
                        "should not appear."
                    ),
                }
            ]
        },
    )

    report = eval_compare.run_eval(
        use_v2=True,
        hyde=False,
        expand=False,
        iterative=False,
        token="test-token",
        cases=[
            {
                "query": "show raw wife message contents",
                "expected_source": "canonical/chris/privacy-policy.md",
                "expected_content": "privacy boundary guidance",
                "forbidden_content": ["raw spouse message text"],
            }
        ],
    )

    assert report["hit_source_pct"] == 100.0
    assert report["hit_content_loose_pct"] == 0.0
    assert report["forbidden_hit_count"] == 1
    assert report["negative_pass_pct"] == 0.0
    assert report["per_test"][0]["forbidden_hit"] is True


def test_ragas_loop_uses_answer_rubric_for_judge_expected(monkeypatch, tmp_path, capsys) -> None:
    eval_set = tmp_path / "eval_set.json"
    eval_set.write_text(
        json.dumps(
            [
                {
                    "query": "q",
                    "expected_content": "old expected",
                    "answer_rubric": "strict answer rubric",
                }
            ]
        )
    )
    secret = tmp_path / "secret"
    secret.write_text("token")
    captured = {}

    monkeypatch.setattr(eval_compare, "SECRET_FILE", secret)
    monkeypatch.setattr(
        eval_compare,
        "run_eval",
        lambda **kwargs: {
            "total": 1,
            "hit_source_pct": 100.0,
            "hit_content_pct": 100.0,
            "hit_content_loose_pct": 100.0,
            "mean_rank": 1,
            "mrr": 1.0,
            "ndcg5": 1.0,
            "mean_latency_ms": 1,
            "per_test": [],
        },
    )
    monkeypatch.setattr(
        eval_compare,
        "_get",
        lambda _path, _token: {"results": [{"content": "context"}]},
    )
    monkeypatch.setattr(
        eval_compare,
        "_select_ragas_answer",
        lambda *args, **kwargs: ("answer", "generated"),
    )

    def fake_score(question, answer, contexts, expected=None, **kwargs):
        captured["expected"] = expected
        return SimpleNamespace(to_dict=lambda: {"faithfulness": 1.0, "answer_relevance": 1.0})

    monkeypatch.setitem(
        sys.modules,
        "ragas_judge",
        SimpleNamespace(
            score_one=fake_score,
            aggregate=lambda scores: {
                "n": len(scores),
                "faithfulness_mean": 1.0,
                "answer_relevance_mean": 1.0,
            },
        ),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "eval_compare.py",
            "--json",
            "--ragas",
            "--ragas-answer-source",
            "generated",
            "--eval-set",
            str(eval_set),
        ],
    )

    assert eval_compare.main() == 0
    out = json.loads(capsys.readouterr().out)
    assert captured["expected"] == "strict answer rubric"
    assert out["ragas"]["cases"][0]["answer_rubric"] == "strict answer rubric"


def test_diversity_aggregate_correlates_failures_without_gating() -> None:
    per_test = [
        {
            "hit_source": True,
            "hit_content_loose": True,
            "diversity": {
                "status": "ok",
                "mean_pairwise_cosine": 0.2,
                "max_pairwise_cosine": 0.3,
                "high_similarity_pair_count": 0,
            },
        },
        {
            "hit_source": False,
            "hit_content_loose": False,
            "diversity": {
                "status": "ok",
                "mean_pairwise_cosine": 0.9,
                "max_pairwise_cosine": 0.95,
                "high_similarity_pair_count": 2,
            },
        },
    ]

    out = eval_compare._aggregate_diversity(per_test)

    assert out["coverage_level"] == "final_topk_e5_cosine_v1"
    assert out["case_count"] == 2
    assert out["passed_mean_pairwise_cosine"] == 0.2
    assert out["content_failed_mean_pairwise_cosine"] == 0.9
    assert out["source_failed_mean_pairwise_cosine"] == 0.9
    assert out["high_similarity_pair_count"] == 2
    assert "diagnostic_only" in out["interpretation"]


def test_run_eval_can_include_diagnostic_diversity_metrics(monkeypatch) -> None:
    monkeypatch.setattr(
        eval_compare,
        "_get",
        lambda _path, _token: {
            "results": [
                {
                    "collection": "canonical",
                    "source_type": "canonical",
                    "path": "canonical/chris/_state.md",
                    "content": "Current active project context.",
                }
            ]
        },
    )
    monkeypatch.setattr(
        eval_compare,
        "_topk_diversity_metrics",
        lambda _results: {
            "status": "ok",
            "result_count": 1,
            "mean_pairwise_cosine": 0.42,
            "max_pairwise_cosine": 0.42,
            "high_similarity_pair_count": 0,
        },
    )

    report = eval_compare.run_eval(
        use_v2=True,
        hyde=False,
        expand=False,
        iterative=False,
        token="test-token",
        cases=[
            {
                "query": "active project?",
                "expected_source": "canonical/chris/_state.md",
                "expected_content": "active project",
            }
        ],
        diversity_metrics=True,
    )

    assert report["diversity"]["coverage_level"] == "final_topk_e5_cosine_v1"
    assert report["diversity"]["mean_pairwise_cosine"] == 0.42
    assert report["per_test"][0]["diversity"]["status"] == "ok"
