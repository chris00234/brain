#!/usr/bin/env python3
"""Render CRON_MAP.md from brain_core.job_definitions.JOB_SCHEDULE."""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "brain_core"))

from scheduler import JOB_SCHEDULE, ScheduledJob  # noqa: E402


def _trigger_text(job: ScheduledJob) -> str:
    raw = str(job.trigger)
    if raw.startswith("cron[") and raw.endswith("]"):
        body = raw.removeprefix("cron[").removesuffix("]")
        body = body.replace("'", "")
        return f"cron({body})"
    if raw.startswith("interval[") and raw.endswith("]"):
        return f"interval({raw.removeprefix('interval[').removesuffix(']')})"
    return raw


def _one_line(text: str) -> str:
    return " ".join((text or "").split())


def _md_escape(text: str) -> str:
    return _one_line(text).replace("|", "\\|")


def _render_table(jobs: list[ScheduledJob], *, include_agent: bool = False) -> list[str]:
    headers = ["Name", "Trigger"]
    if include_agent:
        headers.append("Agent")
    headers.extend(["Budget", "Tags", "Misfire Grace", "Description"])

    lines = [
        "| " + " | ".join(headers) + " |",
        "|" + "|".join("---" for _ in headers) + "|",
    ]
    for job in sorted(jobs, key=lambda j: j.name):
        row = [
            f"`{job.name}`",
            f"`{_trigger_text(job)}`",
        ]
        if include_agent:
            row.append(job.agent)
        tags = ", ".join(job.resource_tags) if job.resource_tags else "-"
        row.extend(
            [job.resource_class, _md_escape(tags), f"{job.misfire_grace}s", _md_escape(job.description)]
        )
        lines.append("| " + " | ".join(row) + " |")
    return lines


def render(jobs: list[ScheduledJob] | None = None) -> str:
    jobs = list(jobs or JOB_SCHEDULE)
    by_agent: dict[str, list[ScheduledJob]] = defaultdict(list)
    for job in jobs:
        by_agent[job.agent].append(job)

    lines = [
        "# Brain Scheduler Cron Map",
        "",
        "> Auto-generated from `brain_core/job_definitions.py` by `cli/render_cron_map.py`.",
        "> Do not hand-edit; run `.venv/bin/python cli/render_cron_map.py --write`.",
        "",
        f"**Total jobs**: {len(jobs)}",
        "**Default `misfire_grace`**: 300s (5min). Heavy nightly jobs override per job.",
        "",
        "## Jobs by owning agent",
        "",
    ]

    for agent in sorted(by_agent):
        agent_jobs = by_agent[agent]
        noun = "job" if len(agent_jobs) == 1 else "jobs"
        lines.extend([f"### {agent} ({len(agent_jobs)} {noun})", ""])
        lines.extend(_render_table(agent_jobs))
        lines.append("")

    lines.extend(["## All jobs", ""])
    lines.extend(_render_table(jobs, include_agent=True))
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="write CRON_MAP.md")
    parser.add_argument("--check", action="store_true", help="fail if CRON_MAP.md is stale")
    args = parser.parse_args()

    output = render()
    target = ROOT / "CRON_MAP.md"
    if args.write:
        target.write_text(output)
        return 0
    if args.check:
        current = target.read_text() if target.exists() else ""
        if current != output:
            print(
                "CRON_MAP.md is stale; run `.venv/bin/python cli/render_cron_map.py --write`", file=sys.stderr
            )
            return 1
        return 0
    print(output, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
