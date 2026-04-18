"""brain_core/active_recall.py — per-turn attention gating.

Biology: thalamus. Intercepts every user prompt (via UserPromptSubmit hook for
Claude Code, before_prompt_build for OpenClaw agents) and decides what context
to inject based on the prompt's intent. This is the module that turns brain
from a passive retrieval store into a per-turn proactive surface.

Pipeline (fast path, <1200 ms hard budget):

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
  6. Confidence sentinel      — if no block score ≥ 0.5, inject a
                                "brain confidence low" system reminder.
  7. Budget                   — 2 KB max, priority: critical > high > medium.
  8. Observability            — insert_action_audit(route='/recall/active', ...)

Fail-open: every step wraps in try/except. Returned blocks are best-effort.
The hook script catches failures and prints a degraded sentinel.

Called from: server.py::recall_active endpoint, OpenClaw brain-active-recall
plugin via the same endpoint.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC
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

log = logging.getLogger("brain.active_recall")

INTENT_ROUTES_PATH = BRAIN_CORE_DIR / "intent_routes.yaml"
DOORBELL_DIR = Path("/tmp")
DOORBELL_TEMPLATE = "{session_id}"
BUDGET_TOKEN_LIMIT = 2048
LOW_CONFIDENCE_THRESHOLD = 0.5
HARD_TIMEOUT_MS = 1200


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

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "content": self.content,
            "source": self.source,
            "score": self.score,
            "priority": self.priority,
            "path": self.path,
        }


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
            except Exception:
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


def _semantic_blocks(
    prompt: str,
    matches: list[IntentMatch],
    seen_hashes: set[str],
    limit: int = 5,
) -> list[InjectionBlock]:
    """Fan out the prompt + matched intents' always_push_queries through
    search_unified.search_all. Returns up to `limit` dedup'd blocks."""
    try:
        import search_unified
    except ImportError:
        return []

    queries = [prompt]
    for m in matches:
        queries.extend(m.always_push_queries)

    all_results: list[dict] = []
    for q in queries[:4]:  # cap to avoid runaway
        if not q or not q.strip():
            continue
        try:
            resp = search_unified.search_all(
                q,
                limit=limit,
                sources=["rag", "canonical", "obsidian"],
                original_query=prompt,
            )
            if isinstance(resp, dict):
                all_results.extend(resp.get("results", []))
        except Exception as e:
            log.debug("search_all failed for %r: %s", q[:40], e)

    # Dedup by path or id within this call
    blocks: list[InjectionBlock] = []
    hashes_this_call: set[str] = set()
    for r in all_results:
        if not isinstance(r, dict):
            continue
        rid = r.get("id") or r.get("path") or r.get("title") or ""
        title = (r.get("title") or r.get("path") or "untitled")[:80]
        content = (r.get("content") or "")[:1200]
        score = float(r.get("score") or 0)
        collection = r.get("collection", "")
        h = _hash(f"semantic:{rid}:{title}:{content[:200]}")
        if h in seen_hashes or h in hashes_this_call:
            continue
        hashes_this_call.add(h)
        # Normalize score 0..1 from the brain's 0..100 range
        norm_score = min(1.0, max(0.0, score / 100.0))
        blocks.append(
            InjectionBlock(
                id=h,
                title=title,
                content=content,
                source=f"semantic:{collection}" if collection else "semantic",
                score=norm_score,
                priority="high" if norm_score >= 0.6 else "medium",
                path=r.get("path"),
            )
        )
        if len(blocks) >= limit:
            break
    return blocks


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


def _doorbell_blocks(session_id: str) -> list[InjectionBlock]:
    """Read /tmp/.brain_doorbell.<session_id>.jsonl if present. DOES NOT clear
    it — the hook script is responsible for clearing to keep the transport
    idempotent for MCP consumers that may also read it.
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
            except json.JSONDecodeError:
                continue
            title = (rec.get("title") or "brain doorbell")[:80]
            content = (rec.get("content") or "")[:800]
            priority = rec.get("priority", "high")
            source_tag = rec.get("source", "brain_loop")
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


# ── Session seen tracking (dedup with decay tiers) ────────────────


@contextmanager
def _autonomy_db_conn():
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


# ── Observability ─────────────────────────────────────────────────


def _audit(
    prompt: str,
    session_id: str,
    agent: str,
    blocks: list[InjectionBlock],
    intents: list[str],
    latency_ms: int,
) -> None:
    if _insert_action_audit is None:
        return
    try:
        atom_ids = [b.id for b in blocks]
        _insert_action_audit(
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
        sem_ids = [b.id for b in blocks if b.source.startswith("semantic") and b.score >= 0.5][:5]
        if sem_ids:
            import search_unified
            from memory_lifecycle import reinforce_on_access

            search_unified._search_bg_pool.submit(reinforce_on_access, sem_ids)
    except Exception:
        pass


# ── Helpers ───────────────────────────────────────────────────────


def _hash(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()[:16]


def _now_iso() -> str:
    from datetime import datetime

    return datetime.now(UTC).isoformat(timespec="seconds")


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
        matches = _match_canonical_routes(prompt)
        intents = [m.intent for m in matches]

        seen_registry = _get_seen(session_id, agent) if session_id else {}
        seen_set = set(seen_hashes or []) | set(seen_registry.keys())

        canonical = _canonical_blocks_from_matches(matches, seen_set)
        semantic = _semantic_blocks(prompt, matches, seen_set, limit=5)
        proactive = _proactive_blocks(seen_set)
        doorbell = _doorbell_blocks(session_id) if session_id else []

        all_blocks = canonical + doorbell + semantic + proactive
        filtered = _apply_decay_filter(all_blocks, seen_registry, turn_idx)

        if not filtered or max((b.score for b in filtered), default=0) < LOW_CONFIDENCE_THRESHOLD:
            if not any(b.priority == "critical" for b in filtered):
                filtered.insert(0, _confidence_sentinel())

        budgeted = _enforce_budget(filtered, BUDGET_TOKEN_LIMIT)

        latency_ms = int((time.time() - t0) * 1000)

        _audit(prompt, session_id, agent, budgeted, intents, latency_ms)
        if session_id:
            _update_seen(session_id, agent, turn_idx, budgeted)

        return {
            "blocks": [b.to_dict() for b in budgeted],
            "intent": ",".join(intents) if intents else None,
            "total_tokens": sum(_rough_tokens(b.content) + _rough_tokens(b.title) for b in budgeted),
            "latency_ms": latency_ms,
            "new_since_last_turn": len(budgeted) > 0,
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
