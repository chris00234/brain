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


_INFRA_REFLECTION = {
    "backend_cooldown": (
        "Backend cooldown — the LLM provider was rate-limited or in an outage window when this task tried to dispatch.",
        "Do not retry inside the cooldown window; respect backoff and let the breaker close.",
        "Wait for cooldown to expire, then re-dispatch with jitter; consider a different backend if cooldown is recurring.",
    ),
    "timeout": (
        "Backend timeout — the LLM call exceeded its wall-clock budget before producing output.",
        "Long-running prompts on a stressed backend; do not loop without backoff.",
        "Increase timeout only if measurements support it; otherwise shorten the prompt or shed load.",
    ),
    "rate_limit": (
        "Rate limit — provider throttled the request, usually subscription/tenant level.",
        "Retrying immediately just burns the cooldown; tighten dispatch concurrency.",
        "Apply exponential backoff with jitter; lower per-minute concurrency for the affected agent.",
    ),
    "gateway": (
        "Gateway transport error — the local CLI process or relay closed mid-call.",
        "Treating gateway closures as task-level bugs; they are infra.",
        "Restart or reconnect the gateway; verify CLI auth before re-dispatching.",
    ),
    "breaker": (
        "Circuit breaker open — repeated failures triggered the local breaker.",
        "Forcing dispatch while the breaker is open hides the underlying failure.",
        "Identify what tripped the breaker (look at backend stderr / cooldown reasons), then half-open with a probe.",
    ),
    "generic_infra": (
        "Infrastructure-class failure — backend/transport/throttle, not a task-design issue.",
        "Reflecting via the same backend while it is unhealthy.",
        "Wait for backend health to recover; adjust queue concurrency before retrying.",
    ),
    "harness_unregistered": (
        "Agent harness or agent id is not registered with the dispatch gateway.",
        "Retrying the same dispatch will fail identically; the LLM-based recorder also cannot reach the missing harness, so reflecting via LLM deadlocks.",
        "Register the missing harness/agent in OpenClaw config (`openclaw agents list` to inspect), or route the task to an agent whose harness exists. Re-dispatch only after registration is confirmed.",
    ),
}


def _classify_infra_error(failure_reason: str) -> str:
    err = (failure_reason or "").lower()
    if "backend_cooldown" in err:
        return "backend_cooldown"
    if "timeout" in err:
        return "timeout"
    if "rate" in err and "limit" in err:
        return "rate_limit"
    if "not registered" in err or "unknown agent" in err:
        return "harness_unregistered"
    if "gateway" in err or "transporterror" in err:
        return "gateway"
    if err.startswith("breaker_") or "circuit breaker" in err:
        return "breaker"
    return "generic_infra"


def record_infra_failure_lesson(
    task_description: str,
    failure_reason: str,
    agent_id: str = "system",
) -> str | None:
    """Record an infra-class failure lesson without an LLM round-trip.

    Backend cooldowns, timeouts, rate limits, and gateway errors are not
    task-design failures — reflecting on them via the same throttled backend
    deadlocks the lesson recorder. Use a deterministic reflection keyed by
    error class, with the same Neo4j MERGE so repeated failures dedupe.
    """
    error_class = _classify_infra_error(failure_reason)
    reflection, avoid, try_next = _INFRA_REFLECTION[error_class]

    # Lesson ID keyed by (agent, error_class) so 1000 backend_cooldowns collapse
    # to one Lesson row per agent — failure_count grows on MATCH.
    lesson_id = "lesson_infra_" + hashlib.md5(f"{agent_id}:{error_class}".encode()).hexdigest()[:12]
    now_iso = datetime.now(UTC).isoformat()

    try:
        from neo4j_client import run_write

        run_write(
            "MERGE (l:Lesson {id: $id}) "
            "ON CREATE SET l.task = $task, l.failure_reason = $reason, "
            "  l.reflection = $reflection, l.avoid = $avoid, l.try_next = $try_next, "
            "  l.agent_id = $agent_id, l.error_class = $error_class, l.kind = 'infra', "
            "  l.created_at = $created_at, l.last_seen_at = $created_at, "
            "  l.failure_count = 1, l.archived = false "
            "ON MATCH SET l.last_seen_at = $created_at, "
            "  l.failure_count = coalesce(l.failure_count, 1) + 1, "
            "  l.failure_reason = $reason "
            "WITH l "
            "MERGE (a:Agent {name: $agent_id}) "
            "MERGE (a)-[:HAS_LESSON]->(l)",
            {
                "id": lesson_id,
                "task": task_description[:500],
                "reason": failure_reason[:500],
                "reflection": reflection,
                "avoid": avoid,
                "try_next": try_next,
                "agent_id": agent_id,
                "error_class": error_class,
                "created_at": now_iso,
            },
        )
        return lesson_id
    except Exception as e:
        log.warning("neo4j infra-lesson write failed: %s", e)
        return None


def record_override_pattern_lesson(
    *,
    signature: str,
    domain: str,
    overrides: int,
    sample_brain_recommendation: str,
    sample_corrections: list[str],
    agent_id: str = "system",
) -> str | None:
    """Mint a deterministic LESSON when Chris keeps overriding the same way.

    Outcome feedback already groups repeat overrides into a stable signature.
    When a signature carries >=3 overrides at high rate the mistake is a
    pattern, not an isolated miss, and the pretool nudge hook needs a
    Lesson node to surface before the next similar action. This recorder
    has no LLM dependency — the brain_recommendation and corrections are
    already authoritative text from the outcomes table.
    """
    lesson_id = "lesson_override_" + hashlib.md5(
        f"{signature}:{agent_id}".encode()
    ).hexdigest()[:12]
    now_iso = datetime.now(UTC).isoformat()

    correction = (sample_corrections[0] if sample_corrections else "").strip()
    wrong = (sample_brain_recommendation or "").strip()
    reflection = (
        f"Override pattern in {domain}: Chris overrode the same recommendation "
        f"{overrides} times. The brain's recommendation kept missing in the same "
        "direction."
    )[:500]
    avoid = (
        f"Do not repeat the recommendation: {wrong[:160]}" if wrong else
        f"Do not repeat the {domain} recommendation Chris overrode {overrides}x"
    )[:200]
    try_next = (
        f"Chris's preferred path: {correction[:160]}" if correction else
        "Re-read Chris's last correction for this domain before recommending"
    )[:200]
    task_description = f"{domain}: avoid {avoid[:120]} | prefer {try_next[:120]}"

    try:
        from neo4j_client import run_write

        run_write(
            "MERGE (l:Lesson {id: $id}) "
            "ON CREATE SET l.task = $task, l.failure_reason = $reason, "
            "  l.reflection = $reflection, l.avoid = $avoid, l.try_next = $try_next, "
            "  l.agent_id = $agent_id, l.kind = 'override_pattern', "
            "  l.domain = $domain, l.signature = $signature, "
            "  l.created_at = $created_at, l.last_seen_at = $created_at, "
            "  l.failure_count = $overrides, l.archived = false "
            "ON MATCH SET l.last_seen_at = $created_at, "
            "  l.failure_count = $overrides, "
            "  l.reflection = $reflection, l.avoid = $avoid, l.try_next = $try_next "
            "WITH l "
            "MERGE (a:Agent {name: $agent_id}) "
            "MERGE (a)-[:HAS_LESSON]->(l)",
            {
                "id": lesson_id,
                "task": task_description[:500],
                "reason": f"chris_override repeated {overrides}x"[:200],
                "reflection": reflection,
                "avoid": avoid,
                "try_next": try_next,
                "agent_id": agent_id,
                "domain": domain,
                "signature": signature,
                "overrides": overrides,
                "created_at": now_iso,
            },
        )
        return lesson_id
    except Exception as e:
        log.warning("neo4j override-lesson write failed: %s", e)
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
