#!/opt/homebrew/bin/python3
"""RAG Boot Context — load relevant context for agent startup via unified search.

Usage:
  boot_context.py <agent_name> [--limit N] [--json]

Returns the most relevant context snippets for the agent's role,
searched across ChromaDB, canonical knowledge, and Obsidian vault.
"""

import json
import subprocess
import sys
import argparse
from pathlib import Path
from datetime import datetime


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
    from config import BRAIN_LOGS_DIR, KNOWLEDGE_DIR, OPENCLAW_DIR
    BOOT_LOG = BRAIN_LOGS_DIR / 'boot-context-log.jsonl'
    CHRIS_IDENTITY = KNOWLEDGE_DIR / 'canonical' / 'chris' / '_identity.md'
    CHRIS_STATE = KNOWLEDGE_DIR / 'canonical' / 'chris' / '_state.md'
except ImportError:
    BOOT_LOG = Path('/Users/chrischo/server/brain/logs/boot-context-log.jsonl')
    CHRIS_IDENTITY = Path('/Users/chrischo/server/knowledge/canonical/chris/_identity.md')
    CHRIS_STATE = Path('/Users/chrischo/server/knowledge/canonical/chris/_state.md')
    OPENCLAW_DIR = Path('/Users/chrischo/.openclaw')


sys.path.insert(0, str(Path(__file__).parent))
try:
    import search_unified as _search_mod
except ImportError:
    _search_mod = None


def search(query, limit=3):
    """In-process search via search_unified (no subprocess overhead)."""
    if _search_mod is None:
        return []
    try:
        payload = _search_mod.search_all(query, limit)
        return payload.get("results", [])
    except Exception as e:
        import logging
        logging.getLogger("brain.boot_context").warning("search failed for %r: %s", query[:50], e)
        return []


def get_scratch_context(agent_name):
    scratch = OPENCLAW_DIR / f'workspace-{agent_name}' / 'SCRATCH.md'
    if scratch.exists():
        content = scratch.read_text().strip()
        if content and len(content) > 50:
            return content[:500]
    return None


def _read_stripped_body(path: Path):
    """Read a canonical note and strip the JSON frontmatter, returning the body or None."""
    if not path.exists():
        return None
    try:
        text = path.read_text()
        if text.startswith('---'):
            parts = text.split('---', 2)
            if len(parts) >= 3:
                body = parts[2].strip()
                return body if body else None
        return text.strip()
    except Exception:
        return None


def get_chris_identity():
    """Immutable Chris identity — name, location, hard rules, values. Loaded every boot."""
    return _read_stripped_body(CHRIS_IDENTITY)


def get_chris_state():
    """Mutable Chris state — projects, tools, focus, recent signals. Regenerated weekly."""
    return _read_stripped_body(CHRIS_STATE)


def _recency_score(created_at: str) -> float:
    if not created_at:
        return 0.3
    try:
        from datetime import datetime, timezone
        dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        age_days = (datetime.now(timezone.utc) - dt).total_seconds() / 86400
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
    try:
        from indexer import chroma_api, _get_collection_id
        col_id = _get_collection_id("semantic_memory")
        if not col_id:
            return ""
        resp = chroma_api("POST",
            f"/api/v2/tenants/default_tenant/databases/default_database/collections/{col_id}/get",
            {"limit": 200, "include": ["documents", "metadatas"],
             "where": {"agent": {"$eq": agent_name}}})
        ids = resp.get("ids", [])
        docs = resp.get("documents", [])
        metas = resp.get("metadatas", [])
        if not docs:
            return ""

        entries = []
        for i, doc in enumerate(docs):
            meta = metas[i] if i < len(metas) else {}
            entries.append({
                "id": ids[i] if i < len(ids) else None,
                "doc": doc,
                "meta": meta,
                "created_at": meta.get("created_at", ""),
            })

        try:
            from neo4j_client import run_query
            id_list = [e["id"] for e in entries if e.get("id")]
            if id_list:
                score_rows = run_query(
                    "UNWIND $ids AS mid OPTIONAL MATCH (m:MemoryAccess {memory_id: mid}) "
                    "RETURN mid, coalesce(m.utility_score, 0.5) AS score",
                    {"ids": id_list}
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
        return "\n".join(lines)
    except Exception:
        return ""


def build_boot_context(agent_name, limit=3):
    queries = AGENT_QUERIES.get(agent_name, DEFAULT_QUERIES)
    sections = []
    seen_sources = set()

    # Working memory — current focus, goals, blocked items
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
            sections.append({
                "section": "Current Focus",
                "content": "\n".join(focus_lines),
                "source": "brain/working_memory",
            })
    except Exception:
        pass

    # Chris identity (immutable core) + state (mutable snapshot)
    identity = get_chris_identity()
    if identity:
        sections.append({
            "section": "Chris Identity",
            "content": identity,
            "source": "canonical/chris/_identity.md",
        })

    state = get_chris_state()
    if state:
        sections.append({
            "section": "Chris State",
            "content": state,
            "source": "canonical/chris/_state.md",
        })

    scratch = get_scratch_context(agent_name)
    if scratch:
        sections.append({
            "section": "Active Task",
            "content": scratch,
            "source": "SCRATCH.md",
        })

    # Agent-specific memory recall — "what did Chris tell ME last time"
    agent_memories = get_agent_memories(agent_name)
    if agent_memories:
        sections.append({
            "section": f"Chris's feedback to {agent_name}",
            "content": agent_memories,
            "source": "semantic_memory",
        })

    # Proactive alerts — things the brain noticed that agents should know
    try:
        from proactive import get_current_insights
        insights = get_current_insights(max_age_hours=12)
        active = [i for i in insights if not i.acted_on][:3]
        if active:
            alert_lines = [f"[{i.severity.upper()}] {i.summary}" for i in active]
            sections.append({
                "section": "Proactive Alerts",
                "content": "\n".join(alert_lines),
                "source": "brain/proactive",
            })
    except Exception:
        pass

    # Pending messages for this agent
    try:
        from agent_messenger import get_pending_messages
        msgs = get_pending_messages(agent_name, limit=5)
        if msgs:
            msg_lines = [f"[{m['from_agent']}] {m['content'][:200]}" for m in msgs]
            sections.append({
                "section": f"Pending Messages for {agent_name}",
                "content": "\n".join(msg_lines),
                "source": "brain/messages",
            })
    except Exception:
        pass

    from concurrent.futures import ThreadPoolExecutor, as_completed
    query_results = {}
    with ThreadPoolExecutor(max_workers=3) as executor:
        future_to_query = {executor.submit(search, q, limit): q for q in queries}
        for fut in as_completed(future_to_query):
            try:
                query_results[future_to_query[fut]] = fut.result()
            except Exception:
                query_results[future_to_query[fut]] = []

    for query in queries:
        for r in query_results.get(query, []):
            if r.get('score', 0) < 40:
                continue
            source_key = r.get('path', r.get('title', ''))[:60]
            if source_key in seen_sources:
                continue
            seen_sources.add(source_key)

            content = r.get('content', '').strip()
            if len(content) > 800:
                content = content[:800] + "..."

            sections.append({
                "section": f"{r.get('collection', 'unknown')}:{r.get('source_type', 'unknown')}",
                "content": content,
                "source": r.get('title', r.get('path', '')).replace(str(Path.home()) + '/', '~/'),
                "score": r.get('score', 0),
            })

    return sections[:14]


def log_boot(agent_name, queries, sections):
    try:
        BOOT_LOG.parent.mkdir(parents=True, exist_ok=True)
        scores = [s.get('score', 0) for s in sections if 'score' in s]
        entry = {
            "timestamp": datetime.now().isoformat(),
            "agent": agent_name,
            "queries": queries,
            "results_count": len(sections),
            "avg_score": round(sum(scores) / len(scores), 2) if scores else 0,
            "total_tokens": sum(len(s.get('content', '').split()) for s in sections),
            "sources": [s.get('source', '') for s in sections],
        }
        with open(BOOT_LOG, 'a') as f:
            f.write(json.dumps(entry, ensure_ascii=False) + '\n')
    except Exception:
        pass


def format_boot_context(agent_name, sections):
    lines = []
    lines.append(f"[Unified Boot Context] Agent: {agent_name} | {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    lines.append(f"Loaded {len(sections)} context blocks (sources: rag, canonical, obsidian)")
    lines.append("")

    for s in sections:
        source = s.get('source', '')
        score_str = f" (score: {s['score']:.1f})" if 'score' in s else ""
        lines.append(f"### {s['section']} — {source}{score_str}")
        lines.append(s['content'])
        lines.append("")

    if not sections:
        lines.append("No relevant boot context found. Starting fresh.")

    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(description="Unified Boot Context Loader")
    parser.add_argument("agent", help="Agent name (ellie, jenna, liz, sage, market)")
    parser.add_argument("-n", "--limit", type=int, default=3, help="Results per query")
    parser.add_argument("--json", action="store_true", help="JSON output")
    args = parser.parse_args()

    agent = args.agent.lower()
    queries = AGENT_QUERIES.get(agent, DEFAULT_QUERIES)
    sections = build_boot_context(agent, args.limit)

    log_boot(agent, queries, sections)

    if args.json:
        print(json.dumps(sections, indent=2, ensure_ascii=False))
    else:
        print(format_boot_context(agent, sections))


if __name__ == '__main__':
    main()
