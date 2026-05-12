"""brain_core/web_search.py — SearXNG client + brain learning loop (Phase M6).

The brain learns from web search outcomes:

1. Agent issues a `searxng_query(q)` call → SearXNG returns ranked results
2. Each call writes a `web_search_attempts` row + N `web_search_results` rows
3. When the agent later marks a result as useful/wrong via /recall/feedback,
   the `outcome` column is updated and the source domain's trust score moves
4. A weekly `web_source_trust_recompute` job aggregates outcomes per domain
   and re-ranks future results by historical accuracy

This module owns the SearXNG client side. The trust-aggregation job lives
in `brain_core/pipeline/web_source_trust.py` (registered in scheduler).

The SearXNG container exposes its JSON API on `127.0.0.1:8888` (port mapped
from the docker bridge — see `~/server/searxng/docker-compose.yml`).
Public access goes through Cloudflare Access at search.chrischodev.com,
which we don't use here because the brain runs on the same host.
"""

from __future__ import annotations

import logging
import sqlite3
import sys
import time
import urllib.parse
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from config import BRAIN_DB
except ImportError:
    BRAIN_DB = Path("/Users/chrischo/server/brain/logs/brain.db")


log = logging.getLogger("brain.web_search")

SEARXNG_URL = "http://127.0.0.1:8888"
DEFAULT_TIMEOUT = 10.0
DEFAULT_LIMIT = 10
MAX_RESULTS = 50

# In-process sync httpx client — short timeouts, no retries.
# Avoid pooling because the brain server already sits behind a thread pool.
_client: httpx.Client | None = None


def _get_client() -> httpx.Client:
    global _client
    if _client is None:
        _client = httpx.Client(
            base_url=SEARXNG_URL,
            timeout=DEFAULT_TIMEOUT,
            follow_redirects=True,
        )
    return _client


@contextmanager
def _conn() -> Iterator[sqlite3.Connection]:
    BRAIN_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(BRAIN_DB))
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


from db import now_iso as _now  # noqa: E402  — single-source UTC stamp helper


def _domain_of(url: str) -> str:
    try:
        return urllib.parse.urlparse(url).netloc.lower()
    except Exception:
        return ""


def searxng_query(query: str, *, n: int = DEFAULT_LIMIT, agent: str = "unknown") -> list[dict]:
    """Hit SearXNG and return ranked results.

    Args:
        query: search query
        n: number of results to return (capped at 50)
        agent: caller agent name (recorded in web_search_attempts)

    Returns:
        list of result dicts: {rank, url, domain, title, snippet, score}
        score is the historical trust score for the domain (0-1.0,
        defaulting to 0.5 for unseen domains).
    """
    n = max(1, min(n, MAX_RESULTS))
    if not query or not query.strip():
        return []

    attempt_id = f"ws_{uuid.uuid4().hex[:12]}"
    t0 = time.time()
    results: list[dict] = []

    try:
        client = _get_client()
        resp = client.get(
            "/search",
            params={"q": query, "format": "json", "language": "all", "safesearch": "0"},
        )
        resp.raise_for_status()
        data = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("searxng query failed: %s", exc)
        return []

    raw_results = (data.get("results") or [])[:n]
    if not raw_results:
        return []

    # Rank, domain, then look up trust scores in one batch
    domains = [_domain_of(r.get("url", "")) for r in raw_results]
    trust_map = _load_domain_trust(domains)

    for i, r in enumerate(raw_results, start=1):
        url = r.get("url", "")
        domain = _domain_of(url)
        results.append(
            {
                "rank": i,
                "url": url,
                "domain": domain,
                "title": (r.get("title") or "")[:200],
                "snippet": (r.get("content") or "")[:500],
                "score": trust_map.get(domain, 0.5),
            }
        )

    # Persist the attempt + results so the trust loop has data to learn from
    _persist_attempt(attempt_id, query, agent, results)

    elapsed_ms = int((time.time() - t0) * 1000)
    log.info("searxng query=%r n=%d elapsed_ms=%d", query[:40], len(results), elapsed_ms)
    return results


def _load_domain_trust(domains: list[str]) -> dict[str, float]:
    """Batch-fetch trust scores for a list of domains."""
    if not domains:
        return {}
    unique = sorted({d for d in domains if d})
    if not unique:
        return {}
    # Placeholder count is bound to len(unique), not user data — the IN clause
    # values themselves are passed as ? bind parameters below.
    placeholders = ",".join("?" * len(unique))
    try:
        with _conn() as conn:
            # Placeholders are `?` bind markers (count bounded by len(unique)),
            # values pass through ? — not user-data SQL injection.
            sql = (
                "SELECT domain, score FROM web_source_trust WHERE domain IN ("  # noqa: S608
                + placeholders
                + ")"
            )
            rows = conn.execute(sql, unique).fetchall()
            return {r["domain"]: r["score"] for r in rows}
    except sqlite3.Error:
        return {}


def _persist_attempt(
    attempt_id: str,
    query: str,
    agent: str,
    results: list[dict],
) -> None:
    """Record the search attempt + per-result rows. Best-effort."""
    if not results:
        return
    try:
        with _conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO web_search_attempts (id, query, ts, agent, intent) " "VALUES (?, ?, ?, ?, ?)",
                (attempt_id, query[:500], _now(), agent, ""),
            )
            conn.executemany(
                "INSERT INTO web_search_results "
                "(attempt_id, rank, url, domain, title, snippet, chosen, outcome) "
                "VALUES (?, ?, ?, ?, ?, ?, 0, NULL)",
                [
                    (
                        attempt_id,
                        r["rank"],
                        r["url"],
                        r["domain"],
                        r["title"],
                        r["snippet"],
                    )
                    for r in results
                ],
            )
            conn.commit()
    except sqlite3.Error as exc:
        log.warning("persist_attempt failed: %s", exc)


def mark_result_outcome(attempt_id: str, rank: int, *, useful: bool) -> bool:
    """Mark a single search result as useful (True) or wrong (False).

    Called by the /recall/feedback hook when an agent acts on a search result.
    """
    if not attempt_id or rank < 1:
        return False
    outcome = "useful" if useful else "wrong"
    try:
        with _conn() as conn:
            cur = conn.execute(
                "UPDATE web_search_results SET chosen=1, outcome=? " "WHERE attempt_id=? AND rank=?",
                (outcome, attempt_id, rank),
            )
            conn.commit()
            return cur.rowcount > 0
    except sqlite3.Error:
        return False


def recompute_domain_trust() -> dict:
    """Aggregate per-domain outcomes and refresh the trust score table.

    Score formula: smoothed Wilson-like ratio of `useful / (useful + wrong)`
    with a Laplace prior of (1, 1). Domains with no signal stay at 0.5.

    Returns a summary dict for the scheduler.
    """
    updated = 0
    try:
        with _conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                "SELECT domain, "
                "  SUM(CASE WHEN outcome='useful' THEN 1 ELSE 0 END) AS n_useful, "
                "  SUM(CASE WHEN outcome='wrong' THEN 1 ELSE 0 END) AS n_wrong, "
                "  COUNT(*) AS n_total "
                "FROM web_search_results "
                "WHERE outcome IS NOT NULL AND domain != '' "
                "GROUP BY domain"
            ).fetchall()
            now = _now()
            for r in rows:
                dom = r["domain"]
                n_useful = (r["n_useful"] or 0) + 1  # Laplace prior
                n_wrong = (r["n_wrong"] or 0) + 1
                score = round(n_useful / (n_useful + n_wrong), 4)
                conn.execute(
                    "INSERT INTO web_source_trust "
                    "(domain, n_used, n_correct, score, last_updated) "
                    "VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(domain) DO UPDATE SET "
                    "  n_used=excluded.n_used, n_correct=excluded.n_correct, "
                    "  score=excluded.score, last_updated=excluded.last_updated",
                    (dom, r["n_total"] or 0, r["n_useful"] or 0, score, now),
                )
                updated += 1
            conn.commit()
    except sqlite3.Error as exc:
        return {"error": str(exc)[:200], "updated": updated}
    return {"updated": updated}


if __name__ == "__main__":
    import json
    import sys as _sys

    out = searxng_query("python list comprehension", n=5, agent="cli_test")
    _sys.stdout.write(json.dumps(out, indent=2) + "\n")
