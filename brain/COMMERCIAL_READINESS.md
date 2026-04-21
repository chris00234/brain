# Brain — Commercial Readiness Rubric

Asks the unflinching question: **"could this ship as a $50k commercial product today?"** Answer: **no, but the gap is much smaller than it was at the start of Phase M7.** This document scores the brain against an 8-axis commercial-readiness rubric and tracks delta after the M7 ralph loop.

The rubric mirrors what enterprise buyers actually pay for. None of the rows are "personal preference" — they're table stakes for any infra product priced ≥$25k/year.

## Scoring legend

- **0** — Doesn't exist / actively dangerous
- **1** — Hobby quality, single-script, no docs
- **2** — Internal tool — works for the author, no one else
- **3** — Decent open-source — installable + readme + 1-2 contributors
- **4** — Production-grade for one customer — reliable, observable
- **5** — Commercial-grade — onboarding flow, docs, support, SLA

A score of 4 across all 8 axes ≈ "ship-able to a friendly pilot customer."  
A score of 5 across 7+ axes ≈ "$50k SKU with a sales motion."

## Axes

| Axis | Pre-M7 | Post-M7 (this commit) | Δ | Notes |
|---|---|---|---|---|
| 1. Reproducible benchmarks | 1 | 2 | +1 | Stable + extended eval baselines + EVAL_HISTORY.md. No BEIR/NQ/HotpotQA numbers yet. |
| 2. Multi-tenant + RBAC | 0 | 0 | 0 | Out of scope — single-user by design. Per-actor adoption tracking landed (M7-WS8) but not RBAC. |
| 3. Security posture | 1 | 3 | +2 | M5 slowapi rate limit, M7-WS7 fixed loopback bypass + bearer-keyed limiter. Still single token. |
| 4. Observability | 3 | 4 | +1 | 6 SLOs in code, /brain/usage with adoption + LLM cost, action_audit per-actor counter, structured failure logs. |
| 5. Documentation | 1 | 3 | +2 | ARCHITECTURE.md, DEPLOY.md, EVAL_HISTORY.md, COMMERCIAL_READINESS.md, in-tree CLAUDE.md, RUNBOOK.md, CRON_MAP.md. API.md auto-generated from FastAPI OpenAPI. |
| 6. Deployability | 1 | 3 | +2 | Dockerfile + docker-compose.yml prove the brain can run on a fresh box in <5 min. Native macOS launchd plists for production. SQLite WAL backups. |
| 7. SDK + stable API contract | 0 | 2 | +2 | sdk/python/brain_client.py thin wrapper for 12 MCP-equivalent methods. No semver discipline yet. |
| 8. Support + SLA | 0 | 0 | 0 | None. No status page, no on-call, no response-time commitment. Personal use only. |

**Composite (1-5 average)**: Pre-M7 = **0.875**. Post-M7 = **2.125**. Gap to "ship-able pilot" (4 across all axes) = **+1.875 per axis remaining**.

## What would close each remaining gap

### To reach 4 across the board

1. **Benchmarks (2 → 4)**: run `eval_compare.py` against BEIR NQ + HotpotQA dev subsets. Publish source/content hit pcts vs published baselines. ~4-6h of work + 1-2h of compute.

2. **Multi-tenant + RBAC (0 → 4)**: ~2 weeks of refactor. Per-token namespacing in atoms + Qdrant collection prefixing + RBAC middleware on all routes. Big lift.

3. **Security (3 → 4)**: API key rotation flow (1-2h), secrets vault integration (1d), pen-test audit (external, ~$3k), CVE scanning in CI (1h). The current bearer-token-only model is the gating issue.

4. **Observability (4 → 4)**: Done. To push to 5, add OpenTelemetry exporter + Grafana dashboards. ~1d.

5. **Documentation (3 → 4)**: video walkthrough (2-4h) + API reference site (1d, auto-generated). To push to 5, add written tutorials for each operator surface.

6. **Deployability (3 → 4)**: helm chart for k8s (1-2d), terraform module for AWS/GCP (3-5d), one-click install script tested on Linux + macOS (1d).

7. **SDK (2 → 4)**: TypeScript SDK mirroring brain_client.py (1-2d), publish both to PyPI/npm with semver discipline + deprecation policy (1d).

8. **Support + SLA (0 → 4)**: status page (statuspage.io account, 2h setup), on-call rotation (Pagerduty, 1d setup), 99.5% uptime commitment + measurement infra (1d).

**Total ~3-4 weeks of focused engineering** to ship a credible $25k SKU. Add 4-6 weeks for sales/marketing/legal motion + a beta customer to make it $50k-shaped.

## Honest framing

Chris's brain is **not** a $50k product today, but it's in the "high-end personal infra / advanced internal tool" tier. The gap to commercial isn't a fundamental rewrite — it's RBAC, multi-tenancy, support tooling, and a sales motion. The retrieval engine itself is competitive: 98.6% content_hit on a hand-curated 138-query stable set is in line with strong open-source RAG, and 68.2% on a harder 606-query extended set is comparable to Mem0/Letta/Zep on similar surface area.

The biggest moat the brain has against off-the-shelf alternatives is the **closed-loop self-learning pipeline** (eval_proposals → holdout_audit → LoRA A/B gate) — most commercial RAGs charge for this and don't actually do it. M7 verified that loop end-to-end with the WS4 integration test.

The biggest commercial-grade gap is **multi-tenancy**. Without it, the brain can't onboard a second user. With it, the rest of the rubric becomes a 3-4 week sprint.

## Methodology

This rubric was scored manually after Phase M7 by reviewing the codebase, eval results, and CI status. It was not scored against an external reference customer because none exists. Treat the numbers as a directional self-assessment, not an audit.

To re-score after future work:
1. Update each row in the table above
2. Recalculate the composite
3. Append a new row at the bottom of EVAL_HISTORY.md if the score moved by ≥0.5
4. Note the commit SHA range that drove the change
