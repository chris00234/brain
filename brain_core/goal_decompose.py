"""brain_core/goal_decompose.py — decompose high-level goals into ordered subtasks.

Given a goal ID, uses Sage (via reason_deep) to break it into concrete,
agent-assignable subtasks with dependency ordering. Falls back to a single
Jenna-assigned task if Sage is unavailable.

Usage:
    from goal_decompose import decompose_goal, get_goal_status

    subtasks = decompose_goal("goal-abc123")
    status = get_goal_status("goal-abc123")
"""

from __future__ import annotations

import json
import logging
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from reasoning import suggest_delegation  # noqa: E402
from task_queue import task_queue  # noqa: E402

log = logging.getLogger("brain.goal_decompose")

MAX_SUBTASKS = 8


# ---------------------------------------------------------------------------
# Prompt builder
# ---------------------------------------------------------------------------

def _build_decomposition_prompt(goal: dict) -> str:
    title = goal.get("title", "Untitled goal")
    description = goal.get("description", "")
    return f"""You are decomposing a goal into concrete subtasks for Chris's agent team.

## Goal
{title}: {description}

## Available Agents
- Liz: code, implementation, debugging, architecture, frontend, backend
- Ellie: infrastructure, Docker, deployment, nginx, monitoring, automation
- Jenna: scheduling, communication, coordination, personal management
- Sage: research, analysis, knowledge synthesis, fact-checking
- Market: content creation, blog posts, SEO, marketing

## Rules
- Each subtask must be independently executable by one agent
- Order by dependency (later tasks may depend on earlier ones)
- Be specific: "implement X in file Y" not "do the thing"
- Maximum {MAX_SUBTASKS} subtasks

Return ONLY a JSON array:
[{{"title": "...", "description": "...", "depends_on_index": [], "suggested_agent": "liz"}}, ...]"""


# ---------------------------------------------------------------------------
# JSON parser (tolerant)
# ---------------------------------------------------------------------------

def _parse_subtasks(text: str) -> list[dict]:
    """Extract a JSON array of subtask dicts from Sage's response.

    Tolerates markdown fences, leading prose, and trailing text.
    Returns empty list on failure.
    """
    cleaned = text.strip()

    # Strip markdown code fences
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned)

    # Direct parse
    try:
        parsed = json.loads(cleaned)
        if isinstance(parsed, list):
            return parsed
    except json.JSONDecodeError:
        pass

    # JSONDecoder fallback: find the first valid JSON array (handles brackets in strings)
    start = text.find("[")
    if start != -1:
        try:
            decoder = json.JSONDecoder()
            arr, _ = decoder.raw_decode(text, start)
            if isinstance(arr, list):
                return arr
        except (json.JSONDecodeError, ValueError):
            pass

    return []


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def decompose_goal(goal_id: str) -> list[dict]:
    """Decompose a goal into subtasks, create them in the task queue.

    Returns the list of created task dicts.
    """
    goal = task_queue.get_goal(goal_id)
    if not goal:
        log.error("Goal not found: %s", goal_id)
        return []

    prompt = _build_decomposition_prompt(goal)

    # Dispatch directly to Sage (not via reason_deep, which wraps in its own prompt)
    try:
        from openclaw_dispatch import dispatch
        result = dispatch(agent="sage", message=prompt, thinking="medium", timeout=90)
        raw_text = result.text if result.ok else ""
    except Exception:
        raw_text = ""

    subtasks_raw = _parse_subtasks(raw_text)

    # Validate and cap
    if not subtasks_raw or not isinstance(subtasks_raw, list):
        log.warning("Sage returned unparseable subtasks for goal %s, using fallback", goal_id)
        return _fallback_single_task(goal_id, goal)

    subtasks_raw = subtasks_raw[:MAX_SUBTASKS]
    max_valid_index = len(subtasks_raw) - 1

    # Warn about invalid dependency indices (out-of-range or forward references)
    for idx, raw in enumerate(subtasks_raw):
        deps = raw.get("depends_on_index", [])
        if not isinstance(deps, list):
            continue
        out_of_range = [i for i in deps if isinstance(i, int) and i > max_valid_index]
        forward_refs = [i for i in deps if isinstance(i, int) and 0 <= i <= max_valid_index and i >= idx]
        if out_of_range:
            log.warning(
                "Invalid out-of-range dependency indices %s for subtask '%s' (cap=%d) — will be ignored",
                out_of_range, raw.get("title", "?"), MAX_SUBTASKS,
            )
        if forward_refs:
            log.warning(
                "Invalid forward-reference dependency indices %s for subtask[%d] '%s' — will be ignored",
                forward_refs, idx, raw.get("title", "?"),
            )

    # Assign agents and create tasks
    created: list[dict] = []
    index_to_task_id: dict[int, str] = {}

    for idx, raw in enumerate(subtasks_raw):
        title = raw.get("title", f"Subtask {idx + 1}")
        description = raw.get("description", "")

        # Use suggest_delegation to pick agent (overrides Sage's suggestion
        # with keyword-based heuristic for consistency)
        delegation = suggest_delegation(title + " " + description)
        agent = delegation.get("agent", raw.get("suggested_agent", "jenna"))

        # Resolve depends_on indices to task IDs
        depends_on_indices = raw.get("depends_on_index", [])
        if not isinstance(depends_on_indices, list):
            depends_on_indices = []
        depends_on_ids = [
            index_to_task_id[i]
            for i in depends_on_indices
            if isinstance(i, int) and i in index_to_task_id
        ]

        task = task_queue.create_task(
            title=title,
            description=description,
            assigned_agent=agent,
            parent_goal_id=goal_id,
            depends_on=depends_on_ids,
            confidence=delegation.get("confidence", 0.5),
            confidence_reasoning=delegation.get("reasoning", ""),
        )
        created.append(task)
        index_to_task_id[idx] = task["id"]

    log.info(
        "Decomposed goal %s into %d subtasks: %s",
        goal_id,
        len(created),
        [t["title"] for t in created],
    )
    return created


def _fallback_single_task(goal_id: str, goal: dict) -> list[dict]:
    """Create a single Jenna-assigned task when decomposition fails."""
    task = task_queue.create_task(
        title=goal.get("title", "Execute goal"),
        description=goal.get("description", ""),
        assigned_agent="jenna",
        parent_goal_id=goal_id,
        depends_on=[],
    )
    log.info("Fallback: created single task %s for goal %s", task["id"], goal_id)
    return [task]


# ---------------------------------------------------------------------------
# Goal status
# ---------------------------------------------------------------------------

def get_goal_status(goal_id: str) -> dict:
    """Return progress summary for a goal and its subtasks."""
    return task_queue.get_goal_progress(goal_id)
