"""Unit tests for atoms_to_skills skill-evolution pipeline.

Covers the quality filters, classifier edge cases, duplicate-title stripping,
and the OpenClaw sync/prune safety logic. No DB or filesystem writes outside
tmp_path.
"""

from __future__ import annotations

from unittest.mock import patch

from atoms_to_skills import (
    SKILL_PREFIX,
    _extract_rule,
    _is_durable_rule,
    _runtime_for_skill_path,
    _strip_duplicated_prefix,
    classify,
    prune_orphan_skills,
    write_skills,
)


class TestDurableRuleFilter:
    def test_rejects_screen_time_narrative(self):
        rule = "Chris screen time patterns across March 14 to March 23, 2026 This consolidated page captures"
        assert not _is_durable_rule(rule)

    def test_rejects_consolidated_page_summary(self):
        rule = "This consolidated page captures Chris's durable operating rules for Claude"
        assert not _is_durable_rule(rule)

    def test_rejects_signal_preamble_leak(self):
        rule = "Signal: preference (score 10/10) Chris wants something but preamble leaked"
        assert not _is_durable_rule(rule)

    def test_rejects_too_short(self):
        assert not _is_durable_rule("Chris wants X")

    def test_accepts_chris_wants(self):
        assert _is_durable_rule("Chris wants no additional LLM spending beyond subscription")

    def test_accepts_must_directive(self):
        assert _is_durable_rule("Deployment rule: every new service MUST be deployed as a Docker container")

    def test_accepts_operational_rule_with_imperative(self):
        # Operational incident-style rules should survive if they contain do-this-do-not-that shape
        rule = "Cloudflare DNS for brain.chrischodev.com: if record shows but does not resolve, delete and recreate"
        assert _is_durable_rule(rule)


class TestDuplicatedPrefixStripper:
    def test_strips_doubled_chris_title(self):
        bad = (
            "Chris wants to use Claude through OpenClaw via his existing Claude subsc "
            "Chris wants to use Claude through OpenClaw via his existing Claude subscription "
            "and explicitly wants to avoid extra paid API usage"
        )
        fixed = _strip_duplicated_prefix(bad)
        assert fixed.startswith(
            "Chris wants to use Claude through OpenClaw via his existing Claude subscription"
        )
        assert "subsc Chris" not in fixed

    def test_no_op_on_clean_rule(self):
        clean = "Chris wants all scheduled jobs heavy on Ollama to run outside 9am-6pm PST"
        assert _strip_duplicated_prefix(clean) == clean

    def test_no_op_on_short_rule(self):
        assert _strip_duplicated_prefix("Chris wants X Y Z") == "Chris wants X Y Z"


class TestRuleExtraction:
    def test_strips_canonical_explanation_sections(self):
        text = (
            "Use canonical memory layer over raw RAG alone "
            "## Why This reduces stale recall and supports provenance. "
            "## Source raw session"
        )
        assert _extract_rule(text) == "Use canonical memory layer over raw RAG alone"


class TestClassifier:
    def test_mcc_atom_lands_in_general_not_coding_style(self):
        """MCC archive atom has 'reactivates' which used to substring-match 'react'."""
        text = "Chris no longer considers MCC active. Do not treat MCC as a current priority unless he reactivates it."
        assert classify(text, "preference:chris_no_longer_considers") == "general"

    def test_screen_time_atom_lands_in_general_not_communication(self):
        """## Summary preamble used to pull screen-time narrative into communication."""
        text = "Chris screen time patterns across March 14 ## Summary This consolidated page captures the distinct work modes"
        assert classify(text, "") == "general"

    def test_orbstack_lands_in_infra_ops(self):
        text = "OrbStack Docker socket deadlocks when 3+ docker CLI commands run concurrently"
        assert classify(text, "decision:orbstack_deadlock") == "infra-ops"

    def test_openclaw_lands_in_agent_orchestration(self):
        text = "OpenClaw Jenna agent should handoff to Liz when the task needs coding"
        assert classify(text, "preference:openclaw_handoff") == "agent-orchestration"

    def test_subscription_lands_in_llm_budget(self):
        text = "Chris wants to use Claude via subscription without paid API billing"
        assert classify(text, "preference:claude_subscription") == "llm-budget"

    def test_qdrant_lands_in_brain_system(self):
        text = "The brain retrieval stack uses Qdrant as its vector store"
        assert classify(text, "decision:qdrant_vector_store") == "brain-system"

    def test_unclassifiable_falls_to_general(self):
        text = "Chris prefers green tea in the morning"
        assert classify(text, "preference:tea") == "general"


class TestWriteSkills:
    def test_writes_to_claude_codex_and_openclaw(self, tmp_path):
        roots = [
            tmp_path / ".claude" / "skills",
            tmp_path / ".codex" / "skills",
            tmp_path / ".openclaw" / "skills",
        ]
        atoms = [
            {
                "text": "Chris wants no additional LLM spending beyond subscription",
                "kind": "preference",
                "confidence": 0.91,
            }
        ]

        with patch("atoms_to_skills.SKILL_DESTINATIONS", roots):
            stats = write_skills({"llm-budget": atoms})

        runtimes = {item["runtime"] for item in stats["written"]}
        assert runtimes == {"claude", "codex", "openclaw"}
        for root in roots:
            assert (root / "brain-learned-llm-budget" / "SKILL.md").exists()

    def test_runtime_for_skill_path_identifies_codex(self, tmp_path):
        assert _runtime_for_skill_path(tmp_path / ".codex" / "skills" / "x" / "SKILL.md") == "codex"


class TestPruneOrphanSkills:
    def test_safety_guard_refuses_when_too_few_domains(self, tmp_path):
        # Create orphan dir, but available_domains has only 2 → should refuse
        dest = tmp_path / "skills"
        dest.mkdir()
        orphan = dest / f"{SKILL_PREFIX}old-domain"
        orphan.mkdir()
        (orphan / "SKILL.md").write_text("stale")

        with patch("atoms_to_skills.SKILL_DESTINATIONS", [dest]):
            stats = prune_orphan_skills({"a", "b"})

        assert "refusing to prune" in stats["skipped_reason"]
        assert orphan.exists()

    def test_prunes_orphan_dirs(self, tmp_path):
        dest = tmp_path / "skills"
        dest.mkdir()
        # kept
        kept = dest / f"{SKILL_PREFIX}infra-ops"
        kept.mkdir()
        (kept / "SKILL.md").write_text("current")
        # orphan
        orphan = dest / f"{SKILL_PREFIX}old-domain"
        orphan.mkdir()
        (orphan / "SKILL.md").write_text("stale")
        # non-brain skill — must be untouched
        unrelated = dest / "unrelated-skill"
        unrelated.mkdir()
        (unrelated / "SKILL.md").write_text("keep")

        with patch("atoms_to_skills.SKILL_DESTINATIONS", [dest]):
            stats = prune_orphan_skills({"infra-ops", "brain-system", "general"})

        assert kept.exists()
        assert unrelated.exists()
        assert not orphan.exists()
        assert any("old-domain" in p["path"] for p in stats["pruned"])

    def test_dry_run_does_not_delete(self, tmp_path):
        dest = tmp_path / "skills"
        dest.mkdir()
        orphan = dest / f"{SKILL_PREFIX}old"
        orphan.mkdir()
        (orphan / "SKILL.md").write_text("x")

        with patch("atoms_to_skills.SKILL_DESTINATIONS", [dest]):
            prune_orphan_skills({"a", "b", "c"}, dry_run=True)

        assert orphan.exists()
