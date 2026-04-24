from __future__ import annotations

import json
import sys
from pathlib import Path

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
            "path": "canonical/decisions/chris-s-claude-openclaw-execution-verification-and-acp-operating-rules.md",
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
