"""brain_client.py — thin Python SDK over the Brain HTTP API.

A 200-line wrapper around the brain's FastAPI surface. Maps 1:1 to the 12
brain_* MCP tools but is callable from any Python script without going
through MCP. Designed so a developer can `pip install brain-client` (once
published) and start using it without reading the FastAPI source.

Auth:
  - Reads bearer token from `~/.openclaw/credentials/.personal_webhook_secret`
    (default), or pass `token=` explicitly to the constructor.
  - All requests get `Authorization: Bearer <token>` and `x-agent: <actor>`
    so the brain's `action_audit` table sees who's calling.

Usage:
    from brain_client import BrainClient

    brain = BrainClient(actor="my_script")

    # 1. Recall
    results = brain.recall("how do we deploy ghost?", limit=5)

    # 2. Store
    mem = brain.store("I prefer Tailwind over CSS modules", category="preference")

    # 3. Reason
    answer = brain.reason("Should we use Postgres or SQLite for the new service?")

    # 4. Web search (Phase M6)
    hits = brain.search_web("Apple Vision Pro release date")

    # 5. Health
    print(brain.health()["status"])
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_BASE = "http://127.0.0.1:8791"
DEFAULT_SECRET = Path("~/.openclaw/credentials/.personal_webhook_secret").expanduser()


class BrainError(Exception):
    """Wraps any non-2xx response from the brain API."""

    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"brain returned {status}: {body[:300]}")
        self.status = status
        self.body = body


class BrainClient:
    """Single-purpose client over the brain's HTTP surface.

    Parameters
    ----------
    base_url : str
        Brain endpoint (default http://127.0.0.1:8791).
    token : str | None
        Bearer token. Falls back to ~/.openclaw/credentials/.personal_webhook_secret.
    actor : str
        The agent name to record in `action_audit`. Use one of:
        jenna | liz | ellie | sage | market | claude-code | <your_script>.
    timeout : float
        Per-request timeout in seconds.
    """

    def __init__(
        self,
        base_url: str = DEFAULT_BASE,
        token: str | None = None,
        actor: str = "sdk",
        timeout: float = 60,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.actor = actor
        self.timeout = timeout
        if token is None and DEFAULT_SECRET.exists():
            token = DEFAULT_SECRET.read_text().strip()
        if not token:
            raise RuntimeError("no bearer token — pass token= or create " f"{DEFAULT_SECRET}")
        self.token = token

    # ── Internal HTTP layer ───────────────────────────────────────────

    def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = self.base_url + path
        if not url.startswith(("http://", "https://")):
            raise ValueError(f"unsupported scheme: {url}")
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = json.dumps(body).encode() if body else None
        req = urllib.request.Request(url, data=data, method=method)  # noqa: S310
        req.add_header("Authorization", f"Bearer {self.token}")
        req.add_header("x-agent", self.actor)
        if data:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310
                raw = resp.read().decode()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            body = e.read().decode() if e.fp else ""
            raise BrainError(e.code, body) from None

    # ── 12 brain_* tool methods ──────────────────────────────────────

    def recall(
        self,
        query: str,
        limit: int = 5,
        collection: str | None = None,
        iterative: bool = False,
    ) -> dict[str, Any]:
        """Hybrid search across all collections. Returns RAG results."""
        params: dict[str, Any] = {"q": query, "n": limit, "actor": self.actor}
        if collection:
            params["collection"] = collection
        if iterative:
            params["iterative"] = "true"
        return self._request("GET", "/recall/v2", params=params)

    def store(
        self,
        content: str,
        category: str = "fact",
        confidence: float = 0.7,
        source: str = "sdk",
    ) -> dict[str, Any]:
        """Insert a memory with auto-supersession via lifecycle pipeline."""
        return self._request(
            "POST",
            "/memory",
            body={
                "content": content,
                "category": category,
                "agent": self.actor,
                "source": source,
                "confidence": confidence,
            },
        )

    def decide(self, situation: str, options: list[dict[str, Any]]) -> dict[str, Any]:
        return self._request(
            "POST",
            "/brain/decide",
            body={"situation": situation, "options": options, "agent": self.actor},
        )

    def reason(self, question: str) -> dict[str, Any]:
        return self._request("POST", "/brain/reason", body={"question": question, "agent": self.actor})

    def ingest(self, content: str, source: str = "sdk_ingest") -> dict[str, Any]:
        return self._request("POST", "/brain/ingest", body={"content": content, "source": source})

    def focus(self, content: str) -> dict[str, Any]:
        return self._request(
            "POST",
            "/brain/focus",
            body={"content": content, "category": "focus", "agent": self.actor},
        )

    def message(self, to_agent: str, content: str, message_type: str = "info") -> dict[str, Any]:
        return self._request(
            "POST",
            "/brain/messages",
            body={
                "from_agent": self.actor,
                "to_agent": to_agent,
                "content": content,
                "message_type": message_type,
                "priority": 5,
            },
        )

    def changes(self, since: str = "7d", until: str = "now") -> dict[str, Any]:
        return self._request("GET", "/brain/changes", params={"since": since, "until": until})

    def evolution(self, topic: str) -> dict[str, Any]:
        return self._request("GET", "/brain/evolution", params={"topic": topic})

    def procedures(self, task_type: str | None = None, limit: int = 5) -> dict[str, Any]:
        params: dict[str, Any] = {"limit": limit}
        if task_type:
            params["task_type"] = task_type
        return self._request("GET", "/brain/procedures", params=params)

    def outcome(self, task_id: str, success: bool, notes: str = "") -> dict[str, Any]:
        suffix = "complete" if success else "reject"
        return self._request(
            "POST",
            f"/brain/tasks/{urllib.parse.quote(task_id)}/{suffix}",
            body={"result": notes, "agent": self.actor},
        )

    def search_web(self, query: str, limit: int = 10) -> dict[str, Any]:
        return self._request(
            "POST",
            "/web/search",
            body={"query": query, "limit": limit, "agent": self.actor},
        )

    # ── Operator surfaces ────────────────────────────────────────────

    def health(self) -> dict[str, Any]:
        return self._request("GET", "/brain/health")

    def usage(self, days: int = 7) -> dict[str, Any]:
        return self._request("GET", "/brain/usage", params={"days": days})

    def slos(self) -> dict[str, Any]:
        return self._request("GET", "/brain/slos")

    # ── Convenience ──────────────────────────────────────────────────

    def __repr__(self) -> str:
        return f"BrainClient(base={self.base_url!r}, actor={self.actor!r})"


__all__ = ["BrainClient", "BrainError"]
