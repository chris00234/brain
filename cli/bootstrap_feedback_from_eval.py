#!/Users/chrischo/server/brain/.venv/bin/python3
"""cli/bootstrap_feedback_from_eval.py — seed search-feedback.jsonl.

The LoRA trainer and LtR blend both need (query, positive, negative)
labeled pairs. Post-Tier-1 the impression-only feedback path has 1211
observations with zero useful=true/false labels because the correct
fix killed auto-reinforcement. This one-shot bootstrap emits synthetic
labels from two sources to unblock training:

  (A) eval_set_stable.json — 138 ground-truth queries with expected
      content substrings. For each: hit /recall/v2, label top-1 as
      useful=true if content matches expected substring; useful=false
      if it doesn't AND the expected id is elsewhere in top-5.

  (B) canonical/*.md + distilled/*.md — active knowledge files. For
      each note, emit a synthetic useful=true pair where query=title
      and result_id=chroma_id. Captures how Chris naturally phrases
      lookups and gives the trainer abundant high-trust semantics.

Idempotent — tracks a (query, result_id, source) triple set so reruns
don't duplicate entries in the feedback log.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

BRAIN_ROOT = Path("/Users/chrischo/server/brain")
KNOWLEDGE_ROOT = Path("/Users/chrischo/server/knowledge")
FEEDBACK_LOG = BRAIN_ROOT / "logs" / "search-feedback.jsonl"
EVAL_SET = BRAIN_ROOT / "cli" / "eval_set_stable.json"
SECRET_FILE = Path("~/.openclaw/credentials/.personal_webhook_secret").expanduser()
BRAIN_URL = "http://127.0.0.1:8791"

BOOTSTRAP_MARKER_EVAL = "eval_bootstrap"
BOOTSTRAP_MARKER_CANONICAL = "canonical_bootstrap"


def _load_secret() -> str:
    return SECRET_FILE.read_text().strip()


def _existing_triples() -> set[tuple[str, str, str]]:
    """Read current feedback log and collect (query, result_id, source) triples
    from any prior bootstrap runs (or real feedback). Skip-set for idempotency."""
    if not FEEDBACK_LOG.exists():
        return set()
    out: set[tuple[str, str, str]] = set()
    with FEEDBACK_LOG.open() as f:
        for line in f:
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            # Bootstrap marker lives in `bootstrap_marker` (post-fix); legacy
            # entries may have it in `source` still — check both.
            marker = e.get("bootstrap_marker") or e.get("source") or ""
            if marker not in (BOOTSTRAP_MARKER_EVAL, BOOTSTRAP_MARKER_CANONICAL):
                continue
            q = e.get("query", "")
            rid = e.get("result_id", "")
            out.add((q, rid, marker))
    return out


def _append_feedback(entries: list[dict]) -> None:
    if not entries:
        return
    FEEDBACK_LOG.parent.mkdir(parents=True, exist_ok=True)
    with FEEDBACK_LOG.open("a") as f:
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")


def _recall_v2(query: str, token: str, n: int = 5) -> dict:
    url = f"{BRAIN_URL}/recall/v2?" + urllib.parse.urlencode({"q": query, "n": str(n)})
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})  # noqa: S310
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
            return json.loads(resp.read().decode())
    except Exception as e:
        return {"error": str(e)[:200]}


def bootstrap_from_eval(token: str, existing: set) -> tuple[int, int, int]:
    """Walk eval_set_stable queries; emit useful=true for matches, useful=false
    for mismatches where expected_id is elsewhere. Returns (positives, negatives, skipped)."""
    if not EVAL_SET.exists():
        print(f"  SKIP: eval set not found at {EVAL_SET}")
        return (0, 0, 0)
    cases = json.loads(EVAL_SET.read_text())
    print(f"  eval cases: {len(cases)}")
    positives = negatives = skipped = 0
    pending: list[dict] = []
    now_iso = datetime.now(UTC).isoformat(timespec="seconds")
    for idx, case in enumerate(cases):
        query = (case.get("query") or "").strip()
        expected_content = (case.get("expected_content") or "").lower()
        if not query or not expected_content:
            skipped += 1
            continue
        payload = _recall_v2(query, token, n=5)
        results = payload.get("results") or []
        if not results:
            skipped += 1
            continue
        # Evaluate each top-5 result's content against expected substring
        top1 = results[0] if isinstance(results[0], dict) else {}
        top1_content = ((top1.get("content") or "") + " " + (top1.get("title") or "")).lower()
        top1_id = top1.get("id") or top1.get("path") or ""
        positive_triple = (query, top1_id, BOOTSTRAP_MARKER_EVAL)
        top1_hit = expected_content in top1_content
        if top1_hit:
            if positive_triple not in existing:
                pending.append(
                    {
                        "query": query[:500],
                        "result_id": top1_id,
                        "result_source": top1.get("collection", ""),
                        "score": 1.0,
                        "useful": True,
                        "served": True,
                        "timestamp": now_iso,
                        "agent": "eval_bootstrap",
                        "source": top1.get("collection", "semantic_memory")
                        if isinstance(top1, dict)
                        else "semantic_memory",
                        "bootstrap_marker": BOOTSTRAP_MARKER_EVAL,
                    }
                )
                existing.add(positive_triple)
                positives += 1
        else:
            # Find expected in rest of top-5
            expected_idx = None
            for j, r in enumerate(results[1:], start=1):
                content = ((r.get("content") or "") + " " + (r.get("title") or "")).lower()
                if expected_content in content:
                    expected_idx = j
                    break
            if expected_idx is not None:
                # Negative on top-1 (wrong winner)
                neg_triple = (query, top1_id, BOOTSTRAP_MARKER_EVAL)
                if neg_triple not in existing:
                    pending.append(
                        {
                            "query": query[:500],
                            "result_id": top1_id,
                            "result_source": top1.get("collection", ""),
                            "score": 0.0,
                            "useful": False,
                            "served": True,
                            "timestamp": now_iso,
                            "agent": "eval_bootstrap",
                            "source": top1.get("collection", "semantic_memory")
                            if isinstance(top1, dict)
                            else "semantic_memory",
                            "bootstrap_marker": BOOTSTRAP_MARKER_EVAL,
                        }
                    )
                    existing.add(neg_triple)
                    negatives += 1
                # Positive on the expected-id that DID surface
                winner = results[expected_idx]
                win_id = winner.get("id") or winner.get("path") or ""
                pos_triple = (query, win_id, BOOTSTRAP_MARKER_EVAL)
                if pos_triple not in existing:
                    pending.append(
                        {
                            "query": query[:500],
                            "result_id": win_id,
                            "result_source": winner.get("collection", ""),
                            "score": 1.0,
                            "useful": True,
                            "served": True,
                            "timestamp": now_iso,
                            "agent": "eval_bootstrap",
                            "source": top1.get("collection", "semantic_memory")
                            if isinstance(top1, dict)
                            else "semantic_memory",
                            "bootstrap_marker": BOOTSTRAP_MARKER_EVAL,
                        }
                    )
                    existing.add(pos_triple)
                    positives += 1
            else:
                skipped += 1
        if (idx + 1) % 25 == 0:
            print(f"    eval {idx+1}/{len(cases)}: +{positives} -{negatives} skip={skipped}")
    _append_feedback(pending)
    return positives, negatives, skipped


def _read_frontmatter(p: Path) -> tuple[dict | None, str]:
    try:
        t = p.read_text(errors="replace")
    except Exception:
        return None, ""
    if not t.startswith("---json"):
        return None, t
    end = t.find("---", 7)
    if end <= 0:
        return None, t
    try:
        meta = json.loads(t[7:end])
    except Exception:
        return None, t
    body = t[end + 3 :]
    return meta, body


def bootstrap_from_canonical(existing: set) -> tuple[int, int]:
    """Walk active canonical + distilled markdown; emit title→chroma_id useful=true pairs."""
    sys.path.insert(0, str(BRAIN_ROOT / "brain_core"))
    from vector_store import get_vector_store  # type: ignore

    store = get_vector_store()

    # Build id index: source_path → [ids]
    points = store.get(
        "canonical",
        limit=20000,
        with_payload=True,
        with_documents=False,
    )
    if not points:
        print("  SKIP: canonical collection empty")
        return (0, 0)
    path_to_ids: dict[str, list[str]] = {}
    for p in points:
        m = p.payload or {}
        src = m.get("source") or m.get("path")
        if not src:
            continue
        path_to_ids.setdefault(src, []).append(p.id)

    positives = scanned = 0
    pending: list[dict] = []
    now_iso = datetime.now(UTC).isoformat(timespec="seconds")

    for md_path in list(KNOWLEDGE_ROOT.rglob("*.md")):
        if "/archived/" in str(md_path) or "/orphaned/" in str(md_path):
            continue
        meta, body = _read_frontmatter(md_path)
        if not meta:
            continue
        if meta.get("status") and meta.get("status") != "active":
            continue
        title = (meta.get("title") or "").strip()
        if not title or len(title) < 4:
            continue
        scanned += 1
        cids = path_to_ids.get(str(md_path), [])
        if not cids:
            continue
        # Emit against the first chunk id (the title-matching chunk is usually chunk 0)
        chroma_id = cids[0]
        triple = (title, chroma_id, BOOTSTRAP_MARKER_CANONICAL)
        if triple in existing:
            continue
        pending.append(
            {
                "query": title[:500],
                "result_id": chroma_id,
                "result_source": "canonical",
                "score": 1.0,
                "useful": True,
                "served": True,
                "timestamp": now_iso,
                "agent": "canonical_bootstrap",
                "source": "canonical",  # Chroma collection name — training_pair_generator.py keys on this
                "bootstrap_marker": BOOTSTRAP_MARKER_CANONICAL,
            }
        )
        existing.add(triple)
        positives += 1
    _append_feedback(pending)
    return positives, scanned


def main() -> int:
    parser = argparse.ArgumentParser(description="Bootstrap labeled feedback from eval + canonical")
    parser.add_argument("--source", choices=["eval", "canonical", "both"], default="both")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    existing = _existing_triples()
    print(f"existing bootstrap triples: {len(existing)}")

    summary: dict = {}
    token = _load_secret()

    if args.source in ("eval", "both"):
        print("=== source A: eval bootstrap ===")
        if args.dry_run:
            print("  (dry-run) skipping network calls")
            summary["eval"] = {"dry_run": True}
        else:
            p, n, s = bootstrap_from_eval(token, existing)
            summary["eval"] = {"positives": p, "negatives": n, "skipped": s}

    if args.source in ("canonical", "both"):
        print("=== source B: canonical bootstrap ===")
        if args.dry_run:
            print("  (dry-run) skipping canonical scan")
            summary["canonical"] = {"dry_run": True}
        else:
            p, scanned = bootstrap_from_canonical(existing)
            summary["canonical"] = {"positives": p, "scanned": scanned}

    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
