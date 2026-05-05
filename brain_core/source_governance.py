"""Source-ingestion governance snapshot for Brain readiness.

This module answers the operational question Chris keeps surfacing: are the
high-value sources actually flowing, and are low-value/raw sources constrained
before they pollute recall?  It is read-only and safe for API/UI readiness.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

BRAIN_ROOT = Path("/Users/chrischo/server/brain")
LOGS_DIR = BRAIN_ROOT / "logs"
SCHEDULER_HISTORY_DB = LOGS_DIR / "scheduler_history.db"


@dataclass(frozen=True)
class GovernedSource:
    id: str
    label: str
    jobs: tuple[str, ...]
    max_age_hours: float
    state_files: tuple[str, ...] = ()
    log_files: tuple[str, ...] = ()
    critical: bool = True
    rationale: str = ""


GOVERNED_SOURCES: tuple[GovernedSource, ...] = (
    GovernedSource(
        id="personal",
        label="Apple Notes, iMessage, Calendar, Reminders",
        jobs=("personal_ingest",),
        state_files=("personal-ingest-state.json",),
        log_files=("jobs/personal_ingest.log",),
        max_age_hours=12,
        rationale="Chris explicitly wants broad personal ingestion, including notes and wife messages.",
    ),
    GovernedSource(
        id="obsidian",
        label="Obsidian vault",
        jobs=("obsidian_sync",),
        state_files=("obsidian-sync-state.json",),
        log_files=("jobs/obsidian_sync.log",),
        max_age_hours=4,
        rationale="Obsidian is a high-value authored-memory source and should not silently stall.",
    ),
    GovernedSource(
        id="openclaw_sessions",
        label="OpenClaw agent sessions",
        jobs=("openclaw_sessions_ingest",),
        state_files=("openclaw-sessions-state.json",),
        log_files=("jobs/openclaw_sessions_ingest.log",),
        max_age_hours=8,
        rationale=(
            "OpenClaw agents must use the Brain effectively, " "so their sessions need timely distillation."
        ),
    ),
    GovernedSource(
        id="claude_code_sessions",
        label="Claude/Codex coding sessions",
        jobs=("claude_code_sessions_ingest",),
        state_files=("claude-code-sessions-state.json",),
        log_files=("jobs/claude_code_sessions_ingest.log",),
        max_age_hours=30,
        rationale="Coding-session memory feeds reusable procedures and bug-prevention context.",
    ),
    GovernedSource(
        id="gmail",
        label="Gmail signal classifier",
        jobs=("gmail_ingest",),
        log_files=("jobs/gmail_ingest.log",),
        max_age_hours=30,
        rationale=(
            "Email is high-signal only after classifier filtering; "
            "freshness proves the classifier path is alive."
        ),
    ),
    GovernedSource(
        id="git_activity",
        label="Git activity distillation",
        jobs=("git_activity_ingest",),
        state_files=("git-activity-state.json",),
        log_files=("jobs/git_activity_ingest.log",),
        max_age_hours=30,
        critical=False,
        rationale=(
            "Useful provenance source; stale status is informational because "
            "not every day has new commits."
        ),
    ),
    GovernedSource(
        id="browser_shell",
        label="Browser and shell high-signal filters",
        jobs=("browser_ingest", "shell_ingest"),
        log_files=("jobs/browser_ingest.log", "jobs/shell_ingest.log"),
        max_age_hours=30,
        critical=False,
        rationale="Noisy behavioral sources must remain classifier-filtered and source-quality downranked.",
    ),
)

CONTROL_CHECKS: tuple[dict[str, Any], ...] = (
    {
        "id": "entry_contract",
        "label": "source-aware entry contract",
        "files": ("brain_core/source_policy.py",),
        "tests": ("tests/unit/test_source_policy.py", "tests/unit/test_brain_ingest_contract.py"),
        "description": "Every ingest payload gets schema/chunk/tag/content-hash provenance fields.",
        "required": True,
    },
    {
        "id": "source_quality_downrank",
        "label": "raw/noisy source downrank",
        "files": (
            "brain_core/source_quality.py",
            "brain_core/rerank.py",
            "brain_core/cross_encoder_rerank.py",
        ),
        "tests": ("tests/unit/test_priority_modules.py",),
        "description": (
            "Raw session dumps and aggregate learning logs are downweighted before answer selection."
        ),
        "required": True,
    },
    {
        "id": "qdrant_write_audit",
        "label": "approved vector write boundary audit",
        "jobs": ("qdrant_write_audit",),
        "logs": ("jobs/qdrant_write_audit.log",),
        "max_age_hours": 48,
        "description": "Detects raw qdrant_client mutating writes outside approved store boundaries.",
        "required": True,
    },
    {
        "id": "entry_contract_audit",
        "label": "entry contract audit",
        "jobs": ("entry_contract_audit",),
        "logs": ("jobs/entry_contract_audit.log",),
        "max_age_hours": 48,
        "description": "Scans indexed vectors for missing provenance contract fields.",
        "json_zero_fields": ("missing_points",),
        "required": True,
    },
    {
        "id": "privacy_negative_audit",
        "label": "personal-source privacy negative audit",
        "jobs": ("privacy_negative_audit",),
        "logs": ("privacy-negative-audit.json",),
        "max_age_hours": 48,
        "description": (
            "Samples high-value personal-source vectors for missing entry contract fields "
            "or raw secret-like content without printing content."
        ),
        "json_zero_fields": ("blocking_findings",),
        "required": True,
    },
    {
        "id": "memory_provenance_lint",
        "label": "canonical/distilled provenance lint",
        "jobs": ("memory_provenance_lint",),
        "logs": ("memory-provenance-lint.json",),
        "max_age_hours": 48,
        "description": "Flags duplicate IDs and missing provenance in canonical/distilled notes.",
        "json_zero_fields": ("errors",),
        "required": False,
    },
    {
        "id": "trust_recompute",
        "label": "cross-source trust recompute",
        "jobs": ("trust_recompute", "web_source_trust_recompute"),
        "description": "Keeps source/domain trust calibrated from feedback and corroboration signals.",
        "required": False,
    },
)


def _now(now: datetime | None = None) -> datetime:
    out = now or datetime.now(UTC)
    if out.tzinfo is None:
        return out.replace(tzinfo=UTC)
    return out.astimezone(UTC)


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        # Scheduler history historically stored local wall time for started_at.
        # Use UTC for freshness math rather than failing the governance check.
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _file_mtime(path: Path) -> datetime | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, UTC)
    except OSError:
        return None


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _load_job_registry() -> set[str]:
    try:
        from job_registry import JOB_REGISTRY
    except Exception:
        try:
            from brain_core.job_registry import JOB_REGISTRY  # type: ignore
        except Exception:
            return set()
    return set(JOB_REGISTRY)


def _load_job_schedule() -> set[str]:
    try:
        from job_definitions import JOB_SCHEDULE
    except Exception:
        try:
            from brain_core.job_definitions import JOB_SCHEDULE  # type: ignore
        except Exception:
            return set()
    return {str(job.name) for job in JOB_SCHEDULE}


def _latest_job_record(
    job_names: tuple[str, ...], db_path: Path = SCHEDULER_HISTORY_DB
) -> dict[str, Any] | None:
    if not db_path.exists() or not job_names:
        return None
    placeholders = ",".join("?" for _ in job_names)
    sql = (
        "select job_name, started_at, finished_at, duration_ms, error from job_history "  # noqa: S608 - placeholder count is generated locally; values are bound separately.
        f"where job_name in ({placeholders}) order by id desc limit 1"
    )
    try:
        with sqlite3.connect(db_path) as con:
            con.row_factory = sqlite3.Row
            row = con.execute(sql, job_names).fetchone()
    except sqlite3.Error:
        return None
    return dict(row) if row else None


def _freshest_file_evidence(files: tuple[str, ...]) -> dict[str, Any] | None:
    best: dict[str, Any] | None = None
    for rel in files:
        path = LOGS_DIR / rel
        if not path.exists():
            continue
        data = _read_json(path) if path.suffix == ".json" else {}
        last_run = _parse_dt(data.get("last_run") or data.get("timestamp") or data.get("generated_at"))
        seen_at = last_run or _file_mtime(path)
        if seen_at is None:
            continue
        evidence = {"kind": "file", "path": str(path), "seen_at": seen_at.isoformat(timespec="seconds")}
        if data:
            evidence["json_keys"] = sorted(list(data.keys()))[:12]
        if best is None or seen_at > _parse_dt(best.get("seen_at")):
            best = evidence
    return best


def _job_evidence(job_names: tuple[str, ...], *, registry: set[str], schedule: set[str]) -> dict[str, Any]:
    registered = sorted(name for name in job_names if name in registry)
    scheduled = sorted(name for name in job_names if name in schedule)
    latest = _latest_job_record(job_names)
    evidence: dict[str, Any] = {
        "registered": registered,
        "scheduled": scheduled,
        "missing_registry": sorted(set(job_names) - registry),
        "missing_schedule": sorted(set(job_names) - schedule),
    }
    if latest:
        evidence["latest_job"] = latest
    return evidence


def _source_status(
    source: GovernedSource, *, now: datetime, registry: set[str], schedule: set[str]
) -> dict[str, Any]:
    job = _job_evidence(source.jobs, registry=registry, schedule=schedule)
    file_evidence = _freshest_file_evidence((*source.state_files, *source.log_files))
    latest_job = job.get("latest_job") if isinstance(job.get("latest_job"), dict) else None
    latest_job_at = _parse_dt((latest_job or {}).get("finished_at") or (latest_job or {}).get("started_at"))
    latest_file_at = _parse_dt((file_evidence or {}).get("seen_at"))
    candidates = [dt for dt in (latest_job_at, latest_file_at) if dt is not None]
    latest_seen = max(candidates) if candidates else None
    age_hours = None if latest_seen is None else round((now - latest_seen).total_seconds() / 3600, 2)

    reasons: list[str] = []
    status = "ok"
    if job["missing_registry"]:
        status = "blocked" if source.critical else "warning"
        reasons.append("missing_registry")
    if job["missing_schedule"]:
        status = "blocked" if source.critical else "warning"
        reasons.append("missing_schedule")
    if latest_job and latest_job.get("error"):
        status = "blocked" if source.critical else "warning"
        reasons.append("latest_job_error")
    if latest_seen is None:
        status = "blocked" if source.critical else "warning"
        reasons.append("no_recent_evidence")
    elif age_hours is not None and age_hours > source.max_age_hours:
        status = "blocked" if source.critical else "warning"
        reasons.append("stale")

    return {
        "id": source.id,
        "label": source.label,
        "status": status,
        "critical": source.critical,
        "jobs": list(source.jobs),
        "max_age_hours": source.max_age_hours,
        "age_hours": age_hours,
        "latest_seen_at": latest_seen.isoformat(timespec="seconds") if latest_seen else None,
        "reasons": reasons,
        "rationale": source.rationale,
        "job_evidence": job,
        "file_evidence": file_evidence,
    }


def _control_status(
    control: dict[str, Any], *, now: datetime, registry: set[str], schedule: set[str]
) -> dict[str, Any]:
    missing_files = [rel for rel in control.get("files", ()) if not (BRAIN_ROOT / rel).exists()]
    missing_tests = [rel for rel in control.get("tests", ()) if not (BRAIN_ROOT / rel).exists()]
    jobs = tuple(control.get("jobs", ()))
    logs = tuple(control.get("logs", ()))
    job = _job_evidence(jobs, registry=registry, schedule=schedule) if jobs else {}
    file_evidence = _freshest_file_evidence(logs) if logs else None
    log_seen = _parse_dt((file_evidence or {}).get("seen_at"))
    max_age = control.get("max_age_hours")
    age_hours = None if log_seen is None else round((now - log_seen).total_seconds() / 3600, 2)

    reasons: list[str] = []
    if missing_files:
        reasons.append("missing_files")
    if missing_tests:
        reasons.append("missing_tests")
    if job.get("missing_registry"):
        reasons.append("missing_registry")
    if job.get("missing_schedule"):
        reasons.append("missing_schedule")
    if logs and file_evidence is None:
        reasons.append("missing_log")
    if age_hours is not None and max_age is not None and age_hours > float(max_age):
        reasons.append("stale_log")

    json_findings: dict[str, Any] = {}
    if file_evidence and control.get("json_zero_fields"):
        data = _read_json(Path(str(file_evidence["path"])))
        for field in control.get("json_zero_fields", ()):  # e.g. errors, missing_points
            value = data.get(field)
            json_findings[str(field)] = value
            try:
                nonzero = float(value or 0) != 0
            except (TypeError, ValueError):
                nonzero = bool(value)
            if nonzero:
                reasons.append(f"nonzero_{field}")

    status = "ok" if not reasons else ("blocked" if control.get("required") else "warning")
    return {
        "id": control["id"],
        "label": control["label"],
        "status": status,
        "required": bool(control.get("required")),
        "description": control.get("description", ""),
        "reasons": reasons,
        "missing_files": missing_files,
        "missing_tests": missing_tests,
        "age_hours": age_hours,
        "job_evidence": job,
        "file_evidence": file_evidence,
        "json_findings": json_findings,
    }


def source_governance_snapshot(*, now: datetime | None = None) -> dict[str, Any]:
    """Return a compact source coverage + pollution-control readiness model."""

    current = _now(now)
    registry = _load_job_registry()
    schedule = _load_job_schedule()
    sources = [_source_status(s, now=current, registry=registry, schedule=schedule) for s in GOVERNED_SOURCES]
    controls = [_control_status(c, now=current, registry=registry, schedule=schedule) for c in CONTROL_CHECKS]
    blockers = [s["id"] for s in sources if s["critical"] and s["status"] == "blocked"]
    blockers.extend(c["id"] for c in controls if c["required"] and c["status"] == "blocked")
    warnings = [s["id"] for s in sources if s["status"] == "warning"]
    warnings.extend(c["id"] for c in controls if c["status"] == "warning")
    return {
        "generated_at": current.isoformat(timespec="seconds"),
        "status": "blocked" if blockers else ("warning" if warnings else "ok"),
        "blockers": blockers,
        "warnings": warnings,
        "coverage": {
            "critical_sources": sum(1 for s in sources if s["critical"]),
            "critical_sources_ok": sum(1 for s in sources if s["critical"] and s["status"] == "ok"),
            "required_controls": sum(1 for c in controls if c["required"]),
            "required_controls_ok": sum(1 for c in controls if c["required"] and c["status"] == "ok"),
        },
        "sources": sources,
        "controls": controls,
    }
