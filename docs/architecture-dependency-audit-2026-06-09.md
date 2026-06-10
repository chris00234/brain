# Brain architecture/dependency audit — 2026-06-09

## Scope

Second pass after PR #5 (`refactor: slim recall route architecture`). Focus:

- FastAPI route modules as HTTP orchestration boundaries.
- Pure recall helpers separated from route bodies.
- Compatibility facades for legacy `routes.recall` imports.
- Stable recall behavior preserved at 100/100.

## Current state

### Good boundaries now in place

- `brain_core/recall_models.py`
  - Pydantic request/response schemas.
  - No FastAPI route dependencies.
- `brain_core/recall_cache.py`
  - Recall response cache and semantic embedding cache state.
  - Single cache-clear implementation.
- `brain_core/recall_response_builders.py`
  - Pure response/cache-key/meta-note/timing builders.
  - No route decorators or mutable route state.
- `brain_core/recall_temporal.py`
  - Python-side temporal post-filtering extracted from `routes.recall`.
  - Exists because ChromaDB cannot range-filter string datetime fields reliably.
- `brain_core/routes/recall.py`
  - Still owns FastAPI decorators, request orchestration, auth dependency inherited from router, and endpoint-specific flow.
  - Re-exports legacy private/public seams required by current tests and internal callers.

### Design pattern fit

- Compatibility facade pattern is appropriate for this repo right now:
  - `routes.recall` remains stable for legacy imports.
  - New modules own cohesive concerns.
  - Contract tests pin facade behavior before deeper extractions.
- FastAPI cross-cutting concerns are not duplicated in the extracted modules:
  - bearer auth remains router-level via `APIRouter(dependencies=[Depends(verify_bearer)])`.
  - rate limiting remains decorator-level.
  - extracted modules contain no HTTP auth/rate-limit logic.
- Dependency direction is mostly correct:
  - route -> models/cache/builders/temporal/governance/search services.
  - extracted pure modules do not import `routes.recall`.

## Patch applied in this pass

- Extracted `_apply_temporal_filter_inplace` into `brain_core/recall_temporal.py`.
- Delegated `routes.recall.clear_caches()` to `brain_core.recall_cache.clear_caches()` instead of duplicating cache-clear logic.
- Extended `tests/unit/test_recall_cache_extraction_contract.py` to pin:
  - route cache clear delegates to extracted cache state.
  - route re-exports the extracted temporal helper.

## Verification

- `uv run ruff check .` — pass.
- `uv run pytest tests/unit/test_recall_cache_extraction_contract.py tests/unit/test_recall_v2_helpers.py -q` — pass.
- `uv run pytest tests/unit -q` — pass, existing skips only.
- `uv run python cli/eval_gate.py --eval-set cli/eval_set_stable.json --baseline cli/eval_baseline_stable.json --track stable --threshold 0 --source-threshold 0 --min-source 100` — pass, `/recall/v2` stable remains 100/100.

## Remaining architecture debt, ranked

1. `brain_core/routes/recall.py` still contains a large topic-specific governance/classifier cluster.
   - Best next seam: move cohesive classifier groups into `brain_core/recall_governance/` behind compatibility aliases.
   - Do not move `_apply_recall_governance_inplace` wholesale without stronger focused tests.
2. `brain_core/search_unified.py` remains a large multi-source search orchestrator.
   - Candidate split: source adapters vs fusion/timing/provenance logic.
3. Import-path convention is inconsistent (`brain_core.x` and top-level `x`).
   - Risk: duplicate module instances if both package and top-level imports load the same file.
   - Needs repo-wide convention and guard before broad changes.
4. Several route modules still own local Pydantic models and helpers.
   - This is acceptable per current preference while routes are small.
   - Extract only when size/coupling creates concrete maintenance pain.
