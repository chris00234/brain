#!/Users/chrischo/server/brain/.venv/bin/python
"""Weekly skill graph indexer + proposed skill extractor + review digest.

Pipeline (runs via `skill_extract` scheduled job, Sundays):
 1. Index existing Hermes profile skills from ~/.hermes/profiles/liz/skills/ into Neo4j as SKILL nodes.
 2. Read recent raw/inbox records (last 7 days of agent learnings + corrections).
 3. Cluster by embedding cosine similarity.
 4. For each cluster with ≥3 members, decide via embedding similarity to existing
    skills whether to propose a NEW skill or an UPDATE to an existing one.
 5. Use CLI-first LLM dispatch to draft proposals, write to ~/.hermes/profiles/liz/skills/_proposed/.
 6. Build a weekly digest of everything in _proposed/ and deliver via direct Telegram.

This closes the self-improvement loop that was stuck at placeholder before:
signal was being generated (raw/inbox populated), but the proposal path was
`# not yet implemented`. (2026-04-12)
"""

import json
import math
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

SKILLS_DIR = Path("/Users/chrischo/.hermes/profiles/liz/skills")
PROPOSED_DIR = SKILLS_DIR / "_proposed"
RAW_INBOX = Path("/Users/chrischo/server/knowledge/raw/inbox")

# Tunables (calibrated 2026-04-12 after the first run collapsed 500 records
# into 1 cluster — e5-large-instruct embeddings sit in a tight normalized cone,
# so 0.78 means "vaguely related" not "clustered")
RECENT_DAYS = 7
MAX_INBOX_FILES = 500
CLUSTER_THRESHOLD = 0.88  # cosine sim to join a cluster (tightened from 0.78)
MAX_CLUSTER_SIZE = 30  # cap cluster size so centroid drift can't swallow everything
MIN_CLUSTER_SIZE = 3  # propose only when ≥3 records cluster together
EXISTING_SKILL_THRESHOLD = 0.82  # cluster sim to existing skill → update, not new (tightened from 0.75)
MAX_PROPOSALS_PER_RUN = 10  # cap so one run can't flood _proposed/


def parse_skill_frontmatter(skill_file: Path) -> dict | None:
    """Parse SKILL.md frontmatter (YAML header between --- markers)."""
    try:
        content = skill_file.read_text()
    except Exception:
        return None

    if not content.startswith("---"):
        return None

    end = content.find("---", 3)
    if end < 0:
        return None

    header = content[3:end]
    try:
        return yaml.safe_load(header) or {}
    except Exception:
        return None


def index_existing_skills():
    """Walk Hermes profile skills and index SKILL.md files into Neo4j."""
    if not SKILLS_DIR.exists():
        print(f"skills dir not found: {SKILLS_DIR}")
        return 0

    try:
        from neo4j_client import run_write
    except Exception as e:
        print(f"neo4j unavailable: {e}")
        return 0

    indexed = 0
    for skill_md in SKILLS_DIR.rglob("SKILL.md"):
        # Skip proposed skills
        if "_proposed" in str(skill_md):
            continue

        meta = parse_skill_frontmatter(skill_md)
        if not meta:
            continue

        name = meta.get("name", skill_md.parent.name)
        description = meta.get("description", "")

        try:
            run_write(
                "MERGE (s:Skill {name: $name}) "
                "ON CREATE SET s.description = $desc, s.path = $path, "
                "  s.created_at = $now, s.use_count = 0 "
                "ON MATCH SET s.description = $desc, s.path = $path, "
                "  s.updated_at = $now",
                {
                    "name": name,
                    "desc": description[:500],
                    "path": str(skill_md),
                    "now": datetime.now().isoformat(),
                },
            )
            indexed += 1
        except Exception:
            continue

    return indexed


# ── Embedding helpers (reuse brain's existing Ollama embedder) ───────────


def _embed(text: str) -> list[float]:
    """Return a single embedding via brain_core.indexer.get_embedding. Empty on failure."""
    try:
        from indexer import get_embedding

        return get_embedding((text or "")[:1000], prefix="passage", use_cache=True) or []
    except Exception as e:
        print(f"  [embed] failed: {e}", file=sys.stderr)
        return []


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


# ── Step 2: read raw/inbox records ───────────────────────────────────────


def load_recent_inbox(days: int = RECENT_DAYS, max_files: int = MAX_INBOX_FILES) -> list[dict]:
    """Load raw/inbox JSON records newer than `days` ago, cap at `max_files`."""
    if not RAW_INBOX.exists():
        return []
    cutoff = datetime.now(UTC) - timedelta(days=days)
    recs = []
    for f in sorted(RAW_INBOX.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        if len(recs) >= max_files:
            break
        try:
            mtime = datetime.fromtimestamp(f.stat().st_mtime, tz=UTC)
            if mtime < cutoff:
                continue
            data = json.loads(f.read_text())
            # Build the text to embed from title + content + key_facts
            title = (data.get("title") or "").strip()
            content = (data.get("content") or "").strip()
            key_facts = data.get("key_facts") or []
            if isinstance(key_facts, list):
                facts_text = "\n".join(str(x) for x in key_facts[:5])
            else:
                facts_text = ""
            text = f"{title}\n{content}\n{facts_text}".strip()
            if len(text) < 20:
                continue
            recs.append(
                {
                    "id": data.get("id") or f.stem,
                    "text": text,
                    "title": title,
                    "domain": data.get("domain", ""),
                    "source": data.get("source", ""),
                    "subtype": data.get("subtype", ""),
                    "path": str(f),
                }
            )
        except Exception:
            continue
    return recs


# ── Step 3: greedy clustering by cosine similarity ───────────────────────


def cluster_by_embedding(
    records: list[dict],
    threshold: float = CLUSTER_THRESHOLD,
    min_size: int = MIN_CLUSTER_SIZE,
    max_size: int = MAX_CLUSTER_SIZE,
) -> list[list[dict]]:
    """Greedy single-pass clustering with size cap.

    Each record joins the first existing cluster whose centroid has cosine
    ≥ threshold AND has fewer than max_size members; otherwise starts a new
    cluster. The size cap prevents centroid-drift runaway where one cluster
    gradually broadens its centroid to match every subsequent record.

    Returns only clusters with ≥ min_size members, sorted by size desc,
    capped at MAX_PROPOSALS_PER_RUN.
    """
    print(f"  embedding {len(records)} records...")
    for r in records:
        r["_vec"] = _embed(r["text"])

    clusters: list[dict] = []  # each cluster: {"centroid": vec, "members": [recs]}
    for r in records:
        if not r["_vec"]:
            continue
        best_idx, best_sim = -1, 0.0
        for i, c in enumerate(clusters):
            # Skip full clusters
            if len(c["members"]) >= max_size:
                continue
            sim = _cosine(r["_vec"], c["centroid"])
            if sim > best_sim:
                best_sim = sim
                best_idx = i
        if best_sim >= threshold and best_idx >= 0:
            c = clusters[best_idx]
            c["members"].append(r)
            # Update centroid as running average
            n = len(c["members"])
            centroid = c["centroid"]
            c["centroid"] = [(centroid[j] * (n - 1) + r["_vec"][j]) / n for j in range(len(centroid))]
        else:
            clusters.append({"centroid": list(r["_vec"]), "members": [r]})

    kept = [c for c in clusters if len(c["members"]) >= min_size]
    kept.sort(key=lambda c: len(c["members"]), reverse=True)
    sizes = [len(c["members"]) for c in kept]
    print(
        f"  clustered {len(records)} records → {len(clusters)} clusters → "
        f"{len(kept)} actionable (≥{min_size}), sizes={sizes}"
    )
    return kept[:MAX_PROPOSALS_PER_RUN]


# ── Step 4: decide new-skill vs update-existing-skill ─────────────────────


def load_existing_skill_embeddings() -> list[dict]:
    """Walk SKILLS_DIR, return [{name, description, path, vec}] for each live SKILL.md.
    Skips anything under _proposed/.
    """
    out = []
    if not SKILLS_DIR.exists():
        return out
    for skill_md in SKILLS_DIR.rglob("SKILL.md"):
        if "_proposed" in str(skill_md):
            continue
        meta = parse_skill_frontmatter(skill_md)
        if not meta:
            continue
        name = meta.get("name", skill_md.parent.name)
        desc = meta.get("description", "")
        text = f"{name}\n{desc}"
        vec = _embed(text)
        if vec:
            out.append({"name": name, "description": desc, "path": str(skill_md), "vec": vec})
    return out


def find_closest_skill(cluster_vec: list[float], skills: list[dict]) -> tuple[dict | None, float]:
    best, best_sim = None, 0.0
    for s in skills:
        sim = _cosine(cluster_vec, s["vec"])
        if sim > best_sim:
            best, best_sim = s, sim
    return best, best_sim


# ── Step 5: CLI-first proposal drafting ───────────────────────────────────


def _dispatch_proposal(prompt: str, schema: str) -> dict | None:
    """Draft a proposal via CLI-first dispatch. Returns parsed dict or None."""
    try:
        from cli_llm import dispatch_with_schema

        return dispatch_with_schema(
            agent="jenna",
            message=prompt,
            schema_description=schema,
            thinking="low",
            timeout=120,
            max_retries=1,
        )
    except Exception as e:
        print(f"  [dispatch] failed: {e}", file=sys.stderr)
        return None


def _evidence_lines(members: list[dict], limit: int = 10) -> str:
    lines = []
    for m in members[:limit]:
        label = m.get("source") or m.get("subtype") or m.get("domain") or "unknown"
        title = (m.get("title") or m.get("text") or "?")[:80]
        lines.append(f"- `{label}` — {title}")
    return "\n".join(lines)


def propose_new_skill(cluster: dict) -> Path | None:
    """Ask Jenna to draft a new skill from the cluster, write to _proposed/."""
    members = cluster["members"]
    samples = "\n\n".join(f"- {m['title'] or m['text'][:80]}\n  {m['text'][:300]}" for m in members[:6])
    prompt = f"""Draft a new OpenClaw skill based on this cluster of {len(members)}
agent learnings that cluster together by semantic similarity.

Learnings in the cluster:
{samples}

Output ONLY this JSON (no markdown fences, no prose):
"""
    schema = """{
  "proposed_name": "<kebab-case skill name, e.g. 'korean-calendar-parser'>",
  "description": "<one sentence, what the skill does, when to use>",
  "why": "<one sentence, what pattern in the learnings motivates this skill>",
  "draft_body": "<3-6 short markdown paragraphs: when to use, how it works, examples>",
  "evidence_count": <int, len of cluster>
}"""
    result = _dispatch_proposal(prompt, schema)
    if not result or "proposed_name" not in result:
        return None

    name = (result.get("proposed_name") or "").strip().replace(" ", "-").lower()
    if not name or len(name) > 80:
        return None
    PROPOSED_DIR.mkdir(parents=True, exist_ok=True)
    out = PROPOSED_DIR / f"{name}.md"
    if out.exists():
        # Dedupe against prior runs
        print(f"  [new] skip (already proposed): {name}")
        return None

    body = f"""---
name: {name}
type: proposed-new-skill
evidence_count: {len(members)}
proposed_at: {datetime.now(UTC).isoformat()}
---

# {name}

**Description**: {result.get('description', '')}

**Why proposed**: {result.get('why', '')}

## Draft

{result.get('draft_body', '')}

## Evidence ({len(members)} records)

{_evidence_lines(members)}
"""
    out.write_text(body)
    print(f"  [new] proposed: {name} ({len(members)} evidence)")
    return out


def propose_skill_update(cluster: dict, existing: dict, similarity: float) -> Path | None:
    """Ask Jenna to draft an UPDATE to an existing skill based on cluster of corrections."""
    members = cluster["members"]
    samples = "\n\n".join(f"- {m['title'] or m['text'][:80]}\n  {m['text'][:300]}" for m in members[:6])
    try:
        current_body = Path(existing["path"]).read_text()[:2000]
    except Exception:
        current_body = existing.get("description", "")

    prompt = f"""A cluster of {len(members)} agent learnings semantically match
an existing skill '{existing['name']}' (cosine similarity {similarity:.2f}).
Propose a refinement to the existing skill based on the new signal.

Existing skill body (first 2000 chars):
{current_body}

New learnings that cluster with this skill:
{samples}

Output ONLY this JSON (no markdown fences, no prose):
"""
    schema = """{
  "target_skill": "<exact name of the existing skill>",
  "summary_of_change": "<one sentence describing what should change and why>",
  "evidence_count": <int>,
  "proposed_diff": "<markdown change description referencing specific sections>"
}"""
    result = _dispatch_proposal(prompt, schema)
    if not result or "target_skill" not in result:
        return None

    safe_name = existing["name"].replace(" ", "-").lower()
    PROPOSED_DIR.mkdir(parents=True, exist_ok=True)
    out = PROPOSED_DIR / f"{safe_name}-update.md"
    if out.exists():
        print(f"  [update] skip (already proposed): {safe_name}-update")
        return None

    body = f"""---
name: {safe_name}-update
type: proposed-skill-update
target_skill: {existing['name']}
target_path: {existing['path']}
cluster_similarity: {round(similarity, 3)}
evidence_count: {len(members)}
proposed_at: {datetime.now(UTC).isoformat()}
---

# Proposed update to skill: {existing['name']}

**Summary**: {result.get('summary_of_change', '')}

## Existing skill path

`{existing['path']}`

## Proposed diff

{result.get('proposed_diff', '')}

## Evidence ({len(members)} records)

{_evidence_lines(members)}
"""
    out.write_text(body)
    print(f"  [update] proposed refinement for: {existing['name']} ({len(members)} evidence)")
    return out


# ── Step 6: weekly digest + Telegram delivery ────────────────────────────


def build_weekly_proposal_digest() -> str:
    """Walk Hermes profile _proposed skills and return a markdown digest (empty string if nothing)."""
    if not PROPOSED_DIR.exists():
        return ""

    proposals = sorted(PROPOSED_DIR.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not proposals:
        return ""

    new_items, update_items = [], []
    for p in proposals:
        meta = parse_skill_frontmatter(p) or {}
        entry = {
            "name": meta.get("name", p.stem),
            "type": meta.get("type", "unknown"),
            "evidence_count": meta.get("evidence_count", "?"),
            "target_skill": meta.get("target_skill", ""),
            "path": str(p),
            "proposed_at": meta.get("proposed_at", ""),
        }
        if meta.get("type") == "proposed-skill-update":
            update_items.append(entry)
        else:
            new_items.append(entry)

    if not new_items and not update_items:
        return ""

    lines = ["🧠 Weekly Skill Proposal Digest", ""]
    if new_items:
        lines.append(f"**New skill proposals** ({len(new_items)}):")
        for e in new_items[:10]:
            lines.append(f"  • `{e['name']}` — {e['evidence_count']} evidence")
            lines.append(f"    {e['path']}")
        lines.append("")
    if update_items:
        lines.append(f"**Skill refinement proposals** ({len(update_items)}):")
        for e in update_items[:10]:
            lines.append(f"  • `{e['target_skill']}` ({e['evidence_count']} evidence)")
            lines.append(f"    {e['path']}")
        lines.append("")
    lines.append(f"Review: `ls -la {PROPOSED_DIR}/`")
    return "\n".join(lines)


def send_digest_to_telegram(digest: str) -> bool:
    """Deliver digest via direct Telegram alert module. Returns True on success."""
    if not digest.strip():
        return False
    try:
        from telegram_alert import send_chris_telegram

        return send_chris_telegram(
            digest,
            source="skill_extractor:weekly_digest",
            severity="info",
        )
    except Exception as e:
        print(f"  [telegram] send failed: {e}", file=sys.stderr)
        return False


# ── Orchestration ─────────────────────────────────────────────────────────


def propose_skills_from_recent_learnings() -> dict:
    """Top-level: read inbox, cluster, propose new or update skills, return stats."""
    records = load_recent_inbox()
    if len(records) < MIN_CLUSTER_SIZE:
        return {"status": "skip", "reason": f"too few recent records ({len(records)})"}

    clusters = cluster_by_embedding(records)
    if not clusters:
        return {"status": "ok", "clusters": 0, "new": 0, "updates": 0}

    existing_skills = load_existing_skill_embeddings()
    print(f"  loaded {len(existing_skills)} existing skills for similarity check")

    new_count = 0
    update_count = 0
    for cluster in clusters:
        closest, sim = find_closest_skill(cluster["centroid"], existing_skills)
        if closest and sim >= EXISTING_SKILL_THRESHOLD:
            out = propose_skill_update(cluster, closest, sim)
            if out:
                update_count += 1
        else:
            out = propose_new_skill(cluster)
            if out:
                new_count += 1

    return {
        "status": "ok",
        "clusters": len(clusters),
        "new": new_count,
        "updates": update_count,
    }


def main():
    from _watchdog import arm as _arm_watchdog

    _arm_watchdog(900, tag="skill_extractor")
    print("=" * 60)
    print(f"skill_extractor — {datetime.now(UTC).isoformat()}")
    print("=" * 60)

    # Step 1: index existing skills into Neo4j
    print("\n[1/3] Indexing OpenClaw skills into Neo4j graph...")
    count = index_existing_skills()
    print(f"  Indexed {count} skills")

    PROPOSED_DIR.mkdir(parents=True, exist_ok=True)

    # Step 2: propose new skills + skill updates from recent learnings
    print("\n[2/3] Proposing skills from recent raw/inbox learnings...")
    result = propose_skills_from_recent_learnings()
    print(f"  {result}")

    # Step 3: build digest + deliver via Telegram
    print("\n[3/3] Building weekly digest...")
    digest = build_weekly_proposal_digest()
    if digest:
        print(f"  digest built ({len(digest)} chars)")
        sent = send_digest_to_telegram(digest)
        print(f"  telegram delivery: {'ok' if sent else 'failed'}")
    else:
        print("  no proposals yet — skipping digest")

    print("\n" + "=" * 60)
    print("skill_extractor done")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
