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
CHROMA_URL = os.getenv("CHROMA_URL", "http://127.0.0.1:8000")
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434")
NEO4J_BOLT_URI = os.getenv("NEO4J_BOLT_URI", "bolt://127.0.0.1:7687")

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
CHROMA_DATA = RAG_DIR / "chroma-data"
CHROMA_DB = CHROMA_DATA / "chroma.sqlite3"
EMBED_CACHE_DB = BRAIN_LOGS_DIR / "embedding_cache.db"
AUTONOMY_DB = BRAIN_LOGS_DIR / "autonomy.db"

# ── Derived paths: openclaw ───────────────────────────────
OPENCLAW_BIN = str(HOME / ".local" / "bin" / "openclaw")
OPENCLAW_CREDENTIALS = OPENCLAW_DIR / "credentials"
SECRET_FILE = OPENCLAW_CREDENTIALS / ".personal_webhook_secret"
ONTOLOGY_GRAPH = OPENCLAW_DIR / "memory" / "ontology" / "graph.jsonl"
OBSIDIAN_VAULT = OPENCLAW_DIR / "workspace" / "obsidian-vault"
OBSIDIAN_VAULT_ICLOUD = HOME / "Library" / "Mobile Documents" / "iCloud~md~obsidian" / "Documents" / "Obsidian-vault"
OBSIDIAN_VAULT_LOCAL = OPENCLAW_DIR / "workspace" / "obsidian-vault"

# ── Embedding model ───────────────────────────────────────
EMBED_MODEL = os.getenv("BRAIN_EMBED_MODEL", "blaifa/multilingual-e5-large-instruct")
EMBED_MODEL_VERSION = os.getenv("BRAIN_EMBED_MODEL_VERSION", "multilingual-e5-large-instruct:v1")

# ── Executables ───────────────────────────────────────────
PYTHON = os.getenv("BRAIN_PYTHON", "/opt/homebrew/bin/python3")

# ── Feature flags ─────────────────────────────────────────
BRAIN_CROSS_ENCODER_ENABLED = os.getenv("BRAIN_CROSS_ENCODER_ENABLED", "false").lower() in ("true", "1", "yes")
BRAIN_TRUST_RANKING_ENABLED = os.getenv("BRAIN_TRUST_RANKING_ENABLED", "false").lower() in ("true", "1", "yes")
BRAIN_DISPATCH_CACHE_ENABLED = os.getenv("BRAIN_DISPATCH_CACHE_ENABLED", "false").lower() in ("true", "1", "yes")
BRAIN_AUTO_HEAL_ENABLED = os.getenv("BRAIN_AUTO_HEAL_ENABLED", "false").lower() in ("true", "1", "yes")
BRAIN_FINETUNE_ENABLED = os.getenv("BRAIN_FINETUNE_ENABLED", "false").lower() in ("true", "1", "yes")
# Round 10 — neuromorphic retrieval
BRAIN_SPREADING_ACTIVATION_ENABLED = os.getenv("BRAIN_SPREADING_ACTIVATION_ENABLED", "false").lower() in ("true", "1", "yes")
BRAIN_SALIENCE_RANKING_ENABLED = os.getenv("BRAIN_SALIENCE_RANKING_ENABLED", "false").lower() in ("true", "1", "yes")
BRAIN_MMR_DIVERSITY_ENABLED = os.getenv("BRAIN_MMR_DIVERSITY_ENABLED", "false").lower() in ("true", "1", "yes")
BRAIN_EPISODIC_BINDING_ENABLED = os.getenv("BRAIN_EPISODIC_BINDING_ENABLED", "false").lower() in ("true", "1", "yes")
BRAIN_MMR_LAMBDA = float(os.getenv("BRAIN_MMR_LAMBDA", "0.85"))  # 0.85 = strongly relevance-biased; 0.6 was too aggressive on single-shot QA
