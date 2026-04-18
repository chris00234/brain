"""brain_core/reasoning_loop.py — Multi-hop reasoning with LangGraph-style checkpoints.

Takes a complex question, runs 3-5 reasoning hops via Jenna with retrieved
evidence at each step. Each step is checkpointed to SQLite so the reasoning
can resume after failure.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

# brain_core is already on sys.path when loaded by server.py, but ensure it
# resolves when imported standalone (e.g. from a CLI smoke test).
sys.path.insert(0, str(Path(__file__).resolve().parent))

import search_unified
from cli_llm import dispatch  # 2026-04-17: migrated from openclaw_dispatch

CHECKPOINT_DB = Path("/Users/chrischo/server/brain/logs/reasoning_checkpoints.db")
MAX_HOPS = 5
DEFAULT_TIMEOUT = 120

PLANNING_PROMPT = """You are the reasoning module of Chris's brain. Given a question, decide the next search to run.

Question: {question}

Evidence so far:
{evidence}

Respond with strict JSON: {{"next_action": "search" | "synthesize", "query": "...", "reason": "..."}}
- "search" = run another search query to gather more evidence
- "synthesize" = enough evidence; produce the final answer
"""

SYNTHESIS_PROMPT = """You are the reasoning module of Chris's brain. Synthesize a final answer from the evidence.

Question: {question}

Evidence:
{evidence}

Respond with strict JSON: {{"answer": "...", "confidence": 0.0-1.0, "citations": ["<source1>", "..."]}}
"""


def _connect():
    CHECKPOINT_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(CHECKPOINT_DB))
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _init_db():
    conn = _connect()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS reasoning_checkpoints (
                thread_id TEXT NOT NULL,
                step INTEGER NOT NULL,
                state_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (thread_id, step)
            )
        """)
        conn.commit()
    finally:
        conn.close()


def _save_checkpoint(thread_id: str, step: int, state: dict) -> None:
    _init_db()
    conn = _connect()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO reasoning_checkpoints (thread_id, step, state_json, created_at) VALUES (?, ?, ?, ?)",
            (thread_id, step, json.dumps(state), datetime.now(UTC).isoformat()),
        )
        conn.commit()
    finally:
        conn.close()


def _load_checkpoints(thread_id: str) -> list[dict]:
    _init_db()
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT step, state_json FROM reasoning_checkpoints WHERE thread_id = ? ORDER BY step",
            (thread_id,),
        ).fetchall()
        return [json.loads(r[1]) for r in rows]
    finally:
        conn.close()


def _strip_json_fence(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = t.split("```", 2)[1]
        if t.startswith("json"):
            t = t[4:]
        if "```" in t:
            t = t.split("```", 1)[0]
    return t.strip()


def _format_evidence(evidence_items: list[dict]) -> str:
    """Format evidence for the LLM prompt. Truncates at 800 chars per item
    (up from 300) to preserve more context for reasoning. Caps at 10 items
    to prevent prompt bloat on long reasoning chains.
    """
    if not evidence_items:
        return "(no evidence yet)"
    lines = []
    for i, e in enumerate(evidence_items[:10], 1):
        title = e.get("title", "") or e.get("path", "")
        content = (e.get("content", "") or "")[:800]
        lines.append(f"[{i}] {title}: {content}")
    if len(evidence_items) > 10:
        lines.append(f"... and {len(evidence_items) - 10} more items")
    return "\n".join(lines)


def run_reasoning(question: str, thread_id: str | None = None, max_hops: int = MAX_HOPS) -> dict:
    """Run a multi-hop reasoning loop. Returns synthesis + all checkpoints.

    If thread_id is provided and exists, resumes from last checkpoint.

    Note: query embedding reuse across hops is handled implicitly by
    ``brain_core.search._embed_mem_cache`` (MD5-hashed on the prompted text).
    Re-searches for the same question or variant within a hop sequence hit the
    in-memory + SQLite embedding cache — no explicit plumbing needed here.
    """
    # Phase 5 autonomy gate: reasoning_loop is L2 by default
    try:
        from autonomy import authorize as _autonomy_authorize

        gate = _autonomy_authorize("reasoning.multihop", context={"question_preview": question[:120]})
        if not gate.allowed:
            return {
                "thread_id": thread_id or "blocked",
                "blocked": True,
                "reason": gate.reason,
                "level": gate.level,
            }
    except Exception:
        pass

    if thread_id is None:
        thread_id = f"reason_{uuid.uuid4().hex[:12]}"

    # Load existing checkpoints
    checkpoints = _load_checkpoints(thread_id)
    evidence: list[dict] = []
    step = 0

    # Phase 5 M1 fix: on resume, if the last checkpoint was a failure
    # (plan_failed, plan_parse_failed, synth_failed), RETRY that hop instead
    # of skipping past it. The last-completed step is the max step with a
    # successful step_type ("init", "search", or "synthesize").
    FAILURE_STEP_TYPES = {"plan_failed", "plan_parse_failed", "synth_failed"}
    for cp in checkpoints:
        cp_step = cp.get("step", 0)
        cp_type = cp.get("step_type", "")
        if cp_type == "search":
            evidence.extend(cp.get("results", []))
        # Only advance step past SUCCESSFUL checkpoints — resume retries the failed one
        if cp_type not in FAILURE_STEP_TYPES:
            step = max(step, cp_step)

    if step == 0 and not any(cp.get("step_type") == "init" for cp in checkpoints):
        # Fresh start — save initial state
        _save_checkpoint(thread_id, 0, {"step": 0, "step_type": "init", "question": question})

    started_at = time.time()
    final_answer = None

    # Hops 1..max_hops are plan+search; synthesis happens when Jenna says so
    # OR when we've exhausted max_hops. Range goes to max_hops+1 (exclusive)
    # so the last iteration is hop = max_hops where we force synthesis.
    for hop in range(step + 1, max_hops + 1):
        if time.time() - started_at > DEFAULT_TIMEOUT:
            break

        is_last_hop = hop == max_hops

        # Ask Jenna: search more or synthesize? On last hop, skip planning and go straight to synthesis.
        if not is_last_hop:
            plan_prompt = PLANNING_PROMPT.format(question=question, evidence=_format_evidence(evidence))
            plan_result = dispatch(agent="jenna", message=plan_prompt, thinking="low", timeout=45)
            if not plan_result.ok:
                _save_checkpoint(
                    thread_id, hop, {"step": hop, "step_type": "plan_failed", "error": plan_result.error}
                )
                break

            try:
                plan = json.loads(_strip_json_fence(plan_result.text))
            except json.JSONDecodeError as e:
                _save_checkpoint(
                    thread_id,
                    hop,
                    {
                        "step": hop,
                        "step_type": "plan_parse_failed",
                        "error": str(e),
                        "raw": plan_result.text[:500],
                    },
                )
                break

            action = plan.get("next_action", "synthesize")

            if action == "search":
                query = plan.get("query", question)
                try:
                    search_result = search_unified.search_all(
                        query, limit=5, sources=["rag", "canonical", "obsidian"]
                    )
                    results = search_result.get("results", [])[:5]
                except Exception:
                    results = []

                evidence.extend(results)
                _save_checkpoint(
                    thread_id,
                    hop,
                    {"step": hop, "step_type": "search", "query": query, "results": results[:5]},
                )
                continue

        # Synthesize (either Jenna said so, or this is the last hop)
        synth_prompt = SYNTHESIS_PROMPT.format(question=question, evidence=_format_evidence(evidence))
        synth_result = dispatch(agent="jenna", message=synth_prompt, thinking="low", timeout=60)
        if not synth_result.ok:
            _save_checkpoint(
                thread_id, hop, {"step": hop, "step_type": "synth_failed", "error": synth_result.error}
            )
            break

        try:
            final_answer = json.loads(_strip_json_fence(synth_result.text))
        except json.JSONDecodeError:
            final_answer = {"answer": synth_result.text, "confidence": 0.5, "citations": []}

        _save_checkpoint(thread_id, hop, {"step": hop, "step_type": "synthesize", "answer": final_answer})
        break

    return {
        "thread_id": thread_id,
        "question": question,
        "answer": (final_answer or {}).get("answer", ""),
        "confidence": (final_answer or {}).get("confidence", 0),
        "citations": (final_answer or {}).get("citations", []),
        "evidence_count": len(evidence),
        "steps": _load_checkpoints(thread_id),
        "duration_ms": int((time.time() - started_at) * 1000),
    }


def resume_reasoning(thread_id: str) -> dict:
    """Resume a reasoning thread from its last checkpoint."""
    checkpoints = _load_checkpoints(thread_id)
    if not checkpoints:
        raise ValueError(f"thread {thread_id} not found")
    # Find the original question from step 0
    initial = next((cp for cp in checkpoints if cp.get("step_type") == "init"), None)
    if not initial:
        raise ValueError(f"thread {thread_id} has no initial state")
    return run_reasoning(initial["question"], thread_id=thread_id)
