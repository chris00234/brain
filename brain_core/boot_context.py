#!/opt/homebrew/bin/python3
"""RAG Boot Context — load relevant context for agent startup via unified search.

Usage:
  boot_context.py <agent_name> [--limit N] [--json]

Returns the most relevant context snippets for the agent's role,
searched across ChromaDB, canonical knowledge, and Obsidian vault.
"""

import argparse
import json
import re
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

AGENT_QUERIES = {
    "ellie": [
        "container restart error crash",
        "docker compose configuration service",
        "nginx cloudflare tunnel proxy",
        "Chris corrections feedback to Ellie",
        "this week infrastructure changes decisions",
    ],
    "jenna": [
        "calendar schedule today this week",
        "reminders pending tasks urgent",
        "Chris preferences daily routine communication",
        "Chris corrections feedback to Jenna",
        "this week decisions priorities",
    ],
    "liz": [
        "code review architecture decisions",
        "debugging errors recent fixes",
        "active projects MCC development",
        "Chris corrections feedback to Liz",
        "this week coding changes decisions",
    ],
    "sage": [
        "research topics active questions",
        "knowledge synthesis contradictions gaps",
        "Chris corrections feedback to Sage",
        "this week decisions patterns learned",
    ],
    "market": [
        "content calendar posts blog",
        "SEO analytics brand strategy",
        "Chris corrections feedback to Market",
        "this week content decisions",
    ],
    "claude": [
        "Chris preferences conventions coding style",
        "recent infrastructure decisions changes",
        "OpenClaw agent configuration brain system",
        "Chris corrections feedback to Claude",
        "this week active projects priorities",
    ],
}

DEFAULT_QUERIES = [
    "recent tasks and decisions",
    "active issues and risks",
    "Chris corrections and preferences",
]

try:
    from config import BRAIN_LOGS_DIR, INBOX_DIR, KNOWLEDGE_DIR, OPENCLAW_DIR

    BOOT_LOG = BRAIN_LOGS_DIR / "boot-context-log.jsonl"
    CHRIS_IDENTITY = KNOWLEDGE_DIR / "canonical" / "chris" / "_identity.md"
    CHRIS_STATE = KNOWLEDGE_DIR / "canonical" / "chris" / "_state.md"
except ImportError:
    BOOT_LOG = Path("/Users/chrischo/server/brain/logs/boot-context-log.jsonl")
    CHRIS_IDENTITY = Path("/Users/chrischo/server/knowledge/canonical/chris/_identity.md")
    CHRIS_STATE = Path("/Users/chrischo/server/knowledge/canonical/chris/_state.md")
    OPENCLAW_DIR = Path("/Users/chrischo/.openclaw")
    INBOX_DIR = Path("/Users/chrischo/server/knowledge/raw/inbox")


# ---------------------------------------------------------------------------
# Block cache — avoids re-fetching static content every session
# ---------------------------------------------------------------------------
_block_cache: dict[str, tuple[float, str]] = {}  # key -> (timestamp, content)
STATIC_TTL = 7 * 24 * 3600  # 7 days — static blocks (identity, state)
DYNAMIC_TTL = 300  # 5 min — dynamic blocks (scratch, memories, alerts, messages, focus, search)


def _cache_get(key: str, ttl: float) -> str | None:
    """Return cached content if fresh, else None."""
    entry = _block_cache.get(key)
    if entry is None:
        return None
    ts, content = entry
    if time.monotonic() - ts > ttl:
        return None
    return content


def _cache_set(key: str, content: str) -> None:
    _block_cache[key] = (time.monotonic(), content)


def flush_cache() -> None:
    """Clear all cached blocks. Call after profile_regen or manual invalidation."""
    _block_cache.clear()


def _predictive_queries(agent: str) -> list[str]:
    """Generate 3-5 adaptive queries based on temporal patterns, calendar, and recent sessions."""
    queries: list[str] = []

    # Step 1 — Focus aggregate signal (<1ms, just file reads)
    try:
        agg_path = BRAIN_LOGS_DIR / "focus-aggregate.jsonl"
        if agg_path.exists():
            now = datetime.now()
            dow_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
            current_dow = dow_names[now.weekday()]
            current_hour = now.hour
            # Parse dow_hour_rollup rows for current day-of-week
            best: list[tuple[int, int]] = []  # (commit_count, hour)
            for line in agg_path.read_text().splitlines():
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if row.get("kind") != "dow_hour_rollup" or row.get("dow") != current_dow:
                    continue
                hour = row.get("hour", -1)
                if abs(hour - current_hour) <= 2:
                    best.append((row.get("commit_count_total", 0), hour))
            best.sort(reverse=True)
            if best and best[0][0] >= 2:
                period = "afternoon" if current_hour >= 12 else "morning"
                queries.append(f"active development work {current_dow} {period}")
            if len(best) >= 2:
                queries.append("recent code changes and commits")
    except Exception:
        pass

    # Step 2 — Calendar lookahead (<10ms)
    try:
        from vector_store import get_vector_store

        store = get_vector_store()
        # "personal" is the canonical home for calendar entries; "calendar"
        # is a legacy name kept for back-compat with old ingest paths.
        cal_collection = "personal" if store.count("personal") > 0 else "calendar"
        points = store.get(
            cal_collection, limit=50, with_payload=True, with_documents=True
        )
        if points:
            docs = [p.document or "" for p in points]
            metas = [p.payload for p in points]
        else:
            docs, metas = [], []
        now_utc = datetime.now(UTC)
        cutoff = now_utc + timedelta(hours=4)
        cal_count = 0
        for doc, meta in zip(docs, metas, strict=False):
            if cal_count >= 2:
                break
            if not meta:
                continue
            date_str = meta.get("date") or meta.get("start_date") or meta.get("timestamp") or ""
            if not date_str:
                continue
            try:
                event_dt = datetime.fromisoformat(date_str)
                if event_dt.tzinfo is None:
                    event_dt = event_dt.replace(tzinfo=UTC)
                if not (now_utc <= event_dt <= cutoff):
                    continue
            except Exception:
                continue
            title = meta.get("title") or meta.get("subject") or (doc[:80] if doc else "")
            if title:
                queries.append(f"{title.strip()[:60]} preparation context")
                cal_count += 1
    except Exception:
        pass

    # Step 3 — Session continuation (<5ms)
    try:
        from working_memory import get_session_summaries

        summaries = get_session_summaries(limit=3)
        if summaries:
            content = summaries[0].get("content", "")
            # First sentence or first 100 chars
            topic = content.split(".")[0][:100].strip() if content else ""
            if topic:
                queries.append(f"{topic} next steps")
    except Exception:
        pass

    return queries[:5]


_search_mod = None


def _load_search_mod():
    global _search_mod
    if _search_mod is not None:
        return _search_mod
    _parent = str(Path(__file__).parent)
    if _parent not in sys.path:
        sys.path.insert(0, _parent)
    try:
        import search_unified

        _search_mod = search_unified
    except ImportError:
        pass
    return _search_mod


def search(query, limit=3):
    """In-process search via search_unified (no subprocess overhead)."""
    mod = _load_search_mod()
    if mod is None:
        return []
    try:
        payload = mod.search_all(query, limit)
        return payload.get("results", [])
    except Exception as e:
        import logging

        logging.getLogger("brain.boot_context").warning("search failed for %r: %s", query[:50], e)
        return []


def get_scratch_context(agent_name):
    key = f"scratch:{agent_name}"
    cached = _cache_get(key, DYNAMIC_TTL)
    if cached is not None:
        return cached
    scratch = OPENCLAW_DIR / f"workspace-{agent_name}" / "SCRATCH.md"
    if scratch.exists():
        content = scratch.read_text().strip()
        if content and len(content) > 50:
            result = content[:500]
            _cache_set(key, result)
            return result
    return None


def _read_stripped_body(path: Path):
    """Read a canonical note and strip the JSON frontmatter, returning the body or None."""
    if not path.exists():
        return None
    try:
        text = path.read_text()
        if text.startswith("---"):
            parts = text.split("---", 2)
            if len(parts) >= 3:
                body = parts[2].strip()
                return body if body else None
        return text.strip()
    except Exception:
        return None


def get_chris_identity():
    """Immutable Chris identity — name, location, hard rules, values. Loaded every boot."""
    cached = _cache_get("chris_identity", STATIC_TTL)
    if cached is not None:
        return cached
    content = _read_stripped_body(CHRIS_IDENTITY)
    if content:
        _cache_set("chris_identity", content)
    return content


def get_chris_state():
    """Mutable Chris state — projects, tools, focus, recent signals. Regenerated weekly."""
    cached = _cache_get("chris_state", STATIC_TTL)
    if cached is not None:
        return cached
    content = _read_stripped_body(CHRIS_STATE)
    if content:
        _cache_set("chris_state", content)
    return content


# 2026-04-17: get_chris_profile alias removed. reasoning.py now imports
# get_chris_state directly with a rename-at-import — cleaner + prevents
# ambiguity about whether a "profile" is identity+state vs state-only.


def _recency_score(created_at: str) -> float:
    if not created_at:
        return 0.3
    try:
        from datetime import datetime

        dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        age_days = (datetime.now(UTC) - dt).total_seconds() / 86400
        if age_days < 1:
            return 1.0
        if age_days < 7:
            return 1.0 - (age_days / 7) * 0.5  # 1.0 -> 0.5
        if age_days < 30:
            return 0.5 - ((age_days - 7) / 23) * 0.4  # 0.5 -> 0.1
        return 0.1
    except Exception:
        return 0.3


def get_agent_memories(agent_name, limit=5):
    """Fetch recent semantic memories specific to this agent — corrections, preferences, decisions.

    Ranks entries by (utility_score * 0.6 + recency * 0.4) where utility comes
    from Neo4j MemoryAccess nodes (MemRL-style usefulness tracking).
    """
    key = f"memories:{agent_name}:{limit}"
    cached = _cache_get(key, DYNAMIC_TTL)
    if cached is not None:
        return cached
    try:
        from vector_store import get_vector_store

        points = get_vector_store().get(
            "semantic_memory",
            filter={"agent": {"$eq": agent_name}},
            limit=200,
            with_payload=True,
            with_documents=True,
        )
        if not points:
            return ""

        entries = []
        for p in points:
            payload = p.payload or {}
            entries.append(
                {
                    "id": p.id,
                    "doc": p.document or "",
                    "meta": payload,
                    "created_at": payload.get("created_at", ""),
                }
            )

        try:
            from neo4j_client import run_query

            id_list = [e["id"] for e in entries if e.get("id")]
            if id_list:
                score_rows = run_query(
                    "UNWIND $ids AS mid OPTIONAL MATCH (m:MemoryAccess {memory_id: mid}) "
                    "RETURN mid, coalesce(m.utility_score, 0.5) AS score",
                    {"ids": id_list},
                )
                score_map = {r["mid"]: float(r["score"]) for r in score_rows}
            else:
                score_map = {}
        except Exception:
            score_map = {}

        for e in entries:
            utility = score_map.get(e["id"], 0.5)
            recency = _recency_score(e["created_at"])
            e["rank_score"] = utility * 0.6 + recency * 0.4

        entries.sort(key=lambda e: e["rank_score"], reverse=True)
        top = entries[:limit]

        lines = []
        for e in top:
            cat = e["meta"].get("category", "")
            date = e["created_at"][:10]
            lines.append(f"[{date} {cat}] {e['doc']}")
        result = "\n".join(lines)
        if result:
            _cache_set(key, result)
        return result
    except Exception:
        return ""


def get_recent_openclaw_distillations(hours: int = 24, limit: int = 5) -> list[dict]:
    """Return the most recent distilled OpenClaw session records from raw/inbox.

    Skips claude agent (has its own ingest path). Sorted by record timestamp desc.
    """
    from inbox_utils import get_recent_inbox_records

    records = [
        r
        for r in get_recent_inbox_records(prefix="raw_oc_", hours=hours, parse=True)
        if not r.get("source_ref", "").startswith("openclaw:claude:")
    ]
    records.sort(key=lambda r: r.get("timestamp", ""), reverse=True)
    return records[:limit]


def build_boot_context(agent_name, limit=3, prompt: str | None = None):
    queries = list(AGENT_QUERIES.get(agent_name, DEFAULT_QUERIES))
    # When the hook passes the user's first prompt, prepend it to the query
    # list so RAG retrieval actually sees current intent instead of only the
    # agent's static baseline queries. This is the turn-0 intent injection.
    if prompt:
        prompt_clean = prompt.strip()
        if 10 <= len(prompt_clean) <= 400:
            queries.insert(0, prompt_clean)
    try:
        queries.extend(_predictive_queries(agent_name))
    except Exception:
        pass
    sections = []
    seen_sources = set()

    # Working memory — current focus, goals, blocked items
    wm_cached = _cache_get(f"working_memory:{agent_name}", DYNAMIC_TTL)
    if wm_cached is not None:
        sections.append({"section": "Current Focus", "content": wm_cached, "source": "brain/working_memory"})
    else:
        try:
            from working_memory import get_working_context

            ctx = get_working_context()
            focus_lines = []
            for g in ctx.get("active_goals", [])[:3]:
                focus_lines.append(f"- GOAL: {g['title']} ({g['progress_pct']}% done)")
            for b in ctx.get("blocked", [])[:2]:
                focus_lines.append(f"- BLOCKED: {b['title']} (waiting on {b['blocked_by']})")
            for n in ctx.get("next_up", [])[:2]:
                focus_lines.append(f"- NEXT: {n['title']} -> {n['agent']}")
            for f_item in ctx.get("manual_focus", [])[:2]:
                focus_lines.append(f"- FOCUS: {f_item['content']}")
            if focus_lines:
                content = "\n".join(focus_lines)
                _cache_set(f"working_memory:{agent_name}", content)
                sections.append(
                    {"section": "Current Focus", "content": content, "source": "brain/working_memory"}
                )
        except Exception:
            pass

    # Recent session summaries — "what Chris was working on"
    try:
        from working_memory import get_session_summaries

        summaries = get_session_summaries(limit=5)
        if summaries:
            session_lines = []
            for s in summaries:
                ts = s.get("created_at", "")[:16].replace("T", " ")
                agent_tag = s.get("agent") or "claude"
                session_lines.append(f"- [{ts} {agent_tag}] {s['content']}")
            sections.append(
                {
                    "section": "Recent Sessions",
                    "content": "\n".join(session_lines),
                    "source": "brain/session_summaries",
                }
            )
    except Exception:
        pass

    # Recent OpenClaw agent conversations (last 24h) — cached 5min
    oc_cached = _cache_get("openclaw_recent_24h", DYNAMIC_TTL)
    if oc_cached is not None:
        sections.append(
            {
                "section": "Recent Agent Conversations (24h)",
                "content": oc_cached,
                "source": "raw/inbox/openclaw",
            }
        )
    else:
        try:
            recent_oc = get_recent_openclaw_distillations(hours=24, limit=5)
            if recent_oc:
                lines = []
                for rec in recent_oc:
                    ts = rec.get("timestamp", "")[:16].replace("T", " ")
                    src_ref = rec.get("source_ref", "")
                    oc_agent = src_ref.split(":")[1] if src_ref.startswith("openclaw:") else "unknown"
                    body = rec.get("content", "")
                    summary = ""
                    for bl in body.split("\n"):
                        bl = bl.strip()
                        if (
                            bl
                            and not bl.startswith("OpenClaw ")
                            and not bl.startswith("Signal:")
                            and not bl.startswith("Context ")
                        ):
                            summary = bl
                            break
                    if not summary:
                        summary = body[:200]
                    lines.append(f"- [{ts} {oc_agent}] {summary[:300]}")
                content = "\n".join(lines)
                _cache_set("openclaw_recent_24h", content)
                sections.append(
                    {
                        "section": "Recent Agent Conversations (24h)",
                        "content": content,
                        "source": "raw/inbox/openclaw",
                    }
                )
        except Exception:
            pass

    # Chris identity (immutable core) + state (mutable snapshot)
    identity = get_chris_identity()
    if identity:
        sections.append(
            {
                "section": "Chris Identity",
                "content": identity,
                "source": "canonical/chris/_identity.md",
            }
        )

    state = get_chris_state()
    if state:
        sections.append(
            {
                "section": "Chris State",
                "content": state,
                "source": "canonical/chris/_state.md",
            }
        )

    scratch = get_scratch_context(agent_name)
    if scratch:
        sections.append(
            {
                "section": "Active Task",
                "content": scratch,
                "source": "SCRATCH.md",
            }
        )

    # Agent-specific memory recall — "what did Chris tell ME last time"
    agent_memories = get_agent_memories(agent_name)
    if agent_memories:
        sections.append(
            {
                "section": f"Chris's feedback to {agent_name}",
                "content": agent_memories,
                "source": "semantic_memory",
            }
        )

    # Phase 6: Atoms due for review (SM-2 spaced repetition signal).
    # Surfaces top-3 atoms whose next_review_at has passed so the agent gets
    # a chance to reinforce/correct them in-session.
    try:
        from sm2 import review_due

        due = review_due(limit=3)
        if due:
            lines = []
            for d in due:
                preview = (d.get("text") or "")[:140].replace("\n", " ")
                lines.append(f"- [{d.get('tier')}] {preview}")
            sections.append(
                {
                    "section": "Atoms due for review (SM-2)",
                    "content": "\n".join(lines),
                    "source": "brain/atoms",
                }
            )
    except Exception:
        pass

    # Predictive Context (2026-04-17 A) — context-aware prefetch based on
    # current focus_items. Surfaces past atoms/decisions/lessons most likely
    # relevant to what Chris is currently doing. Complementary to the existing
    # temporal _predictive_queries at line 110 (which asks "what time is it")
    # — this asks "what is Chris focused on right now".
    pred_cached = _cache_get("predictive_top", DYNAMIC_TTL)
    if pred_cached is not None:
        sections.append(
            {
                "section": "Predictive Context (relevant to current focus)",
                "content": pred_cached,
                "source": "brain/predictive",
            }
        )
    else:
        try:
            from predictive import predict_relevant_context

            top = predict_relevant_context(limit=3)
            if top:
                lines = []
                for item in top:
                    title = item.get("title") or item.get("content", "")[:60]
                    reason = item.get("reason", "")
                    prio = item.get("priority", 0)
                    lines.append(f"[prio={prio:.1f} | {reason}] {title[:100]}")
                content = "\n".join(lines)
                _cache_set("predictive_top", content)
                sections.append(
                    {
                        "section": "Predictive Context (relevant to current focus)",
                        "content": content,
                        "source": "brain/predictive",
                    }
                )
        except Exception:
            pass

    # Attention priority queue (2026-04-17 D wiring) — biologically-inspired
    # priority-ordered insights. Replaces flat "Proactive Alerts" with the
    # attention_queue output (severity × novelty × valence). Each item shown
    # auto-bumps shown_count so habituation kicks in across sessions — the
    # same alert surfaces less aggressively after Chris has seen it 3x.
    # Fails open to the legacy proactive block.
    attn_cached = _cache_get("attention_top", DYNAMIC_TTL)
    if attn_cached is not None:
        sections.append(
            {"section": "Attention (top priorities)", "content": attn_cached, "source": "brain/attention"}
        )
    else:
        try:
            from attention import mark_shown, top_attention

            top_items = top_attention(limit=3)
            if top_items:
                lines = []
                for item in top_items:
                    sev = str(item.get("severity", "info")).upper()
                    summary = str(item.get("summary", ""))[:200]
                    prio = item.get("priority", 0)
                    lines.append(f"[{sev} prio={prio:.2f}] {summary}")
                    # Mark shown for habituation — fails open.
                    try:
                        mark_shown(item.get("id", ""))
                    except Exception:
                        pass
                content = "\n".join(lines)
                _cache_set("attention_top", content)
                sections.append(
                    {
                        "section": "Attention (top priorities)",
                        "content": content,
                        "source": "brain/attention",
                    }
                )
        except Exception:
            # Legacy fallback to proactive insights if attention module fails.
            try:
                from proactive import get_current_insights

                insights = get_current_insights(max_age_hours=12)
                active = [i for i in insights if not i.acted_on][:3]
                if active:
                    content = "\n".join(f"[{i.severity.upper()}] {i.summary}" for i in active)
                    sections.append(
                        {"section": "Proactive Alerts", "content": content, "source": "brain/proactive"}
                    )
            except Exception:
                pass

    # Pending messages for this agent
    msgs_cached = _cache_get(f"messages:{agent_name}", DYNAMIC_TTL)
    if msgs_cached is not None:
        sections.append(
            {
                "section": f"Pending Messages for {agent_name}",
                "content": msgs_cached,
                "source": "brain/messages",
            }
        )
    else:
        try:
            from agent_messenger import get_pending_messages

            msgs = get_pending_messages(agent_name, limit=5)
            if msgs:
                content = "\n".join(f"[{m['from_agent']}] {m['content'][:200]}" for m in msgs)
                _cache_set(f"messages:{agent_name}", content)
                sections.append(
                    {
                        "section": f"Pending Messages for {agent_name}",
                        "content": content,
                        "source": "brain/messages",
                    }
                )
        except Exception:
            pass

    search_key = f"search:{agent_name}:{limit}"
    search_cached = _cache_get(search_key, DYNAMIC_TTL)
    if search_cached is not None:
        try:
            for s in json.loads(search_cached):
                sections.append(s)
        except Exception:
            pass  # corrupted cache — fall through to fresh fetch
    else:
        from concurrent.futures import ThreadPoolExecutor, as_completed

        query_results = {}
        with ThreadPoolExecutor(max_workers=3) as executor:
            future_to_query = {executor.submit(search, q, limit): q for q in queries}
            for fut in as_completed(future_to_query):
                try:
                    query_results[future_to_query[fut]] = fut.result()
                except Exception:
                    query_results[future_to_query[fut]] = []

        search_sections = []
        for query in queries:
            for r in query_results.get(query, []):
                if r.get("score", 0) < 40:
                    continue
                source_key = r.get("path", r.get("title", ""))[:60]
                if source_key in seen_sources:
                    continue
                seen_sources.add(source_key)

                content = r.get("content", "").strip()
                if len(content) > 800:
                    content = content[:800] + "..."

                search_sections.append(
                    {
                        "section": f"{r.get('collection', 'unknown')}:{r.get('source_type', 'unknown')}",
                        "content": content,
                        "source": r.get("title", r.get("path", "")).replace(str(Path.home()) + "/", "~/"),
                        "score": r.get("score", 0),
                    }
                )

        if search_sections:
            _cache_set(search_key, json.dumps(search_sections, ensure_ascii=False))
        sections.extend(search_sections)

    # Intent-aware rerank: when the hook passed the user's prompt, reorder
    # the RAG-derived sections (the ones carrying a 'score') by cheap token
    # overlap with the prompt. Structural sections (Identity, Current Focus,
    # Recent Sessions, Attention, Predictive, Messages) keep their slot —
    # they're stable context blocks, not query-dependent retrieval hits.
    if prompt and sections:
        sections = _rerank_by_intent(sections, prompt)

    return sections[:15]


_INTENT_TOKEN_RE = re.compile(r"[a-z0-9가-힣]{2,}")


def _rerank_by_intent(sections: list[dict], prompt: str) -> list[dict]:
    """Reorder search-derived sections by token overlap with the prompt.

    Keeps the first N "structural" sections in place (Current Focus, Recent
    Sessions, Chris Identity/State, Active Task, Agent Memories, Atoms due,
    Predictive, Attention, Pending Messages — everything ahead of the RAG
    search expansion). Only the retrieval-result sections (those with a
    'score' key) get reranked against the prompt.
    """
    tokens_p = set(_INTENT_TOKEN_RE.findall((prompt or "").lower()))
    if not tokens_p:
        return sections

    structural: list[dict] = []
    scored: list[tuple[float, int, dict]] = []
    for i, s in enumerate(sections):
        if "score" in s:
            body = (s.get("content") or "")[:800].lower() + " " + (s.get("source") or "").lower()
            tokens_s = set(_INTENT_TOKEN_RE.findall(body))
            overlap = len(tokens_p & tokens_s) / max(len(tokens_p | tokens_s), 1)
            # Blend prior score with overlap so strong RAG hits aren't
            # ignored when the prompt doesn't overlap vocabulary exactly.
            prior = float(s.get("score", 0) or 0) / 100.0
            intent_score = overlap * 0.7 + min(prior, 1.0) * 0.3
            scored.append((intent_score, i, s))
        else:
            structural.append(s)
    scored.sort(key=lambda t: (-t[0], t[1]))
    # Drop retrieval hits with near-zero intent overlap when we have enough
    # signal from structural + top-ranked hits. Prevents old employment
    # records and predictive fragments from filling slots 13-15.
    kept_scored = [s for (score, _, s) in scored if score > 0.02]
    if len(structural) + len(kept_scored) < 6:
        kept_scored = [s for (_, _, s) in scored]
    return structural + kept_scored


def log_boot(agent_name, queries, sections):
    try:
        BOOT_LOG.parent.mkdir(parents=True, exist_ok=True)
        scores = [s.get("score", 0) for s in sections if "score" in s]
        entry = {
            "timestamp": datetime.now(UTC).isoformat(),
            "agent": agent_name,
            "queries": queries,
            "results_count": len(sections),
            "avg_score": round(sum(scores) / len(scores), 2) if scores else 0,
            "total_tokens": sum(len(s.get("content", "").split()) for s in sections),
            "sources": [s.get("source", "") for s in sections],
        }
        with open(BOOT_LOG, "a") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


def format_boot_context(agent_name, sections):
    lines = []
    lines.append(
        f"[Unified Boot Context] Agent: {agent_name} | {datetime.now(UTC).strftime('%Y-%m-%d %H:%M')} UTC"
    )
    lines.append(f"Loaded {len(sections)} context blocks (sources: rag, canonical, obsidian)")
    lines.append("")

    for s in sections:
        source = s.get("source", "")
        score_str = f" (score: {s['score']:.1f})" if "score" in s else ""
        lines.append(f"### {s['section']} — {source}{score_str}")
        lines.append(s["content"])
        lines.append("")

    if not sections:
        lines.append("No relevant boot context found. Starting fresh.")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Unified Boot Context Loader")
    parser.add_argument("agent", help="Agent name (ellie, jenna, liz, sage, market)")
    parser.add_argument("-n", "--limit", type=int, default=3, help="Results per query")
    parser.add_argument("--json", action="store_true", help="JSON output")
    parser.add_argument(
        "--prompt",
        default=None,
        help="Current user prompt — used to rerank RAG sections by intent",
    )
    args = parser.parse_args()

    agent = args.agent.lower()
    queries = AGENT_QUERIES.get(agent, DEFAULT_QUERIES)
    sections = build_boot_context(agent, args.limit, prompt=args.prompt)

    log_boot(agent, queries, sections)

    if args.json:
        print(json.dumps(sections, indent=2, ensure_ascii=False))
    else:
        print(format_boot_context(agent, sections))


if __name__ == "__main__":
    main()
