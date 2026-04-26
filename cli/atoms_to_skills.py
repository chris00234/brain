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
             ─→ emit runtime skill files:
                 ~/.claude/skills/brain-learned-<domain>/SKILL.md
                 ~/.codex/skills/brain-learned-<domain>/SKILL.md
                 ~/.openclaw/skills/brain-learned-<domain>/SKILL.md
             ─→ each SKILL has frontmatter (name/description) + bullet rules

Skills are loaded by the supported runtimes at session start. `description`
field determines contextual invocation. Domain scoping prevents "every rule
loaded every session" bloat that kills global guidance readability.

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

# 2026-04-17 multi-destination: generate skills for Claude Code, Codex, and
# OpenClaw. Format (YAML frontmatter + markdown body) is identical — the
# runtimes scan their respective skill directories at session start.
#
# Claude Code:
#   ~/.claude/skills/brain-learned-<domain>/SKILL.md
#
# Codex:
#   ~/.codex/skills/brain-learned-<domain>/SKILL.md
#
# OpenClaw (global, all 5 agents can invoke):
#   ~/.openclaw/skills/brain-learned-<domain>/SKILL.md
SKILL_DESTINATIONS = [
    Path.home() / ".claude" / "skills",
    Path.home() / ".codex" / "skills",
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
            "qdrant",
            "qdrant collection",
            "vector store",
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


_KEYWORD_CACHE: dict[str, re.Pattern] = {}


def _kw_pattern(kw: str) -> re.Pattern:
    """Compile + cache a word-boundary pattern for a keyword/phrase.

    Plain substring matching caused false positives — e.g. 'react' matched
    'reactivates', dragging an MCC-archive atom into coding-style. Word
    boundaries fix that while still matching multi-word phrases like
    'commit message' because re.escape + \\b works on spaces.
    """
    pat = _KEYWORD_CACHE.get(kw)
    if pat is None:
        pat = re.compile(r"\b" + re.escape(kw) + r"\b", re.I)
        _KEYWORD_CACHE[kw] = pat
    return pat


def classify(atom_text: str, topic_key: str | None) -> str:
    """Map atom → domain via word-boundary keyword match against topic_key + text.

    Strips canonical scaffolding from text before matching so preamble words
    like 'Summary' or 'Signal' don't leak into classification.
    """
    text_clean = _CANONICAL_PREAMBLE.sub(" ", atom_text[:600])
    haystack = f"{topic_key or ''} {text_clean}"
    for domain, keywords, _desc in DOMAIN_TAXONOMY:
        if any(_kw_pattern(kw).search(haystack) for kw in keywords):
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

# Narrative/synthesis markers — if present, atom is a session summary or
# consolidated note, not a durable rule. Hard reject.
_NARRATIVE_REJECTS = (
    "this consolidated page",
    "consolidated page captures",
    "chris screen time patterns",
    "chris's screen time patterns",
    "chris screen time",
    "signal: preference (score",
    "signal: decision (score",
    "signal: correction (score",
    "review this proposed canonical",
    "## source summary",
    "## observations",
    "## distilled evidence",
)

# Rule must contain at least one durability signal (Chris-verb, imperative,
# or ops-shaped directive). Broad enough to keep legitimate infra decisions.
_RULE_SIGNAL = re.compile(
    r"\bchris(?:'s|'s)?\s+(?:want|prefer|require|insist|expect|need|treat|value|hate|avoid|ask|request|use|maintain|keep|set|disable|enable|run|deploy|import|configure|reject|accept|allow)s?\b"
    r"|\b(?:never|always|must|should|don'?t|doesn'?t|do(?:es)? not|only|avoid|require[ds]?)\b"
    r"|\b(?:runs?|deployed|deployment|setup|rule|pattern)\s+(?:as|on|in|at|via|:|rule)\b"
    r"|\bmust be\b"
    r"|\bdeployment rule\b",
    re.I,
)


def _strip_duplicated_prefix(rule: str) -> str:
    """Cut the half-truncated title when atom shape is '[title-trunc] [body-with-same-start]'.

    The preamble stripper leaves behind:
      'Chris prefers X via subsc  Chris prefers X via subscription and ...'
    We detect the second occurrence of the first ~6 words and keep from there.
    """
    words = rule.split()
    if len(words) < 10:
        return rule
    sig = " ".join(words[:6])
    if len(sig) < 20:
        return rule
    # Search for the signature reappearing after position 20 (past the truncated copy)
    idx = rule.find(sig, 20)
    if 0 < idx < 200:
        return rule[idx:].strip()
    return rule


def _is_durable_rule(rule: str) -> bool:
    """Filter out session-narrative and synthesis-note atoms.

    Keeps rules that carry a durability signal (imperative verb, Chris-verb,
    or ops directive). Rejects consolidated-page summaries, screen-time
    narratives, and canonical-preamble leaks.
    """
    if len(rule) < 40:
        return False
    low = rule.lower()
    if any(m in low for m in _NARRATIVE_REJECTS):
        return False
    return bool(_RULE_SIGNAL.search(rule))


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
    joined = _strip_duplicated_prefix(joined)
    # Canonical atom bodies can concatenate a short rule with explanatory
    # markdown sections ("## Why", "## Context"). Skills should carry the
    # actionable rule, not the full canonical note prose.
    joined = re.split(
        r"\s+##\s+(?:why|context|source|observations|distilled evidence|merge suggestion)\b",
        joined,
        maxsplit=1,
        flags=re.I,
    )[0].strip()
    return joined[:300].strip()


def render_skill(domain: str, description: str, atoms: list[dict]) -> str:
    """Build SKILL.md content for a domain."""
    generated = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    rule_lines: list[str] = []
    seen_fps: set[str] = set()
    for a in atoms:
        rule = _extract_rule(a["text"])
        if not _is_durable_rule(rule):
            continue
        # Dedupe by normalized first 120 chars (strip punctuation, collapse spaces).
        # Previous 80-char prefix dedup left 4 near-identical Claude-subscription rules
        # side by side because early words diverged but the underlying rule was identical.
        norm = re.sub(r"[^a-z0-9 ]+", "", rule[:120].lower())
        norm = re.sub(r"\s+", " ", norm).strip()
        fp = norm[:100]
        if fp in seen_fps:
            continue
        seen_fps.add(fp)
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


def _runtime_for_skill_path(path: Path) -> str:
    parts = set(path.parts)
    if ".claude" in parts:
        return "claude"
    if ".codex" in parts:
        return "codex"
    if ".openclaw" in parts:
        return "openclaw"
    return "unknown"


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

        # Write to each destination (Claude Code + Codex + OpenClaw). Identical content.
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
                    "runtime": _runtime_for_skill_path(skill_file),
                    "n_atoms": len(atoms),
                }
            )

    return stats


def prune_orphan_skills(available_domains: set[str], dry_run: bool = False) -> dict:
    """Remove brain-learned-<domain> dirs whose domain no longer has atoms.

    Safety guard: if fewer than 3 domains survived this run, skip pruning —
    likely a filter regression or empty DB, not a legitimate domain collapse.
    """
    stats: dict = {"pruned": [], "kept": []}
    if len(available_domains) < 3:
        stats["skipped_reason"] = f"only {len(available_domains)} domains — refusing to prune"
        return stats

    for dest_root in SKILL_DESTINATIONS:
        if not dest_root.exists():
            continue
        for child in dest_root.iterdir():
            if not child.is_dir() or not child.name.startswith(SKILL_PREFIX):
                continue
            domain = child.name[len(SKILL_PREFIX) :]
            if domain in available_domains:
                stats["kept"].append(str(child))
                continue
            if dry_run:
                stats["pruned"].append({"path": str(child), "dry_run": True})
                continue
            # Delete the orphan SKILL dir (SKILL.md + dir). Only inside our namespace.
            for f in child.iterdir():
                f.unlink()
            child.rmdir()
            stats["pruned"].append({"path": str(child)})
    return stats


def run_openclaw_skill_sync(dry_run: bool = False) -> dict:
    """Delegate OpenClaw registry + per-agent allowlist updates to skill_sync.

    atoms_to_skills owns atom→SKILL.md rendering. OpenClaw config mutation has
    exactly one owner (`cli/skill_sync.py`) so generated brain-learned-* and
    auto-* skills cannot drift through two separate reconciliation paths.
    """
    try:
        from skill_sync import attach_generated_skills, reconcile_registry

        registry = reconcile_registry(dry_run=dry_run)
        attach = attach_generated_skills(dry_run=dry_run)
        return {"ok": True, "registry": registry, "attach": attach, "dry_run": dry_run}
    except Exception as exc:
        return {"ok": False, "reason": str(exc)[:200], "dry_run": dry_run}


def sync_openclaw_agents(written_domains: set[str], dry_run: bool = False) -> dict:
    """Compatibility shim; use run_openclaw_skill_sync for new code."""
    _ = written_domains
    return run_openclaw_skill_sync(dry_run=dry_run)


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

    # Domains that successfully produced a skill file OR are unchanged (already on disk).
    # Both count as "available" — we want all to be allowlisted per-agent.
    # In dry-run mode, write_skills routes these to 'skipped' with reason=dry_run,
    # so we also count dry_run skips as available so the sync preview is accurate.
    available_domains = (
        {e["domain"] for e in stats["written"]}
        | {e["domain"] for e in stats["unchanged"]}
        | {e["domain"] for e in stats["skipped"] if e.get("reason") == "dry_run"}
    )
    prune_stats = prune_orphan_skills(available_domains, dry_run=args.dry_run)
    stats["orphan_prune"] = prune_stats

    sync_stats = run_openclaw_skill_sync(dry_run=args.dry_run)
    stats["openclaw_sync"] = sync_stats

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
        print()
        pruned = prune_stats.get("pruned", [])
        if prune_stats.get("skipped_reason"):
            print(f"Orphan prune: SKIPPED ({prune_stats['skipped_reason']})")
        elif pruned:
            print(f"Orphan prune: {len(pruned)} dirs removed")
            for p in pruned:
                print(f"  - {p['path']}")
        print()
        if not sync_stats.get("ok"):
            print(f"OpenClaw sync: SKIPPED ({sync_stats.get('reason')})")
        else:
            attach = sync_stats.get("attach", {})
            print(f"OpenClaw sync: {len(attach.get('agents_touched', []))} agents updated")
            if attach.get("attached"):
                print(f"  attached generated skills: {attach['attached']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
