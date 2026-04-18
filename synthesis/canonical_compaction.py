#!/opt/homebrew/bin/python3
"""Canonical compaction — cluster similar notes for merge proposals.

The biggest performance lever for the brain (llm-wiki comparison):
262 fragment-sized canonical notes should consolidate into ~40-60
high-quality pages. This job finds candidate clusters via embedding
cosine similarity.

Pipeline:
  1. Load all active canonical notes
  2. Embed title + body[:500] for each (cached via embedding_cache.db)
  3. Compute pairwise cosine similarity
  4. Union-find cluster with threshold 0.85
  5. Filter clusters ≥3 members
  6. Write cluster report to reports/canonical_compaction/YYYY-MM-DD.{json,md}
  7. (next phase) Dispatch Sage to draft consolidated pages

Usage:
  canonical_compaction.py [--threshold 0.85] [--min-cluster 3] [--dry-run]

This is deliberately report-only for now. Human reviews clusters before
any merges land. Merge drafts + auto-apply are follow-ups.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "pipeline"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "brain_core"))

from common import ROOT, iter_note_paths, parse_note
from indexer import get_embedding

CANONICAL_DIR = ROOT / "canonical"
REPORT_DIR = ROOT / "reports" / "canonical_compaction"
SKIP_NAMES = {"index.md", "_index.md", "_identity.md", "_state.md", "_profile.md"}

DEFAULT_THRESHOLD = 0.94
DEFAULT_MIN_CLUSTER = 3
DEFAULT_MAX_CLUSTER = 20
EMBED_TEXT_LIMIT = 600


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _load_notes() -> list[dict]:
    out = []
    for path in iter_note_paths(CANONICAL_DIR):
        if path.name in SKIP_NAMES or ".bak" in path.suffix:
            continue
        try:
            meta, body = parse_note(path)
        except Exception:
            continue
        if meta.get("type") != "canonical" or meta.get("status") != "active":
            continue
        text = ((meta.get("title") or path.stem) + "\n" + (body or ""))[:EMBED_TEXT_LIMIT]
        out.append(
            {
                "id": meta.get("id") or path.stem,
                "title": meta.get("title") or path.stem,
                "domain": meta.get("domain") or "other",
                "subtype": meta.get("subtype") or "",
                "path": str(path.relative_to(ROOT)),
                "text": text,
                "created_at": meta.get("created_at") or "",
                "updated_at": meta.get("updated_at") or "",
            }
        )
    return out


def _embed_all(notes: list[dict]) -> None:
    total = len(notes)
    for i, n in enumerate(notes):
        try:
            n["embedding"] = get_embedding(n["text"])
        except Exception as e:
            print(f"  embed failed for {n['id']}: {e}", file=sys.stderr)
            n["embedding"] = None
        if (i + 1) % 50 == 0:
            print(f"  embedded {i + 1}/{total}", file=sys.stderr)


class _UnionFind:
    def __init__(self, n: int) -> None:
        self.parent = list(range(n))

    def find(self, x: int) -> int:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[ra] = rb


def _cluster(
    notes: list[dict], threshold: float, max_size: int
) -> tuple[list[list[int]], list[tuple[float, int, int]]]:
    """Greedy clique-like clustering.

    Simple union-find over-clusters because similarity chains transitively.
    Instead: for each note, find its top neighbors at or above threshold,
    build undirected edges, then do connected components BUT cap cluster
    size by splitting on weakest intra-cluster edges.
    """
    valid = [i for i, n in enumerate(notes) if n.get("embedding")]
    edges: list[tuple[float, int, int]] = []
    for ai, i in enumerate(valid):
        ei = notes[i]["embedding"]
        for j in valid[ai + 1 :]:
            ej = notes[j]["embedding"]
            sim = _cosine(ei, ej)
            if sim >= threshold:
                edges.append((sim, i, j))
    # Sort edges strongest first for greedy component merging
    edges.sort(reverse=True)

    uf = _UnionFind(len(notes))
    # Track cluster size so we don't create mega-clusters
    sizes: dict[int, int] = {i: 1 for i in valid}
    for sim, a, b in edges:
        ra, rb = uf.find(a), uf.find(b)
        if ra == rb:
            continue
        if sizes[ra] + sizes[rb] > max_size:
            continue  # skip merges that would overflow
        uf.union(a, b)
        new_root = uf.find(a)
        sizes[new_root] = sizes[ra] + sizes[rb]

    groups: dict[int, list[int]] = {}
    for i in valid:
        root = uf.find(i)
        groups.setdefault(root, []).append(i)
    return [g for g in groups.values() if len(g) >= 2], edges


def _cluster_summary(cluster: list[int], notes: list[dict], edges: list[tuple[float, int, int]]) -> dict:
    members = [notes[i] for i in cluster]
    member_ids = {notes[i]["id"] for i in cluster}
    sims = [sim for sim, a, b in edges if notes[a]["id"] in member_ids and notes[b]["id"] in member_ids]
    avg_sim = sum(sims) / len(sims) if sims else 0.0
    min_sim = min(sims) if sims else 0.0
    max_sim = max(sims) if sims else 0.0
    domains = sorted({m["domain"] for m in members})
    # Titles sorted by length (shortest = likely representative)
    titles = sorted({m["title"][:100] for m in members}, key=len)
    return {
        "size": len(members),
        "domains": domains,
        "avg_sim": round(avg_sim, 3),
        "min_sim": round(min_sim, 3),
        "max_sim": round(max_sim, 3),
        "representative_title": titles[0] if titles else "",
        "titles": titles[:10],
        "members": [{"id": m["id"], "title": m["title"][:100], "path": m["path"]} for m in members],
    }


def _write_report(clusters_summary: list[dict], n_total: int, threshold: float) -> tuple[Path, Path]:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    date = datetime.now(UTC).strftime("%Y-%m-%d")
    json_path = REPORT_DIR / f"{date}.json"
    md_path = REPORT_DIR / f"{date}.md"

    total_in_clusters = sum(c["size"] for c in clusters_summary)
    consolidation_ratio = round(total_in_clusters / max(len(clusters_summary), 1), 1)

    payload = {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "total_canonical": n_total,
        "threshold": threshold,
        "cluster_count": len(clusters_summary),
        "notes_in_clusters": total_in_clusters,
        "projected_reduction": f"{total_in_clusters} → {len(clusters_summary)}",
        "consolidation_ratio_avg": consolidation_ratio,
        "clusters": clusters_summary,
    }
    json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")

    lines = [
        f"# Canonical Compaction Report — {date}",
        "",
        f"_Generated {payload['generated_at']}_",
        "",
        f"- Total active canonical notes: **{n_total}**",
        f"- Embedding similarity threshold: **{threshold}**",
        f"- Candidate clusters (≥2 members): **{len(clusters_summary)}**",
        f"- Notes inside clusters: **{total_in_clusters}**",
        f"- Avg notes per cluster: **{consolidation_ratio}**",
        f"- Projected consolidation: **{total_in_clusters} → {len(clusters_summary)}** pages",
        f"- Singleton notes (no nearby neighbors): **{n_total - total_in_clusters}**",
        "",
        "These clusters are candidates for merge into consolidated entity/topic pages.",
        "Review and approve before running the merge draft job.",
        "",
    ]
    if not clusters_summary:
        lines.append(
            "_No clusters above threshold. Lower the threshold or the canonical layer is already well-factored._"
        )
    else:
        for idx, c in enumerate(sorted(clusters_summary, key=lambda x: -x["size"]), start=1):
            lines.append(f"## Cluster {idx} — {c['size']} notes — avg sim {c['avg_sim']}")
            lines.append("")
            lines.append(f"**Domains:** {', '.join(c['domains']) or '?'}  ")
            lines.append(f"**Sim range:** {c['min_sim']} – {c['max_sim']}  ")
            lines.append(f"**Representative title:** {c['representative_title']}")
            lines.append("")
            lines.append("Members:")
            for m in c["members"][:20]:
                lines.append(f"- `{m['id']}` — {m['title']} — `{m['path']}`")
            if len(c["members"]) > 20:
                lines.append(f"- _… {len(c['members']) - 20} more_")
            lines.append("")
    md_path.write_text("\n".join(lines) + "\n")
    return json_path, md_path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    parser.add_argument("--min-cluster", type=int, default=DEFAULT_MIN_CLUSTER)
    parser.add_argument("--max-cluster", type=int, default=DEFAULT_MAX_CLUSTER)
    parser.add_argument("--dry-run", action="store_true", help="don't write report")
    args = parser.parse_args()

    notes = _load_notes()
    print(f"[canonical_compaction] loaded {len(notes)} active canonical notes", file=sys.stderr)

    _embed_all(notes)
    embedded = sum(1 for n in notes if n.get("embedding"))
    print(f"  {embedded}/{len(notes)} embeddings ready", file=sys.stderr)

    clusters, edges = _cluster(notes, args.threshold, args.max_cluster)
    clusters = [c for c in clusters if len(c) >= args.min_cluster]
    print(
        f"  {len(clusters)} clusters ≥ {args.min_cluster} members at threshold {args.threshold}",
        file=sys.stderr,
    )

    summaries = [_cluster_summary(c, notes, edges) for c in clusters]
    summaries.sort(key=lambda s: -s["size"])

    if args.dry_run:
        out = {
            "status": "dry-run",
            "total": len(notes),
            "clusters": len(summaries),
            "notes_in_clusters": sum(s["size"] for s in summaries),
            "top_cluster_size": summaries[0]["size"] if summaries else 0,
        }
        print(json.dumps(out))
        return 0

    json_path, md_path = _write_report(summaries, len(notes), args.threshold)
    print(
        json.dumps(
            {
                "status": "ok",
                "total_canonical": len(notes),
                "cluster_count": len(summaries),
                "notes_in_clusters": sum(s["size"] for s in summaries),
                "projected_pages": len(summaries) + (len(notes) - sum(s["size"] for s in summaries)),
                "report": str(md_path.relative_to(ROOT)),
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
