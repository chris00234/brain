"""Brain memory provider — Hermes ↔ brain HTTP (127.0.0.1:8791) bridge.

Implements Hermes's MemoryProvider ABC so brain becomes the agent's
canonical memory backend. Profile-aware: each Hermes profile is tagged
as `agent:<profile_name>` so atoms can be filtered/attributed per persona.

Architecture (Phase 3 — 2026-05-23 OpenClaw → Hermes migration):

  Hermes profile (jenna|liz|ellie|sage|market)
    │
    │ initialize(agent_identity="<profile>") ──→ self._profile
    │
    ├─ prefetch(query)   ──→ GET /recall/v2 (filtered by agent/profile)
    │                         returns top-K results as system prompt context
    │
    └─ sync_turn(u, a)   ──→ POST /memory (tagged agent:<profile>)
                              brain cosine update gate + supersedes chain
                              decide canonical promotion

Why this matters: Hermes ships GitHub Issues #6320 (session/memory
contamination across profiles) and #4726 (profile-scoped namespaces
unimplemented). The default holographic provider can leak between
profiles. brain owns canonical truth; this provider routes writes
and reads through brain's existing cross-agent attribution mechanism
(qdrant `agent` filter + tags + cosine update gate).

Tool surface: empty (brain MCP server already exposes 21 tools via
mcp_servers.brain in config.yaml — no duplication).
"""

from __future__ import annotations

import json
import logging
import os
import queue
import threading
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from agent.memory_provider import MemoryProvider

log = logging.getLogger(__name__)

# ─── Config ──────────────────────────────────────────────────────────────────

BRAIN_HOST = os.environ.get("BRAIN_HTTP_HOST", "127.0.0.1")
BRAIN_PORT = int(os.environ.get("BRAIN_HTTP_PORT", "8791"))
BRAIN_BASE = f"http://{BRAIN_HOST}:{BRAIN_PORT}"
SECRET_FILE = Path.home() / ".brain/credentials/.personal_webhook_secret"

PREFETCH_K = int(os.environ.get("BRAIN_PREFETCH_K", "5"))
PREFETCH_TIMEOUT_S = float(os.environ.get("BRAIN_PREFETCH_TIMEOUT", "3"))
WRITE_TIMEOUT_S = float(os.environ.get("BRAIN_WRITE_TIMEOUT", "5"))


# ─── HTTP helpers ────────────────────────────────────────────────────────────


def _load_bearer() -> str | None:
    try:
        return SECRET_FILE.read_text().strip()
    except FileNotFoundError:
        return None


def _brain_request(
    path: str,
    method: str = "GET",
    body: dict[str, Any] | None = None,
    timeout: float = 5.0,
    actor: str | None = None,
) -> dict[str, Any] | None:
    """One brain HTTP round-trip. Returns dict or None on failure."""
    bearer = _load_bearer()
    if not bearer:
        log.warning("brain provider: secret file missing — auth will fail")
        return None
    url = BRAIN_BASE + path
    headers = {"Authorization": f"Bearer {bearer}", "Content-Type": "application/json"}
    if actor:
        headers["x-agent"] = actor
    data: bytes | None = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(  # noqa: S310 - BRAIN_BASE is fixed to local HTTP.
        url, data=data, method=method, headers=headers
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            return json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # pragma: no cover — observability is the only contract
        log.debug("brain request %s %s failed: %s", method, path, exc)
        return None


# ─── Provider ────────────────────────────────────────────────────────────────


class BrainMemoryProvider(MemoryProvider):
    """Brain HTTP-backed memory provider, profile-aware via agent tag."""

    def __init__(self) -> None:
        self._profile: str = "default"
        self._hermes_home: str = ""
        self._platform: str = "cli"
        self._session_id: str = ""
        self._write_queue: queue.Queue[dict[str, Any] | None] = queue.Queue()
        self._writer_thread: threading.Thread | None = None
        self._stopping = threading.Event()

    @property
    def name(self) -> str:
        return "brain"

    # ── Required: availability check (no network) ────────────────────────────

    def is_available(self) -> bool:
        """Config check only. Secret file must exist."""
        return SECRET_FILE.is_file()

    # ── Required: lifecycle ──────────────────────────────────────────────────

    def initialize(self, session_id: str, **kwargs: Any) -> None:
        self._session_id = session_id
        self._hermes_home = kwargs.get("hermes_home", "") or ""
        self._platform = kwargs.get("platform", "cli")

        # Profile name comes from agent_identity (kwargs from MemoryManager).
        # Fallback: derive from HERMES_HOME path (~/.hermes/profiles/<name>).
        profile = kwargs.get("agent_identity") or ""
        if not profile and self._hermes_home:
            parts = Path(self._hermes_home).parts
            if "profiles" in parts:
                i = parts.index("profiles")
                if i + 1 < len(parts):
                    profile = parts[i + 1]
        self._profile = profile or "default"

        # Verify brain reachable (best-effort; non-fatal if down).
        health = _brain_request("/brain/health", timeout=3.0)
        if health is None:
            log.warning("brain provider: brain HTTP unreachable at %s — sessions will degrade", BRAIN_BASE)
        else:
            log.info(
                "brain provider initialized: profile=%s session=%s platform=%s",
                self._profile,
                self._session_id,
                self._platform,
            )

        # Start background writer thread.
        self._writer_thread = threading.Thread(target=self._writer_loop, name="brain-mem-writer", daemon=True)
        self._writer_thread.start()

    def shutdown(self) -> None:
        self._write_queue.put(None)  # sentinel
        if self._writer_thread:
            self._writer_thread.join(timeout=2.0)
        self._stopping.set()

    # ── System prompt block (static) ─────────────────────────────────────────

    def system_prompt_block(self) -> str:
        return (
            f"You have access to Chris's brain (canonical memory at "
            f"{BRAIN_BASE}) via this profile: '{self._profile}'.\n"
            f"Memory writes and reads flow through brain's HTTP API, tagged "
            f"with agent:{self._profile} for profile-scoped recall. The brain "
            f"MCP tools (brain_recall, brain_store, brain_decide, etc.) are "
            f"the explicit interface; prefetched recall is injected per turn."
        )

    # ── Prefetch (per-turn recall) ───────────────────────────────────────────

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        if not query or not query.strip():
            return ""
        params = urllib.parse.urlencode(
            {
                "q": query,
                "n": PREFETCH_K,
                "collection": "semantic_memory",
                "agent": self._profile,
            }
        )
        resp = _brain_request(
            f"/recall/v2?{params}",
            timeout=PREFETCH_TIMEOUT_S,
            actor=self._profile,
        )
        if not resp or not resp.get("results"):
            return ""
        return self._format_recall(resp["results"])

    def _format_recall(self, results: list[dict[str, Any]]) -> str:
        lines: list[str] = []
        for r in results[:PREFETCH_K]:
            title = (r.get("title") or "").strip() or "(untitled)"
            content = (r.get("content") or "").strip().replace("\n", " ")
            if len(content) > 320:
                content = content[:317] + "..."
            score = r.get("score", 0.0)
            lines.append(f"  [{score:.2f}] {title} — {content}")
        if not lines:
            return ""
        return "Brain recall (profile=" + self._profile + "):\n" + "\n".join(lines)

    # ── Sync turn (background write) ─────────────────────────────────────────

    def sync_turn(self, user_content: str, assistant_content: str, *, session_id: str = "") -> None:
        if not user_content and not assistant_content:
            return
        payload = {
            "content": self._render_turn(user_content, assistant_content),
            "category": "other",
            "agent": self._profile,
            "source": "hermes",
            "confidence": 0.5,  # Hermes turns are raw — brain promotes after cosine gate
            "reason": (
                "kind=session_turn " f"session={session_id or self._session_id} " f"platform={self._platform}"
            )[:300],
        }
        self._write_queue.put(payload)

    def _render_turn(self, user: str, assistant: str) -> str:
        u = (user or "").strip()
        a = (assistant or "").strip()
        if u and a:
            return f"User: {u}\nAssistant: {a}"
        return u or a

    def _writer_loop(self) -> None:
        while True:
            item = self._write_queue.get()
            if item is None:
                return  # shutdown sentinel
            try:
                _brain_request(
                    "/memory", method="POST", body=item, timeout=WRITE_TIMEOUT_S, actor=self._profile
                )
            except Exception as exc:  # pragma: no cover
                log.debug("brain provider write failed: %s", exc)

    # ── Tool surface (deliberately empty) ────────────────────────────────────

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        # Brain MCP server (mcp_servers.brain in config.yaml) already exposes
        # 21 tools. No duplication here.
        return []

    # ── Built-in Hermes memory tool mirror ───────────────────────────────────

    def on_memory_write(
        self,
        action: str,
        target: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        if action not in {"add", "replace"} or not content.strip():
            return
        metadata = dict(metadata or {})
        session = metadata.get("session_id") or self._session_id
        payload = {
            "content": content.strip(),
            "category": "preference" if target == "user" else "other",
            "agent": self._profile,
            "source": "hermes",
            "confidence": 0.65,
            "reason": (
                "kind=builtin_memory_write "
                f"action={action} target={target} "
                f"session={session} platform={self._platform}"
            )[:300],
        }
        self._write_queue.put(payload)

    # ── Optional hook: end-of-session distillation ───────────────────────────

    def on_session_end(self, messages: list[dict[str, Any]]) -> None:
        if not messages:
            return
        # Build one session-summary atom so brain has a coarse-grained record
        # even if individual sync_turn writes were sampled out.
        joined: list[str] = []
        for m in messages[-20:]:  # last 20 messages — cap for size
            role = m.get("role", "?")
            content = (m.get("content") or "").strip()
            if not content:
                continue
            if len(content) > 200:
                content = content[:197] + "..."
            joined.append(f"{role}: {content}")
        if not joined:
            return
        payload = {
            "content": "Hermes session ended.\n" + "\n".join(joined),
            "category": "other",
            "agent": self._profile,
            "source": "hermes",
            "confidence": 0.4,
            "reason": ("kind=session_summary " f"session={self._session_id} " f"platform={self._platform}")[
                :300
            ],
        }
        _brain_request("/memory", method="POST", body=payload, timeout=WRITE_TIMEOUT_S, actor=self._profile)


# Hermes plugin loader convention: top-level class exposed at module level.
PROVIDER_CLASS = BrainMemoryProvider
