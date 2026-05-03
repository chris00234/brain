"""proactive_playbooks — learned event-class playbook detector.

Turns repeated Chris patterns into executable, safety-gated prompts for the
brain loop. This is intentionally not another cron/hook system: it feeds the
existing proactive insight + attention + brain_loop pipeline with structured
"safe read-only playbook" work when recent events match a learned class.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

try:
    from config import BRAIN_DB, BRAIN_LOGS_DIR
except ImportError:  # pragma: no cover - local fallback
    BRAIN_LOGS_DIR = Path("/Users/chrischo/server/brain/logs")
    BRAIN_DB = BRAIN_LOGS_DIR / "brain.db"

log = logging.getLogger("brain.proactive_playbooks")

RECENT_EVENT_HOURS = 6
MAX_RECENT_EVENTS = 80
MAX_EVENTS_PER_CLASS = 3
DISCOVERY_LOOKBACK_DAYS = 30
MAX_DISCOVERY_EVENTS = 1200
MIN_DYNAMIC_SUPPORT = 3
SAFE_MODE = "read_only_or_advisory"
DYNAMIC_PLAYBOOKS_FILE = BRAIN_LOGS_DIR / "proactive_playbook_classes.json"

_EXCLUDED_SOURCE_TYPES = {
    "atoms_hot_path",
    "memory",
    "reflection",
    "synthesis",
    "query_synthesis",
    "brain_command_ack",
}

_DYNAMIC_DISCOVERY_SOURCE_TYPES = {
    "openclaw_session",
    "claude_code_session",
    "shell",
    "telegram",
    "brain_command",
}

_QUESTION_MARKERS = (
    "?",
    "어떻게",
    "뭐",
    "무엇",
    "왜",
    "언제",
    "알려",
    "확인",
    "비교",
    "차이",
    "진행",
    "되어가",
    "됐어",
    "되는거",
    "해야",
    "what",
    "how",
    "why",
    "status",
    "check",
    "compare",
    "diff",
    "difference",
    "progress",
    "done",
    "working",
    "먼저",
    "묻기",
    "물어보기",
    "proactive",
    "proactively",
)

_STOPWORDS = {
    "the",
    "and",
    "for",
    "with",
    "that",
    "this",
    "from",
    "have",
    "what",
    "how",
    "why",
    "when",
    "where",
    "should",
    "would",
    "could",
    "please",
    "about",
    "after",
    "before",
    "into",
    "then",
    "just",
    "need",
    "needs",
    "needed",
    "check",
    "status",
    "chris",
    "chrischo",
    "users",
    "server",
    "raw",
    "event",
    "session",
    "내가",
    "나는",
    "이거",
    "이건",
    "그거",
    "그럼",
    "이제",
    "계속",
    "항상",
    "매번",
    "어떻게",
    "뭐",
    "무엇",
    "왜",
    "좀",
    "잘",
    "되는",
    "되어가",
    "됐어",
    "있는",
    "없는",
    "같은",
    "관련",
    "해서",
    "해줘",
    "알려줘",
}

_GENERIC_DYNAMIC_SAFE_ACTIONS = (
    "retrieve recent related raw events, memories, and current local state for this learned pattern",
    "infer the likely next check Chris expects from repeated evidence and run only read-only diagnostics",
    "summarize what changed, current status, blockers, and evidence-backed next steps",
    "record whether the playbook was useful so future confidence can be raised or lowered",
)

_GENERIC_DYNAMIC_STOP_CONDITIONS = (
    "do not mutate files, services, schedules, credentials, remote resources, or paid settings without explicit request",
    "ask before destructive, external-production, credentialed, or high-cost actions",
    "if evidence is weak or ambiguous, surface a concise proposed playbook instead of acting beyond read-only checks",
)


@dataclass(frozen=True)
class EventClassPlaybook:
    name: str
    title: str
    keywords: tuple[str, ...]
    learned_pattern: str
    safe_actions: tuple[str, ...]
    stop_conditions: tuple[str, ...]
    severity: str = "info"
    min_keyword_matches: int = 1
    support: int = 0
    dynamic: bool = False


@dataclass(frozen=True)
class RecentEvent:
    id: str
    timestamp: str
    source_type: str
    actor: str
    content: str


@dataclass(frozen=True)
class PlaybookCandidate:
    event_class: str
    title: str
    summary: str
    detail: str
    severity: str
    safe_actions: tuple[str, ...]
    stop_conditions: tuple[str, ...]
    evidence: tuple[dict, ...]
    confidence: float


PLAYBOOKS: tuple[EventClassPlaybook, ...] = (
    EventClassPlaybook(
        name="software_update",
        title="post-update change review",
        keywords=(
            "update",
            "updated",
            "upgrade",
            "upgraded",
            "npm install -g",
            "pnpm update",
            "brew upgrade",
            "pip install -U",
            "version bump",
        ),
        learned_pattern=(
            "After tools, apps, packages, or model/provider packages change, Chris tends to ask what changed "
            "from the previous version and whether anything needs follow-up."
        ),
        safe_actions=(
            "identify previous and current version/commit/package identifiers when locally available",
            "summarize changelog/release-note deltas and compatibility risks",
            "run read-only status/doctor/smoke checks that the tool itself exposes",
            "surface follow-up migrations, config changes, or rollback notes without changing state",
        ),
        stop_conditions=(
            "do not install, downgrade, restart production services, or edit configs unless already requested",
            "ask before credentialed external calls or destructive migration steps",
        ),
        severity="info",
    ),
    EventClassPlaybook(
        name="proactive_miss_feedback",
        title="missed proactive opportunity review",
        keywords=(
            "먼저 알려",
            "먼저 안 알려",
            "물어보기 전에",
            "묻기 전에",
            "내가 물어보기",
            "내가 묻기",
            "자발적으로",
            "proactive",
            "proactively",
            "before i ask",
            "why didn't you tell",
            "why did you not tell",
        ),
        learned_pattern=(
            "When Chris says the system should have told him before he asked, Brain should treat that as "
            "negative feedback on the proactive layer and immediately turn the missed opportunity into a "
            "future trigger candidate."
        ),
        safe_actions=(
            "inspect the correction plus recent prior raw events to infer what signal should have triggered earlier",
            "summarize the missed proactive opportunity as an intent class with evidence and confidence",
            "promote or tune a dynamic read-only playbook when the signal is repeated or clearly safe",
            "record the miss as feedback so threshold/autonomy can be adjusted without Chris updating classes manually",
        ),
        stop_conditions=(
            "do not overfit from one ambiguous complaint; require evidence before raising autonomy beyond read-only",
            "do not execute destructive, credentialed, paid, or external-production actions while learning from the miss",
        ),
        severity="warning",
    ),
    EventClassPlaybook(
        name="service_restart",
        title="post-restart health check",
        keywords=(
            "restart",
            "restarted",
            "kickstart",
            "launchctl",
            "systemctl restart",
            "docker compose restart",
            "gateway restart",
        ),
        learned_pattern=(
            "After restarts, Chris expects confirmation that the service actually recovered, not just that the "
            "restart command ran."
        ),
        safe_actions=(
            "check service status/process/listener health through read-only commands",
            "tail recent logs for errors since the restart timestamp",
            "run configured health endpoint or lightweight CLI status if available",
            "report memory/port/session anomalies and the exact evidence used",
        ),
        stop_conditions=(
            "do not restart again, unload launch agents, or mutate service state without a fresh explicit request",
            "ask before touching production/external services or credentials",
        ),
        severity="warning",
    ),
    EventClassPlaybook(
        name="config_change",
        title="post-config-change impact review",
        keywords=(
            "configure",
            "config",
            "settings",
            "doctor --fix",
            "plist",
            "env var",
            "environment variable",
            "migration",
            "migrated",
        ),
        learned_pattern=(
            "After config or doctor/fix changes, Chris usually wants the changed surface, impact, risk, and "
            "rollback point summarized proactively."
        ),
        safe_actions=(
            "identify changed config files/keys and before/after values when available",
            "validate the config with read-only lint/status/doctor checks",
            "summarize behavioral impact, compatibility risks, and rollback path",
            "check dependent services for stale runtime config without mutating them",
        ),
        stop_conditions=(
            "do not apply additional fixes, rewrite configs, or rotate credentials without explicit request",
            "ask before changes that affect external-production or account-level state",
        ),
        severity="warning",
    ),
    EventClassPlaybook(
        name="model_provider_change",
        title="post-model/provider verification",
        keywords=(
            "model",
            "provider",
            "oauth",
            "auth login",
            "api key",
            "codex",
            "claude",
            "openai",
            "anthropic",
            "bedrock",
            "ollama",
        ),
        learned_pattern=(
            "After model/provider/auth changes, Chris tends to need capability, auth, fallback, and smoke-test "
            "confirmation before trusting the new route."
        ),
        safe_actions=(
            "check local auth/model status without exposing secrets",
            "compare capability/fallback differences against the previous route if known",
            "run a minimal non-sensitive smoke prompt only when a local configured CLI makes that safe",
            "summarize likely breakage surfaces: streaming, tools, context, cost, and fallback",
        ),
        stop_conditions=(
            "do not reveal tokens, rotate credentials, purchase quota, or change defaults without explicit request",
            "ask before external paid calls if no safe local smoke path exists",
        ),
        severity="warning",
    ),
    EventClassPlaybook(
        name="deploy_or_release",
        title="post-deploy/release smoke check",
        keywords=(
            "deploy",
            "deployed",
            "release",
            "released",
            "push to prod",
            "migration ran",
            "backup restore",
            "rolled out",
        ),
        learned_pattern=(
            "After deploys or releases, Chris expects a concise verification packet: what changed, health, logs, "
            "and rollback risk."
        ),
        safe_actions=(
            "capture commit/version/release identifier and changed scope",
            "run read-only status, health, and recent-error log checks",
            "verify backup/migration status when the project exposes a read-only check",
            "report rollback notes and any unverified gaps",
        ),
        stop_conditions=(
            "do not deploy again, roll back, run migrations, or mutate cloud state without explicit request",
            "ask before production writes or credentialed external operations",
        ),
        severity="warning",
    ),
    EventClassPlaybook(
        name="background_job_status",
        title="background job progress check",
        keywords=(
            "background job",
            "background jobs",
            "scheduler",
            "scheduled job",
            "cron",
            "launchagent",
            "job failed",
            "job failure",
            "backlog",
            "queue drain",
            "batch job",
        ),
        learned_pattern=(
            "Chris repeatedly asks how background jobs, queues, and scheduled work are going; the Brain should "
            "surface progress, failures, next-run times, and blocked jobs before he has to ask."
        ),
        safe_actions=(
            "list relevant jobs/queues with last run, next run, and recent failure counts",
            "tail recent scheduler/job logs for errors and stuck progress markers",
            "summarize what completed, what is running, what is queued, and what is blocked",
            "recommend safe next checks or manual intervention only when evidence shows a blocker",
        ),
        stop_conditions=(
            "do not restart jobs, clear queues, or modify schedules without explicit request",
            "ask before running heavy catch-up jobs or work that may consume significant LLM/API quota",
        ),
        severity="warning",
    ),
    EventClassPlaybook(
        name="quota_or_usage_pressure",
        title="quota/usage pressure report",
        keywords=(
            "usage limit",
            "rate limit",
            "quota",
            "billing",
            "cost spike",
            "llm usage",
            "token usage",
            "api usage",
            "limit reached",
        ),
        learned_pattern=(
            "When quota, rate-limit, billing, or LLM usage pressure appears, Chris expects the Brain to identify "
            "the culprit, current burn rate, impact, and safe throttling options proactively."
        ),
        safe_actions=(
            "read local usage/backlog/breaker metrics and identify the highest-volume source",
            "summarize current rate, baseline, ratio, affected agents/jobs, and expected recovery window",
            "surface existing safe throttles or already-active governors without changing paid settings",
            "note what work is delayed or degraded because of quota pressure",
        ),
        stop_conditions=(
            "do not purchase quota, change paid plan settings, rotate keys, or disable important jobs without explicit request",
            "ask before triggering additional external API calls if quota is already constrained",
        ),
        severity="warning",
    ),
    EventClassPlaybook(
        name="oauth_or_auth_flow_change",
        title="OAuth/auth flow smoke check",
        keywords=(
            "oauth flow",
            "oauth callback",
            "auth callback",
            "login flow",
            "auth login",
            "token refresh",
            "refresh token",
            "credential",
            "credentials",
            "secretref",
        ),
        learned_pattern=(
            "After auth/OAuth/credential changes, Chris expects a smoke test of the affected route and a secret-safe "
            "risk summary because these changes often fail only at runtime."
        ),
        safe_actions=(
            "check configured auth profile/status without printing secrets or tokens",
            "verify callback/redirect/config presence through read-only local inspection",
            "run a minimal non-sensitive auth/status smoke if a local CLI exposes one",
            "summarize affected agents/providers/services and likely rollback path",
        ),
        stop_conditions=(
            "do not print secrets, rotate credentials, revoke tokens, or start browser/account flows without explicit request",
            "ask before external account operations or paid provider calls",
        ),
        severity="warning",
    ),
    EventClassPlaybook(
        name="backup_or_restore",
        title="backup/restore verification",
        keywords=(
            "backup",
            "backed up",
            "restore",
            "restored",
            "snapshot",
            "dump",
            "archive",
            "disaster recovery",
        ),
        learned_pattern=(
            "After backups, snapshots, or restores, Chris expects proof that artifacts exist, are recent, and are "
            "restorable enough to trust."
        ),
        safe_actions=(
            "list newest backup/snapshot artifacts with size, timestamp, and target component",
            "run read-only verify/check commands when available",
            "summarize coverage gaps, stale backups, and restore-readiness risks",
            "identify the safest rollback/restore reference without executing it",
        ),
        stop_conditions=(
            "do not restore, delete old backups, compact archives, or upload/download large artifacts without explicit request",
            "ask before touching production data or remote storage credentials",
        ),
        severity="warning",
    ),
    EventClassPlaybook(
        name="infra_edge_change",
        title="infra edge health review",
        keywords=(
            "docker",
            "compose",
            "container",
            "nginx",
            "cloudflare",
            "cloudflared",
            "tunnel",
            "dns",
            "ssl",
            "tls",
            "port",
            "firewall",
        ),
        learned_pattern=(
            "For Docker, nginx, Cloudflare, DNS, TLS, and port changes, Chris expects an evidence-backed health "
            "check instead of assuming the edge path is still reachable."
        ),
        safe_actions=(
            "inspect container/process/listener/status output through read-only commands",
            "check local health endpoints and recent edge/proxy logs without changing routing",
            "summarize exposed ports, tunnel/DNS/TLS status, and likely client impact",
            "flag stale config/runtime mismatches and safe rollback references",
        ),
        stop_conditions=(
            "do not restart containers, edit routing, change DNS, or modify Cloudflare/nginx state without explicit request",
            "ask before external-production checks that require credentials or could alter rate limits",
        ),
        severity="warning",
    ),
    EventClassPlaybook(
        name="ui_or_visual_change",
        title="UI/visual regression check",
        keywords=(
            "ui change",
            "visual",
            "screenshot",
            "pixel",
            "css",
            "layout",
            "frontend",
            "control ui",
            "dashboard",
            "page refresh",
        ),
        learned_pattern=(
            "After UI or visual changes, Chris tends to expect screenshot evidence, obvious regression checks, "
            "and a concise mismatch list rather than a text-only claim."
        ),
        safe_actions=(
            "capture or locate current screenshots when the project exposes a safe local path",
            "compare visible layout, copy, spacing, and interaction affordances against the stated target",
            "run available frontend lint/test/build checks",
            "report visual gaps with concrete selectors/files/screenshots where available",
        ),
        stop_conditions=(
            "do not deploy UI changes or mutate production data while testing visuals without explicit request",
            "ask before using external browser accounts or credentialed production pages",
        ),
        severity="info",
    ),
    EventClassPlaybook(
        name="code_change_verification",
        title="post-code-change verification packet",
        keywords=(
            "code change",
            "edited",
            "patched",
            "refactor",
            "fix implemented",
            "tests passed",
            "typecheck",
            "lint",
            "ruff",
            "pytest",
        ),
        learned_pattern=(
            "After code edits or fixes, Chris expects a verified packet: changed files, tests run, remaining risks, "
            "and no fake completion claims."
        ),
        safe_actions=(
            "summarize changed files and behavior scope from local diff/status",
            "run targeted tests/lint/typecheck that are safe and relevant",
            "report exact pass/fail evidence and distinguish pre-existing failures from new ones",
            "surface remaining risks, untested paths, and follow-up verification steps",
        ),
        stop_conditions=(
            "do not commit, push, or run destructive test fixtures without explicit request",
            "ask before long-running or external-production test suites",
        ),
        severity="info",
    ),
)


def _now() -> datetime:
    return datetime.now(UTC)


def _parse_iso(ts: str) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt
    except (TypeError, ValueError):
        return None


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(BRAIN_DB), timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def _recent_events(hours: int = RECENT_EVENT_HOURS, limit: int = MAX_RECENT_EVENTS) -> list[RecentEvent]:
    if not BRAIN_DB.exists():
        return []
    cutoff = (_now() - timedelta(hours=hours)).isoformat(timespec="seconds")
    try:
        with _connect() as conn:
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='raw_events'"
            ).fetchone()
            if not exists:
                return []
            rows = conn.execute(
                """
                SELECT id, timestamp, source_type, actor, substr(content, 1, 1200) AS content
                FROM raw_events
                WHERE timestamp >= ?
                ORDER BY timestamp DESC
                LIMIT ?
                """,
                (cutoff, limit),
            ).fetchall()
    except sqlite3.Error as exc:
        log.debug("recent event scan failed: %s", exc)
        return []

    events: list[RecentEvent] = []
    for row in rows:
        source_type = row["source_type"] or ""
        if source_type in _EXCLUDED_SOURCE_TYPES:
            continue
        content = (row["content"] or "").strip()
        if len(content) < 8:
            continue
        events.append(
            RecentEvent(
                id=row["id"],
                timestamp=row["timestamp"] or "",
                source_type=source_type,
                actor=row["actor"] or "unknown",
                content=content,
            )
        )
    return events


def _matches(playbook: EventClassPlaybook, content: str) -> str | None:
    lower = content.lower()
    hits: list[str] = []
    for keyword in playbook.keywords:
        if keyword.lower() in lower:
            hits.append(keyword)
    if len(hits) >= max(1, playbook.min_keyword_matches):
        return ",".join(hits[:3])
    return None


def _tokenize_signal(content: str) -> list[str]:
    """Return stable signal tokens for automatic class discovery."""
    cleaned = re.sub(r"https?://\S+|[a-f0-9]{8,}|\d{2,}", " ", content.lower())
    raw_tokens = re.findall(r"[a-z][a-z0-9_-]{2,}|[가-힣]{2,}", cleaned)
    tokens: list[str] = []
    for token in raw_tokens:
        token = token.strip("_-")
        if len(token) < 2 or token in _STOPWORDS:
            continue
        tokens.append(token[:40])
    return tokens


def _looks_like_pattern_request(content: str) -> bool:
    lower = content.lower()
    return any(marker in lower for marker in _QUESTION_MARKERS)


def _cluster_signature(tokens: list[str]) -> tuple[str, ...] | None:
    if len(tokens) < 2:
        return None
    counts = Counter(tokens)
    ranked = [tok for tok, _ in counts.most_common(5)]
    if len(ranked) < 2:
        return None
    # Stable, order-insensitive intent signature. Two or three strong terms are
    # enough to group repeated asks while avoiding a single broad keyword.
    return tuple(sorted(ranked[: min(3, len(ranked))]))


def _confidence(event_count: int, learned_evidence_count: int) -> float:
    base = 0.62 + min(0.18, event_count * 0.06)
    if learned_evidence_count:
        base += min(0.15, learned_evidence_count * 0.05)
    return round(min(0.92, base), 2)


def _learned_evidence(playbook: EventClassPlaybook, limit: int = 3) -> list[dict]:
    """Best-effort proof that this is learned, not just a static rule.

    Uses raw/canonical recall if available, but never blocks playbook detection.
    """
    try:
        from search_unified import search_all

        query = f"Chris recurring preference after {playbook.name} proactively {playbook.title}"
        resp = search_all(
            query,
            limit=limit,
            sources=["rag", "canonical"],
            collections=["semantic_memory", "canonical", "experience"],
        )
        rows = resp.get("results", []) if isinstance(resp, dict) else []
    except Exception as exc:
        log.debug("learned evidence lookup failed for %s: %s", playbook.name, exc)
        return []

    evidence: list[dict] = []
    for row in rows[:limit]:
        content = str(row.get("content") or row.get("document") or "")
        if not content:
            continue
        evidence.append(
            {
                "kind": "learned_pattern_memory",
                "id": row.get("id") or row.get("source"),
                "score": row.get("score"),
                "preview": content[:220],
            }
        )
    return evidence


def _playbook_to_dict(playbook: EventClassPlaybook, *, event_ids: list[str] | None = None) -> dict:
    now = _now().isoformat(timespec="seconds")
    return {
        "name": playbook.name,
        "title": playbook.title,
        "keywords": list(playbook.keywords),
        "learned_pattern": playbook.learned_pattern,
        "safe_actions": list(playbook.safe_actions),
        "stop_conditions": list(playbook.stop_conditions),
        "severity": playbook.severity,
        "min_keyword_matches": playbook.min_keyword_matches,
        "support": playbook.support,
        "dynamic": playbook.dynamic,
        "event_ids": event_ids or [],
        "created_at": now,
        "updated_at": now,
    }


def _playbook_from_dict(row: dict) -> EventClassPlaybook | None:
    try:
        return EventClassPlaybook(
            name=str(row["name"]),
            title=str(row.get("title") or "learned pattern check"),
            keywords=tuple(str(x) for x in row.get("keywords", []) if str(x).strip()),
            learned_pattern=str(row.get("learned_pattern") or "Brain learned this repeated pattern."),
            safe_actions=tuple(str(x) for x in row.get("safe_actions", []) if str(x).strip()),
            stop_conditions=tuple(str(x) for x in row.get("stop_conditions", []) if str(x).strip()),
            severity=str(row.get("severity") or "info"),
            min_keyword_matches=max(1, int(row.get("min_keyword_matches") or 1)),
            support=max(0, int(row.get("support") or 0)),
            dynamic=bool(row.get("dynamic", True)),
        )
    except (KeyError, TypeError, ValueError):
        return None


def load_dynamic_playbooks() -> list[EventClassPlaybook]:
    if not DYNAMIC_PLAYBOOKS_FILE.exists():
        return []
    try:
        data = json.loads(DYNAMIC_PLAYBOOKS_FILE.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        log.debug("dynamic playbook load failed: %s", exc)
        return []
    rows = data.get("playbooks", []) if isinstance(data, dict) else []
    playbooks: list[EventClassPlaybook] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        playbook = _playbook_from_dict(row)
        if playbook and playbook.keywords:
            playbooks.append(playbook)
    return playbooks


def _write_dynamic_playbooks(rows: list[dict]) -> None:
    DYNAMIC_PLAYBOOKS_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "updated_at": _now().isoformat(timespec="seconds"),
        "min_support": MIN_DYNAMIC_SUPPORT,
        "safe_mode": SAFE_MODE,
        "playbooks": rows,
    }
    tmp = DYNAMIC_PLAYBOOKS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    tmp.replace(DYNAMIC_PLAYBOOKS_FILE)


def _dynamic_name(signature: tuple[str, ...]) -> str:
    slug = "_".join(re.sub(r"[^a-z0-9가-힣]+", "_", tok).strip("_") for tok in signature)
    slug = re.sub(r"_+", "_", slug).strip("_")[:48] or "pattern"
    digest = hashlib.sha256("|".join(signature).encode()).hexdigest()[:8]
    return f"learned_{slug}_{digest}"


def _dynamic_title(signature: tuple[str, ...]) -> str:
    return "learned pattern check: " + ", ".join(signature[:3])


def _build_dynamic_playbook(signature: tuple[str, ...], support: int) -> EventClassPlaybook:
    title = _dynamic_title(signature)
    learned_pattern = (
        f"Brain observed this unmatched pattern at least {support} times without a hand-written class. "
        "Chris prefers the system to generalize recurring asks into a safe proactive execution layer."
    )
    return EventClassPlaybook(
        name=_dynamic_name(signature),
        title=title,
        keywords=signature,
        learned_pattern=learned_pattern,
        safe_actions=_GENERIC_DYNAMIC_SAFE_ACTIONS,
        stop_conditions=_GENERIC_DYNAMIC_STOP_CONDITIONS,
        severity="info",
        min_keyword_matches=min(2, len(signature)),
        support=support,
        dynamic=True,
    )


def discover_dynamic_playbooks(
    *,
    events: list[RecentEvent] | None = None,
    persist: bool = True,
) -> list[EventClassPlaybook]:
    """Discover new event classes without code changes.

    Auto-promotes only repeated unmatched intent signatures (support >= 3).
    Generated playbooks stay read-only/advisory and require two keyword hits
    to avoid broad single-token noise.
    """
    discovery_events = events
    if discovery_events is None:
        discovery_events = _recent_events(hours=DISCOVERY_LOOKBACK_DAYS * 24, limit=MAX_DISCOVERY_EVENTS)
    if not discovery_events:
        return load_dynamic_playbooks()

    existing_dynamic_rows: list[dict] = []
    existing_dynamic = load_dynamic_playbooks()
    if DYNAMIC_PLAYBOOKS_FILE.exists():
        try:
            data = json.loads(DYNAMIC_PLAYBOOKS_FILE.read_text())
            existing_dynamic_rows = [r for r in data.get("playbooks", []) if isinstance(r, dict)]
        except (OSError, json.JSONDecodeError):
            existing_dynamic_rows = []

    all_known = (*PLAYBOOKS, *existing_dynamic)
    clusters: dict[tuple[str, ...], list[RecentEvent]] = defaultdict(list)
    for event in discovery_events:
        if event.source_type not in _DYNAMIC_DISCOVERY_SOURCE_TYPES:
            continue
        content = event.content
        if not _looks_like_pattern_request(content):
            continue
        if any(_matches(playbook, content) for playbook in all_known):
            continue
        signature = _cluster_signature(_tokenize_signal(content))
        if not signature:
            continue
        clusters[signature].append(event)

    by_name = {str(row.get("name")): row for row in existing_dynamic_rows}
    new_playbooks: list[EventClassPlaybook] = []
    for signature, matched_events in clusters.items():
        support = len({ev.id for ev in matched_events})
        if support < MIN_DYNAMIC_SUPPORT:
            continue
        playbook = _build_dynamic_playbook(signature, support)
        event_ids = [ev.id for ev in matched_events[:10]]
        existing = by_name.get(playbook.name)
        if existing:
            existing["support"] = max(int(existing.get("support") or 0), support)
            existing["event_ids"] = sorted(set(existing.get("event_ids", []) + event_ids))[:20]
            existing["updated_at"] = _now().isoformat(timespec="seconds")
        else:
            by_name[playbook.name] = _playbook_to_dict(playbook, event_ids=event_ids)
            new_playbooks.append(playbook)

    if persist and (new_playbooks or by_name):
        _write_dynamic_playbooks(sorted(by_name.values(), key=lambda r: str(r.get("name"))))

    return load_dynamic_playbooks() if persist else [*existing_dynamic, *new_playbooks]


def _candidate_id(playbook_name: str, events: list[dict]) -> str:
    day = _now().date().isoformat()
    newest = events[0].get("id", "") if events else ""
    raw = f"{playbook_name}|{day}|{newest}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def detect_playbook_candidates(
    *,
    events: list[RecentEvent] | None = None,
    include_learned_evidence: bool = True,
    include_dynamic_discovery: bool = True,
) -> list[PlaybookCandidate]:
    """Return event-class playbooks that should be surfaced now.

    This is read-only: it does not execute commands. The execution layer is the
    existing brain_loop/action trigger path, which receives structured safe
    actions plus stop conditions and passes through autonomy.authorize().
    """
    explicit_events = events is not None
    events = events if events is not None else _recent_events()
    if not events:
        return []

    if include_dynamic_discovery:
        dynamic_playbooks = (
            discover_dynamic_playbooks(events=events, persist=False)
            if explicit_events
            else discover_dynamic_playbooks(persist=True)
        )
    else:
        dynamic_playbooks = load_dynamic_playbooks()
    all_playbooks = (*PLAYBOOKS, *dynamic_playbooks)

    candidates: list[PlaybookCandidate] = []
    for playbook in all_playbooks:
        matched: list[dict] = []
        for event in events:
            keyword = _matches(playbook, event.content)
            if not keyword:
                continue
            event_dt = _parse_iso(event.timestamp)
            age_min = None
            if event_dt:
                age_min = round((_now() - event_dt).total_seconds() / 60, 1)
            matched.append(
                {
                    "kind": "recent_event",
                    "id": event.id,
                    "timestamp": event.timestamp,
                    "source_type": event.source_type,
                    "actor": event.actor,
                    "matched_keyword": keyword,
                    "age_minutes": age_min,
                    "preview": re.sub(r"\s+", " ", event.content)[:240],
                }
            )
            if len(matched) >= MAX_EVENTS_PER_CLASS:
                break
        if not matched:
            continue

        learned = _learned_evidence(playbook) if include_learned_evidence else []
        summary = f"{playbook.title} after recent {playbook.name.replace('_', ' ')} signal"
        actions = "\n".join(f"- {a}" for a in playbook.safe_actions)
        stops = "\n".join(f"- {s}" for s in playbook.stop_conditions)
        detail = (
            f"Detected a recent `{playbook.name}` event class. Learned pattern: {playbook.learned_pattern}\n\n"
            f"Safe execution layer ({SAFE_MODE}) should proactively do:\n{actions}\n\n"
            f"Safety stop conditions:\n{stops}\n\n"
            "Report the result concisely with evidence and any verification gaps."
        )
        evidence = [
            {
                "kind": "playbook",
                "event_class": playbook.name,
                "safe_mode": SAFE_MODE,
                "safe_actions": list(playbook.safe_actions),
                "stop_conditions": list(playbook.stop_conditions),
            },
            *matched,
            *learned,
        ]
        candidates.append(
            PlaybookCandidate(
                event_class=playbook.name,
                title=playbook.title,
                summary=summary,
                detail=detail,
                severity=playbook.severity,
                safe_actions=playbook.safe_actions,
                stop_conditions=playbook.stop_conditions,
                evidence=tuple(evidence),
                confidence=_confidence(len(matched), len(learned)),
            )
        )
    return candidates


def candidates_as_insights() -> list[dict]:
    """Return dictionaries shaped for proactive.ProactiveInsight construction."""
    rows = []
    for cand in detect_playbook_candidates():
        event_evidence = [e for e in cand.evidence if e.get("kind") == "recent_event"]
        rows.append(
            {
                "id": _candidate_id(cand.event_class, event_evidence),
                "category": "playbook",
                "severity": cand.severity,
                "summary": cand.summary,
                "detail": cand.detail,
                "evidence": list(cand.evidence),
                "confidence": cand.confidence,
            }
        )
    return rows
