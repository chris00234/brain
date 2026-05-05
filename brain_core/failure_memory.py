"""brain_core/failure_memory.py — Reflexion-style failure learning.

When an agent task fails, dispatch to Jenna for reflection. Store as LESSON node
in Neo4j. Before running similar tasks, query recent lessons and inject into prompt.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime, timedelta

from cli_llm import cli_dispatch

log = logging.getLogger("brain.failure_memory")

REFLECTION_PROMPT = """A task failed. Generate a concise reflection (2-3 sentences) that captures:
1. Why it likely failed
2. What to try next time
3. What to avoid

Task: {task}
Error: {error}
Context: {context}

Respond with strict JSON: {{"reflection": "...", "avoid": "...", "try_next": "..."}}
"""


def _lessons_schema_available(run_query) -> bool:
    """Avoid noisy Neo4j warnings when the optional Lesson graph is absent."""
    try:
        label_rows = run_query("CALL db.labels() YIELD label RETURN collect(label) AS labels", {})
        labels = set(label_rows[0].get("labels") or []) if label_rows else set()
        if "Agent" not in labels or "Lesson" not in labels:
            return False
        rel_rows = run_query(
            "CALL db.relationshipTypes() YIELD relationshipType "
            "RETURN collect(relationshipType) AS rels",
            {},
        )
        rels = set(rel_rows[0].get("rels") or []) if rel_rows else set()
        return "HAS_LESSON" in rels
    except Exception:
        return True


def record_failure_lesson(
    task_description: str,
    failure_reason: str,
    agent_id: str = "system",
    context: str = "",
) -> str | None:
    """Record a task failure as a LESSON node in Neo4j via MemRL pattern.

    Returns lesson_id if recorded, None on failure.
    """
    # Generate reflection via Jenna
    prompt = REFLECTION_PROMPT.format(
        task=task_description[:500],
        error=failure_reason[:500],
        context=context[:500],
    )
    # 2026-04-17: migrated from openclaw dispatch (95MB session replay) to cli_dispatch
    result = cli_dispatch(prompt, backend="codex", timeout=45)
    if not result.ok:
        log.warning("failed to generate reflection: %s", result.error)
        return None

    # Parse JSON response
    text = result.text.strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        if text.startswith("json"):
            text = text[4:]
        if "```" in text:
            text = text.split("```", 1)[0]

    try:
        parsed = json.loads(text.strip())
    except json.JSONDecodeError:
        parsed = {"reflection": text, "avoid": "", "try_next": ""}

    # Lesson ID is stable across retries — hash of task + agent only, no timestamp.
    # This lets MERGE (l:Lesson {id: $id}) properly deduplicate repeated failures.
    lesson_id = "lesson_" + hashlib.md5(f"{task_description}:{agent_id}".encode()).hexdigest()[:12]

    now_iso = datetime.now(UTC).isoformat()

    # Store in Neo4j — MERGE semantics deduplicate repeated failures.
    # ON MATCH bumps failure_count + last_seen_at; ON CREATE sets initial fields.
    try:
        from neo4j_client import run_write

        run_write(
            "MERGE (l:Lesson {id: $id}) "
            "ON CREATE SET l.task = $task, l.failure_reason = $reason, "
            "  l.reflection = $reflection, l.avoid = $avoid, l.try_next = $try_next, "
            "  l.agent_id = $agent_id, l.created_at = $created_at, l.last_seen_at = $created_at, "
            "  l.failure_count = 1, l.archived = false "
            "ON MATCH SET l.last_seen_at = $created_at, "
            "  l.failure_count = coalesce(l.failure_count, 1) + 1, "
            "  l.reflection = $reflection, l.failure_reason = $reason "
            "WITH l "
            "MERGE (a:Agent {name: $agent_id}) "
            "MERGE (a)-[:HAS_LESSON]->(l)",
            {
                "id": lesson_id,
                "task": task_description[:500],
                "reason": failure_reason[:500],
                "reflection": parsed.get("reflection", "")[:500],
                "avoid": parsed.get("avoid", "")[:200],
                "try_next": parsed.get("try_next", "")[:200],
                "agent_id": agent_id,
                "created_at": now_iso,
            },
        )
        log.info("recorded lesson %s for agent %s", lesson_id, agent_id)
        return lesson_id
    except Exception as e:
        log.warning("neo4j write failed: %s", e)
        return None


def get_similar_lessons(task_description: str, agent_id: str = "system", limit: int = 3) -> list[dict]:
    """Query Neo4j for lessons similar to the given task.

    Empty task_description → returns most recent lessons for the agent (list-all mode).
    Non-empty → tries Jaro-Winkler similarity via APOC, falls back to CONTAINS match.
    """
    try:
        from neo4j_client import run_query

        if not _lessons_schema_available(run_query):
            return []

        # List-all mode: empty task_description returns recent lessons
        if not task_description or not task_description.strip():
            return run_query(
                "MATCH (a:Agent {name: $agent})-[:HAS_LESSON]->(l:Lesson) "
                "WHERE l.archived = false "
                "RETURN l.id AS id, l.task AS task, l.reflection AS reflection, "
                "  l.avoid AS avoid, l.try_next AS try_next, l.created_at AS created_at, "
                "  coalesce(l.failure_count, 1) AS failure_count "
                "ORDER BY l.created_at DESC LIMIT $limit",
                {"agent": agent_id, "limit": limit},
            )

        # Try APOC similarity first
        try:
            rows = run_query(
                "MATCH (a:Agent {name: $agent})-[:HAS_LESSON]->(l:Lesson) "
                "WHERE l.archived = false "
                "WITH l, apoc.text.jaroWinklerDistance(toLower(l.task), toLower($task)) AS sim "
                "WHERE sim >= 0.7 "
                "RETURN l.id AS id, l.task AS task, l.reflection AS reflection, "
                "  l.avoid AS avoid, l.try_next AS try_next, sim "
                "ORDER BY sim DESC LIMIT $limit",
                {"agent": agent_id, "task": task_description, "limit": limit},
            )
            if rows:
                return rows
        except Exception:
            pass

        # Fallback: substring containment
        task_words = [w for w in task_description.lower().split() if len(w) > 3][:5]
        if not task_words:
            return []

        rows = run_query(
            "MATCH (a:Agent {name: $agent})-[:HAS_LESSON]->(l:Lesson) "
            "WHERE l.archived = false "
            "  AND ANY(w IN $words WHERE toLower(l.task) CONTAINS w) "
            "RETURN l.id AS id, l.task AS task, l.reflection AS reflection, "
            "  l.avoid AS avoid, l.try_next AS try_next "
            "ORDER BY l.created_at DESC LIMIT $limit",
            {"agent": agent_id, "words": task_words, "limit": limit},
        )
        return rows
    except Exception as e:
        log.debug("lesson query failed: %s", e)
        return []


def archive_old_lessons(days: int = 365) -> int:
    """Archive lessons older than N days (default 1 year). Returns count archived."""
    try:
        from neo4j_client import run_query, run_write

        cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        # Count first (read-only), then archive (write)
        count_rows = run_query(
            "MATCH (l:Lesson) WHERE l.created_at < $cutoff AND l.archived = false "
            "RETURN count(l) AS count",
            {"cutoff": cutoff},
        )
        count = count_rows[0]["count"] if count_rows else 0
        if count > 0:
            run_write(
                "MATCH (l:Lesson) WHERE l.created_at < $cutoff AND l.archived = false "
                "SET l.archived = true",
                {"cutoff": cutoff},
            )
        return count
    except Exception:
        return 0


if __name__ == "__main__":
    # Smoke test
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "test":
        lesson_id = record_failure_lesson(
            "Test task for failure memory",
            "Intentional test failure",
            agent_id="system",
            context="smoke test",
        )
        print(f"Recorded: {lesson_id}")
        similar = get_similar_lessons("Test task", agent_id="system")
        print(f"Similar: {len(similar)}")
