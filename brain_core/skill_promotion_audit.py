"""Read-only audit for auto skill promotion provenance and rollback gates."""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Any

try:
    from config import BRAIN_LOGS_DIR
except Exception:  # pragma: no cover - standalone fallback
    BRAIN_LOGS_DIR = Path("/Users/chrischo/server/brain/logs")

from skill_materializer import (
    CLAUDE_SKILLS_DIR,
    CODEX_SKILLS_DIR,
    MIN_STEPS,
    MIN_SUCCESS_COUNT,
    OPENCLAW_SKILLS_DIR,
    PROMOTION_CONTRACT_VERSION,
    _load_usage,
    _parse_frontmatter,
)

MIN_OUTCOME_LINKED_OUTCOMES = 5
MIN_OUTCOME_PROCEDURES_WITH_OUTCOMES = 1
MIN_OUTCOME_SUCCESS_RATE = 60.0

REQUIRED_ROOTS = (CLAUDE_SKILLS_DIR, CODEX_SKILLS_DIR, OPENCLAW_SKILLS_DIR)


def _procedure_map(db_path: Path | None = None) -> dict[str, dict[str, Any]]:
    path = db_path or (BRAIN_LOGS_DIR / "autonomy.db")
    if not path.exists():
        return {}
    with sqlite3.connect(str(path)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM procedures").fetchall()
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        out[str(row["id"])] = dict(row)
    return out


def _procedure_outcome_stats(proc_ids: set[str], db_path: Path | None = None) -> dict[str, Any]:
    path = db_path or (BRAIN_LOGS_DIR / "autonomy.db")
    if not path.exists():
        return {"status": "missing_db", "linked_outcomes": 0, "procedures_with_outcomes": 0}
    try:
        with sqlite3.connect(str(path)) as conn:
            conn.row_factory = sqlite3.Row
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(outcomes)").fetchall()}
            if "procedure_ids" not in cols:
                return {"status": "missing_schema", "linked_outcomes": 0, "procedures_with_outcomes": 0}
            rows = conn.execute("SELECT procedure_ids, chris_override FROM outcomes").fetchall()
            proc_rows = conn.execute("SELECT id, success_count FROM procedures").fetchall()
    except sqlite3.Error as exc:
        return {
            "status": "error",
            "error": str(exc)[:200],
            "linked_outcomes": 0,
            "procedures_with_outcomes": 0,
        }

    procedure_success_counts = {
        str(row["id"]): int(row["success_count"] or 0) for row in proc_rows if str(row["id"]) in proc_ids
    }
    stats = {
        pid: {
            "outcomes": 0,
            "successes": 0,
            "failures": 0,
            "source_success_count": procedure_success_counts.get(pid, 0),
        }
        for pid in proc_ids
    }
    linked_outcomes = 0
    for row in rows:
        try:
            linked = json.loads(row["procedure_ids"] or "[]")
        except Exception:
            linked = []
        linked = [str(pid) for pid in linked if str(pid) in proc_ids]
        if not linked:
            continue
        linked_outcomes += 1
        success = int(row["chris_override"] or 0) == 0
        for pid in linked:
            stats[pid]["outcomes"] += 1
            if success:
                stats[pid]["successes"] += 1
            else:
                stats[pid]["failures"] += 1
    procedures_with_outcomes = sum(1 for s in stats.values() if s["outcomes"] > 0)
    successes = sum(s["successes"] for s in stats.values())
    failures = sum(s["failures"] for s in stats.values())
    total_links = successes + failures
    source_success_count = sum(procedure_success_counts.values())
    procedures_with_source_successes = sum(1 for n in procedure_success_counts.values() if n > 0)
    effective_successes = successes + source_success_count
    effective_total = total_links + source_success_count
    return {
        "status": "ok",
        "linked_outcomes": linked_outcomes,
        "procedure_links": total_links,
        "procedures_with_outcomes": procedures_with_outcomes,
        "source_success_count": source_success_count,
        "procedures_with_source_successes": procedures_with_source_successes,
        "effective_linked_outcomes": effective_total,
        "effective_successes": effective_successes,
        "effective_failures": failures,
        "successes": successes,
        "failures": failures,
        "success_rate": round((successes / total_links) * 100.0, 2) if total_links else None,
        "effective_success_rate": (
            round((effective_successes / effective_total) * 100.0, 2) if effective_total else None
        ),
        "per_procedure": stats,
    }


def _outcome_maturity(outcome_delta: dict[str, Any]) -> dict[str, Any]:
    status = str(outcome_delta.get("status") or "unknown")
    if status in {"missing_schema", "error"}:
        return {
            "status": "blocked",
            "readiness_blocking": True,
            "reason": status,
            "min_linked_outcomes": MIN_OUTCOME_LINKED_OUTCOMES,
            "min_procedures_with_outcomes": MIN_OUTCOME_PROCEDURES_WITH_OUTCOMES,
            "min_success_rate": MIN_OUTCOME_SUCCESS_RATE,
        }
    linked = int(outcome_delta.get("effective_linked_outcomes") or outcome_delta.get("linked_outcomes") or 0)
    task_linked = int(outcome_delta.get("linked_outcomes") or 0)
    source_success_count = int(outcome_delta.get("source_success_count") or 0)
    procedures = int(
        outcome_delta.get("procedures_with_outcomes")
        or outcome_delta.get("procedures_with_source_successes")
        or 0
    )
    success_rate = outcome_delta.get("effective_success_rate")
    if success_rate is None:
        success_rate = outcome_delta.get("success_rate")
    if linked < MIN_OUTCOME_LINKED_OUTCOMES or procedures < MIN_OUTCOME_PROCEDURES_WITH_OUTCOMES:
        return {
            "status": "insufficient_data",
            "readiness_blocking": True,
            "linked_outcomes": linked,
            "task_linked_outcomes": task_linked,
            "source_success_count": source_success_count,
            "procedures_with_outcomes": procedures,
            "min_linked_outcomes": MIN_OUTCOME_LINKED_OUTCOMES,
            "min_procedures_with_outcomes": MIN_OUTCOME_PROCEDURES_WITH_OUTCOMES,
            "min_success_rate": MIN_OUTCOME_SUCCESS_RATE,
        }
    if success_rate is None or float(success_rate) < MIN_OUTCOME_SUCCESS_RATE:
        return {
            "status": "blocked",
            "readiness_blocking": True,
            "linked_outcomes": linked,
            "task_linked_outcomes": task_linked,
            "source_success_count": source_success_count,
            "procedures_with_outcomes": procedures,
            "success_rate": success_rate,
            "min_success_rate": MIN_OUTCOME_SUCCESS_RATE,
        }
    return {
        "status": "ok",
        "readiness_blocking": False,
        "linked_outcomes": linked,
        "task_linked_outcomes": task_linked,
        "source_success_count": source_success_count,
        "procedures_with_outcomes": procedures,
        "success_rate": success_rate,
        "min_linked_outcomes": MIN_OUTCOME_LINKED_OUTCOMES,
        "min_procedures_with_outcomes": MIN_OUTCOME_PROCEDURES_WITH_OUTCOMES,
        "min_success_rate": MIN_OUTCOME_SUCCESS_RATE,
    }


def _auto_skill_groups(roots: tuple[Path, ...] = REQUIRED_ROOTS) -> dict[str, dict[str, Any]]:
    groups: dict[str, dict[str, Any]] = defaultdict(lambda: {"paths": [], "frontmatter": {}, "usage": {}})
    for root in roots:
        usage = _load_usage(root)
        if root.exists():
            for skill_dir in root.iterdir():
                if not skill_dir.is_dir() or not skill_dir.name.startswith("auto-"):
                    continue
                skill_md = skill_dir / "SKILL.md"
                fm = _parse_frontmatter(skill_md) if skill_md.exists() else {}
                if (
                    str(fm.get("auto_generated", "")).lower() not in {"true", "1", "yes"}
                    and skill_dir.name not in usage
                ):
                    continue
                slug = skill_dir.name
                groups[slug]["paths"].append(str(skill_md))
                groups[slug]["frontmatter"][str(root)] = fm
        for slug, record in usage.items():
            if str(slug).startswith("auto-"):
                groups[str(slug)]["usage"][str(root)] = record
    return dict(groups)


def skill_promotion_audit_snapshot(
    *, roots: tuple[Path, ...] = REQUIRED_ROOTS, db_path: Path | None = None
) -> dict[str, Any]:
    procs = _procedure_map(db_path)
    groups = _auto_skill_groups(roots)
    skills: list[dict[str, Any]] = []
    blockers: list[str] = []
    warnings: list[str] = []
    audited_proc_ids: set[str] = set()

    for slug, group in sorted(groups.items()):
        frontmatter_by_root: dict[str, dict[str, Any]] = group.get("frontmatter") or {}
        usage_by_root: dict[str, dict[str, Any]] = group.get("usage") or {}
        reasons: list[str] = []
        root_names = {str(root) for root in roots}
        present_roots = set(frontmatter_by_root)
        missing_roots = sorted(root_names - present_roots)
        if missing_roots:
            reasons.append("missing_runtime_skill")

        first_fm = next(iter(frontmatter_by_root.values()), {}) if frontmatter_by_root else {}
        proc_id = str(first_fm.get("brain_procedure_id") or "")
        proc = procs.get(proc_id)
        if proc_id:
            audited_proc_ids.add(proc_id)
        if not proc_id:
            reasons.append("missing_procedure_id")
        elif not proc:
            reasons.append("missing_backing_procedure")
        if str(first_fm.get("promotion_contract_version") or "") != PROMOTION_CONTRACT_VERSION:
            reasons.append("missing_promotion_contract")
        if str(first_fm.get("rollback_strategy") or "") != "archive_generated_auto_skill_dir":
            reasons.append("missing_rollback_strategy")
        try:
            source_episode_count = int(
                first_fm.get("source_episode_count") or first_fm.get("success_count") or 0
            )
        except ValueError:
            source_episode_count = 0
        if source_episode_count < MIN_SUCCESS_COUNT:
            reasons.append("insufficient_source_episodes")
        if proc:
            try:
                proc_success = int(proc.get("success_count") or 0)
            except (TypeError, ValueError):
                proc_success = 0
            if proc_success < MIN_SUCCESS_COUNT:
                reasons.append("backing_procedure_below_success_gate")
            steps = str(proc.get("steps") or "")
            if steps.count('"') < MIN_STEPS * 2 and steps.count("[") == 0:
                warnings.append(f"{slug}:steps_parse_weak")
        sidecar_contract_roots = [
            root
            for root, usage in usage_by_root.items()
            if usage.get("promotion_contract_version") == PROMOTION_CONTRACT_VERSION
        ]
        if len(sidecar_contract_roots) < len(roots):
            reasons.append("missing_usage_contract")

        status = "ok" if not reasons else "blocked"
        if status == "blocked":
            blockers.append(slug)
        skills.append(
            {
                "slug": slug,
                "status": status,
                "reasons": reasons,
                "brain_procedure_id": proc_id,
                "source_episode_count": source_episode_count,
                "runtime_paths": group.get("paths") or [],
                "present_runtime_count": len(present_roots),
                "required_runtime_count": len(roots),
                "usage_contract_count": len(sidecar_contract_roots),
            }
        )

    outcome_delta = _procedure_outcome_stats(audited_proc_ids, db_path=db_path)
    outcome_maturity = _outcome_maturity(outcome_delta)
    if outcome_delta.get("status") in {"missing_schema", "error"}:
        warnings.append(f"skill_outcome_delta:{outcome_delta.get('status')}")

    return {
        "status": "blocked" if blockers else "ok",
        "blockers": blockers,
        "warnings": warnings,
        "coverage": {
            "auto_skills": len(skills),
            "contract_ok": sum(1 for s in skills if s["status"] == "ok"),
            "required_runtimes": len(roots),
        },
        "promotion_contract_version": PROMOTION_CONTRACT_VERSION,
        "outcome_delta": outcome_delta,
        "outcome_maturity": outcome_maturity,
        "skills": skills,
    }
