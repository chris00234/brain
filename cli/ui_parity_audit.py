#!/usr/bin/env python3
"""Static Brain API ↔ UI parity audit.

This is intentionally lightweight: it checks that world-level readiness surfaces
that exist in FastAPI/API clients also have a concrete brain-ui reference. It
prevents backend-only gates from silently missing Chris's dashboard.
"""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

BRAIN_ROOT = Path(__file__).resolve().parents[1]
UI_ROOT = BRAIN_ROOT.parent / "brain-ui"
REPORT_FILE = BRAIN_ROOT / "logs" / "ui_parity_audit.json"
sys.path.insert(0, str(BRAIN_ROOT))

from brain_core.readiness_surface_manifest import (  # noqa: E402
    READINESS_SURFACE_MANIFEST_VERSION,
    manifest_snapshot,
    readiness_fields_for,
)


@dataclass(frozen=True)
class ParityCheck:
    id: str
    label: str
    backend_tokens: tuple[str, ...] = ()
    ui_tokens: tuple[str, ...] = ()
    backend_paths: tuple[str, ...] = ()
    api_client_paths: tuple[str, ...] = ()
    readiness_fields: tuple[str, ...] = ()
    required: bool = True
    rationale: str = ""


CHECKS: tuple[ParityCheck, ...] = (
    ParityCheck(
        id="ops_readiness",
        label="ops readiness aggregate",
        backend_tokens=("/brain/ops/readiness", "readiness_snapshot", "release_readiness"),
        ui_tokens=("opsReadiness", "Readiness", "Release hygiene"),
        backend_paths=("/brain/ops/readiness",),
        api_client_paths=("/brain/ops/readiness",),
        readiness_fields=readiness_fields_for("ops_readiness"),
        rationale="Top-level readiness/blockers must be visible, not just API-only.",
    ),
    ParityCheck(
        id="slo_incidents",
        label="SLO roster and remediation ledger",
        backend_tokens=("/brain/slos", "slo_incident_ledger", "slo_escalation_ledger"),
        ui_tokens=("fetchSLOs", "Auto-remediation", "SLO roster"),
        backend_paths=("/brain/slos", "/brain/slo-incidents"),
        api_client_paths=("/brain/slos", "/brain/ops/readiness"),
        readiness_fields=readiness_fields_for("slo_incidents"),
        rationale="Operations failures need dashboard evidence and remediation traceability.",
    ),
    ParityCheck(
        id="agent_execution_truth",
        label="agent execution truth ledger",
        backend_tokens=(
            "/brain/task-dispatch-attempts",
            "/brain/tasks/{task_id}/execution",
            "task_dispatch_attempts",
        ),
        ui_tokens=("dispatchAttempts", "Agent Execution Truth", "task-dispatch-attempts"),
        backend_paths=("/brain/task-dispatch-attempts", "/brain/tasks/{task_id}/execution"),
        api_client_paths=("/brain/task-dispatch-attempts",),
        rationale="Chris's fake-automation concern requires visible handoff→dispatch evidence.",
    ),
    ParityCheck(
        id="retrieval_eval_gates",
        label="retrieval, CRAG, RAGAS, adversarial, holdout eval gates",
        backend_tokens=(
            "crag_regression",
            "crag_correction_regression",
            "ragas_eval",
            "adversarial_eval",
            "holdout_eval",
        ),
        ui_tokens=("CRAG gate", "CRAG correction", "RAGAS eval", "Adversarial eval", "Holdout eval"),
        backend_paths=("/brain/ops/readiness",),
        api_client_paths=("/brain/ops/readiness",),
        readiness_fields=readiness_fields_for("retrieval_eval_gates"),
        rationale="Retrieval quality gates must be visible with status/case evidence.",
    ),
    ParityCheck(
        id="source_governance",
        label="high-value source governance",
        backend_tokens=("source_governance", "critical_sources_ok", "required_controls_ok"),
        ui_tokens=("Source governance", "critical_sources_ok", "required_controls"),
        backend_paths=("/brain/ops/readiness",),
        api_client_paths=("/brain/ops/readiness",),
        readiness_fields=readiness_fields_for("source_governance"),
        rationale="Chris's source-pollution concern requires visible source/control coverage.",
    ),
    ParityCheck(
        id="skill_promotion",
        label="auto-skill promotion and outcome loop",
        backend_tokens=("skill_promotion", "outcome_delta", "promotion_contract_version"),
        ui_tokens=("Skill promotion", "Outcome links", "promotion_contract_version"),
        backend_paths=("/brain/ops/readiness",),
        api_client_paths=("/brain/ops/readiness",),
        readiness_fields=readiness_fields_for("skill_promotion"),
        rationale="Auto-generated skills need visible provenance/rollback/outcome evidence.",
    ),
    ParityCheck(
        id="failure_lesson_outcome",
        label="failure lesson outcome loop",
        backend_tokens=("failure_lesson_outcome", "linked_outcomes", "lessons_with_outcomes"),
        ui_tokens=("Failure lessons", "linked outcomes", "lessons with outcomes"),
        backend_paths=("/brain/ops/readiness",),
        api_client_paths=("/brain/ops/readiness",),
        readiness_fields=readiness_fields_for("failure_lesson_outcome"),
        rationale="Reflexion lessons need visible post-use outcome evidence, not write-only storage.",
    ),
    ParityCheck(
        id="openclaw_gateway",
        label="OpenClaw gateway health",
        backend_tokens=("openclaw_gateway", "openclaw_gateway_health"),
        ui_tokens=("OpenClaw Gateway", "127.0.0.1:18789"),
        backend_paths=("/brain/ops/readiness",),
        api_client_paths=("/brain/ops/readiness",),
        readiness_fields=readiness_fields_for("openclaw_gateway"),
        rationale="Automated agent work depends on gateway health; dashboard must show it.",
    ),
    ParityCheck(
        id="graph_stats",
        label="graph stats and graph nodes",
        backend_tokens=("/brain/graph/stats", "/brain/graph/nodes"),
        ui_tokens=("graphStats", "graphNodes", "Entity graph"),
        backend_paths=("/brain/graph/stats", "/brain/graph/nodes"),
        api_client_paths=("/brain/graph/stats", "/brain/graph/nodes"),
        rationale="Graph/canonical memory layer should have UI visibility.",
    ),
    ParityCheck(
        id="mcp_tools",
        label="MCP/tool visibility",
        backend_tokens=("/brain/tools", "mcp"),
        ui_tokens=("mcpTools", "MCP", "brain.mcpTools"),
        backend_paths=("/brain/tools",),
        api_client_paths=("/brain/tools",),
        rationale="Tool/MCP availability should not be hidden from the dashboard.",
    ),
)


def _read_all(paths: Iterable[Path]) -> str:
    chunks: list[str] = []
    for path in paths:
        try:
            chunks.append(f"\n# {path}\n" + path.read_text(encoding="utf-8", errors="ignore"))
        except OSError:
            continue
    return "\n".join(chunks)


def _source_text() -> tuple[str, str]:
    backend_paths = [
        BRAIN_ROOT / "brain_core" / "ops_readiness.py",
        BRAIN_ROOT / "brain_core" / "routes" / "agency.py",
        BRAIN_ROOT / "brain_core" / "routes" / "dashboard.py",
        BRAIN_ROOT / "brain_core" / "routes" / "health.py",
        BRAIN_ROOT / "brain_core" / "routes" / "knowledge.py",
        BRAIN_ROOT / "brain_core" / "routes" / "stores.py",
        BRAIN_ROOT / "brain_core" / "slos.py",
        BRAIN_ROOT / "brain_core" / "source_governance.py",
        BRAIN_ROOT / "brain_core" / "skill_promotion_audit.py",
        BRAIN_ROOT / "brain_core" / "failure_lesson_audit.py",
    ]
    ui_paths = list((UI_ROOT / "src").rglob("*.ts")) + list((UI_ROOT / "src").rglob("*.tsx"))
    return _read_all(backend_paths), _read_all(ui_paths)


def _missing(tokens: tuple[str, ...], haystack: str) -> list[str]:
    return [token for token in tokens if token not in haystack]


def _discover_backend_routes() -> set[str]:
    """Derive actual FastAPI route strings from route decorators."""

    route_re = re.compile(r"@(?:router|app)\.(?:get|post|patch|delete|put)\(\s*['\"]([^'\"]+)['\"]")
    routes: set[str] = set()
    for path in (BRAIN_ROOT / "brain_core" / "routes").rglob("*.py"):
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        routes.update(route_re.findall(text))
    return routes


def _discover_api_client_paths(ui_text: str) -> set[str]:
    """Derive API paths used by the TypeScript client/UI source."""

    path_re = re.compile(
        r"[\"`](/(?:brain|memory|recall|jobs|profile|admin|capture|location|health|agent|collections|metrics|web|learn|chris)[^\"`]*)"
    )
    paths: set[str] = set()
    for raw in path_re.findall(ui_text):
        path = raw.split("?", 1)[0]
        path = re.sub(r"\$\{[^}]+\}", "{param}", path)
        paths.add(path)
    return paths


def run() -> dict:
    backend_text, ui_text = _source_text()
    backend_routes = _discover_backend_routes()
    api_client_paths = _discover_api_client_paths(ui_text)
    rows = []
    for check in CHECKS:
        missing_backend = _missing(check.backend_tokens, backend_text)
        missing_ui = _missing(check.ui_tokens, ui_text)
        missing_backend_paths = [p for p in check.backend_paths if p not in backend_routes]
        missing_api_client_paths = [p for p in check.api_client_paths if p not in api_client_paths]
        missing_readiness_fields = _missing(check.readiness_fields, backend_text)
        status = (
            "ok"
            if not missing_backend
            and not missing_ui
            and not missing_backend_paths
            and not missing_api_client_paths
            and not missing_readiness_fields
            else "blocked"
        )
        rows.append(
            {
                **asdict(check),
                "status": status,
                "missing_backend_tokens": missing_backend,
                "missing_ui_tokens": missing_ui,
                "missing_backend_paths": missing_backend_paths,
                "missing_api_client_paths": missing_api_client_paths,
                "missing_readiness_fields": missing_readiness_fields,
            }
        )
    required = [r for r in rows if r["required"]]
    blocked = [r for r in required if r["status"] != "ok"]
    report = {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "status": "ok" if not blocked else "blocked",
        "total": len(rows),
        "required": len(required),
        "ok": sum(1 for r in rows if r["status"] == "ok"),
        "blocked": len(blocked),
        "coverage_level": "route_api_client_manifest_v1",
        "readiness_manifest_version": READINESS_SURFACE_MANIFEST_VERSION,
        "readiness_manifest": manifest_snapshot(),
        "backend_route_count": len(backend_routes),
        "api_client_path_count": len(api_client_paths),
        "rows": rows,
    }
    REPORT_FILE.parent.mkdir(parents=True, exist_ok=True)
    REPORT_FILE.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return report


def main() -> int:
    report = run()
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if report["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
