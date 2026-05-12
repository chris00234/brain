# Brain research refresh — 2026-05-07

## Current external signal

- **xMemory / Beyond RAG for Agent Memory** (arXiv:2602.02007v3, 2026-04-11): strongest new implementation signal. Agent memories are coherent and correlated, so fixed top-k similarity can over-return redundant spans. Recommended Brain action: measure final top-k semantic redundancy inside existing evals before changing retrieval behavior.
- **A-MEM** (arXiv:2502.12110, NeurIPS 2025): dynamic indexing, linking, and memory evolution aligns with Brain's existing canonical/ontology/skill-promotion direction; no new dependency needed now.
- **ERL / trajectory-informed memory generation** (arXiv 2603.24639 / 2603.10600, March 2026): reinforces the outcome-linked Reflexion/failure-lesson loop already added; next value is better observability and outcome calibration, not another heuristic writer.
- **GitHub ecosystem**: Mem0, Zep/Graphiti, Letta, and Agent-Memory-Paper-List remain useful references, but Brain already has local Qdrant/Neo4j/canonical layers. Importing a full memory platform would duplicate same-purpose infrastructure and violate the current project directive unless a narrow benchmark proves lift.

## Claude debate outcome

Claude challenged the initial idea of adding a new standalone readiness/UI audit. The stronger implementation is to add a **diagnostic field to existing eval reports** so redundancy can be correlated with source/content failures first. This avoids audit-surface sprawl and keeps retrieval behavior unchanged until evidence supports an A/B reranker.

Claude artifacts:

- `.omx/artifacts/claude-memory-diversity-audit-20260507T212748Z.md`
- `.omx/artifacts/claude-we-are-improving-chris-cho-s-local-brain-codebase-at-users-c-2026-05-07T21-29-44-493Z.md`

## Implemented from this refresh

Added opt-in final-top-k semantic redundancy diagnostics to `cli/eval_compare.py`:

- `--diversity-metrics` computes e5 cosine pairwise overlap over final returned top-k results.
- Per-report aggregate includes `coverage_level=final_topk_e5_cosine_v1`, case count, error count, mean/max pairwise cosine, high-similarity pair count, and separate means for passed/content-failed/source-failed cases.
- The field is explicitly diagnostic-only: retrieval changes should be promoted only if high redundancy correlates with downstream failures.
- Regression coverage added in `tests/unit/test_eval_compare_source.py`.

Live seed on 2026-05-07:

```bash
uv run python cli/eval_compare.py --limit 5 --diversity-metrics --json \
  | tee logs/eval-diversity-sample-2026-05-07.json
```

Sample result: baseline mean pairwise cosine 0.9037, v2 mean pairwise cosine 0.9175, v2 high-similarity pairs 27, and content-failed mean pairwise cosine 0.8863 on the first five eval cases. This is enough to justify tracking on larger eval runs, not enough to change retrieval yet.

## Sources

- https://arxiv.org/html/2602.02007v3
- https://arxiv.org/abs/2502.12110
- https://arxiv.org/html/2603.24639v1
- https://arxiv.org/html/2603.10600v1
- https://github.com/Shichun-Liu/Agent-Memory-Paper-List
- https://github.com/mem0ai/mem0
- https://github.com/getzep/graphiti
- https://github.com/letta-ai/letta
