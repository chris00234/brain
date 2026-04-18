#!/opt/homebrew/bin/python3
"""ECC-style skill evolution from atoms (2026-04-17).

Inspired by friend's `memory-to-instincts.py` + `evolve --generate` pipeline.
Chris's brain atoms (tier=core|semantic, kind=preference|decision|correction)
capture durable rules he's stated. Most never make it into CLAUDE.md because
they're domain-specific or emerged mid-session. This script auto-promotes
high-confidence atoms into Claude Code skills that activate contextually
instead of polluting the global CLAUDE.md.

Flow:
    atoms.db ─→ group by domain (keyword-based taxonomy)
             ─→ per-domain: rank by confidence + recency, cap at top N
             ─→ emit ~/.claude/skills/brain-learned-<domain>/SKILL.md
             ─→ each SKILL has frontmatter (name/description) + bullet rules

Skills loaded by Claude Code at session start. `description` field determines
when Claude auto-invokes it. Domain scoping prevents "every rule loaded every
session" bloat that kills CLAUDE.md readability.

Non-destructive: script only WRITES skill files. Source atoms stay in brain.db.
Idempotent: deletes + rewrites per-domain skill each run (weekly scheduled).
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

BRAIN_DB = Path("/Users/chrischo/server/brain/logs/brain.db")
SKILL_PREFIX = "brain-learned-"

# 2026-04-17 multi-destination: generate skills for both Claude Code and
# OpenClaw. Format (YAML frontmatter + markdown body) is identical — both
# runtimes scan their respective skill directories at session start.
#
# Claude Code:
#   ~/.claude/skills/brain-learned-<domain>/SKILL.md
#
# OpenClaw (global, all 5 agents can invoke):
#   ~/.openclaw/skills/brain-learned-<domain>/SKILL.md
SKILL_DESTINATIONS = [
    Path.home() / ".claude" / "skills",
    Path.home() / ".openclaw" / "skills",
]

# Domain taxonomy — keywords mapped to domains. Order matters: first match wins.
# Keywords are matched against topic_key + text (case-insensitive).
DOMAIN_TAXONOMY: list[tuple[str, list[str], str]] = [
    (
        "llm-budget",
        [
            "llm spend",
            "api billing",
            "api key",
            "subscription",
            "additional paid",
            "paid api",
            "extra charge",
            "chatgpt pro",
            "claude max",
            "api usage",
        ],
        "Chris's LLM budget and subscription rules. Use when deciding how to "
        "dispatch LLM calls (CLI vs API vs OpenClaw), picking models, or estimating costs.",
    ),
    (
        "infra-ops",
        [
            "docker",
            "nginx",
            "cloudflare",
            "orbstack",
            "homelab",
            "container",
            "compose",
            "reverse proxy",
            "ingress",
            "chromadb",
            "ollama",
            "neo4j",
            "launchd",
            "launchagent",
            "minio",
            "backup",
        ],
        "Chris's infrastructure conventions — Docker, nginx, Cloudflare, homelab, "
        "launchd, backups. Use when modifying ops, deploying services, or touching config files.",
    ),
    (
        "brain-system",
        [
            "brain",
            "rag",
            "second-brain",
            "canonical",
            "atoms",
            "semantic memory",
            "chromadb collection",
            "embedding",
            "retrieval",
            "eval",
            "slo",
            "recall/v2",
            "brain-ui",
        ],
        "Chris's second-brain system design decisions. Use when working on the brain "
        "codebase (~/server/brain/), RAG pipeline, or brain-ui dashboard.",
    ),
    (
        "agent-orchestration",
        [
            "openclaw",
            "jenna",
            "liz",
            "ellie",
            "sage",
            "market",
            "agent handoff",
            "inter-agent",
            "session_id",
            "acp",
            "gateway",
        ],
        "Chris's OpenClaw multi-agent operating model — Jenna/Liz/Ellie/Sage/Market "
        "roles, handoff protocol, execution/reporting contract. Use when orchestrating "
        "or dispatching to any OpenClaw agent.",
    ),
    (
        "claude-code-ops",
        [
            "claude code",
            "claude md",
            "claude.md",
            "mcp",
            "hook",
            "sessionend",
            "userpromptsubmit",
            "tool call",
            "interactive agent",
        ],
        "How Chris wants Claude Code to behave — hooks, CLAUDE.md rules, MCP tool "
        "usage, session handling. Use when touching ~/.claude/ configs or hook scripts.",
    ),
    (
        "coding-style",
        [
            "typescript",
            "react",
            "vite",
            "tailwind",
            "shadcn",
            "next.js",
            "fastapi",
            "python",
            "conventional commits",
            "strict mode",
            "interface",
            "functional component",
        ],
        "Chris's code style preferences — TypeScript strict, React functional, "
        "Tailwind, FastAPI, conventional commits. Use when writing or reviewing code.",
    ),
    (
        "communication",
        [
            "communication",
            "concise",
            "direct",
            "emoji",
            "summary",
            "commit message",
            "report format",
            "korean",
            "response format",
            "bilingual",
        ],
        "How Chris wants responses formatted — direct, concise, no emoji, no filler, "
        "skip summaries. Use every turn in interactive conversation.",
    ),
    (
        "scheduling",
        [
            "quiet hours",
            "work hours",
            "9am-6pm",
            "off-hours",
            "nightly",
            "cron",
            "scheduled",
            "alert",
            "telegram alert",
        ],
        "Chris's scheduling + alert preferences — quiet hours 22:30-07:30 PT, "
        "no heavy LLM/Ollama during 9-6pm, alert discipline. Use when scheduling "
        "jobs or sending notifications.",
    ),
]

DEFAULT_DOMAIN = (
    "general",
    [],
    "Miscellaneous durable preferences from Chris that didn't match a specific domain.",
)


def classify(atom_text: str, topic_key: str | None) -> str:
    """Map atom → domain via keyword match against topic_key + text."""
    haystack = f"{topic_key or ''} {atom_text[:500]}".lower()
    for domain, keywords, _desc in DOMAIN_TAXONOMY:
        if any(kw in haystack for kw in keywords):
            return domain
    return DEFAULT_DOMAIN[0]


def load_atoms() -> list[dict]:
    """Load high-value atoms for skill synthesis."""
    conn = sqlite3.connect(str(BRAIN_DB))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT id, text, kind, topic_key, confidence, tier, updated_at, scope
        FROM atoms
        WHERE tier IN ('core', 'semantic')
          AND kind IN ('preference', 'decision', 'correction')
          AND (superseded_by IS NULL OR superseded_by = '')
          AND confidence >= 0.65
        ORDER BY confidence DESC, updated_at DESC
        """
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# Preamble can appear mid-string (title + body concatenated), so no ^ anchor.
_CANONICAL_PREAMBLE = re.compile(
    r"#\s*Summary\s*(?:OpenClaw\s+\w+\s+session\s+\([^)]+\)\s+)?" r"Signal:\s*\w+\s*\(score\s+\d+/\d+\)\s*",
    re.I,
)
# Also catch the "title" repetition pattern: atom text starts with the title
# words, then the body starts with "# Summary ..." with the same title repeated.
_DUP_TITLE_SEP = re.compile(r"^(.{30,}?)\s*#\s*Summary\s+", re.I)


def _extract_rule(text: str) -> str:
    """Trim atom text down to a single actionable rule line.

    Atoms often start with:
      - canonical-note scaffolding ("## Statement\n\nReview this proposed...")
      - OpenClaw session preamble ("# Summary OpenClaw jenna session (2026-04-01) Signal: preference (score 10/10)")
      - JSON fragments at split boundaries

    Strip all that, return the claim only. Cap at 300 chars.
    """
    lines = text.splitlines()
    meaningful: list[str] = []
    skip_prefixes = (
        "## Statement",
        "## Source Summary",
        "## Observations",
        "## Distilled Evidence",
        "## Merge Suggestion",
        "## Summary",
        "---json",
        "---",
        "Review this proposed",
        "- Derived from raw",
    )
    for ln in lines:
        s = ln.strip()
        if not s:
            continue
        if any(s.startswith(p) for p in skip_prefixes):
            continue
        # Skip lines that look like JSON fragments
        if s.startswith(('"', '",')) or s.endswith((":", ",", "[", "{")):
            continue
        meaningful.append(s)
        if len(" ".join(meaningful)) > 400:
            break
    joined = " ".join(meaningful).strip()
    # First: remove the canonical preamble anywhere it appears.
    joined = _CANONICAL_PREAMBLE.sub(" ", joined)
    # Second: if the text has a "title + # Summary + body" shape, prefer body.
    # Ex: "Chris prefers X via subscription # Summary OpenClaw jenna session (...)
    #      Chris prefers X via subscription-based setup..."
    # The real claim is the body AFTER "# Summary ...". Cut off the duplicated
    # title prefix by finding "# Summary" and keeping what's after.
    cut_idx = joined.find("# Summary")
    if cut_idx > 0:
        joined = joined[cut_idx + len("# Summary") :].strip()
        joined = _CANONICAL_PREAMBLE.sub(" ", joined)
    joined = re.sub(r"\s+", " ", joined).strip()
    return joined[:300].strip()


def render_skill(domain: str, description: str, atoms: list[dict]) -> str:
    """Build SKILL.md content for a domain."""
    generated = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    rule_lines: list[str] = []
    seen_rules: set[str] = set()
    for a in atoms:
        rule = _extract_rule(a["text"])
        if not rule or len(rule) < 30:
            continue
        # Dedupe by first 80 chars
        fp = rule[:80].lower()
        if fp in seen_rules:
            continue
        seen_rules.add(fp)
        kind = a["kind"]
        conf = f"{a['confidence']:.2f}"
        rule_lines.append(f"- **[{kind} · {conf}]** {rule}")
        if len(rule_lines) >= 12:  # cap so skills stay readable
            break

    if not rule_lines:
        return ""

    return f"""---
name: brain-learned-{domain}
description: {description}
---

# Chris's durable rules — {domain}

> Auto-generated from brain atoms (tier=core/semantic, kind=preference/decision/correction,
> confidence ≥ 0.65). Regenerated weekly by `atoms_to_skills.py`.
>
> Last updated: {generated} · {len(rule_lines)} rules from {len(atoms)} atoms

## Rules

{chr(10).join(rule_lines)}

## Source

These rules come from Chris's brain atoms database (`brain.db:atoms`). Each
bullet is a durable preference/decision/correction Chris has stated in prior
sessions. Confidence score is the brain's own calibration (higher = more
reinforcement).

To override or update a rule, tell Chris directly in-session — the SessionEnd
learn pipeline will capture the correction, the confidence ledger will weight
new input, and next week's regeneration will reflect it.

If a rule here contradicts something Chris just said, **the new statement wins**
— brain memory is a starting point, not a veto.
"""


def write_skills(by_domain: dict[str, list[dict]], dry_run: bool = False) -> dict:
    stats: dict = {"written": [], "skipped": [], "total_atoms": 0, "unchanged": []}
    for dest in SKILL_DESTINATIONS:
        dest.mkdir(parents=True, exist_ok=True)

    # Build description map
    desc_map = {d[0]: d[2] for d in DOMAIN_TAXONOMY}
    desc_map[DEFAULT_DOMAIN[0]] = DEFAULT_DOMAIN[2]

    for domain, atoms in by_domain.items():
        stats["total_atoms"] += len(atoms)
        content = render_skill(domain, desc_map.get(domain, ""), atoms)
        if not content:
            stats["skipped"].append({"domain": domain, "reason": "no_renderable_rules", "n": len(atoms)})
            continue

        # Write to each destination (Claude Code + OpenClaw). Identical content.
        for dest_root in SKILL_DESTINATIONS:
            skill_dir = dest_root / f"{SKILL_PREFIX}{domain}"
            skill_file = skill_dir / "SKILL.md"

            if skill_file.exists() and skill_file.read_text() == content:
                stats["unchanged"].append({"domain": domain, "path": str(skill_file)})
                continue

            if dry_run:
                stats["skipped"].append({"domain": domain, "reason": "dry_run", "path": str(skill_file)})
                continue

            skill_dir.mkdir(parents=True, exist_ok=True)
            skill_file.write_text(content)
            stats["written"].append(
                {
                    "domain": domain,
                    "path": str(skill_file),
                    "runtime": "claude" if ".claude/" in str(skill_file) else "openclaw",
                    "n_atoms": len(atoms),
                }
            )

    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate Claude Code skills from brain atoms")
    parser.add_argument("--dry-run", action="store_true", help="Don't write files")
    parser.add_argument("--json", action="store_true", help="JSON output")
    args = parser.parse_args()

    atoms = load_atoms()
    by_domain: dict[str, list[dict]] = defaultdict(list)
    for a in atoms:
        by_domain[classify(a["text"], a.get("topic_key"))].append(a)

    # Rank within domain by confidence then recency; keep top 25 per domain.
    # 2026-04-17 fix: prior sort used `a.get("updated_at") or ""` ascending,
    # so among atoms with equal confidence, OLDEST won. Invert with reverse=True
    # on the secondary key by sorting tuple (negated confidence, -timestamp_epoch).
    def _recency_epoch(a: dict) -> float:
        ts = a.get("updated_at") or ""
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
        except Exception:
            return 0.0

    for d in by_domain:
        by_domain[d].sort(key=lambda a: (-a["confidence"], -_recency_epoch(a)))
        by_domain[d] = by_domain[d][:25]

    stats = write_skills(by_domain, dry_run=args.dry_run)

    if args.json:
        print(json.dumps(stats, indent=2, default=str))
    else:
        print(f"Source atoms considered: {len(atoms)}")
        print(f"Domains: {len(by_domain)}")
        for d, al in sorted(by_domain.items(), key=lambda x: -len(x[1])):
            print(f"  {d}: {len(al)}")
        print()
        print(f"Skills written:   {len(stats['written'])}")
        print(f"Skills unchanged: {len(stats['unchanged'])}")
        print(f"Skills skipped:   {len(stats['skipped'])}")
        for w in stats["written"]:
            print(f"  WROTE {w['domain']} ({w['n_atoms']} atoms) → {w['path']}")
        for s in stats["skipped"]:
            print(f"  SKIP  {s['domain']}: {s['reason']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
