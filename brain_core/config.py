"""brain_core/config.py — centralized path and URL configuration.

All paths default to the current user's home directory layout.
Override any path via environment variables for portability.
"""

import os
from pathlib import Path

# ── Base directories ──────────────────────────────────────
HOME = Path(os.getenv("BRAIN_HOME", str(Path.home())))
BRAIN_DIR = Path(os.getenv("BRAIN_DIR", str(HOME / "server" / "brain")))
KNOWLEDGE_DIR = Path(os.getenv("KNOWLEDGE_DIR", str(HOME / "server" / "knowledge")))
RAG_DIR = Path(os.getenv("RAG_DIR", str(HOME / "server" / "rag")))
OPENCLAW_DIR = Path(os.getenv("OPENCLAW_DIR", str(HOME / ".openclaw")))

# ── Service URLs ──────────────────────────────────────────
QDRANT_URL = os.getenv("QDRANT_URL", "http://127.0.0.1:6333")
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434")
NEO4J_BOLT_URI = os.getenv("NEO4J_BOLT_URI", "bolt://127.0.0.1:7687")
BRAIN_RERANKER_URL = os.getenv("BRAIN_RERANKER_URL", "http://127.0.0.1:8792")
BRAIN_RERANKER_TIMEOUT_MS = int(os.getenv("BRAIN_RERANKER_TIMEOUT_MS", "1000"))

# ── Derived paths: brain ──────────────────────────────────
BRAIN_CORE_DIR = BRAIN_DIR / "brain_core"
BRAIN_LOGS_DIR = BRAIN_DIR / "logs"
BRAIN_VENV = BRAIN_DIR / ".venv"
FAILURE_LOG = BRAIN_LOGS_DIR / "failures.jsonl"
JOBS_LOGS_DIR = BRAIN_LOGS_DIR / "jobs"

# ── Derived paths: knowledge ──────────────────────────────
INBOX_DIR = KNOWLEDGE_DIR / "raw" / "inbox"
CANONICAL_DIR = KNOWLEDGE_DIR / "canonical"
DISTILLED_DIR = KNOWLEDGE_DIR / "distilled"
SCHEMA_DIR = KNOWLEDGE_DIR / "schemas"
REPORTS_DIR = KNOWLEDGE_DIR / "reports"
PROFILE_FILE = CANONICAL_DIR / "chris" / "_profile.md"  # legacy; split into _identity.md + _state.md
IDENTITY_FILE = CANONICAL_DIR / "chris" / "_identity.md"
STATE_FILE = CANONICAL_DIR / "chris" / "_state.md"
DISTILLED_DAILY = DISTILLED_DIR / "daily"
WEEKLY_DIR = CANONICAL_DIR / "chris" / "weekly"
MONTHLY_DIR = CANONICAL_DIR / "chris" / "monthly"

# ── Derived paths: rag ────────────────────────────────────
BRAIN_HOME = HOME / "server"  # ~/server — root of all server services
QDRANT_DATA = BRAIN_DIR / "qdrant-data"
EMBED_CACHE_DB = BRAIN_LOGS_DIR / "embedding_cache.db"
AUTONOMY_DB = BRAIN_LOGS_DIR / "autonomy.db"
# audit_log.py owns audit.db; BRAIN_AUDIT_DB env overrides for test isolation
# (pytest fixtures redirect to tmp_path so dev scripts don't pollute prod).
AUDIT_DB = BRAIN_LOGS_DIR / "audit.db"
# fact_store.py owns facts.db — structured (entity, attribute, value) triples
# with temporal validity, separate from the atoms graph.
FACTS_DB = BRAIN_LOGS_DIR / "facts.db"

# ── Derived paths: openclaw ───────────────────────────────
OPENCLAW_BIN = str(HOME / ".local" / "bin" / "openclaw")
OPENCLAW_CREDENTIALS = OPENCLAW_DIR / "credentials"
SECRET_FILE = OPENCLAW_CREDENTIALS / ".personal_webhook_secret"
OPENCLAW_ONTOLOGY_GRAPH = OPENCLAW_DIR / "memory" / "ontology" / "graph.jsonl"
ONTOLOGY_GRAPH = Path(os.getenv("BRAIN_ONTOLOGY_GRAPH", str(BRAIN_DIR / "data" / "ontology" / "graph.jsonl")))
OBSIDIAN_VAULT = OPENCLAW_DIR / "workspace" / "obsidian-vault"
OBSIDIAN_VAULT_ICLOUD = (
    HOME / "Library" / "Mobile Documents" / "iCloud~md~obsidian" / "Documents" / "Obsidian-vault"
)
OBSIDIAN_VAULT_LOCAL = OPENCLAW_DIR / "workspace" / "obsidian-vault"

# ── Embedding model ───────────────────────────────────────
EMBED_MODEL = os.getenv("BRAIN_EMBED_MODEL", "blaifa/multilingual-e5-large-instruct")
EMBED_MODEL_VERSION = os.getenv("BRAIN_EMBED_MODEL_VERSION", "multilingual-e5-large-instruct:v2")
# v2 (2026-04-17): bumped to force reindex after chunker fixes (Source Summary
# skip + frontmatter strip). Incremental indexer compares embed_model_version
# + mtime to skip unchanged files — old chunks with dirty frontmatter survive
# across reindexes when file mtime hasn't changed. Bumping the version
# invalidates the mtime-equality fast-path and re-embeds all content through
# the current cleaner chunker.

# ── Executables ───────────────────────────────────────────
PYTHON = os.getenv("BRAIN_PYTHON", "/Users/chrischo/server/brain/.venv/bin/python")

# ── Feature flags ─────────────────────────────────────────
BRAIN_CROSS_ENCODER_ENABLED = os.getenv("BRAIN_CROSS_ENCODER_ENABLED", "false").lower() in (
    "true",
    "1",
    "yes",
)
BRAIN_TRUST_RANKING_ENABLED = os.getenv("BRAIN_TRUST_RANKING_ENABLED", "false").lower() in (
    "true",
    "1",
    "yes",
)
BRAIN_DISPATCH_CACHE_ENABLED = os.getenv("BRAIN_DISPATCH_CACHE_ENABLED", "false").lower() in (
    "true",
    "1",
    "yes",
)
BRAIN_AUTO_HEAL_ENABLED = os.getenv("BRAIN_AUTO_HEAL_ENABLED", "false").lower() in ("true", "1", "yes")
BRAIN_FINETUNE_ENABLED = os.getenv("BRAIN_FINETUNE_ENABLED", "false").lower() in ("true", "1", "yes")
# Round 10 — neuromorphic retrieval (enabled 2026-04-11 after manual verification)
BRAIN_SPREADING_ACTIVATION_ENABLED = os.getenv("BRAIN_SPREADING_ACTIVATION_ENABLED", "true").lower() in (
    "true",
    "1",
    "yes",
)
BRAIN_SALIENCE_RANKING_ENABLED = os.getenv("BRAIN_SALIENCE_RANKING_ENABLED", "true").lower() in (
    "true",
    "1",
    "yes",
)
BRAIN_MMR_DIVERSITY_ENABLED = os.getenv("BRAIN_MMR_DIVERSITY_ENABLED", "true").lower() in ("true", "1", "yes")
BRAIN_EPISODIC_BINDING_ENABLED = os.getenv("BRAIN_EPISODIC_BINDING_ENABLED", "true").lower() in (
    "true",
    "1",
    "yes",
)
# 2026-04-16 R-10: keep MMR at 0.85 — Tier-2 drop to 0.6 regressed eval
# by ~1pt and content_hit is the hill to defend. The confidence-skip
# gate still protects against diversifying well-separated results; the
# remaining wins come from canonical trust bonus + idempotency fixes.
BRAIN_MMR_LAMBDA = float(os.getenv("BRAIN_MMR_LAMBDA", "0.85"))

# 2026-04-17 Phase 3: learned-to-rank blend. Default OFF; enable via
# BRAIN_LTR_ENABLED=true once cli/ltr_train.py has run and saved weights.
BRAIN_LTR_ENABLED = os.getenv("BRAIN_LTR_ENABLED", "false").lower() in ("true", "1", "yes")

# Ontology recall expansion: default OFF until recall eval + p95 gates pass.
# When enabled, search_unified adds cached 1-hop ontology entity names to the
# search query only; rerank/relevance still use the user's original query.
BRAIN_ONTOLOGY_EXPANSION_ENABLED = os.getenv("BRAIN_ONTOLOGY_EXPANSION_ENABLED", "false").lower() in (
    "true",
    "1",
    "yes",
)
BRAIN_ONTOLOGY_EXPANSION_MAX_TERMS = int(os.getenv("BRAIN_ONTOLOGY_EXPANSION_MAX_TERMS", "5"))
BRAIN_ONTOLOGY_EXPANSION_MODE = os.getenv("BRAIN_ONTOLOGY_EXPANSION_MODE", "rewrite").strip().lower()
BRAIN_ONTOLOGY_SIDECAR_LIMIT = int(os.getenv("BRAIN_ONTOLOGY_SIDECAR_LIMIT", "5"))
# Keep this allowlist narrow. Stable eval on 2026-04-24 rejected broad
# graph relations (`depends_on`, `manages`, `proxies`) despite acceptable
# latency because source/content hit regressed. Add relations only after
# cli/eval_ontology_expansion.py passes content/source + p95 gates.
BRAIN_ONTOLOGY_EXPANSION_SOURCE = os.getenv("BRAIN_ONTOLOGY_EXPANSION_SOURCE", "neo4j").strip().lower()
BRAIN_ONTOLOGY_EXPANSION_RELATIONS = tuple(
    rel.strip()
    for rel in os.getenv("BRAIN_ONTOLOGY_EXPANSION_RELATIONS", "has_agent,owned_by,owns").split(",")
    if rel.strip()
)
BRAIN_ONTOLOGY_CONDITIONAL_EXPANSION_ENABLED = os.getenv(
    "BRAIN_ONTOLOGY_CONDITIONAL_EXPANSION_ENABLED", "false"
).lower() in ("true", "1", "yes")

# Phase 3 — atoms truth layer (Brain v1 plan)
# BRAIN_ATOMS_ENABLED: master flag for atoms write path. Default off until verified.
# BRAIN_ATOMS_READ: read path uses atoms for tier/supersession filtering. Stays off through Phase 5.
# BRAIN_ENABLE_ATOMS_MIGRATION: whether check_and_migrate runs the atoms backfill on startup.
BRAIN_ATOMS_ENABLED = os.getenv("BRAIN_ATOMS_ENABLED", "false").lower() in ("true", "1", "yes")
BRAIN_ATOMS_READ = os.getenv("BRAIN_ATOMS_READ", "false").lower() in ("true", "1", "yes")
BRAIN_ENABLE_ATOMS_MIGRATION = os.getenv("BRAIN_ENABLE_ATOMS_MIGRATION", "false").lower() in (
    "true",
    "1",
    "yes",
)
BRAIN_DB = BRAIN_LOGS_DIR / "brain.db"


def load_bearer_secret() -> str:
    """Load the personal webhook bearer secret from disk.

    Centralizes the path + read + strip pattern that was duplicated across
    8+ call sites (self_heal, slo_monitor, ingest/healthcheck, pipeline/
    memory_nudge, etc.). Raises FileNotFoundError if the secret is missing
    so callers fail loud instead of silently auth-bypassing.
    """
    return SECRET_FILE.read_text().strip()
