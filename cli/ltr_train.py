#!/Users/chrischo/server/brain/.venv/bin/python3
"""cli/ltr_train.py — fit sklearn LogisticRegression on recall features.

2026-04-17 Phase 3: weekly trainer for the learned-to-rank blend.

Data source:
  (1) search-feedback.jsonl — every event with useful=true/false.
      For each, re-call /recall/v2 with the query, find the result_id
      in the top-5, extract its feature vector, emit (features, label).
  (2) eval-report-stable.json per_test — for each case where the
      expected_content appeared in top-5, label that result useful=1
      and the others (if top-1 missed) useful=0.

The bootstrap script already seeded search-feedback.jsonl with ~5k
entries so this trains on real-ish data.

Output: LogisticRegression coef_ + intercept_ persisted via
brain_core/ltr_blend.save_params(). Weekly cron keeps it fresh.
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
sys.path.insert(0, str(BRAIN_ROOT / "brain_core"))

FEEDBACK_LOG = BRAIN_ROOT / "logs" / "search-feedback.jsonl"
SECRET_FILE = Path("~/.brain/credentials/.personal_webhook_secret").expanduser()
BRAIN_URL = "http://127.0.0.1:8791"

MIN_SAMPLES = 50
MAX_EVENTS = 500  # cap — each unique query triggers one recall call
RECALL_LIMIT = 5  # top-K to hunt result_id in
WORKERS = 8  # parallel /recall/v2 callers


def _recall_v2(query: str, token: str) -> list[dict]:
    url = f"{BRAIN_URL}/recall/v2?" + urllib.parse.urlencode({"q": query, "n": str(RECALL_LIMIT)})
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
        return data.get("results") or []
    except Exception:
        return []


def _load_feedback_events() -> list[dict]:
    if not FEEDBACK_LOG.exists():
        return []
    out: list[dict] = []
    with FEEDBACK_LOG.open() as f:
        for line in f:
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if e.get("useful") is None:
                continue
            out.append(e)
    return out[-MAX_EVENTS:]


def collect_training_data(token: str) -> tuple[list[list[float]], list[int]]:
    """Build X, y arrays by replaying queries and extracting features.

    Parallelized recall phase: distinct queries run concurrently via a
    ThreadPoolExecutor so a 500-event training run finishes in ~30s
    rather than ~5min serial. Duplicate queries (common when many
    bootstrap events target the same canonical title) share one recall.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    from ltr_blend import extract_features

    events = _load_feedback_events()
    print(f"  replaying {len(events)} feedback events ({WORKERS} workers)")

    # Prioritize eval-source over canonical-source — eval queries are more
    # realistic search phrasings. When capping at MAX_EVENTS, keep eval first.
    eval_events = [e for e in events if e.get("bootstrap_marker") == "eval_bootstrap"]
    other_events = [e for e in events if e.get("bootstrap_marker") != "eval_bootstrap"]
    events = (eval_events + other_events)[:MAX_EVENTS]

    # Batch unique queries
    query_set: list[str] = []
    seen_q: set[str] = set()
    for e in events:
        q = e.get("query", "")
        if q and q not in seen_q:
            seen_q.add(q)
            query_set.append(q)
    print(f"  unique queries to replay: {len(query_set)}")

    # Parallel recall
    query_results: dict[str, list[dict]] = {}
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        fut_to_q = {pool.submit(_recall_v2, q, token): q for q in query_set}
        for done, fut in enumerate(as_completed(fut_to_q), 1):
            q = fut_to_q[fut]
            try:
                query_results[q] = fut.result()
            except Exception:
                query_results[q] = []
            if done % 50 == 0:
                print(f"    recall {done}/{len(query_set)}")

    # Build features per event
    X: list[list[float]] = []
    y: list[int] = []
    for e in events:
        q = e.get("query", "")
        rid = e.get("result_id", "")
        label = 1 if e.get("useful") else 0
        if not q or not rid:
            continue
        results = query_results.get(q) or []
        match = next((r for r in results if r.get("id") == rid or r.get("path") == rid), None)
        if match is None:
            if label == 0 and results:
                match = results[0]
            else:
                continue
        X.append(extract_features(match))
        y.append(label)
    return X, y


def fit_and_save(X: list[list[float]], y: list[int]) -> dict:
    if len(X) < MIN_SAMPLES:
        return {"status": "insufficient_samples", "n": len(X), "min": MIN_SAMPLES}
    if sum(y) == 0 or sum(y) == len(y):
        return {"status": "no_class_variance", "positives": sum(y), "total": len(y)}

    from sklearn.linear_model import LogisticRegression
    from sklearn.metrics import roc_auc_score
    from sklearn.model_selection import cross_val_score

    lr = LogisticRegression(max_iter=500, class_weight="balanced", C=1.0)
    lr.fit(X, y)

    # Cross-validated AUC so we know whether the weights generalize
    try:
        auc_scores = cross_val_score(
            LogisticRegression(max_iter=500, class_weight="balanced"),
            X,
            y,
            cv=min(5, sum(y), len(y) - sum(y)),
            scoring="roc_auc",
        )
        cv_auc = float(auc_scores.mean())
    except Exception:
        cv_auc = float("nan")

    # Training AUC
    try:
        train_auc = float(roc_auc_score(y, lr.predict_proba(X)[:, 1]))
    except Exception:
        train_auc = float("nan")

    params = {
        "coef": [float(c) for c in lr.coef_[0].tolist()],
        "intercept": float(lr.intercept_[0]),
        "classes": [int(c) for c in lr.classes_.tolist()],
        "n_samples": len(X),
        "n_positives": int(sum(y)),
        "train_auc": round(train_auc, 4) if train_auc == train_auc else None,  # NaN guard
        "cv_auc": round(cv_auc, 4) if cv_auc == cv_auc else None,
        "fit_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "feature_names": [
            "rrf_score_norm",
            "keyword_score",
            "vector_score",
            "trust_tier_norm",
            "recency_norm",
            "confidence_raw",
            "rerank_score_norm",
            "has_graph_boost",
            "has_canonical_trust",
        ],
    }

    # Persist
    from ltr_blend import save_params

    save_params(params)

    return {
        "status": "ok",
        "n_samples": len(X),
        "n_positives": int(sum(y)),
        "train_auc": params["train_auc"],
        "cv_auc": params["cv_auc"],
        "coef": params["coef"],
        "intercept": params["intercept"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Train LtR logistic regression on recall feedback")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    token = SECRET_FILE.read_text().strip() if SECRET_FILE.exists() else ""
    if not token:
        print(json.dumps({"status": "error", "reason": "no secret"}))
        return 1

    X, y = collect_training_data(token)
    if args.dry_run:
        print(json.dumps({"status": "dry_run", "n_samples": len(X), "n_positives": sum(y)}, indent=2))
        return 0
    result = fit_and_save(X, y)
    print(json.dumps(result, indent=2))
    return 0 if result.get("status") == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
