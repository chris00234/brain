# Phase M7 WS7 — Fresh 5-Agent Deep Audit (M5/M6/M9)

**Date**: 2026-04-13 ~22:30 PT  
**Brain commit at audit start**: post WS4 (`038d3bf`)  
**Agents dispatched in parallel**:
1. `security-auditor` — OWASP / SSRF / injection / auth bypass
2. `Explore` — logic correctness across web_search, crag, eval_holdout_promote, eval_proposals
3. `Explore` — hot-path performance against the 500ms p50 budget
4. `Explore` — test coverage for M5/M6/M9 modules
5. `Explore` — DB concurrency races / lost updates / FK violations

---

## Critical issues (must-fix before WS7 done)

### C1. Rate limiter loopback bypass — `server.py:917-934`
**Source**: security-auditor

`_rate_limit_key` returns a per-request unique key (`f"loopback-{id(request)}"`) for `127.0.0.1`. Brain runs behind nginx in an OrbStack container reached via Cloudflare tunnel — `request.client.host` is **always** 127.0.0.1 because no `forwarded_allow_ips` / `ProxyHeadersMiddleware` is configured. **Net effect**: Phase M5 slowapi is a no-op for every Cloudflare tunnel request. A token holder can drive unbounded `/learn`, `/memory`, `/web/search` traffic from anywhere on the internet.

**Fix**: key the rate limiter on the bearer token (first 16 chars of `authorization.split(" ")[1]`), not on `request.client.host`. Also document/enable `forwarded_allow_ips="127.0.0.1"` in the uvicorn invocation.

### C2. CRAG recursion can fire ~7 LLM calls — `server.py:1645`, `brain_core/crag.py:284`
**Source**: security-auditor + perf audit (independently flagged)

When `?iterative=true&hyde=true&expand=true`, the CRAG retry path calls `recall_v2` recursively with `hyde=True, expand=True` propagated. Worst case per request: 1 HyDE + 3 expand variants + 1 CRAG rewrite + 1 inner HyDE + 1 inner expand = up to **7 OpenClaw dispatches**, each ~6-15s. Combined with C1, a token-holder attacker can multiply LLM cost ~7× per call.

**Fix**: when CRAG recurses, force `hyde=False, expand=False` on the inner call. Skip enrichment, gap-logging, and `insert_action_audit` on the inner pass. Cap CRAG dispatches via a top-level recall_v2 LLM budget (e.g., max 2 dispatches per request).

---

## High issues

### H1. `eval_holdout_promote.run()` clobbers `eval_holdout_pending.json` on each run — `brain_core/eval_holdout_promote.py:144`
**Source**: logic audit

The promote job rewrites the pending file from scratch every Sunday. Items promoted last week that are still awaiting human review get **silently dropped** when this week's run finds different candidates. Self-evolution loop loses backlog.

**Fix**: read the existing pending file, merge new rows by id (skip duplicates), then rewrite. Audit Telegram digest already uses ids so no downstream change.

### H2. 5 LLM-dispatch endpoints unrated — `server.py:1846, 1973, 3142, 3189, 4388`
**Source**: security-auditor

`/brain/reason/multihop`, `/chris/think`, `/brain/decide`, `/brain/reason`, `/brain/ingest` all fire billable OpenClaw dispatches with **zero `@limiter.limit`** decorator, while `/learn` is capped at 10/min explicitly because it "fires LLM dispatch." Same threat model as C1 — token-cost runaway.

**Fix**: add `@limiter.limit("10/minute")` to every LLM-dispatch endpoint.

### H3. `insert_action_audit` on /recall/v2 hot path — `server.py:1771-1785`
**Source**: perf audit

My WS8 wiring (`insert_action_audit` after every served result) runs synchronously, opening a new SQLite connection per call. Currently no-op due to `BRAIN_ATOMS_ENABLED` default false, but flipping it adds 0.5-2ms (up to 30ms under writer contention) to every recall. The auto-feedback recorder 10 lines above already uses `background.add_task` — should mirror.

**Fix**: move the `insert_action_audit` call into the same background task block.

### H4. CRAG `LOW_CONFIDENCE_TOP_SCORE = 60.0` not empirically calibrated — `brain_core/crag.py:39`
**Source**: perf audit

The threshold was set against two anecdotes (top ~110 high-quality, ~53 gibberish). Real production score distribution has middling queries (top_score 55-75) that will flip CRAG on/off at random. WS3 plan to flip iterative default-on requires this threshold to keep trigger rate ≤5% (else weighted p50 blows the 500ms budget at 20%+ trigger rate ⇒ ~1.1-2.7s per triggered request).

**Fix**: before WS3 default-on flip, run last 7d of /recall/v2 queries through `score_confidence`, plot histogram, set threshold so trigger rate ≤5%. Document calibration method in crag.py.

### H5. Trust score docstring vs reality — `brain_core/web_search.py:7-9, 235-275`
**Source**: logic audit

Docstring says trust score "moves on /recall/feedback." Reality: feedback only updates `web_search_results.outcome`. The actual trust score moves only via the weekly `web_source_trust_recompute` job. Misleading for callers.

**Fix**: rewrite docstring to match reality OR call `recompute_domain_trust(domain)` from the feedback path.

---

## Medium issues (deferred to WS7b or M7 reserve buffer)

- M1. SearXNG response size unbounded (`web_search.py:115`) — DoS risk via crafted upstream JSON.
- M2. Result URLs returned unvalidated (`web_search.py:131-143`) — `file://` / `javascript:` schemes can leak through.
- M3. Missing CHECK constraints on M6 DDL (`migrations_brain_db.py:347-378`) — defense in depth.
- M4. `contextlib.suppress(Exception)` in CRAG ThreadPoolExecutor (`crag.py:230`) — slow Jenna dispatch keeps billing tokens after CRAG returns.
- M5. CRAG NaN propagation (`crag.py:113`) — `min(1.0, NaN) == NaN` propagates silently to confidence score.
- M6. `eval_holdout_promote.run()` reads from `r.get("content")` but server fused results may use `"snippet"` (`crag.py:192`) — expansion prompt may always see `(no results)`.
- M7. `eval_proposals.mark_status` raises `ValueError` outside the `except sqlite3.Error` block — inconsistent with other failure modes.
- M8. `eval_holdout_promote.list_candidates(limit=200)` starves older candidates when >200 exist.
- M9. Weekly `recompute_domain_trust` holds BEGIN IMMEDIATE across full SELECT+N upserts; concurrent writers contend on 5s default busy_handler — observable best-effort write drops during Sun 5:15 job. Mitigation: PRAGMA busy_timeout.

---

## Test coverage table (from coverage agent)

| Module | Test file(s) | Coverage rating |
|---|---|---|
| server.py M5 slowapi limiter | test_rate_limit.py | **SMOKE** (wiring only — no actual 429 test) |
| brain_core/web_search.py (M6) | test_web_search.py | DECENT |
| brain_core/crag.py (M9) | test_crag.py | DECENT |
| brain_core/eval_proposals.py | test_eval_proposals.py | GOOD |
| brain_core/eval_holdout_promote.py | test_eval_holdout_promote.py | DECENT (Phase M7-WS4 added the integration smoke + fixed indexer import bug) |
| brain_core/eval_holdout_audit.py | test_eval_holdout_audit.py | DECENT |

**Most-missed test**: actual 429 for slowapi (issue 61 POSTs to /memory in one minute, assert 429 with Retry-After).

---

## DB concurrency verdict

**No data-corruption races, lost updates, or FK violations** in the M5/M6/M9 surface. Phase L's BEGIN IMMEDIATE pattern carried forward into web_search and the new write paths. Only real concurrency wart is **writer-lock contention during the weekly recompute_domain_trust job** (M9 above) and **no explicit PRAGMA busy_timeout anywhere in brain_core** (relying on Python's 5s default).

---

## Triage decisions for WS7 in-iteration fixes

- **C1 (rate limiter)**: FIX in this iter
- **C2 (CRAG recursion)**: FIX in this iter
- **H1 (pending clobber)**: FIX in this iter
- **H2 (LLM dispatch limiters)**: FIX in this iter
- **H3 (action_audit background)**: FIX in this iter
- **H4 (CRAG threshold calibration)**: defer to WS3 prerequisite (calibration is part of flipping default-on)
- **H5 (trust docstring)**: FIX (trivial)
- M1-M9: DEFER to M7 reserve buffer iters or M8

---

## Verdict

Phase M5/M6/M9 surface area is **functionally correct** but has 2 critical security regressions in production deployment shape (rate limiter ineffective, CRAG amplifies LLM cost). Both are fixable in <1hr of focused work. Once C1+C2 land, Phase M5 actually mitigates the threat model it was designed for, and Phase M9 can safely flip default-on per WS3 (after H4 calibration).

Concurrency is safe. Logic correctness is mostly good (one real bug: H1 pending clobber). Hot path is currently fine but H3 + H4 are required before WS3 default-on flip.

**Total fix scope**: ~5-7 file edits, ~150 LOC delta.
