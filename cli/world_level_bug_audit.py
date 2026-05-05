#!/usr/bin/env python3
"""Executable bug-fix ledger for the world-level Brain hardening pass.

This audit answers a narrow question: did this pass find and lock the high-impact
bug classes that would otherwise make the Brain look autonomous, safe, or ready
when it is not?  It is intentionally static and content-safe; it checks files,
required evidence tokens, and forbidden stale/direct-dispatch patterns.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPORT_FILE = ROOT / "logs" / "world-level-bug-audit.json"


@dataclass(frozen=True)
class EvidenceToken:
    path: str
    token: str


@dataclass(frozen=True)
class ForbiddenToken:
    path: str
    token: str
    reason: str


@dataclass(frozen=True)
class BugCheck:
    id: str
    label: str
    bug: str
    fix: str
    evidence: tuple[EvidenceToken, ...]
    forbidden: tuple[ForbiddenToken, ...] = ()


CHECKS: tuple[BugCheck, ...] = (
    BugCheck(
        id="fake_automation_dispatch_truth",
        label="fake automation / silent non-execution",
        bug=(
            "Tasks and UI could imply autonomous agent work was running while "
            "dispatch was deferred, skipped, or invisible."
        ),
        fix=(
            "Persist task dispatch attempts with trace IDs and expose task-to-dispatch "
            "execution evidence through API/UI readiness surfaces."
        ),
        evidence=(
            EvidenceToken("brain_core/task_queue.py", "task_dispatch_attempts"),
            EvidenceToken("brain_core/task_queue.py", "trace_id"),
            EvidenceToken("brain_core/routes/agency.py", "/brain/tasks/{task_id}/execution"),
            EvidenceToken("cli/ui_parity_audit.py", "Agent Execution Truth"),
            EvidenceToken("tests/unit/test_task_dispatch_attempts.py", "dispatch_attempts"),
        ),
    ),
    BugCheck(
        id="openclaw_primary_bypass",
        label="OpenClaw-primary background LLM bypass",
        bug=(
            "Background and evaluation jobs could bypass the CLI/subscription path and "
            "silently depend on OpenClaw agent sessions as the primary transport."
        ),
        fix=(
            "Centralize background LLM work through cli_llm with Codex gpt-5.5 first, "
            "Spark second, and OpenClaw only as final fallback."
        ),
        evidence=(
            EvidenceToken("brain_core/cli_llm.py", "FALLBACK_CHAIN"),
            EvidenceToken("brain_core/cli_llm.py", "gpt-5.5"),
            EvidenceToken("brain_core/cli_llm.py", "gpt-5.3-codex-spark"),
            EvidenceToken("ingest/llm_dispatch.py", "from cli_llm import dispatch"),
            EvidenceToken("brain_core/routes/brain_ops.py", "cli_llm.get_usage_stats"),
            EvidenceToken("tests/unit/test_cli_first_dispatch_contract.py", "keeps_openclaw_last"),
        ),
        forbidden=(
            ForbiddenToken(
                "brain_core/brain_loop.py",
                "from openclaw_dispatch import dispatch",
                "brain_loop must not directly bypass cli_llm fallback/backlog policy",
            ),
            ForbiddenToken(
                "brain_core/routes/brain_ops.py",
                "import openclaw_dispatch",
                "/brain/usage must report the current CLI-first surface",
            ),
            ForbiddenToken(
                "ingest/llm_dispatch.py",
                "OPENCLAW_BIN",
                "shared ingest dispatcher must not shell out directly to OpenClaw",
            ),
        ),
    ),
    BugCheck(
        id="task_evaluation_alert_policy",
        label="task evaluation approval-alert mismatch",
        bug=(
            "Task evaluation could alert Chris as if approval was needed instead of "
            "reporting the safe action Brain already took."
        ),
        fix=(
            "Send action-summary notifications with task_queue:evaluation_action_summary "
            "and persist metadata showing the held/safe follow-up action."
        ),
        evidence=(
            EvidenceToken("brain_core/task_queue.py", "task_queue:evaluation_action_summary"),
            EvidenceToken("brain_core/task_queue.py", "TASK EVALUATION ACTION"),
            EvidenceToken("brain_core/task_queue.py", "task_evaluation_alert_policy"),
            EvidenceToken("tests/unit/test_task_dispatch_attempts.py", "action_summary_not_escalation_alert"),
            EvidenceToken("README.md", "TASK EVALUATION ACTION"),
        ),
    ),
    BugCheck(
        id="privacy_negative_payload_gate",
        label="personal-source privacy negative payloads",
        bug=(
            "High-value personal-source vectors could contain missing provenance fields "
            "or secret-like raw content without a blocking audit."
        ),
        fix=(
            "Add redaction metadata, privacy-negative sampling, content suppression, "
            "and optional redacted-vector reindexing tests."
        ),
        evidence=(
            EvidenceToken("brain_core/source_policy.py", "redact_sensitive_text"),
            EvidenceToken("brain_core/source_policy.py", "PRIVACY_REDACTION_VERSION"),
            EvidenceToken("cli/privacy_negative_audit.py", "content_suppressed"),
            EvidenceToken("cli/privacy_negative_audit.py", "_reindex_redacted_points"),
            EvidenceToken("tests/unit/test_privacy_negative_audit.py", "reindex_redacted_points"),
        ),
    ),
    BugCheck(
        id="source_pollution_governance_gap",
        label="high-value source freshness and pollution controls",
        bug=(
            "Important sources could stall or noisy sources could pollute recall without "
            "a single readiness-facing governance check."
        ),
        fix=(
            "Add governed source roster plus required controls for provenance, source "
            "quality downrank, write audit, entry-contract audit, and privacy negatives."
        ),
        evidence=(
            EvidenceToken("brain_core/source_governance.py", "GOVERNED_SOURCES"),
            EvidenceToken("brain_core/source_governance.py", "CONTROL_CHECKS"),
            EvidenceToken("brain_core/source_governance.py", "critical_sources_ok"),
            EvidenceToken("brain_core/source_governance.py", "required_controls_ok"),
            EvidenceToken("tests/unit/test_source_governance.py", "source_governance"),
        ),
    ),
    BugCheck(
        id="failure_lesson_write_only_loop",
        label="failure lessons without outcome linkage",
        bug=(
            "Failure lessons could be stored but never tied to later outcomes, making the "
            "Reflexion loop write-only."
        ),
        fix=(
            "Audit outcomes.lesson_ids, linked outcomes, success rate, and readiness "
            "blocking/insufficient-data states."
        ),
        evidence=(
            EvidenceToken("brain_core/failure_lesson_audit.py", "failure_lesson_outcome_snapshot"),
            EvidenceToken("brain_core/failure_lesson_audit.py", "lesson_ids"),
            EvidenceToken("brain_core/failure_lesson_audit.py", "linked_outcomes"),
            EvidenceToken("brain_core/ops_readiness.py", "failure_lesson_outcome"),
            EvidenceToken("tests/unit/test_failure_lesson_audit.py", "lesson_ids"),
        ),
    ),
    BugCheck(
        id="backend_only_readiness_surface",
        label="backend-only gates hidden from Brain UI",
        bug=(
            "Backend readiness gates could exist but remain invisible in brain-ui, so Chris "
            "would still lack operational truth."
        ),
        fix=(
            "Add a static API-to-UI parity audit covering execution truth, retrieval gates, "
            "source governance, skill promotion, failure lessons, gateway, graph, and MCP."
        ),
        evidence=(
            EvidenceToken("cli/ui_parity_audit.py", "CHECKS"),
            EvidenceToken("cli/ui_parity_audit.py", "agent_execution_truth"),
            EvidenceToken("cli/ui_parity_audit.py", "source_governance"),
            EvidenceToken("cli/ui_parity_audit.py", "failure_lesson_outcome"),
            EvidenceToken("tests/unit/test_ui_parity_audit.py", "ui_parity_audit"),
        ),
    ),
    BugCheck(
        id="completion_truth_gap",
        label="green ops mistaken for world-level completion",
        bug=(
            "Green readiness/SLO checks could be misreported as satisfying the entire "
            "world-level prompt even while prompt rows remained open."
        ),
        fix=(
            "Add prompt-to-artifact completion audit that intentionally blocks final claims "
            "until every row and required artifact is green."
        ),
        evidence=(
            EvidenceToken("cli/world_level_completion_audit.py", "completion_ready"),
            EvidenceToken("cli/world_level_completion_audit.py", "--fail-on-open"),
            EvidenceToken("tests/unit/test_world_level_completion_audit.py", "ready_for_final_review"),
            EvidenceToken("docs/world-level-brain-audit-2026-05-05.md", "Prompt-to-artifact checklist"),
        ),
    ),
)


def _read(path: str) -> str:
    try:
        return (ROOT / path).read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""


def evaluate_check(check: BugCheck) -> dict:
    missing_files = sorted({item.path for item in check.evidence if not (ROOT / item.path).exists()})
    missing_tokens = [asdict(item) for item in check.evidence if item.token not in _read(item.path)]
    forbidden_hits = [asdict(item) for item in check.forbidden if item.token in _read(item.path)]
    status = "pass" if not missing_files and not missing_tokens and not forbidden_hits else "blocked"
    return {
        "id": check.id,
        "label": check.label,
        "bug": check.bug,
        "fix": check.fix,
        "status": status,
        "missing_files": missing_files,
        "missing_tokens": missing_tokens,
        "forbidden_hits": forbidden_hits,
        "evidence_files": sorted({item.path for item in check.evidence}),
    }


def run(*, report_file: Path = REPORT_FILE, write_report: bool = True) -> dict:
    rows = [evaluate_check(check) for check in CHECKS]
    blocked = [row for row in rows if row["status"] != "pass"]
    report = {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "status": "ok" if not blocked else "blocked",
        "bug_classes_checked": len(rows),
        "bug_classes_locked": sum(1 for row in rows if row["status"] == "pass"),
        "blocked": len(blocked),
        "content_suppressed": True,
        "rows": rows,
    }
    if write_report:
        report_file.parent.mkdir(parents=True, exist_ok=True)
        report_file.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return report


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="World-level Brain executable bug-fix audit")
    parser.add_argument("--json", action="store_true", help="Emit JSON only")
    parser.add_argument("--fail-on-blocked", action="store_true", help="Exit 1 if any bug check is blocked")
    args = parser.parse_args(list(argv) if argv is not None else None)

    report = run()
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print(
            f"status={report['status']} locked={report['bug_classes_locked']}/{report['bug_classes_checked']}"
        )
        for row in report["rows"]:
            if row["status"] != "pass":
                print(f"BLOCKED: {row['id']}")
    return 1 if args.fail_on_blocked and report["status"] != "ok" else 0


if __name__ == "__main__":
    raise SystemExit(main())
