"""brain_core/ltr_blend.py — learned linear blend for recall scoring.

2026-04-17 Phase 3: sklearn LogisticRegression fitted on feedback +
eval-per-test labels. Replaces the last remaining hand-tuned multiplier
in the scoring cascade with a data-driven blend.

Feature vector (9 dims) per result:
  [rrf_score, keyword_score, vector_score, trust_tier,
   recency_norm, confidence_raw, rerank_score_norm,
   has_graph_boost (0/1), has_canonical_trust (0/1)]

At recall time, each top-K result is scored through the classifier
(`predict_proba(feat)[0][1]`) and the result's `score` is blended:

  final = (1 - LTR_WEIGHT) * current_score + LTR_WEIGHT * lr_score * 100

LTR_WEIGHT is tuned conservatively (default 0.25) so the learned
signal enhances rather than replaces the existing cascade.

Feature flag: `BRAIN_LTR_ENABLED` (default False). When off, this
module is a pure no-op — search_unified imports it and calls
`apply_if_enabled()` which returns immediately on disabled state.

Weights persist to brain_config_store under key `ltr_weights.v1` as
JSON-serialized {coef_, intercept_, classes_, features_fit_at,
n_samples, eval_auc}. Training script is `cli/ltr_train.py`.
"""

from __future__ import annotations

import json
import logging
import math
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

log = logging.getLogger("brain.ltr_blend")

sys.path.insert(0, str(Path(__file__).resolve().parent))

LTR_WEIGHT = 0.25  # blend weight for learned score vs existing cascade
LTR_CONFIG_KEY = "ltr_weights.v1"
FEATURE_DIM = 9


def _enabled() -> bool:
    return os.environ.get("BRAIN_LTR_ENABLED", "").strip().lower() in ("1", "true", "yes", "on")


def _load_params() -> dict | None:
    if not _enabled():
        return None
    try:
        import brain_config_store

        raw = brain_config_store.get(LTR_CONFIG_KEY)
        if not raw:
            return None
        return json.loads(raw)
    except Exception:
        return None


def save_params(params: dict) -> None:
    try:
        import brain_config_store

        brain_config_store.set(LTR_CONFIG_KEY, json.dumps(params), updated_by="ltr_train")
    except Exception as _exc:
        log.debug("silenced exception in ltr_blend.py: %s", _exc)


def extract_features(result: dict) -> list[float]:
    """Pull the 9-dim feature vector from a recall result dict.

    All features normalized to comparable ranges so the logistic fit
    converges without per-feature scaling gymnastics.
    """
    meta = result.get("metadata") or {}
    dbg = result.get("_debug") or {}

    # rrf_score typically 0-100 post-normalization
    rrf = float(result.get("rrf_score") or 0.0)
    # keyword + vector live in metadata (from hybrid_search stage)
    kw = float(meta.get("keyword_score") or result.get("keyword_score") or 0.0)
    vec = float(meta.get("vector_score") or result.get("vector_score") or 0.0)
    trust = float(result.get("trust_tier") or 0)
    # recency: days since created_at, log-compressed + clamped to [0, 1]
    recency_norm = 0.0
    created = result.get("created_at") or meta.get("created_at")
    if created:
        try:
            dt = datetime.fromisoformat(str(created).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            age_days = max(0.0, (datetime.now(UTC) - dt).total_seconds() / 86400.0)
            recency_norm = 1.0 / (1.0 + math.log1p(age_days) / 5.0)
        except Exception as _exc:
            log.debug("silenced exception in ltr_blend.py: %s", _exc)
    conf_raw = float(result.get("confidence_raw") or result.get("confidence") or 0.5)
    rerank = float(result.get("rerank_score") or 0.0) / 200.0  # typical rerank 0-200 range
    has_graph = 1.0 if (meta.get("graph_boost") or dbg.get("graph_boost")) else 0.0
    has_canonical_trust = (
        1.0 if (dbg.get("canonical_trust_bonus") or dbg.get("canonical_trust_override")) else 0.0
    )

    return [rrf / 100.0, kw, vec, trust / 3.0, recency_norm, conf_raw, rerank, has_graph, has_canonical_trust]


def _predict_proba(features: list[float], params: dict) -> float:
    """Score a single feature vector. Returns P(useful=True) in [0, 1]."""
    coef = params.get("coef")
    intercept = params.get("intercept", 0.0)
    if not coef or len(coef) != len(features):
        return 0.5  # uninformative — falls back to cascade-only
    z = intercept + sum(c * x for c, x in zip(coef, features, strict=False))
    # sigmoid with overflow guard
    if z > 30:
        return 1.0
    if z < -30:
        return 0.0
    return 1.0 / (1.0 + math.exp(-z))


def apply_if_enabled(results: list[dict]) -> list[dict]:
    """Apply learned blend to a result list in-place.

    When BRAIN_LTR_ENABLED is false or weights missing, returns results
    unchanged. Callers invoke this AFTER existing rerank/decay stages so
    the learned signal refines rather than competes.
    """
    params = _load_params()
    if not params or not results:
        return results
    for r in results:
        if not isinstance(r, dict):
            continue
        try:
            feat = extract_features(r)
            lr_score = _predict_proba(feat, params)
            cur = float(r.get("score") or 0.0)
            blended = (1.0 - LTR_WEIGHT) * cur + LTR_WEIGHT * lr_score * 100.0
            r["score"] = round(blended, 2)
            dbg = dict(r.get("_debug") or {})
            dbg["ltr_prob"] = round(lr_score, 4)
            dbg["ltr_applied"] = True
            r["_debug"] = dbg
        except Exception as _exc:
            log.debug("silenced exception in ltr_blend.py: %s", _exc)
            continue
    return results
