from __future__ import annotations

import argparse
from pathlib import Path

from common import ROOT, dump_json, find_similar_canonical, iter_note_paths, parse_markdown_frontmatter, slugify, utc_now, write_markdown_frontmatter


def load_proposal(path: Path) -> tuple[dict, str]:
    metadata, body = parse_markdown_frontmatter(path)
    if metadata.get("type") != "canonical":
        raise SystemExit(f"Expected canonical proposal, got {metadata.get('type')}")
    if metadata.get("review_state") != "proposed":
        raise SystemExit(f"Expected proposal with review_state=proposed, got {metadata.get('review_state')}")
    return metadata, body


def deactivate_superseded(note_id: str, replacement_id: str) -> None:
    for note_path in iter_note_paths(ROOT / "canonical"):
        metadata, body = parse_markdown_frontmatter(note_path)
        if metadata.get("id") != note_id:
            continue
        metadata["status"] = "superseded"
        metadata["superseded_by"] = replacement_id
        metadata["updated_at"] = utc_now()
        metadata["valid_to"] = utc_now()
        write_markdown_frontmatter(note_path, metadata, body)
        return
    raise SystemExit(f"Superseded note not found: {note_id}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Promote a canonical proposal into the canonical store")
    parser.add_argument("proposal_path", help="path to proposal markdown note")
    parser.add_argument("--owner", default="chris")
    parser.add_argument("--scope", default="global", choices=["global", "project", "time-bounded"])
    parser.add_argument("--supersede", action="append", default=[])
    parser.add_argument("--target-id", help="override canonical note id")
    args = parser.parse_args()

    proposal_path = Path(args.proposal_path).resolve()
    metadata, body = load_proposal(proposal_path)

    canonical_id = args.target_id or metadata["id"].replace("proposal_", "", 1)
    metadata["id"] = canonical_id
    metadata["status"] = "active"
    metadata["owner"] = args.owner
    metadata["scope"] = args.scope
    metadata["review_state"] = "confirmed"
    metadata["last_reviewed_at"] = utc_now()
    metadata["updated_at"] = utc_now()
    metadata["supersedes"] = args.supersede
    metadata["superseded_by"] = None

    for superseded_id in args.supersede:
        deactivate_superseded(superseded_id, canonical_id)

    file_name = slugify(metadata["title"]) + ".md"
    target = ROOT / "canonical" / metadata["domain"] / file_name

    # Dedup: check if similar canonical note already exists
    existing = find_similar_canonical(metadata.get("title", ""), body)
    if existing and existing != target:
        # Merge into existing: append sources, keep longer content
        ex_meta, ex_body = parse_markdown_frontmatter(existing)
        ex_meta["sources"] = list(set(ex_meta.get("sources", []) + metadata.get("sources", [])))
        ex_meta["updated_at"] = utc_now()
        if len(body) > len(ex_body):
            ex_body = body
        write_markdown_frontmatter(existing, ex_meta, ex_body)
        print(f"  MERGED into existing: {existing.name}")
        try:
            import sys as _s
            _s.path.insert(0, str(Path(__file__).resolve().parent.parent / "brain_core"))
            from audit_log import log_event
            log_event("merge", entity_a=str(existing), entity_b=str(target),
                      resolution="canonical_merge", reason="Jaccard > 0.7 with existing canonical")
        except Exception:
            pass
        target = existing  # for audit trail
    else:
        write_markdown_frontmatter(target, metadata, body)

    # Extract entities from promoted canonical note into Neo4j graph (best-effort)
    try:
        import sys as _sys
        _sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "brain_core"))
        from entity_graph import extract_and_store_entities
        note_text = f"{metadata.get('title', '')} {body[:500]}"
        extract_and_store_entities(note_text, canonical_id)
    except Exception:
        pass

    # Auto-extract structured facts from canonical note (best-effort)
    try:
        from fact_store import store_fact
        title = metadata.get("title", "")
        domain = metadata.get("domain", "")
        entities_list = metadata.get("entities", [])
        confidence = metadata.get("confidence", 0.7)
        # Store the canonical note itself as a fact
        if title and domain:
            for entity in entities_list[:5]:
                store_fact(
                    entity=entity.lower(),
                    attribute=f"{domain}_knowledge",
                    value=title,
                    source=str(target),
                    source_type="canonical",
                    confidence=confidence,
                )
    except Exception:
        pass

    # Phase 3 atoms-truth-layer mirror: project canonical note as a tier='core' atom.
    # Best-effort, gated by BRAIN_ATOMS_ENABLED.
    try:
        from atoms_store import upsert_atom
        title = metadata.get("title", "") or canonical_id
        body_preview = body.strip()[:500]
        text = (title + "\n" + body_preview)[:2000]
        upsert_atom(
            text=text,
            chroma_id=f"canonical:{canonical_id}",
            kind="decision" if metadata.get("domain") == "decisions" else "fact",
            confidence=float(metadata.get("confidence", 0.8) or 0.8),
            tier="core",
            canonical=True,
            version_of=canonical_id,
            distilled_by="canonical",
            collection_hint="canonical",
            valid_from=metadata.get("valid_from"),
            valid_until=metadata.get("valid_to"),
            provenance={"path": str(target), "owner": args.owner, "scope": args.scope},
        )
    except Exception:
        pass

    audit_payload = {
        "timestamp": utc_now(),
        "action": "promote_canonical",
        "proposal_path": str(proposal_path.relative_to(ROOT)),
        "canonical_path": str(target.relative_to(ROOT)),
        "canonical_id": canonical_id,
        "owner": args.owner,
        "supersedes": args.supersede,
    }
    audit_name = f"promote_{canonical_id}_{utc_now().replace(':', '-')}.json"
    dump_json(ROOT / "reports" / "review-queue" / audit_name, audit_payload)

    # Clean up proposal after successful promotion
    if proposal_path.exists() and target.exists():
        proposal_path.unlink()

    print(target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
