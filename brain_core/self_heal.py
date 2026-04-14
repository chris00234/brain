"""brain_core/self_heal.py — Self-healing dispatcher for brain degradation signals.

Takes HealingSignal events and runs matching healer functions. All healers are
conservative: they can only improve or revert to a known-good state.

Rate-limited per (signal_type, target) to prevent thrashing.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Any, Optional

log = logging.getLogger("brain.self_heal")

HEAL_LOG = Path("/Users/chrischo/server/brain/logs/self_heal.jsonl")
HEAL_STATE_DB = Path("/Users/chrischo/server/brain/logs/self_heal_state.db")
RATE_LIMIT_SECONDS = 6 * 3600  # 6 hours per (signal_type, target)

try:
    from config import BRAIN_AUTO_HEAL_ENABLED, load_bearer_secret
except ImportError:
    BRAIN_AUTO_HEAL_ENABLED = False

    def load_bearer_secret() -> str:
        return Path("/Users/chrischo/.openclaw/credentials/.personal_webhook_secret").read_text().strip()

# Optional whitelist: if set, ONLY signals with `signal.source` in this set
# are actioned, even when BRAIN_AUTO_HEAL_ENABLED=true. Empty = all sources.
# Used for staged rollout (week 2: slo_monitor only → week 3: add eval_gate).
_auto_heal_sources_raw = os.environ.get("BRAIN_AUTO_HEAL_SOURCES", "").strip()
BRAIN_AUTO_HEAL_SOURCES: set[str] = (
    {s.strip() for s in _auto_heal_sources_raw.split(",") if s.strip()}
    if _auto_heal_sources_raw
    else set()
)


@dataclass
class HealingSignal:
    source: str           # "eval_gate", "slo_monitor", "memory_leak_detector"
    signal_type: str      # "eval_regression", "slo_latency_breach", "memory_growth"
    severity: str         # "low", "medium", "high", "critical"
    metric: str           # e.g. "recall_p95_ms", "hit_content_pct"
    value: float
    baseline: float
    target: str = "default"  # e.g. collection name, agent name
    context: Optional[dict] = field(default=None)

    def to_dict(self) -> dict:
        return asdict(self)


_state_db_initialized = False


def _init_state_db():
    global _state_db_initialized
    HEAL_STATE_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(HEAL_STATE_DB))
    if not _state_db_initialized:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS heal_history (
                signal_type TEXT NOT NULL,
                target TEXT NOT NULL,
                last_action_at REAL NOT NULL,
                action TEXT NOT NULL,
                result TEXT NOT NULL,
                PRIMARY KEY (signal_type, target)
            )
        """)
        conn.commit()
        _state_db_initialized = True
    return conn


def _is_rate_limited(signal_type: str, target: str) -> bool:
    conn = None
    try:
        conn = _init_state_db()
        row = conn.execute(
            "SELECT last_action_at FROM heal_history WHERE signal_type=? AND target=?",
            (signal_type, target),
        ).fetchone()
        if not row:
            return False
        return (time.time() - row[0]) < RATE_LIMIT_SECONDS
    except Exception as e:
        log.warning("rate_limit_check failed, failing closed: %s", e)
        return True
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _record_action(signal_type: str, target: str, action: str, result: str):
    conn = None
    try:
        conn = _init_state_db()
        conn.execute(
            "INSERT OR REPLACE INTO heal_history VALUES (?, ?, ?, ?, ?)",
            (signal_type, target, time.time(), action, result),
        )
        conn.commit()
    except Exception as e:
        log.warning("record_action failed: %s", e)
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _log_event(signal: HealingSignal, action: str, result: str, before: dict | None = None, after: dict | None = None):
    try:
        HEAL_LOG.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "signal": signal.to_dict(),
            "action": action,
            "result": result,
            "before": before,
            "after": after,
        }
        with HEAL_LOG.open("a") as f:
            f.write(json.dumps(entry, default=str) + "\n")
    except Exception:
        pass


# ── Healers ─────────────────────────────────────────────────

def heal_eval_regression(signal: HealingSignal) -> dict:
    """Triggered when eval_gate detects score drop > threshold.

    Strategy: always trigger reindex (the main corrective lever). If cross-encoder
    is off, ALSO emit a suggestion to enable it — but don't gate reindex on it.
    """
    suggestions: list[str] = []
    try:
        from config import BRAIN_CROSS_ENCODER_ENABLED
    except ImportError:
        BRAIN_CROSS_ENCODER_ENABLED = False
    if not BRAIN_CROSS_ENCODER_ENABLED:
        suggestions.append("enable_cross_encoder")

    try:
        import urllib.request
        secret = load_bearer_secret()
        req = urllib.request.Request(
            "http://127.0.0.1:8791/jobs/reindex",
            method="POST",
            headers={"Authorization": f"Bearer {secret}"},
        )
        urllib.request.urlopen(req, timeout=10)
        result = "triggered"
        if suggestions:
            result += f" (also: {','.join(suggestions)})"
        return {"action": "trigger_reindex", "result": result}
    except Exception as e:
        return {"action": "trigger_reindex", "result": f"failed: {e}"}


def heal_slo_latency(signal: HealingSignal) -> dict:
    """Triggered on repeated SLO latency breaches.

    Escalation: breach count determines action.
    """
    breach_count = signal.context.get("breach_count", 1) if signal.context else 1

    if breach_count < 5:
        return {"action": "log_only", "result": "breach count too low"}

    if breach_count < 8:
        # Vacuum embed cache + prewarm
        try:
            import urllib.request
            secret = load_bearer_secret()
            req = urllib.request.Request(
                "http://127.0.0.1:8791/jobs/log_rotation",
                method="POST",
                headers={"Authorization": f"Bearer {secret}"},
            )
            urllib.request.urlopen(req, timeout=5)
            return {"action": "vacuum_embed_cache", "result": "triggered"}
        except Exception as e:
            return {"action": "vacuum_embed_cache", "result": f"failed: {e}"}

    if breach_count < 10:
        try:
            import urllib.request
            secret = load_bearer_secret()
            req = urllib.request.Request(
                "http://127.0.0.1:8791/jobs/reindex",
                method="POST",
                headers={"Authorization": f"Bearer {secret}"},
            )
            urllib.request.urlopen(req, timeout=5)
            return {"action": "trigger_reindex", "result": "triggered"}
        except Exception as e:
            return {"action": "trigger_reindex", "result": f"failed: {e}"}

    # 10+ breaches — give up, escalate
    return {"action": "escalate_to_human", "result": "needs manual intervention"}


def heal_memory_growth(signal: HealingSignal) -> dict:
    """Triggered when a collection grows >20% WoW."""
    try:
        import urllib.request
        secret = load_bearer_secret()
        # Trigger consolidation + dedup
        for job in ("memory_consolidation",):
            req = urllib.request.Request(
                f"http://127.0.0.1:8791/jobs/{job}",
                method="POST",
                headers={"Authorization": f"Bearer {secret}"},
            )
            urllib.request.urlopen(req, timeout=5)
        return {"action": "consolidate_and_dedup", "result": "triggered"}
    except Exception as e:
        return {"action": "consolidate_and_dedup", "result": f"failed: {e}"}


def heal_embed_cache(signal: HealingSignal) -> dict:
    """Triggered when embed cache bloats past threshold."""
    return {"action": "vacuum_scheduled", "result": "will run in next log_rotation"}


def heal_collection_fragmentation(signal: HealingSignal) -> dict:
    """Triggered on HNSW index fragmentation."""
    return {"action": "suggest_hnsw_rebuild", "result": "suggested"}


HEALERS: dict[str, Callable[[HealingSignal], dict]] = {
    "eval_regression": heal_eval_regression,
    "slo_latency_breach": heal_slo_latency,
    "content_quality_breach": heal_eval_regression,  # same action: trigger reindex
    "memory_growth": heal_memory_growth,
    "embed_cache_bloat": heal_embed_cache,
    "collection_fragmentation": heal_collection_fragmentation,
}


SIGNAL_TO_KIND = {
    "eval_regression": "heal.reindex",
    "slo_latency_breach": "heal.vacuum_embed_cache",  # vacuum first, escalate later
    "content_quality_breach": "heal.reindex",
    "memory_growth": "heal.memory_consolidation",
    "embed_cache_bloat": "heal.vacuum_embed_cache",
    "collection_fragmentation": "heal.reindex",
}


def _heal_kind(signal: HealingSignal) -> str:
    return SIGNAL_TO_KIND.get(signal.signal_type, f"heal.{signal.signal_type}")


def dispatch(signal: HealingSignal) -> dict:
    """Route a healing signal to its handler. Returns action taken."""
    # Always log the signal
    if not BRAIN_AUTO_HEAL_ENABLED:
        _log_event(signal, "disabled", "BRAIN_AUTO_HEAL_ENABLED=false")
        return {"action": "disabled", "result": "flag off"}

    # Staged rollout: if BRAIN_AUTO_HEAL_SOURCES is set, gate by source.
    if BRAIN_AUTO_HEAL_SOURCES and signal.source not in BRAIN_AUTO_HEAL_SOURCES:
        _log_event(signal, "source_filtered", f"source={signal.source} not in whitelist {sorted(BRAIN_AUTO_HEAL_SOURCES)}")
        return {"action": "source_filtered", "result": f"source {signal.source} not in whitelist"}

    # Phase 5: route through autonomy gate before existing rate-limit + healer call.
    kind = _heal_kind(signal)
    try:
        from autonomy import authorize as _autonomy_authorize

        gate = _autonomy_authorize(
            kind,
            context={"signal_type": signal.signal_type, "target": signal.target, "source": signal.source},
        )
        if not gate.allowed:
            _log_event(signal, "autonomy_blocked", f"kind={kind} reason={gate.reason}")
            return {"action": "autonomy_blocked", "result": gate.reason}
        if gate.requires_ack:
            # L1 → propose only: log + return without firing healer
            _log_event(signal, "autonomy_propose", f"kind={kind} L1 propose-only")
            return {"action": "autonomy_propose", "result": "L1 propose-only — see audit_log"}
    except Exception as exc:
        # Gate failure should not block heal — fall through with warning
        _log_event(signal, "autonomy_error", str(exc)[:200])

    if _is_rate_limited(signal.signal_type, signal.target):
        _log_event(signal, "rate_limited", f"within {RATE_LIMIT_SECONDS}s window")
        return {"action": "rate_limited", "result": "skipped"}

    healer = HEALERS.get(signal.signal_type)
    if not healer:
        _log_event(signal, "no_healer", f"no handler for {signal.signal_type}")
        return {"action": "no_healer", "result": "unknown signal type"}

    try:
        result = healer(signal)
        _record_action(signal.signal_type, signal.target, result.get("action", "?"), result.get("result", "?"))
        _log_event(signal, result.get("action", "?"), result.get("result", "?"))
        # Phase 5: feed outcome to breaker so repeated failures open the CB
        try:
            from breakers import record_result as _record_breaker

            ok = "failed" not in (result.get("result", "") or "").lower()
            _record_breaker(kind, ok=ok, error=result.get("result", "") if not ok else "")
        except Exception:
            pass
        return result
    except Exception as e:
        _log_event(signal, "error", str(e))
        try:
            from breakers import record_result as _record_breaker

            _record_breaker(kind, ok=False, error=str(e)[:200])
        except Exception:
            pass
        return {"action": "error", "result": str(e)}


def recent_actions(limit: int = 20) -> list[dict]:
    """Return recent healing actions for /brain/self-heal/status endpoint."""
    if not HEAL_LOG.exists():
        return []
    try:
        with HEAL_LOG.open() as f:
            lines = f.readlines()[-limit:]
        return [json.loads(l) for l in lines if l.strip()]
    except Exception:
        return []
