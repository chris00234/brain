from __future__ import annotations

import argparse
import logging
from pathlib import Path

from common import (
    ROOT,
    dump_json,
    find_similar_canonical,
    iter_note_paths,
    parse_markdown_frontmatter,
    slugify,
    utc_now,
    warn_ontology_metadata,
    write_markdown_frontmatter,
)

log = logging.getLogger("brain.promote_canonical")

ENTITY_SKIP = {"chris cho", "chris", "daehyun", "daehyun cho", "chrischo"}


def _load_entity_targets() -> list[tuple[list[str], str]]:
    """Return [(search_terms, entity_id)] for every canonical/entities/ page.

    Phase 4 (llm-wiki): during canonical promotion, auto-populate the
    relations[] field with `{"type": "mentions", "target": <entity_id>}`
    for every entity whose name or alias appears in the note body.
    """
    entities_dir = ROOT / "canonical" / "entities"
    if not entities_dir.exists():
        return []
    out: list[tuple[list[str], str]] = []
    for page in entities_dir.glob("*.md"):
        try:
            meta, _ = parse_markdown_frontmatter(page)
        except Exception:
            continue
        entity_id = meta.get("id") or ""
        if not entity_id.startswith("entity_"):
            continue
        names = set()
        for name in meta.get("entities", []):
            if isinstance(name, str) and len(name) >= 3 and name.lower() not in ENTITY_SKIP:
                names.add(name.lower())
        terms = [n for n in names if len(n) >= 3]
        if terms:
            out.append((terms, entity_id))
    return out


def _inject_entity_mentions(metadata: dict, body: str) -> int:
    """Add 'mentions' relations for entity pages referenced in the body.
    Returns the number of new relations added. Idempotent."""
    targets = _load_entity_targets()
    if not targets:
        return 0
    body_lower = body.lower()
    existing = {
        (rel.get("type"), rel.get("target")) for rel in metadata.get("relations", []) if isinstance(rel, dict)
    }
    added = 0
    relations = list(metadata.get("relations", []))
    for terms, entity_id in targets:
        if any(term in body_lower for term in terms):
            if ("mentions", entity_id) in existing:
                continue
            relations.append({"type": "mentions", "target": entity_id})
            added += 1
    if added:
        metadata["relations"] = relations
    return added


def load_proposal(path: Path) -> tuple[dict, str]:
    metadata, body = parse_markdown_frontmatter(path)
    if metadata.get("type") != "canonical":
        raise SystemExit(f"Expected canonical proposal, got {metadata.get('type')}")
    if metadata.get("review_state") != "proposed":
        raise SystemExit(f"Expected proposal with review_state=proposed, got {metadata.get('review_state')}")
    return metadata, body


def _mirror_supersession_to_vector_store(note_path: Path, replacement_id: str) -> None:
    """Supersession → canonical payload mirror via VectorStore.

    Multiple canonical rows per source path (chunking) are all flipped in
    one update_payload call. Indexer convention stores the filesystem path
    under `source`.
    """
    try:
        import sys as _sys

        _sys.path.insert(0, str(Path("/Users/chrischo/server/brain/brain_core")))
        from vector_store import get_vector_store  # type: ignore

        store = get_vector_store()
        pts = store.get(
            "canonical",
            filter={"source": str(note_path)},
            limit=100,
            with_payload=False,
            with_vectors=False,
            with_documents=False,
        )
        ids = [p.id for p in pts]
        if not ids:
            return
        store.update_payload(
            "canonical",
            ids=ids,
            patch={
                "status": "superseded",
                "superseded_by": replacement_id,
                "updated_at": utc_now(),
            },
        )
    except Exception as exc:
        log.warning(
            "vector_store_mirror failed for note_path=%s replacement_id=%s: %s",
            note_path,
            replacement_id,
            exc,
        )


def _reconsolidate_via_sage(old_body: str, new_body: str, title: str) -> str | None:
    """2026-04-16 Tier 3 #1: memory reconsolidation (Nader 2000).

    Instead of binary supersede (destroy old, replace with new), Sage
    merges the two versions into a reconsolidated body that preserves
    evidence from both. The old version is then superseded (as before),
    but the replacement carries continuity of provenance.

    Returns the reconsolidated body text or None on failure (caller
    falls back to pure supersession).
    """
    try:
        import sys as _s

        _s.path.insert(0, str(Path(__file__).resolve().parent.parent / "brain_core"))
        from cli_llm import dispatch as _dispatch

        prompt = (
            f"Reconsolidate two versions of a canonical knowledge note into "
            f"ONE merged version that preserves all distinct facts and marks "
            f"conflicts explicitly (never silently drops evidence).\n\n"
            f"Title: {title}\n\n"
            f"Existing version:\n---\n{old_body[:3000]}\n---\n\n"
            f"New evidence version:\n---\n{new_body[:3000]}\n---\n\n"
            f"Rules:\n"
            f"- Keep every distinct factual claim from both versions.\n"
            f"- If two claims conflict, mark with `**Conflict:**` and "
            f"state both.\n"
            f"- Prefer newer evidence for time-sensitive claims; note "
            f"the older claim as historical context.\n"
            f"- Preserve any existing structure (headings, lists).\n"
            f"- Output ONLY the merged markdown body. No commentary.\n"
        )
        result = _dispatch(agent="sage", message=prompt, thinking="low", timeout=60)
        if not getattr(result, "ok", False):
            return None
        merged = (result.text or "").strip()
        # Safety: reject if Sage returned near-empty or suspiciously short
        if len(merged) < min(len(old_body), len(new_body)) // 2:
            return None
        return merged
    except Exception:
        return None


def deactivate_superseded(
    note_id: str,
    replacement_id: str,
    replacement_body: str | None = None,
    reconsolidate: bool = True,
) -> None:
    """Deactivate a canonical note because a replacement has landed.

    2026-04-16 Tier 3 #1: when `reconsolidate` is True and the caller
    passes the new body text, Sage merges old+new before the old version
    is superseded. The replacement file body is rewritten to the merged
    content. When reconsolidation fails or is disabled, falls back to
    the original pure-supersession behavior.
    """
    for note_path in iter_note_paths(ROOT / "canonical"):
        metadata, body = parse_markdown_frontmatter(note_path)
        if metadata.get("id") != note_id:
            continue
        if reconsolidate and replacement_body is not None:
            merged = _reconsolidate_via_sage(body, replacement_body, metadata.get("title", note_id))
            if merged:
                # Caller is expected to find the replacement note via
                # replacement_id and overwrite its body with the merged
                # text — we stash the merge on metadata so the caller can
                # pick it up without a second Sage call.
                metadata["reconsolidated_body"] = merged
                metadata["reconsolidated_at"] = utc_now()
        metadata["status"] = "superseded"
        metadata["superseded_by"] = replacement_id
        metadata["updated_at"] = utc_now()
        metadata["valid_to"] = utc_now()
        write_markdown_frontmatter(note_path, metadata, body)
        _mirror_supersession_to_vector_store(note_path, replacement_id)
        return
    # 2026-04-18: previously raised SystemExit. With multiple --supersede IDs,
    # one missing ID took out the whole promotion after earlier IDs had already
    # been marked superseded on disk — leaving pointers to a replacement that
    # would never be written. Warn + continue so the rest of the promotion
    # completes; caller can re-run with corrected IDs.
    print(f"[promote_canonical] WARN: superseded note not found: {note_id} (skipping)")
    return


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

    # 2026-04-16 Tier 3 #1: pass the new body into the superseder so Sage
    # can reconsolidate old+new before the old version is marked stale.
    # Collects any merged bodies so the replacement note can incorporate
    # them before being written to disk.
    # 2026-04-18: after re-reading, clear the transient reconsolidated_body /
    # reconsolidated_at keys from the superseded note's frontmatter. They were
    # only intended to hand the merge off to this loop — persisting them left
    # up to 3KB of duplicated body inside every superseded note's frontmatter
    # forever, bloating parse_note() calls and pipeline passes.
    reconsolidated_bodies: list[str] = []
    for superseded_id in args.supersede:
        deactivate_superseded(
            superseded_id,
            canonical_id,
            replacement_body=body,
            reconsolidate=True,
        )
        # Re-read to see if reconsolidation produced a merged body.
        for p in iter_note_paths(ROOT / "canonical"):
            m2, b2 = parse_markdown_frontmatter(p)
            if m2.get("id") == superseded_id and m2.get("reconsolidated_body"):
                reconsolidated_bodies.append(m2["reconsolidated_body"])
                # Strip the transient keys and rewrite so frontmatter stays lean.
                m2.pop("reconsolidated_body", None)
                m2.pop("reconsolidated_at", None)
                try:
                    write_markdown_frontmatter(p, m2, b2)
                except Exception:
                    pass
                break
    # If any reconsolidation landed, prefer the most recent merged body
    # over the raw proposal body for the canonical record — ensures no
    # evidence is silently dropped during promotion.
    if reconsolidated_bodies:
        body = reconsolidated_bodies[-1]

    file_name = slugify(metadata["title"]) + ".md"
    target = ROOT / "canonical" / metadata["domain"] / file_name

    # Phase 4 (llm-wiki): auto-populate relations for entity mentions
    mention_count = _inject_entity_mentions(metadata, body)
    if mention_count:
        print(f"  auto-linked {mention_count} entity mention(s) via relations[]")

    warn_ontology_metadata(metadata, str(target))

    # Dedup: check if similar canonical note already exists
    existing = find_similar_canonical(metadata.get("title", ""), body)
    if existing and existing != target:
        # Merge into existing: append sources, keep longer content
        ex_meta, ex_body = parse_markdown_frontmatter(existing)
        ex_meta["sources"] = list(set(ex_meta.get("sources", []) + metadata.get("sources", [])))
        ex_meta["updated_at"] = utc_now()
        warn_ontology_metadata(ex_meta, str(existing))
        if len(body) > len(ex_body):
            ex_body = body
        write_markdown_frontmatter(existing, ex_meta, ex_body)
        print(f"  MERGED into existing: {existing.name}")
        try:
            import sys as _s

            _s.path.insert(0, str(Path(__file__).resolve().parent.parent / "brain_core"))
            from audit_log import log_event

            log_event(
                "merge",
                entity_a=str(existing),
                entity_b=str(target),
                resolution="canonical_merge",
                reason="Jaccard > 0.7 with existing canonical",
            )
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
    except Exception as exc:
        log.warning("entity_graph_extract failed canonical_id=%s: %s", canonical_id, exc)

    # 2026-04-16 Tier 3 #10: HyDE at promote time (Gao et al. 2022).
    # Generate 3-5 hypothetical queries the note would answer, embed
    # them with prefix="query" (the asymmetric-retrieval lookup side of
    # multilingual-e5), and index as additional vectors pointing to the
    # same canonical doc. Dramatically improves recall on varied
    # phrasings without re-ingesting or duplicating the document.
    # Query-side embedding is the crucial trick: direct doc embedding
    # (passage-prefixed) can miss queries that phrase the same concept
    # very differently; query-prefixed HyDE vectors bridge that gap.
    try:
        _s.path.insert(0, str(Path(__file__).resolve().parent.parent / "brain_core"))
        from hyde import generate_hypothetical as _gen_hyp
        from indexer import get_embedding as _get_emb_pc
        from vector_store import get_vector_store as _get_store_pc

        hyp_title = metadata.get("title", "")[:100]
        # The hyde module's generate_hypothetical already returns a cached
        # single hypothetical string per query. Build 3 different "seeds"
        # so we get 3 diverse hypothetical queries.
        hypothetical_queries: list[str] = []
        for seed_prefix in (
            "What is",
            "How does",
            "When should I",
        ):
            seed_q = f"{seed_prefix} {hyp_title}?"
            try:
                hyp = _gen_hyp(seed_q, allow_dispatch=True)
                if hyp and len(hyp) > 20:
                    hypothetical_queries.append(hyp[:500])
            except Exception as exc:
                log.debug("hyde generation skipped seed=%r: %s", seed_q[:80], exc)
                continue
        # Index each hypothetical as a query-prefixed multi-vector
        # pointing at the canonical_id. Uses a dedicated sub-id namespace
        # so they don't collide with the canonical doc embedding.
        if hypothetical_queries:
            ids = [f"{canonical_id}::hyde::{i}" for i in range(len(hypothetical_queries))]
            embeddings = []
            for hq in hypothetical_queries:
                try:
                    e = _get_emb_pc(hq, use_cache=True, prefix="query")
                    embeddings.append(e or None)
                except Exception as exc:
                    log.debug("hyde embed failed hq=%r: %s", hq[:80], exc)
                    embeddings.append(None)
            # Drop failed embeds
            filtered = [
                (i, h, e) for i, h, e in zip(ids, hypothetical_queries, embeddings, strict=False) if e
            ]
            if filtered:
                _get_store_pc().upsert(
                    "canonical",
                    ids=[f[0] for f in filtered],
                    vectors=[f[2] for f in filtered],
                    documents=[f[1] for f in filtered],
                    payloads=[
                        {
                            "type": "canonical-hyde",
                            "canonical_id": canonical_id,
                            "canonical_path": str(target),
                            "title": hyp_title,
                            "hyde_index": i,
                            "created_at": utc_now(),
                        }
                        for i, (_, _, _) in enumerate(filtered)
                    ],
                )
                print(f"  HyDE: indexed {len(filtered)} hypothetical query vectors for {canonical_id}")
    except Exception as _hyde_err:
        # Never block promotion on HyDE failure — best-effort enrichment.
        print(f"  HyDE skipped: {_hyde_err}")

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
    except Exception as exc:
        log.warning("fact_store_mirror failed canonical_id=%s: %s", canonical_id, exc)

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
    except Exception as exc:
        log.warning(
            "atoms_store_upsert failed canonical_id=%s target=%s: %s",
            canonical_id,
            target,
            exc,
        )

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
