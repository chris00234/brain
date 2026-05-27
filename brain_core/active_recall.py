"""brain_core/active_recall.py — per-turn attention gating.

Biology: thalamus. Intercepts every user prompt (via UserPromptSubmit hook for
Claude Code and Hermes profile hooks) and decides what context
to inject based on the prompt's intent. This is the module that turns brain
from a passive retrieval store into a per-turn proactive surface.

Pipeline (fast path, <1200 ms hard budget):

  0. Judgment layer           — prompt-shape classifier decides whether memory
                                is useful at all, and sets per-intent budget.
  1. L0 canonical guarantees  — YAML-driven keyword match returns file paths
     that MUST surface regardless of vector scores. Prevents "design standard
     loses to noisy vector hits."
  2. L1 semantic fan-out      — search_unified.search_all over the actual prompt.
                                Hits the 60s embedding similarity cache for hot
                                prompts. No LLM.
  3. L2 proactive sweep       — proactive.get_current_insights() for urgent
                                items < 6 h old. Per-turn surface of the 6-hour
                                insight generator.
  4. L3 doorbell read         — /tmp/.brain_doorbell.<session_id>.jsonl —
                                brain_loop has explicitly queued urgent context
                                for this session. Consumed here (cleared by the
                                hook script).
  5. Dedup                    — against session_context[session_id, agent,
                                'recall_seen'] with decay tiers (critical ∞,
                                preference 20 turns, proactive 5 turns).
  6. Confidence sentinel      — diagnostic-only fallback, disabled by default
                                so hooks do not inject low-value noise.
  7. Budget                   — 2 KB max, priority: critical > high > medium.
  8. Observability            — insert_action_audit(route='/recall/active', ...)

Fail-open: every step wraps in try/except. Returned blocks are best-effort.
The hook script catches failures and prints a degraded sentinel.

Called from: server.py::recall_active endpoint and Hermes profile Brain MCP/hooks
via the same endpoint.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

try:
    import yaml  # PyYAML is in the brain venv
except ImportError:
    yaml = None

try:
    from config import AUTONOMY_DB, BRAIN_CORE_DIR, BRAIN_LOGS_DIR, HOME
except ImportError:
    HOME = Path.home()
    BRAIN_CORE_DIR = Path(__file__).parent
    BRAIN_LOGS_DIR = BRAIN_CORE_DIR.parent / "logs"
    AUTONOMY_DB = BRAIN_LOGS_DIR / "autonomy.db"

# Top-level module imports (PEP 8). search_unified stays lazy because it pulls
# in Chroma/Ollama/Neo4j clients and we want a fast module load for testing;
# everything else is safe to import eagerly.
try:
    from proactive import get_current_insights as _get_current_insights
except ImportError:
    _get_current_insights = None  # type: ignore[assignment]

try:
    from atoms_store import insert_action_audit as _insert_action_audit
except ImportError:
    _insert_action_audit = None  # type: ignore[assignment]

try:
    from judgment_layer import arbitrate_blocks as _arbitrate_blocks
    from judgment_layer import classify_prompt as _classify_prompt
except ImportError:
    _arbitrate_blocks = None  # type: ignore[assignment]
    _classify_prompt = None  # type: ignore[assignment]

try:
    from judgment_feedback import record as _record_judgment_feedback
except ImportError:
    _record_judgment_feedback = None  # type: ignore[assignment]

log = logging.getLogger("brain.active_recall")

INTENT_ROUTES_PATH = BRAIN_CORE_DIR / "intent_routes.yaml"
DOORBELL_DIR = Path("/tmp")  # noqa: S108 — brain_loop writes per-session-id doorbell files here; the hook script reads+clears them in-process.
DOORBELL_TEMPLATE = "{session_id}"
BUDGET_TOKEN_LIMIT = 2048
LOW_CONFIDENCE_THRESHOLD = 0.5
HARD_TIMEOUT_MS = 1200
SEMANTIC_MIN_SCORE = float(os.getenv("BRAIN_ACTIVE_RECALL_SEMANTIC_MIN_SCORE", "0.82"))
SEMANTIC_MIN_SCORE_WITH_INTENT = float(
    os.getenv("BRAIN_ACTIVE_RECALL_SEMANTIC_MIN_SCORE_WITH_INTENT", "0.72")
)
DISABLE_GENERIC_SUMMARY_BLOCKS = os.getenv("BRAIN_ACTIVE_RECALL_GENERIC_SUMMARIES", "0").lower() not in {
    "1",
    "true",
    "yes",
    "on",
}


# ── Types ─────────────────────────────────────────────────────────

PRIORITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}


@dataclass
class InjectionBlock:
    id: str
    title: str
    content: str
    source: str  # canonical | semantic | proactive | doorbell | confidence_sentinel
    score: float
    priority: str  # critical | high | medium | low
    path: str | None = None
    # 2026-04-18: actual ChromaDB document id (not the local dedup hash in `id`).
    # Needed by reinforce_on_access for the MemoryBank bump on semantic blocks.
    memory_id: str | None = None
    include_reason: str | None = None
    token_estimate: int | None = None
    freshness: str | None = None
    risk_flags: list[str] = field(default_factory=list)
    compiler_score: float | None = None
    contract_category: str | None = None

    def to_dict(self) -> dict:
        out = {
            "id": self.id,
            "title": self.title,
            "content": self.content,
            "source": self.source,
            "score": self.score,
            "priority": self.priority,
            "path": self.path,
            "memory_id": self.memory_id,
        }
        if self.include_reason:
            out["include_reason"] = self.include_reason
        if self.token_estimate is not None:
            out["token_estimate"] = self.token_estimate
        if self.freshness:
            out["freshness"] = self.freshness
        if self.risk_flags:
            out["risk_flags"] = list(self.risk_flags)
        if self.compiler_score is not None:
            out["compiler_score"] = self.compiler_score
        if self.contract_category:
            out["contract_category"] = self.contract_category
        return out


@dataclass
class IntentMatch:
    intent: str
    canonical_paths: list[str] = field(default_factory=list)
    always_push_queries: list[str] = field(default_factory=list)
    priority: str = "medium"
    max_tokens: int = 600


# ── YAML loader (module-level cache) ──────────────────────────────

_routes_cache: dict | None = None
_routes_cache_mtime: float = 0.0


def _load_routes(force_reload: bool = False) -> dict:
    """Load intent_routes.yaml. Cached by mtime so edits on disk pick up
    on next call without a service restart."""
    global _routes_cache, _routes_cache_mtime

    if yaml is None:
        log.warning("PyYAML not available — intent routing disabled")
        return {"intents": {}}

    try:
        mtime = INTENT_ROUTES_PATH.stat().st_mtime
    except FileNotFoundError:
        log.warning("intent_routes.yaml not found at %s", INTENT_ROUTES_PATH)
        return {"intents": {}}

    if not force_reload and _routes_cache is not None and mtime <= _routes_cache_mtime:
        return _routes_cache

    try:
        with INTENT_ROUTES_PATH.open() as f:
            _routes_cache = yaml.safe_load(f) or {"intents": {}}
        _routes_cache_mtime = mtime
    except Exception as e:
        log.warning("failed to load intent_routes.yaml: %s", e)
        if _routes_cache is None:
            _routes_cache = {"intents": {}}
    return _routes_cache


# ── Intent matching ───────────────────────────────────────────────


def _match_canonical_routes(prompt: str) -> list[IntentMatch]:
    """Return all intents whose keywords match the prompt. An intent matches
    if ANY of its keywords (EN or KO) is a substring of the lowercased prompt.
    Multiple intents may match — we return them all so every guaranteed path
    surfaces."""
    if not prompt:
        return []

    routes = _load_routes()
    intents_cfg = (routes or {}).get("intents") or {}
    if not intents_cfg:
        return []

    lowered = prompt.lower()
    matches: list[IntentMatch] = []
    for intent_name, cfg in intents_cfg.items():
        if _intent_blocked_by_context(intent_name, lowered):
            continue
        keywords_en = [k.lower() for k in (cfg.get("keywords_en") or [])]
        keywords_ko = cfg.get("keywords_ko") or []  # Korean doesn't need lowercasing
        hit = False
        for kw in keywords_en:
            if kw and kw in lowered:
                hit = True
                break
        if not hit:
            for kw in keywords_ko:
                if kw and kw in prompt:
                    hit = True
                    break
        if hit:
            matches.append(
                IntentMatch(
                    intent=intent_name,
                    canonical_paths=list(cfg.get("canonical_paths") or []),
                    always_push_queries=list(cfg.get("always_push_queries") or []),
                    priority=cfg.get("priority", "medium"),
                    max_tokens=int(cfg.get("max_tokens", 600)),
                )
            )
    return matches


def _intent_blocked_by_context(intent_name: str, lowered_prompt: str) -> bool:
    """Suppress broad intent routes when the prompt is about implementation,
    not the domain object itself.

    The visual route intentionally includes broad words like "image" and
    "이미지" so "what was the image I sent?" works. But prompts about the
    image-processing pipeline ("Gemini API vs subscription CLI") should not
    fan out to remembered screenshots/photos.
    """
    if intent_name == "brain_self" and (
        _looks_like_llm_budget_prompt(lowered_prompt) or not _looks_like_brain_ops_prompt(lowered_prompt)
    ):
        return True

    if intent_name != "visual":
        return False
    technical_markers = (
        "api",
        "backend",
        "gemini",
        "codex",
        "claude",
        "gpt",
        "ingest",
        "caption",
        "pipeline",
        "model",
        "subscription",
        "구독",
        "비용",
        "백엔드",
        "파이프라인",
        "캡션",
    )
    return any(marker in lowered_prompt for marker in technical_markers)


def _looks_like_llm_budget_prompt(lowered_prompt: str) -> bool:
    """Keep broad brain_self routes out of LLM cost/subscription questions."""
    budget_markers = (
        "llm",
        "model cost",
        "extra cost",
        "no extra cost",
        "subscription",
        "api billing",
        "paid api",
        "local model",
        "local llm",
        "gpt subscription",
        "claude subscription",
        "codex subscription",
        "비용",
        "구독",
        "추가 비용",
        "과금",
        "유료 api",
        "로컬 모델",
        "로컬 llm",
        "지피티 구독",
        "클로드 구독",
    )
    return any(marker in lowered_prompt for marker in budget_markers)


def _looks_like_brain_ops_prompt(lowered_prompt: str) -> bool:
    """Allow RUNBOOK/CRON/STORAGE only for operational brain questions.

    The word "brain/브레인/뇌" is too broad for prehook routing. Strategic
    quality/intelligence questions should receive policy/goal/decision context,
    not operational runbooks.
    """
    ops_markers = (
        "scheduler",
        "cron",
        "storage",
        "runbook",
        "healthcheck",
        "health check",
        "backup",
        "qdrant",
        "chroma",
        "chromadb",
        "neo4j",
        "server",
        "launchd",
        "mcp",
        "transport",
        "action_audit",
        "brain_loop",
        "atoms",
        "database",
        "db",
        "monitoring",
        "metrics",
        "스케줄러",
        "크론",
        "저장",
        "스토리지",
        "런북",
        "헬스체크",
        "백업",
        "서버",
        "장애",
        "운영",
        "모니터링",
        "메트릭",
        "아톰",
        "데이터베이스",
    )
    return any(marker in lowered_prompt for marker in ops_markers)


def _load_canonical_path(raw_path: str) -> tuple[str, str] | None:
    """Expand ~ and load a canonical file or directory. For directories, glob
    recursively for .md files up to 3 files. Returns (title, content) or None."""
    path = Path(raw_path.replace("~", str(HOME)))
    if not path.exists():
        return None

    if path.is_file():
        try:
            content = path.read_text(errors="replace")[:8000]
            title = path.stem
            return (title, content)
        except Exception:
            return None

    if path.is_dir():
        md_files = sorted(path.rglob("*.md"))[:3]
        combined_parts = []
        for f in md_files:
            try:
                body = f.read_text(errors="replace")[:2500]
                combined_parts.append(f"#### {f.relative_to(path)}\n{body}")
            except Exception as _exc:
                log.debug("silenced exception in active_recall.py: %s", _exc)
                continue
        if not combined_parts:
            return None
        return (path.name, "\n\n".join(combined_parts)[:8000])

    return None


def _canonical_blocks_from_matches(
    matches: list[IntentMatch],
    seen_hashes: set[str],
) -> list[InjectionBlock]:
    """Read every canonical_path from every matched intent and emit blocks.

    CR1 fix (2026-04-14): critical-priority canonical blocks are NEVER
    filtered by seen_hashes. These are routes explicitly marked as
    "always surface when the intent matches" (frontend_design, credentials,
    live_state, etc.) — if Chris asks about design on turn 50 the design
    standard must still be there, not hidden because it surfaced once
    on turn 3. Non-critical blocks retain the seen-dedup. The decay
    filter downstream enforces per-turn re-inject windows so even
    non-critical content comes back after its cooldown.
    """
    blocks: list[InjectionBlock] = []
    hashes_this_call: set[str] = set()
    for match in matches:
        for raw in match.canonical_paths:
            loaded = _load_canonical_path(raw)
            if not loaded:
                continue
            title, content = loaded
            h = _hash(f"canonical:{raw}:{content[:500]}")
            if h in hashes_this_call:
                continue
            # Critical intent routes always re-surface. Non-critical
            # are deduped via seen_hashes to avoid per-turn spam.
            if match.priority != "critical" and h in seen_hashes:
                continue
            hashes_this_call.add(h)
            blocks.append(
                InjectionBlock(
                    id=h,
                    title=title,
                    content=content[: match.max_tokens * 4],  # rough token→char estimate
                    source="canonical",
                    score=1.0,  # bypass scoring — guaranteed
                    priority=match.priority,
                    path=raw,
                )
            )
    return blocks


# ── Semantic layer via search_all ─────────────────────────────────


_SEMANTIC_POOL_MAX_WORKERS = 4
_SEMANTIC_OVERALL_TIMEOUT_S = 1.2  # hard cap for the whole fanout
_SEMANTIC_FAST_TIMEOUT_S = 0.35  # when canonical policy already satisfies the hook contract
# Shared module-level pool so a slow worker thread does not block the caller
# on a context manager's shutdown(wait=True). Abandoned futures keep running
# on the pool and their results get discarded on completion.
from concurrent.futures import ThreadPoolExecutor as _ActiveRecallPool  # noqa: E402

_semantic_pool = _ActiveRecallPool(max_workers=_SEMANTIC_POOL_MAX_WORKERS, thread_name_prefix="active_recall")


def _semantic_blocks(
    prompt: str,
    matches: list[IntentMatch],
    seen_hashes: set[str],
    limit: int = 5,
    min_score: float | None = None,
    timeout_s: float | None = None,
) -> list[InjectionBlock]:
    """Fan out the prompt + matched intents' always_push_queries through
    search_unified.search_all. Returns up to `limit` dedup'd blocks.

    2026-04-20 perf: parallelized across queries with a module-level
    ThreadPoolExecutor. Serial fanout used to hit 2-3s p99. Parallel fanout
    lets one slow query drop out instead of gating the response, and uses a
    shared pool so the function itself never blocks on pool shutdown.
    """
    try:
        import search_unified
    except ImportError:
        return []

    from concurrent.futures import TimeoutError as _FutTimeout
    from concurrent.futures import as_completed

    queries = [prompt]
    for m in matches:
        queries.extend(m.always_push_queries)
    queries = [q for q in queries[:4] if q and q.strip()]
    if not queries:
        return []

    def _one(q: str) -> list[dict]:
        try:
            resp = search_unified.search_all(
                q,
                limit=limit,
                sources=["rag", "canonical", "obsidian"],
                original_query=prompt,
            )
            if isinstance(resp, dict):
                return list(resp.get("results") or [])
        except Exception as e:
            log.debug("search_all failed for %r: %s", q[:40], e)
        return []

    all_results: list[dict] = []
    futures = {_semantic_pool.submit(_one, q): q for q in queries}
    overall_timeout = timeout_s if timeout_s is not None else _SEMANTIC_OVERALL_TIMEOUT_S
    try:
        for fut in as_completed(futures, timeout=overall_timeout):
            try:
                all_results.extend(fut.result())
            except Exception as e:
                log.debug("active_recall semantic query errored for %r: %s", futures[fut][:40], e)
    except _FutTimeout:
        # Some queries did not finish within the overall budget. Collect
        # whatever HAS completed, abandon the rest — they stay on the pool
        # but their result is ignored. Under normal load the pool drains
        # in 1-2s and workers are available on the next tick.
        done = sum(1 for f in futures if f.done())
        log.info(
            "active_recall fanout exceeded %.1fs budget (%d/%d queries done)",
            overall_timeout,
            done,
            len(futures),
        )
        import contextlib

        for f in futures:
            if f.done():
                with contextlib.suppress(Exception):
                    all_results.extend(f.result())

    # Dedup by path/id and near-duplicate content within this call. Active
    # recall runs every turn, so repeated generic Summary atoms are worse
    # than a missed optional hint: they dilute the prompt and make the model
    # over-anchor on stale/redundant context.
    blocks: list[InjectionBlock] = []
    hashes_this_call: set[str] = set()
    content_signatures: list[set[str]] = []
    generic_summary_seen = False
    for r in all_results:
        if not isinstance(r, dict):
            continue
        rid = r.get("id") or r.get("path") or r.get("title") or ""
        title = (r.get("title") or r.get("path") or "untitled")[:80]
        content = (r.get("content") or "")[:1200]
        score = float(r.get("score") or 0)
        collection = r.get("collection", "")
        if _is_noisy_semantic_result(title, content, r.get("path")):
            continue
        if _looks_like_usage_snapshot(title, content, r.get("path")) and not _prompt_allows_usage_snapshot(
            prompt
        ):
            continue
        is_generic_summary = _is_generic_summary_title(title)
        if is_generic_summary and (DISABLE_GENERIC_SUMMARY_BLOCKS or generic_summary_seen):
            continue
        norm_score = min(1.0, max(0.0, score / 100.0))
        score_floor = min_score
        if score_floor is None:
            score_floor = SEMANTIC_MIN_SCORE_WITH_INTENT if matches else SEMANTIC_MIN_SCORE
        if norm_score < score_floor:
            continue
        if not _semantic_result_matches_prompt(prompt, title, content):
            continue
        h = _hash(f"semantic:{rid}:{title}:{content[:200]}")
        if h in seen_hashes or h in hashes_this_call:
            continue
        signature = _content_signature(content)
        if signature and any(_jaccard(signature, prior) >= 0.72 for prior in content_signatures):
            continue
        hashes_this_call.add(h)
        if signature:
            content_signatures.append(signature)
        if is_generic_summary:
            generic_summary_seen = True
        blocks.append(
            InjectionBlock(
                id=h,
                title=title,
                content=content,
                source=f"semantic:{collection}" if collection else "semantic",
                score=norm_score,
                priority="high" if norm_score >= 0.6 else "medium",
                path=r.get("path"),
                # 2026-04-18: propagate real ChromaDB id for reinforce_on_access.
                memory_id=r.get("id"),
            )
        )
        if len(blocks) >= limit:
            break
    return blocks


def _is_generic_summary_title(title: str) -> bool:
    return bool(re.match(r"(?i)^\s*summary(?:\s*\(part\s*\d+\))?\s*$", title or ""))


def _is_noisy_semantic_result(title: str, content: str, path: str | None) -> bool:
    haystack = f"{title}\n{path or ''}\n{content[:200]}".lower()
    if "raw_shell_" in haystack:
        return True
    # Auto-generated dist_* mirrors: file-change snapshots, commit-message
    # snapshots, raw shell. They verbatim contain code paths / commit bodies
    # and recursively match any brain-internal query, but they're stale and
    # the live file/git state is the truth. Drop from active recall.
    path_low = (path or "").lower()
    if (
        "dist_received_at_" in path_low
        or "/dist_author_chris_cho_body_" in path_low
        or "/dist_raw_shell_" in path_low
    ):
        return True
    title_stripped = (title or "").lstrip()
    if title_stripped.startswith(('{"_received_at"', '{"author"', '{"cwd"', '{"file_path"')):
        return True
    if re.match(r'^\s*\{\s*"(author|_received_at|cwd|file_path)"\s*:', (content or "")[:200]):
        return True
    if title.lstrip().startswith("### Metadata"):
        return True
    return bool(re.match(r"(?is)^\s*(?:#\s*)?metadata\b", content or ""))


def _looks_like_usage_snapshot(title: str, content: str, path: str | None) -> bool:
    """Detect stale operational accounting snapshots.

    These rows are useful when Chris asks for usage/accounting, but they are
    harmful context on strategic prompts because an old dollar/token figure
    reads like current state.
    """
    haystack = f"{title}\n{path or ''}\n{content[:500]}".lower()
    has_usage = any(
        marker in haystack
        for marker in (
            "llm 사용량",
            "사용량",
            "token usage",
            "tokens",
            "prompt tokens",
            "response tokens",
            "billing",
            "spend",
        )
    )
    has_snapshot = any(
        marker in haystack
        for marker in (
            "지난 7일",
            "최근 7일",
            "last 7 days",
            "이번 달",
            "total cost",
            "총 비용",
            "$",
        )
    )
    return has_usage and has_snapshot


def _prompt_allows_usage_snapshot(prompt: str) -> bool:
    p = (prompt or "").lower()
    return any(
        marker in p
        for marker in (
            "사용량",
            "토큰",
            "얼마나 썼",
            "얼마 썼",
            "지출",
            "청구",
            "집계",
            "지난 7일",
            "최근 7일",
            "이번 달",
            "usage",
            "token",
            "tokens",
            "billing",
            "spend",
            "spent",
            "accounting",
        )
    )


_QUERY_STOPWORDS = {
    "그리고",
    "그러면",
    "그럼",
    "계속",
    "다시",
    "여기서",
    "이제",
    "진행",
    "진행해줘",
    "확인",
    "해줘",
    "하는",
    "있는",
    "것도",
    "what",
    "when",
    "where",
    "which",
    "this",
    "that",
    "with",
    "from",
    "into",
    "active",
    "recall",
    "improve",
    "improvement",
    "가능",
    "가능해",
    "가능한지",
    "개선",
    "관련",
    "관련없는",
    "결과",
    "결과값",
}


def _query_terms(prompt: str) -> set[str]:
    terms = _content_signature(prompt)
    return {t for t in terms if t not in _QUERY_STOPWORDS and len(t) >= 3}


def _semantic_result_matches_prompt(prompt: str, title: str, content: str) -> bool:
    terms = _query_terms(prompt)
    if not terms:
        return False
    hay_terms = _content_signature(f"{title}\n{content[:600]}")
    overlap = terms & hay_terms
    if not overlap:
        return False
    # One shared generic token is too weak for active-recall injection. This
    # catches cases like "active recall 관련없는 결과값 개선" retrieving a broad
    # OpenClaw memory only because it says "개선/recall" somewhere.
    return not (len(terms) >= 4 and len(overlap) < 2)


def _content_signature(text: str) -> set[str]:
    normalized = re.sub(r"(?im)^signal:\s*\w+\s*$", " ", text or "")
    normalized = re.sub(r"(?im)^openclaw\s+\w+\s+session\s*\([^)]*\)\s*$", " ", normalized)
    normalized = re.sub(r"\b20\d{2}-\d{2}-\d{2}\b", " ", normalized)
    return {tok for tok in re.findall(r"[a-zA-Z가-힣0-9]{3,}", normalized.lower()) if len(tok) >= 3}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / max(1, len(a | b))


# ── Proactive layer ───────────────────────────────────────────────


def _proactive_blocks(seen_hashes: set[str]) -> list[InjectionBlock]:
    """Pull urgent insights from proactive.get_current_insights (existing module).
    Only severity high and age < 6 h."""
    if _get_current_insights is None:
        return []
    try:
        insights = _get_current_insights(max_age_hours=6, severity="urgent") or []
        insights.extend(_get_current_insights(max_age_hours=6, severity="warning") or [])
    except Exception as e:
        log.debug("proactive.get_current_insights failed: %s", e)
        return []

    blocks: list[InjectionBlock] = []
    hashes_this_call: set[str] = set()
    for ins in insights[:3]:
        title = (getattr(ins, "summary", None) or "proactive insight")[:80]
        content = (getattr(ins, "detail", None) or "")[:800]
        severity = getattr(ins, "severity", "info")
        iid = getattr(ins, "id", None) or _hash(f"proactive:{title}")
        h = _hash(f"proactive:{iid}")
        if h in seen_hashes or h in hashes_this_call:
            continue
        hashes_this_call.add(h)
        blocks.append(
            InjectionBlock(
                id=h,
                title=f"⚠ {title}",
                content=content,
                source="proactive",
                score=0.9 if severity == "urgent" else 0.7,
                priority="critical" if severity == "urgent" else "high",
            )
        )
    return blocks


# ── Doorbell layer ────────────────────────────────────────────────


def _doorbell_blocks(session_id: str, *, prompt: str = "") -> list[InjectionBlock]:
    """Read /tmp/.brain_doorbell.<session_id>.jsonl if present. DOES NOT clear
    it — the hook script is responsible for clearing to keep the transport
    idempotent for MCP consumers that may also read it.

    Doorbell is an interrupt lane, but UserPromptSubmit context must remain
    prompt-relevant. A queued item is injected only when it is explicitly
    critical or overlaps the current prompt. Non-matching items are intentionally
    left to digest/Telegram surfaces instead of polluting the model's immediate
    working context.
    """
    if not session_id:
        return []
    path = DOORBELL_DIR / f".brain_doorbell.{session_id}.jsonl"
    if not path.exists():
        return []
    blocks: list[InjectionBlock] = []
    try:
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError as _exc:
                log.debug("silenced exception in active_recall.py: %s", _exc)
                continue
            title = (rec.get("title") or "brain doorbell")[:80]
            content = (rec.get("content") or "")[:800]
            priority = rec.get("priority", "high")
            source_tag = rec.get("source", "brain_loop")
            severity = _safe_float(rec.get("severity"), 0.0)
            if not _doorbell_relevant(
                prompt, title=title, content=content, priority=priority, severity=severity
            ):
                continue
            h = _hash(f"doorbell:{title}:{content[:100]}")
            blocks.append(
                InjectionBlock(
                    id=h,
                    title=f"🔔 {title}",
                    content=content,
                    source=f"doorbell:{source_tag}",
                    score=1.0,  # brain explicitly pushed this — always show
                    priority=priority if priority in PRIORITY_ORDER else "high",
                )
            )
    except Exception as e:
        log.debug("doorbell read failed: %s", e)
    return blocks


def _doorbell_relevant(
    prompt: str,
    *,
    title: str,
    content: str,
    priority: str,
    severity: float,
) -> bool:
    """Return True when a doorbell belongs in this prompt's context.

    This is deliberately deterministic and cheap. It avoids the old behavior
    where every urgent queue item was injected into every next prompt, even
    when Chris was asking about an unrelated policy or implementation detail.
    """
    if priority == "critical" or severity >= 9.0:
        return True
    prompt_terms = _query_terms(prompt)
    if not prompt_terms:
        return False
    doorbell_terms = _content_signature(f"{title}\n{content[:800]}")
    return bool(prompt_terms & doorbell_terms)


def _safe_float(value: object, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


# ── Session seen tracking (dedup with decay tiers) ────────────────


@contextmanager
def _autonomy_db_conn() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(str(AUTONOMY_DB), timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _get_seen(session_id: str, agent: str) -> dict[str, dict]:
    """Read session_context entry storing the seen-block registry for this session.

    Stored as JSON: { "<hash>": {"last_turn": int, "priority": str}, ... }
    """
    if not session_id:
        return {}
    try:
        with _autonomy_db_conn() as conn:
            row = conn.execute(
                "SELECT value FROM session_context " "WHERE session_id=? AND agent=? AND key='recall_seen'",
                (session_id, agent),
            ).fetchone()
            if not row:
                return {}
            try:
                return json.loads(row["value"]) or {}
            except json.JSONDecodeError:
                return {}
    except sqlite3.Error:
        return {}


def _update_seen(
    session_id: str,
    agent: str,
    turn_idx: int,
    blocks: list[InjectionBlock],
) -> None:
    """Update seen tracker with current turn's block hashes + priorities."""
    if not session_id:
        return
    current = _get_seen(session_id, agent)
    for b in blocks:
        current[b.id] = {"last_turn": turn_idx, "priority": b.priority}

    # Garbage collect entries that are way past decay
    garbage = []
    for h, meta in current.items():
        last = meta.get("last_turn", 0)
        pri = meta.get("priority", "medium")
        if pri == "critical":
            continue
        if (pri == "high" and turn_idx - last > 40) or turn_idx - last > 20:
            garbage.append(h)
    for h in garbage:
        del current[h]

    try:
        now = _now_iso()
        with _autonomy_db_conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO session_context "
                "(session_id, agent, key, value, updated_at) VALUES (?,?,?,?,?)",
                (session_id, agent, "recall_seen", json.dumps(current), now),
            )
    except sqlite3.Error as e:
        log.debug("update_seen failed: %s", e)


def _apply_decay_filter(
    blocks: list[InjectionBlock],
    seen: dict[str, dict],
    turn_idx: int,
) -> list[InjectionBlock]:
    """Remove blocks whose hashes are in seen AND still within their decay window.

    Decay tiers (CR1 fix 2026-04-14):
      critical  — re-inject every 15 turns so canonical truth re-surfaces
                  even deep into a session (was 10^6 = never)
      high      — re-inject after 20 turns
      medium    — re-inject after 10 turns
      low       — re-inject after 5 turns

    Critical canonical routes (design standard, credentials, live state)
    still bypass the seen_hashes filter in _canonical_blocks_from_matches —
    the re-inject cooldown here only applies to already-delivered blocks,
    not to route-matched blocks, which always surface.
    """
    decay_turns = {"critical": 15, "high": 20, "medium": 10, "low": 5}
    out = []
    for b in blocks:
        meta = seen.get(b.id)
        if not meta:
            out.append(b)
            continue
        last_turn = int(meta.get("last_turn", 0))
        cooldown = decay_turns.get(b.priority, 10)
        if turn_idx - last_turn >= cooldown:
            out.append(b)
    return out


# ── Confidence sentinel ───────────────────────────────────────────


def _confidence_sentinel() -> InjectionBlock:
    return InjectionBlock(
        id=_hash("sentinel:low_confidence"),
        title="Brain confidence low",
        content=(
            "No canonical or high-confidence semantic match for this prompt. "
            "Proceeding on general knowledge — consider asking Chris for "
            "clarification if the request is domain-specific."
        ),
        source="confidence_sentinel",
        score=0.0,
        priority="low",
    )


def _confidence_sentinel_enabled() -> bool:
    return os.getenv("BRAIN_ACTIVE_RECALL_CONFIDENCE_SENTINEL", "0").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


# ── Budget enforcement ────────────────────────────────────────────


def _rough_tokens(text: str) -> int:
    # ~4 chars per token on average for mixed EN/KO content
    return max(1, len(text) // 4)


def _enforce_budget(blocks: list[InjectionBlock], limit: int) -> list[InjectionBlock]:
    """Sort by priority then score, keep blocks until token budget is hit.

    CR2 fix (2026-04-14): F8 previously used `break` on critical overflow
    to prevent a critical block from being silently replaced by lower-
    priority filler. But the sort order already makes that impossible —
    critical blocks are tried BEFORE non-critical, so a non-critical
    block can never "take" the critical's slot. The break only hurt: if
    the first block was a critical larger than budget, `break` killed
    all lower-priority packing too, leaving Chris with a sentinel-only
    response. Fix: log the critical overflow and continue packing. The
    dropped critical is logged for observability.
    """
    sorted_blocks = sorted(
        blocks,
        key=lambda b: (PRIORITY_ORDER.get(b.priority, 9), -b.score),
    )
    kept: list[InjectionBlock] = []
    budget_used = 0
    for b in sorted_blocks:
        tokens = _rough_tokens(b.content) + _rough_tokens(b.title)
        if budget_used + tokens > limit:
            if b.priority == "critical":
                log.warning(
                    "budget_overflow_critical id=%s tokens=%d remaining=%d",
                    b.id,
                    tokens,
                    limit - budget_used,
                )
            continue
        kept.append(b)
        budget_used += tokens
    return kept


# ── Context compiler ────────────────────────────────────────────────


def _compile_context_blocks(
    blocks: list[InjectionBlock],
    *,
    judgment: object | None = None,
) -> list[InjectionBlock]:
    """Annotate selected blocks with deterministic inclusion metadata.

    This is deliberately a metadata-only pass. It explains the context plan
    without changing ordering, scores, or budget behavior, keeping the hook
    regression surface small.
    """

    intent = _judgment_intent(judgment)
    for block in blocks:
        token_estimate = _rough_tokens(block.content) + _rough_tokens(block.title)
        flags = _compiler_risk_flags(block, token_estimate)
        block.token_estimate = token_estimate
        block.freshness = _compiler_freshness(block)
        block.risk_flags = flags
        block.include_reason = _compiler_include_reason(block, intent=intent)
        block.compiler_score = _compiler_score(block, flags)
        block.contract_category = _contract_category(block)
    return blocks


def _judgment_intent(judgment: object | None) -> str:
    if judgment is None:
        return "unknown"
    return str(getattr(judgment, "intent", "") or "unknown")


def _compiler_include_reason(block: InjectionBlock, *, intent: str) -> str:
    source = (block.source or "").lower()
    if source == "canonical":
        return f"canonical guarantee matched intent={intent}"
    if source.startswith("doorbell"):
        return "explicit urgent doorbell for this session"
    if source.startswith("proactive"):
        return "recent proactive insight passed urgency gate"
    if source.startswith("semantic"):
        return f"semantic evidence passed score floor for intent={intent}"
    if source == "confidence_sentinel":
        return "diagnostic fallback because no high-confidence context survived"
    return f"selected by active recall arbitration for intent={intent}"


def _compiler_freshness(block: InjectionBlock) -> str:
    haystack = f"{block.title}\n{block.path or ''}\n{block.content[:500]}".lower()
    if _contains_temporal_marker(haystack, ("today", "yesterday", "어제", "오늘", "최근", "last ")):
        return "time_sensitive"
    if _looks_like_usage_snapshot(block.title, block.content, block.path):
        return "snapshot"
    if any(marker in haystack for marker in ("stale", "obsolete", "superseded", "archived", "폐기", "대체")):
        return "possibly_stale"
    if block.source == "canonical" or "canonical" in (block.path or "").lower():
        return "canonical"
    return "unknown"


def _contains_temporal_marker(text: str, markers: tuple[str, ...]) -> bool:
    return any(marker in text for marker in markers)


def _compiler_risk_flags(block: InjectionBlock, token_estimate: int) -> list[str]:
    flags: list[str] = []
    source = (block.source or "").lower()
    if token_estimate > 600:
        flags.append("large_block")
    if source.startswith("semantic") and block.score < 0.8:
        flags.append("low_semantic_score")
    if _compiler_freshness(block) in {"snapshot", "possibly_stale", "time_sensitive"}:
        flags.append(_compiler_freshness(block))
    if source.startswith("doorbell"):
        flags.append("session_push")
    if source == "canonical":
        flags.append("canonical_authority")
    return flags


def _compiler_score(block: InjectionBlock, flags: list[str]) -> float:
    priority_bonus = {"critical": 0.25, "high": 0.15, "medium": 0.05, "low": 0.0}.get(block.priority, 0.0)
    source_bonus = {
        "canonical": 0.2,
        "doorbell": 0.18,
        "proactive": 0.08,
        "semantic": 0.05,
        "confidence_sentinel": -0.1,
    }.get(_compiler_source_family(block), 0.0)
    risk_penalty = 0.03 * len([f for f in flags if f not in {"canonical_authority", "session_push"}])
    return round(
        max(0.0, min(1.0, float(block.score or 0.0) + priority_bonus + source_bonus - risk_penalty)), 3
    )


def _compiler_source_family(block: InjectionBlock) -> str:
    source = (block.source or "").lower()
    if source == "canonical":
        return "canonical"
    if source.startswith("doorbell"):
        return "doorbell"
    if source.startswith("proactive"):
        return "proactive"
    if source.startswith("semantic"):
        return "semantic"
    if source == "confidence_sentinel":
        return "confidence_sentinel"
    return "other"


def _context_compiler_report(blocks: list[InjectionBlock]) -> dict:
    flags: dict[str, int] = {}
    token_estimates = []
    for block in blocks:
        if block.token_estimate is not None:
            token_estimates.append(block.token_estimate)
        for flag in block.risk_flags:
            flags[flag] = flags.get(flag, 0) + 1
    return {
        "version": 1,
        "annotated_blocks": sum(1 for b in blocks if b.include_reason),
        "estimated_tokens": sum(token_estimates),
        "risk_flags": dict(sorted(flags.items())),
    }


def _context_contract_report(blocks: list[InjectionBlock], *, judgment: object | None = None) -> dict:
    categories: dict[str, int] = {}
    for block in blocks:
        category = block.contract_category or _contract_category(block)
        categories[category] = categories.get(category, 0) + 1
    return {
        "version": 1,
        "intent": _judgment_intent(judgment),
        "allowed_categories": _allowed_contract_categories(judgment),
        "block_categories": dict(sorted(categories.items())),
        "suppresses_raw_doorbell": True,
        "uses_llm": False,
        "contract": "inject only prompt-relevant policy, goals/current task, recent decisions, risk constraints, or direct evidence",
    }


def _semantic_timeout_for_contract(
    prompt: str,
    canonical: list[InjectionBlock],
    judgment: object | None,
) -> float | None:
    """Shorten semantic fanout when critical canonical context already exists.

    UserPromptSubmit is latency-sensitive. If a critical canonical route already
    supplies the relevant policy/constraint, semantic recall becomes optional
    enrichment and should not hold the prompt for the full 1.2s budget unless
    Chris asks for recent/current/stale/task context.
    """
    if not canonical or not any(block.priority in {"critical", "high"} for block in canonical):
        return None
    if judgment is not None and str(getattr(judgment, "intent", "")) == "factual_question":
        return None
    if _prompt_requests_extra_context(prompt):
        return None
    return _SEMANTIC_FAST_TIMEOUT_S


def _prompt_requests_extra_context(prompt: str) -> bool:
    lowered = (prompt or "").lower()
    return any(
        marker in lowered
        for marker in (
            "recent",
            "current",
            "latest",
            "stale",
            "history",
            "decision",
            "task",
            "prehook",
            "hook",
            "최근",
            "현재",
            "지금",
            "결정",
            "판단",
            "작업",
            "남은",
            "이력",
        )
    )


def _contract_category(block: InjectionBlock) -> str:
    source = (block.source or "").lower()
    haystack = f"{block.title}\n{block.path or ''}\n{block.content[:800]}".lower()
    if source.startswith("doorbell") or source.startswith("proactive"):
        return "urgent_interrupt"
    if any(marker in haystack for marker in _RISK_CONSTRAINT_MARKERS):
        return "risk_constraint"
    if any(marker in haystack for marker in _CURRENT_TASK_MARKERS):
        return "current_task"
    if any(marker in haystack for marker in _GOAL_MARKERS):
        return "project_goal"
    if any(marker in haystack for marker in _RECENT_DECISION_MARKERS):
        return "recent_decision"
    if any(marker in haystack for marker in _POLICY_MARKERS):
        return "policy"
    return "direct_evidence"


def _allowed_contract_categories(judgment: object | None) -> list[str]:
    if judgment is not None and not bool(getattr(judgment, "needs_memory", True)):
        return ["urgent_interrupt"]
    return [
        "policy",
        "project_goal",
        "current_task",
        "recent_decision",
        "risk_constraint",
        "direct_evidence",
        "urgent_interrupt",
    ]


_POLICY_MARKERS = (
    "prefers",
    "preference",
    "requires",
    "expects",
    "wants",
    "should",
    "policy",
    "rule",
    "선호",
    "원해",
    "요구",
    "정책",
    "규칙",
)

_GOAL_MARKERS = (
    "goal",
    "objective",
    "direction",
    "project",
    "brain system",
    "목표",
    "방향",
    "프로젝트",
    "브레인 시스템",
)

_CURRENT_TASK_MARKERS = (
    "current task",
    "next step",
    "in progress",
    "pending",
    "남은 작업",
    "다음 단계",
    "진행중",
    "진행 중",
)

_RECENT_DECISION_MARKERS = (
    "decision",
    "decided",
    "ledger",
    "outcome",
    "결정",
    "판단",
    "결과",
)

_RISK_CONSTRAINT_MARKERS = (
    "no extra cost",
    "extra llm api",
    "extra api",
    "api spend",
    "paid api",
    "spend",
    "subscription",
    "resource",
    "latency",
    "stability",
    "scalability",
    "regression",
    "local llm",
    "추가 비용",
    "유료 api",
    "구독",
    "리소스",
    "지연",
    "안정",
    "확장",
    "회귀",
    "로컬 llm",
)


# ── Observability ─────────────────────────────────────────────────


def _audit(
    prompt: str,
    session_id: str,
    agent: str,
    blocks: list[InjectionBlock],
    intents: list[str],
    latency_ms: int,
) -> int | None:
    audit_id = None
    if _insert_action_audit is None:
        return None
    try:
        atom_ids = [b.id for b in blocks]
        audit_id = _insert_action_audit(
            route="/recall/active",
            query_text=(prompt or "")[:2000],
            tool="active_recall",
            actor=agent or "claude",
            retrieved_atom_ids=atom_ids,
            session_id=session_id,
        )
    except Exception as e:
        log.debug("action_audit insert failed: %s", e)

    # v3 Layer B — reinforce accessed semantic_memory ids (MemoryBank +
    # Neo4j MemRL). Fire-and-forget through the shared bg pool so retrieval
    # latency stays under the 1.2s hook budget.
    try:
        # 2026-04-18: previously used `b.id`, which is a local dedup hash
        # (`_hash(f"semantic:{rid}:{title}:{content[:200]}")`) — not the
        # ChromaDB memory_id that reinforce_on_access expects. The reinforce
        # call found zero matches every time and silently no-op'd. MemoryBank
        # reinforcement-on-access was dead on the active_recall path. Now
        # uses the real ChromaDB id carried on `memory_id`.
        sem_ids = [
            b.memory_id for b in blocks if b.source.startswith("semantic") and b.score >= 0.5 and b.memory_id
        ][:5]
        if sem_ids:
            import search_unified
            from memory_lifecycle import reinforce_on_access

            search_unified._search_bg_pool.submit(reinforce_on_access, sem_ids)
    except Exception as _exc:
        log.debug("silenced exception in active_recall.py: %s", _exc)
    return audit_id


def _quality_report(
    blocks: list[InjectionBlock],
    *,
    judgment: object | None = None,
    arbitration: object | None = None,
) -> dict:
    generic_summary_count = sum(1 for b in blocks if _is_generic_summary_title(b.title))
    noisy_count = sum(1 for b in blocks if _is_noisy_semantic_result(b.title, b.content, b.path))
    semantic_count = sum(1 for b in blocks if b.source.startswith("semantic"))
    report = {
        "block_count": len(blocks),
        "semantic_count": semantic_count,
        "generic_summary_count": generic_summary_count,
        "noisy_count": noisy_count,
        "max_score": max((b.score for b in blocks), default=0.0),
    }
    if judgment is not None and hasattr(judgment, "to_dict"):
        report["judgment"] = judgment.to_dict()
    if arbitration is not None and hasattr(arbitration, "to_quality_dict"):
        report["arbitration"] = arbitration.to_quality_dict()
    report["compiler"] = _context_compiler_report(blocks)
    report["context_contract"] = _context_contract_report(blocks, judgment=judgment)
    return report


# ── Helpers ───────────────────────────────────────────────────────


def _hash(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()[:16]


from db import now_iso as _now_iso  # noqa: E402  — single-source UTC stamp helper

# ── Public entry point ────────────────────────────────────────────


def build_injection(
    prompt: str,
    session_id: str,
    turn_idx: int = 0,
    agent: str = "claude",
    cwd: str | None = None,
    seen_hashes: list[str] | None = None,
) -> dict:
    """Top-level per-turn injection builder. Returns a plain dict matching
    RecallActiveResponse in server.py. Called from POST /recall/active.

    Fail-open: any step failure returns a degraded response with empty blocks
    rather than raising — the hook must never block the user's prompt.
    """
    t0 = time.time()
    try:
        judgment = _classify_prompt(prompt, cwd=cwd) if _classify_prompt is not None else None
        memory_needed = bool(getattr(judgment, "needs_memory", True))
        allow_semantic = bool(getattr(judgment, "allow_semantic", True))
        allow_proactive = bool(getattr(judgment, "allow_proactive", True))

        matches = _match_canonical_routes(prompt) if memory_needed else []
        intents = [m.intent for m in matches]

        seen_registry = _get_seen(session_id, agent) if session_id else {}
        seen_set = set(seen_hashes or []) | set(seen_registry.keys())

        canonical = _canonical_blocks_from_matches(matches, seen_set)
        semantic_limit = int(getattr(judgment, "max_blocks", 5) or 5)
        semantic_min_score = getattr(judgment, "min_semantic_score", None)
        semantic = (
            _semantic_blocks(
                prompt,
                matches,
                seen_set,
                limit=max(1, min(5, semantic_limit)),
                min_score=semantic_min_score,
                timeout_s=_semantic_timeout_for_contract(prompt, canonical, judgment),
            )
            if allow_semantic
            else []
        )
        proactive = _proactive_blocks(seen_set) if allow_proactive else []
        doorbell = _doorbell_blocks(session_id, prompt=prompt) if session_id else []

        all_blocks = canonical + doorbell + semantic + proactive
        filtered = _apply_decay_filter(all_blocks, seen_registry, turn_idx)
        arbitration = None
        if _arbitrate_blocks is not None and judgment is not None:
            arbitration = _arbitrate_blocks(filtered, judgment)
            filtered = list(arbitration.blocks)

        if (
            _confidence_sentinel_enabled()
            and (not filtered or max((b.score for b in filtered), default=0) < LOW_CONFIDENCE_THRESHOLD)
            and not any(b.priority == "critical" for b in filtered)
        ):
            filtered.insert(0, _confidence_sentinel())

        budget_limit = int(getattr(judgment, "max_tokens", BUDGET_TOKEN_LIMIT) or BUDGET_TOKEN_LIMIT)
        filtered = _compile_context_blocks(filtered, judgment=judgment)
        budgeted = _enforce_budget(filtered, min(BUDGET_TOKEN_LIMIT, budget_limit))

        latency_ms = int((time.time() - t0) * 1000)

        audit_id = _audit(prompt, session_id, agent, budgeted, intents, latency_ms)
        if _record_judgment_feedback is not None:
            try:
                _record_judgment_feedback(
                    action_audit_id=audit_id,
                    session_id=session_id,
                    actor=agent,
                    judgment=judgment,
                    arbitration=arbitration,
                    block_count=len(budgeted),
                    semantic_count=sum(1 for b in budgeted if b.source.startswith("semantic")),
                    latency_ms=latency_ms,
                )
            except Exception as exc:
                log.debug("judgment feedback write failed: %s", exc)
        if session_id:
            _update_seen(session_id, agent, turn_idx, budgeted)

        return {
            "blocks": [b.to_dict() for b in budgeted],
            "intent": ",".join(intents) if intents else None,
            "total_tokens": sum(_rough_tokens(b.content) + _rough_tokens(b.title) for b in budgeted),
            "latency_ms": latency_ms,
            "new_since_last_turn": len(budgeted) > 0,
            "quality": _quality_report(budgeted, judgment=judgment, arbitration=arbitration),
            "degraded": False,
        }
    except Exception as e:
        log.warning("active_recall.build_injection failed: %s", e, exc_info=True)
        return {
            "blocks": [],
            "intent": None,
            "total_tokens": 0,
            "latency_ms": int((time.time() - t0) * 1000),
            "new_since_last_turn": False,
            "degraded": True,
        }
