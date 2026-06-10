"""Regression coverage for the OpenClaw -> Hermes current-runtime migration."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

BRAIN_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BRAIN_ROOT / "brain_core"))
sys.path.insert(0, str(BRAIN_ROOT / "cli"))
HERMES_HOME = Path("/Users/chrischo/.hermes")
HERMES_AGENT_ROOT = HERMES_HOME / "hermes-agent"


def test_source_governance_tracks_hermes_sessions_as_current_source():
    import source_governance

    sources = {source.id: source for source in source_governance.GOVERNED_SOURCES}

    assert "hermes_sessions" in sources
    source = sources["hermes_sessions"]
    assert "Hermes" in source.label
    assert all("openclaw" not in job for job in source.jobs)
    assert all("openclaw" not in path for path in source.state_files + source.log_files)


def test_skill_sync_targets_hermes_profiles_not_openclaw_registry():
    import skill_sync

    assert Path("/Users/chrischo/.hermes/profiles") == skill_sync.HERMES_PROFILES_ROOT
    assert skill_sync.HERMES_PROFILE_SKILLS_DIRS["liz"] == skill_sync.SKILLS_DIR
    assert not hasattr(skill_sync, "OPENCLAW_ROOT")
    assert "liz" in skill_sync.HERMES_PROFILE_SKILLS_DIRS


def test_skill_materializer_syncs_hermes_registry_not_openclaw():
    import skill_materializer

    assert Path("/Users/chrischo/.hermes/profiles/liz/skills") == skill_materializer.HERMES_SKILLS_DIR
    assert hasattr(skill_materializer, "_sync_hermes_skill_indexes")


def test_profile_dispatch_uses_hermes_cli_not_openclaw_agent():
    import openclaw_dispatch

    import config

    source = Path(openclaw_dispatch.__file__).read_text(encoding="utf-8")

    assert config.HERMES_BIN == "/Users/chrischo/.local/bin/hermes"
    assert openclaw_dispatch.HERMES_BIN == config.HERMES_BIN
    assert "OPENCLAW_BIN" not in source
    assert '"--profile"' in source
    assert '"chat"' in source
    assert '"--source"' in source
    assert "brain-dispatch" in source
    assert '"agent",' not in source
    assert '"--agent"' not in source


def test_hermes_configs_use_brain_memory_and_disable_discord():
    yaml = pytest.importorskip("yaml")
    configs = [HERMES_HOME / "config.yaml", *sorted((HERMES_HOME / "profiles").glob("*/config.yaml"))]

    assert configs
    for cfg_path in configs:
        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        assert data["memory"]["provider"] == "brain", cfg_path
        assert data["discord"]["enabled"] is False, cfg_path
        assert data["platforms"]["discord"]["enabled"] is False, cfg_path


def test_brain_memory_provider_uses_current_brain_api_contract():
    source = (BRAIN_ROOT / "hermes_integration/brain_memory_provider/__init__.py").read_text(encoding="utf-8")

    assert '"x-agent"' in source
    # New provider parameterises `n` (constraint queries use PREFETCH_K * 2);
    # the contract is that PREFETCH_K is the prefetch budget at the call site
    # and the recall payload still carries an `n` key.
    assert "n=PREFETCH_K" in source
    assert '"n": n' in source
    assert 'collection="semantic_memory"' in source
    assert '"agent": self._profile' in source
    assert '"source": "hermes"' in source
    assert '"kind"' not in source
    assert '"tags"' not in source


def test_recall_v2_supports_profile_agent_filter():
    source = (BRAIN_ROOT / "brain_core/routes/recall.py").read_text(encoding="utf-8")

    assert "agent: str | None = Query" in source
    assert 'where = {"agent": agent} if agent else None' in source
    assert "filter_agent={agent}" in source


def test_profile_primary_docs_point_to_hermes_soul_files():
    import search_unified

    for profile, doc in search_unified._AGENT_PRIMARY_DOCS.items():
        assert doc == f"/Users/chrischo/.hermes/profiles/{profile}/SOUL.md"
        assert Path(doc).exists()


def test_direct_telegram_health_reads_hermes_profile_env():
    import telegram_alert

    source = Path(telegram_alert.__file__).read_text(encoding="utf-8")

    assert "TELEGRAM_BOT_TOKEN" in source
    assert ".hermes/profiles/jenna/.env" in source
    assert ".openclaw/.env" not in source


def test_gateway_does_not_start_tokenless_discord_adapter():
    source = (HERMES_AGENT_ROOT / "gateway/config.py").read_text(encoding="utf-8")

    assert "discord disabled: no DISCORD_BOT_TOKEN/API key configured" in source
    assert "discord_cfg.enabled = False" in source


def test_scheduler_uses_hermes_telegram_audit_not_openclaw_audit():
    registry = (BRAIN_ROOT / "brain_core/job_registry.py").read_text(encoding="utf-8")
    ci_runner = (BRAIN_ROOT / "cli/ci_runner.py").read_text(encoding="utf-8")

    assert "hermes_telegram_target_audit" in registry
    assert "audit_hermes_telegram_targets.py" in registry
    assert "openclaw_telegram_target_audit" not in registry
    assert "audit_openclaw_telegram_targets.py" not in registry
    assert "hermes_telegram_target_audit" in ci_runner


def test_claude_home_instructions_mark_openclaw_archived():
    text = Path("/Users/chrischo/CLAUDE.md").read_text(encoding="utf-8")

    assert "Hermes profiles" in text
    assert "Configs in `~/.hermes/profiles/<name>/`" in text
    assert "OpenClaw is archived legacy" in text
    assert "Configs in `~/.openclaw/`" not in text
