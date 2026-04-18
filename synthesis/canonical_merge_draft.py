#!/opt/homebrew/bin/python3
"""Compaction Phase 2 — Sage-drafted consolidated canonical pages.

Reads the latest canonical_compaction cluster report and dispatches Sage to
produce a single consolidated canonical draft per cluster. Drafts land in
`reports/canonical_compaction/drafts/YYYY-MM-DD/` with `status: draft`
frontmatter. They are NOT promoted automatically — Chris reviews, then
manually applies.

Cost bound: default N=3 clusters per run (~$0.05-0.15 LLM cost).

Usage:
  canonical_merge_draft.py [--limit 3] [--dry-run] [--cluster-ids 0,1,2]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "brain_core"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "pipeline"))

from cli_llm import dispatch  # migrated 2026-04-17
from common import ROOT, parse_note, render_note

REPORT_DIR = ROOT / "reports" / "canonical_compaction"
DRAFTS_BASE = REPORT_DIR / "drafts"
LOGS_DIR = Path("/Users/chrischo/server/brain/logs")

DEFAULT_LIMIT = 3
MEMBER_BODY_LIMIT = 800
DISPATCH_TIMEOUT = 300

PROMPT = """You are Sage. Merge the canonical notes below into one consolidated canonical page.

These N notes cluster by embedding similarity and are candidates for consolidation
into a single higher-quality page. Your job is to produce a consolidated note that
captures ALL the knowledge without losing specificity.

CLUSTER METADATA:
- size: {size} notes
- avg_sim: {avg_sim}
- dominant domains: {domains}

MEMBER NOTES:
{member_block}

Return ONLY a JSON object (no prose, no fences):
{{
  "title": "human-readable title under 100 chars that captures the consolidated topic",
  "summary": "2-3 sentences describing what this consolidated page IS",
  "key_facts": ["fact1", "fact2", "fact3", "fact4", "fact5"],
  "entities": ["mentioned entity 1", "entity 2"],
  "provenance_summary": "1 sentence explaining consolidation source",
  "domain": "chris|decisions|infra|projects|incidents",
  "superseded_note_ids": ["id1", "id2", ...]
}}

Rules:
- Preserve every distinct fact from the members — do NOT drop specificity
- Key facts must be concrete ground truths, not meta-observations
- superseded_note_ids MUST list every member note id (comma-separated)
- domain: pick the most common from the member domains
- Do NOT wrap in ```json``` fences

JSON only:"""


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return slug[:80]


def _latest_report() -> Path | None:
    if not REPORT_DIR.exists():
        return None
    reports = sorted(REPORT_DIR.glob("*.json"), reverse=True)
    return reports[0] if reports else None


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _load_member(note_rel: str) -> tuple[dict, str] | None:
    path = ROOT / note_rel
    if not path.exists():
        return None
    try:
        meta, body = parse_note(path)
    except Exception:
        return None
    return meta, body


def _build_prompt(cluster: dict) -> tuple[str, list[str]]:
    """Returns (prompt, loaded_note_ids)."""
    members = cluster.get("members") or []
    blocks = []
    loaded_ids = []
    for idx, m in enumerate(members, 1):
        loaded = _load_member(m["path"])
        if loaded is None:
            continue
        meta, body = loaded
        nid = meta.get("id") or m["id"]
        title = meta.get("title") or m.get("title", "")
        loaded_ids.append(nid)
        body_snippet = body.strip()[:MEMBER_BODY_LIMIT]
        blocks.append(f"[{idx}] id={nid}\n  title: {title}\n  body: {body_snippet}")
    member_block = "\n\n".join(blocks) or "_no readable members_"
    prompt = PROMPT.format(
        size=cluster.get("size", 0),
        avg_sim=cluster.get("avg_sim", 0),
        domains=", ".join(cluster.get("domains", [])),
        member_block=member_block[:12000],
    )
    return prompt, loaded_ids


def _dispatch_sage(prompt: str) -> dict | None:
    result = dispatch(
        agent="sage",
        message=prompt,
        thinking="medium",
        timeout=DISPATCH_TIMEOUT,
        backlog_kind="synthesis",
        backlog_payload={"source": "canonical_merge_draft"},
    )
    if not result.ok:
        print(f"  sage dispatch failed: {(result.error or '')[:200]}", file=sys.stderr)
        return None
    text = (result.text or "").strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        brace_start = text.find("{")
        brace_end = text.rfind("}")
        if brace_start != -1 and brace_end > brace_start:
            try:
                return json.loads(text[brace_start : brace_end + 1])
            except Exception:
                pass
        print(f"  sage returned non-JSON: {text[:200]}", file=sys.stderr)
        return None


def _render_draft_body(synth: dict, cluster_idx: int, cluster_size: int) -> str:
    lines = [
        "## Summary",
        "",
        (synth.get("summary") or "_pending_").strip(),
        "",
        "## Key Facts",
        "",
    ]
    facts = synth.get("key_facts") or []
    for f in facts:
        if isinstance(f, str) and f.strip():
            lines.append(f"- {f.strip()}")
    lines.append("")
    entities = synth.get("entities") or []
    if entities:
        lines.append("## Mentioned Entities")
        lines.append("")
        for e in entities:
            if isinstance(e, str) and e.strip():
                lines.append(f"- {e.strip()}")
        lines.append("")
    lines.append("---")
    lines.append(f"_Draft consolidated from cluster {cluster_idx} ({cluster_size} notes)._")
    lines.append("_Review before promoting to canonical/._")
    return "\n".join(lines)


def _write_draft(cluster_idx: int, cluster: dict, synth: dict, loaded_ids: list[str]) -> Path:
    date = datetime.now(UTC).strftime("%Y-%m-%d")
    out_dir = DRAFTS_BASE / date
    out_dir.mkdir(parents=True, exist_ok=True)
    now = _utc_now()

    title = (synth.get("title") or cluster.get("representative_title") or f"Cluster {cluster_idx}")[:180]
    slug = _slugify(title) or f"cluster_{cluster_idx}"
    note_id = f"draft_merge_cluster_{cluster_idx}_{slug[:50]}"

    _VALID_DOMAINS = {"chris", "projects", "infra", "decisions", "incidents", "entities"}
    raw_domain = synth.get("domain") or (cluster.get("domains") or ["decisions"])[0]
    domain = raw_domain if raw_domain in _VALID_DOMAINS else "decisions"
    superseded = synth.get("superseded_note_ids") or []
    # Always include all loaded member ids even if Sage omitted them
    superseded = sorted(set(list(superseded) + loaded_ids))

    meta = {
        "id": note_id,
        "type": "canonical",
        "domain": domain,
        "subtype": "consolidated-draft",
        "title": title,
        "status": "draft",
        "visibility": "private",
        "confidence": 0.75,
        "created_at": now,
        "updated_at": now,
        "last_reviewed_at": now,
        "owner": "system",
        "scope": "global",
        "valid_from": None,
        "valid_to": None,
        "sources": [
            "sage:canonical_merge_draft",
            f"cluster:{cluster_idx}",
            f"compaction_report:{date}",
        ],
        "provenance_summary": (
            synth.get("provenance_summary")
            or f"Consolidated draft from cluster {cluster_idx} ({cluster.get('size', 0)} member notes)"
        )[:300],
        "entities": synth.get("entities") or [],
        "relations": [{"type": "supersedes", "target": sid} for sid in superseded],
        "review_state": "proposed",
        "change_policy": "review_required",
        "supersedes": superseded,
        "superseded_by": None,
    }
    body = _render_draft_body(synth, cluster_idx, cluster.get("size", 0))
    path = out_dir / f"cluster_{cluster_idx:02d}_{slug[:50]}.md"
    path.write_text(render_note(meta, body))
    return path


def _log(record: dict) -> None:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    with (LOGS_DIR / "canonical_merge_draft.jsonl").open("a") as f:
        f.write(json.dumps(record) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument("--dry-run", action="store_true", help="show prompts without dispatching")
    parser.add_argument("--cluster-ids", type=str, help="comma-separated cluster indices to force")
    args = parser.parse_args()

    report_path = _latest_report()
    if not report_path:
        print(json.dumps({"status": "no_report"}))
        return 1

    report = json.loads(report_path.read_text())
    clusters = sorted(report.get("clusters", []), key=lambda c: -c.get("size", 0))
    if not clusters:
        print(json.dumps({"status": "no_clusters"}))
        return 0

    if args.cluster_ids:
        ids = [int(x) for x in args.cluster_ids.split(",") if x.strip()]
        selected = [clusters[i] for i in ids if 0 <= i < len(clusters)]
    else:
        selected = clusters[: args.limit]

    print(
        f"[canonical_merge_draft] report={report_path.name} selected={len(selected)} clusters",
        file=sys.stderr,
    )

    processed: list[dict] = []
    for cluster_idx, cluster in enumerate(selected):
        print(
            f"  [{cluster_idx}] size={cluster.get('size', 0)} repr={cluster.get('representative_title', '')[:60]}",
            file=sys.stderr,
        )
        prompt, loaded_ids = _build_prompt(cluster)
        if args.dry_run:
            processed.append(
                {
                    "cluster_idx": cluster_idx,
                    "size": cluster.get("size"),
                    "prompt_chars": len(prompt),
                    "loaded_ids": len(loaded_ids),
                    "status": "dry-run",
                }
            )
            continue

        synth = _dispatch_sage(prompt)
        if synth is None:
            processed.append(
                {
                    "cluster_idx": cluster_idx,
                    "status": "dispatch_failed",
                }
            )
            _log(
                {
                    "at": _utc_now(),
                    "cluster_idx": cluster_idx,
                    "status": "dispatch_failed",
                }
            )
            continue

        path = _write_draft(cluster_idx, cluster, synth, loaded_ids)
        processed.append(
            {
                "cluster_idx": cluster_idx,
                "size": cluster.get("size"),
                "path": str(path.relative_to(ROOT)),
                "title": synth.get("title", "")[:80],
                "superseded": len(synth.get("superseded_note_ids") or loaded_ids),
                "status": "drafted",
            }
        )
        _log(
            {
                "at": _utc_now(),
                "cluster_idx": cluster_idx,
                "path": str(path),
                "status": "drafted",
            }
        )

    print(json.dumps({"status": "ok", "processed": processed}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
