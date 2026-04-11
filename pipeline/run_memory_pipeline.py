from __future__ import annotations

import argparse
import json
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from common import ROOT, dump_json

PIPELINE_DIR = Path(__file__).resolve().parent


def run_command(command: list[str], cwd: Path) -> dict[str, Any]:
    result = subprocess.run(command, cwd=cwd, text=True, capture_output=True, check=False)
    payload: Any
    try:
        payload = json.loads(result.stdout) if result.stdout.strip() else None
    except json.JSONDecodeError:
        payload = result.stdout.strip()
    return {
        "command": command,
        "returncode": result.returncode,
        "stdout": payload,
        "stderr": result.stderr.strip(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run export bridge + raw ingest + batch distill pipeline")
    parser.add_argument("--session-export", type=Path)
    parser.add_argument("--source-name", default="session-export")
    parser.add_argument("--raw-output", type=Path, default=ROOT / "raw" / "inbox")
    parser.add_argument("--distilled-output", type=Path, default=ROOT / "distilled")
    parser.add_argument("--report", type=Path, default=ROOT / "reports" / "review-queue" / "pipeline_report.json")
    parser.add_argument("--review-queue", type=Path, default=ROOT / "reports" / "review-queue")
    parser.add_argument("--generate-proposals", action="store_true")
    parser.add_argument("--proposal-min-confidence", type=float, default=0.60)
    parser.add_argument("--proposal-min-sources", type=int, default=1)
    parser.add_argument("--proposal-merge-threshold", type=float, default=0.45)
    parser.add_argument("--proposal-max-merge-hints", type=int, default=2)
    parser.add_argument("--proposal-duplicate-threshold", type=float, default=0.82)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    steps: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="memory-pipeline-") as temp_dir:
        temp_path = Path(temp_dir)
        bridged_jsonl = temp_path / "bridged.jsonl"

        if args.session_export:
            bridge_cmd = [
                "python3",
                str(PIPELINE_DIR / "bridge_exports.py"),
                "session-export",
                str(args.session_export),
                "--output-file",
                str(bridged_jsonl),
            ]
            if args.dry_run:
                steps.append({"command": bridge_cmd, "dry_run": True})
            else:
                steps.append(run_command(bridge_cmd, ROOT))

            ingest_cmd = [
                "python3",
                str(PIPELINE_DIR / "ingest_adapters.py"),
                "chat-jsonl",
                str(bridged_jsonl),
                "--source-name",
                args.source_name,
                "--output-dir",
                str(args.raw_output),
            ]
            if args.dry_run:
                steps.append({"command": ingest_cmd, "dry_run": True})
            else:
                steps.append(run_command(ingest_cmd, ROOT))

        distill_cmd = [
            "python3",
            str(PIPELINE_DIR / "batch_distill.py"),
            "--input-dir",
            str(args.raw_output),
            "--output-dir",
            str(args.distilled_output),
        ]
        if args.dry_run:
            steps.append({"command": distill_cmd, "dry_run": True})
        else:
            steps.append(run_command(distill_cmd, ROOT))

        if args.generate_proposals:
            propose_cmd = [
                "python3",
                str(PIPELINE_DIR / "batch_propose.py"),
                "--input-dir",
                str(args.distilled_output),
                "--review-queue",
                str(args.review_queue),
                "--manifest",
                str(args.review_queue / "batch_propose_manifest.json"),
                "--min-confidence",
                str(args.proposal_min_confidence),
                "--min-sources",
                str(args.proposal_min_sources),
                "--merge-threshold",
                str(args.proposal_merge_threshold),
                "--max-merge-hints",
                str(args.proposal_max_merge_hints),
                "--duplicate-threshold",
                str(args.proposal_duplicate_threshold),
            ]
            if args.dry_run:
                steps.append({"command": propose_cmd, "dry_run": True})
            else:
                steps.append(run_command(propose_cmd, ROOT))

    report = {
        "status": "ok",
        "dry_run": args.dry_run,
        "steps": steps,
    }
    dump_json(args.report, report)
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
