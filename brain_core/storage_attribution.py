"""brain_core/storage_attribution.py — per-class storage breakdown for SLOs.

Codex round-6 picked storage attribution as Phase 4: the existing
``logs_dir_total_mb`` SLO emits one scalar; remediation maps both total
and growth to generic ``log_rotation``. When growth fires, the operator
has no fast way to ask "which class blew up?". This module computes a
per-class breakdown so slo_remediation can name the culprit and
budgeted retention can target a single class instead of a global sweep.

Class definitions (intent + glob):
  database_primary    brain.db / autonomy.db / llm_usage.db / scheduler_history.db
  database_cache      embedding_cache.db / metrics_history.db
  backups             logs/backups/** + logs/*.bak* + logs/*.pre_* + logs/*.pre-*
  job_logs            logs/jobs/**
  training            logs/training/**
  jsonl_streams       logs/*.jsonl + logs/**/*.jsonl
  feedback_logs       logs/search-feedback.jsonl / decision-feedback.jsonl / etc
  other               everything else

Output schema:
  {
    ts, total_bytes, total_mb,
    classes: [{class, bytes, mb, pct_of_total, file_count, top_files: [...]}, ...],
    top_contributor: {class, mb, pct_of_total},
    budgets: {<class>: budget_mb}    # optional, from brain_config_store
  }

2026-05-20 W4 Phase 3 (codex round-6 #4): closes the gap where the
controller (Phase 2) could propose throttle mutations on logs_dir
breach but had no view of which class to target.
"""

from __future__ import annotations

import fnmatch
import json
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

log = logging.getLogger("brain.storage_attribution")

sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import BRAIN_LOGS_DIR  # noqa: E402

# Patterns are matched in registration order against POSIX-relative paths under
# BRAIN_LOGS_DIR. First match wins. Use leading "*/<x>" to match any depth.
_CLASS_PATTERNS: list[tuple[str, list[str]]] = [
    (
        "database_primary",
        [
            "brain.db",
            "brain.db-*",
            "autonomy.db",
            "autonomy.db-*",
            "llm_usage.db",
            "llm_usage.db-*",
            "scheduler_history.db",
            "scheduler_history.db-*",
        ],
    ),
    (
        "database_cache",
        [
            "embedding_cache.db",
            "embedding_cache.db-*",
            "metrics_history.db",
            "metrics_history.db-*",
        ],
    ),
    (
        "backups",
        [
            "backups/**",
            "*.bak",
            "*.bak.*",
            "*.pre_*",
            "*.pre-*",
            "*.tar*",
            "*.gz",
        ],
    ),
    ("job_logs", ["jobs/**"]),
    ("training", ["training/**"]),
    ("feedback_logs", ["*-feedback.jsonl", "search-feedback.jsonl", "decision-feedback.jsonl"]),
    ("jsonl_streams", ["*.jsonl", "**/*.jsonl"]),
]

_DEFAULT_CLASS = "other"
_TOP_FILES_PER_CLASS = 5


def _classify(rel_path: str) -> str:
    for cls, patterns in _CLASS_PATTERNS:
        for pat in patterns:
            if fnmatch.fnmatchcase(rel_path, pat):
                return cls
    return _DEFAULT_CLASS


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _budgets() -> dict[str, float]:
    """Read per-class size budgets from brain_config_store. Empty when not
    configured — attribution still works, just without budget headroom calc.
    """
    try:
        import brain_config_store as _bcs

        raw = _bcs.get("storage_attribution.budgets_mb") if hasattr(_bcs, "get") else None
    except Exception:
        raw = None
    if isinstance(raw, dict):
        return {str(k): float(v) for k, v in raw.items()}
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return {str(k): float(v) for k, v in parsed.items()}
        except Exception as exc:
            log.debug("storage_attribution budget parse failed: %s", exc)
    return {}


def compute_attribution(top_files_per_class: int = _TOP_FILES_PER_CLASS) -> dict[str, Any]:
    """Walk BRAIN_LOGS_DIR once, classify each file, aggregate per class."""
    started = datetime.now(UTC)
    classes: dict[str, dict[str, Any]] = {}
    total_bytes = 0
    try:
        for p in BRAIN_LOGS_DIR.rglob("*"):
            if not p.is_file():
                continue
            try:
                size = p.stat().st_size
            except OSError:
                continue
            try:
                rel = str(p.relative_to(BRAIN_LOGS_DIR))
            except ValueError:
                rel = p.name
            cls = _classify(rel)
            entry = classes.setdefault(
                cls,
                {"class": cls, "bytes": 0, "file_count": 0, "_files": []},
            )
            entry["bytes"] += size
            entry["file_count"] += 1
            entry["_files"].append((rel, size))
            total_bytes += size
    except Exception as exc:
        log.warning("storage_attribution walk failed: %s", exc)

    budgets = _budgets()
    classes_out: list[dict[str, Any]] = []
    for entry in classes.values():
        b = int(entry["bytes"])
        mb = round(b / (1024 * 1024), 2)
        pct = round(100.0 * b / total_bytes, 2) if total_bytes else 0.0
        files_sorted = sorted(entry["_files"], key=lambda kv: -kv[1])[:top_files_per_class]
        top_files = [{"path": rel, "mb": round(s / (1024 * 1024), 2)} for rel, s in files_sorted]
        cls_entry = {
            "class": entry["class"],
            "bytes": b,
            "mb": mb,
            "pct_of_total": pct,
            "file_count": int(entry["file_count"]),
            "top_files": top_files,
        }
        if entry["class"] in budgets:
            budget = budgets[entry["class"]]
            cls_entry["budget_mb"] = budget
            cls_entry["over_budget_mb"] = round(max(0.0, mb - budget), 2)
        classes_out.append(cls_entry)
    classes_out.sort(key=lambda c: -c["bytes"])

    top = classes_out[0] if classes_out else None
    top_contributor = (
        {"class": top["class"], "mb": top["mb"], "pct_of_total": top["pct_of_total"]} if top else None
    )
    duration_ms = int((datetime.now(UTC) - started).total_seconds() * 1000)
    return {
        "ts": _now(),
        "duration_ms": duration_ms,
        "total_bytes": total_bytes,
        "total_mb": round(total_bytes / (1024 * 1024), 2),
        "classes": classes_out,
        "top_contributor": top_contributor,
        "budgets": budgets,
    }


def top_class(window: dict[str, Any] | None = None) -> dict[str, Any]:
    """Convenience: return just the top contributor for slo_remediation."""
    data = window or compute_attribution(top_files_per_class=0)
    return data.get("top_contributor") or {"class": "unknown", "mb": 0.0, "pct_of_total": 0.0}


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="brain logs/ per-class size attribution")
    parser.add_argument("action", choices=["compute", "top"], nargs="?", default="compute")
    parser.add_argument("--top-files", type=int, default=_TOP_FILES_PER_CLASS)
    args = parser.parse_args()
    if args.action == "top":
        print(json.dumps(top_class(), indent=2, ensure_ascii=False))  # noqa: T201
    else:
        print(  # noqa: T201
            json.dumps(compute_attribution(top_files_per_class=args.top_files), indent=2, ensure_ascii=False)
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
