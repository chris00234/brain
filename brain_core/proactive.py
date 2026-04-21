"""brain_core/proactive.py — proactive insight generator.

Runs every 6 hours via APScheduler. Scans multiple data signals for things
Chris should know about, generates alerts injected into agent boot context,
and dispatches urgent items to Jenna via Telegram.

All checks are lightweight — no LLM calls except for urgent Telegram alerts.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from cli_llm import dispatch as _dispatch  # migrated 2026-04-17
from vector_store import get_vector_store
from search_unified import search_all

try:
    from config import BRAIN_DIR, BRAIN_LOGS_DIR
except ImportError:
    BRAIN_DIR = Path("/Users/chrischo/server/brain")
    BRAIN_LOGS_DIR = BRAIN_DIR / "logs"

log = logging.getLogger("brain.proactive")

INSIGHTS_FILE = BRAIN_LOGS_DIR / "proactive_insights.jsonl"
PST = timezone(timedelta(hours=-8))
PDT = timezone(timedelta(hours=-7))
INSIGHT_TTL_HOURS = 48


# ── Data structure ───────────────────────────────────────


@dataclass
class ProactiveInsight:
    id: str
    category: str  # schedule | contradiction | trend | health | pattern
    severity: str  # info | warning | urgent
    summary: str
    detail: str
    evidence: list
    generated_at: str
    expires_at: str
    acted_on: bool = False

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> ProactiveInsight:
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


def _make_id(*parts: str) -> str:
    """Deterministic SHA-256 from concatenated parts — for dedup."""
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _expires_iso(hours: int = INSIGHT_TTL_HOURS) -> str:
    return (datetime.now(UTC) + timedelta(hours=hours)).isoformat(timespec="seconds")


def _pst_hour(ts_iso: str) -> int | None:
    """Extract Pacific time hour from ISO timestamp (handles DST). Returns None on parse failure."""
    try:
        dt = datetime.fromisoformat(ts_iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        # Use zoneinfo for proper DST handling (PST vs PDT)
        try:
            from zoneinfo import ZoneInfo

            pacific = ZoneInfo("America/Los_Angeles")
        except ImportError:
            # Fallback: approximate with PDT (UTC-7) — off by 1h in winter
            pacific = PDT
        return dt.astimezone(pacific).hour
    except Exception:
        return None


# ── JSONL persistence ────────────────────────────────────


def _read_insights() -> list[ProactiveInsight]:
    if not INSIGHTS_FILE.exists():
        return []
    insights = []
    for line in INSIGHTS_FILE.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            insights.append(ProactiveInsight.from_dict(json.loads(line)))
        except Exception:
            continue
    return insights


def _write_insights(insights: list[ProactiveInsight]) -> None:
    INSIGHTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    lock_path = INSIGHTS_FILE.with_suffix(".lock")
    lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
    fcntl.flock(lock_fd, fcntl.LOCK_EX)
    try:
        tmp = INSIGHTS_FILE.with_suffix(".tmp")
        with tmp.open("w") as f:
            for ins in insights:
                f.write(json.dumps(ins.to_dict()) + "\n")
        tmp.replace(INSIGHTS_FILE)
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


# ── Check functions ──────────────────────────────────────


def check_schedule_gaps() -> list[ProactiveInsight]:
    """Search calendar for events in next 48h, flag those with no prep materials."""
    insights = []
    try:
        points = get_vector_store().get("personal", limit=100, with_payload=True, with_documents=True)
        if not points:
            return []
        docs = [p.document or "" for p in points]
        metas = [p.payload for p in points]

        now = datetime.now(UTC)
        cutoff = now + timedelta(hours=48)

        for doc, meta in zip(docs, metas, strict=False):
            if not doc or not meta:
                continue
            # Look for date signals in metadata or document text
            event_date_str = meta.get("date") or meta.get("start_date") or meta.get("timestamp") or ""
            event_title = meta.get("title") or meta.get("subject") or doc[:80]

            # Try to parse date to check if within 48h window
            if event_date_str:
                try:
                    event_dt = datetime.fromisoformat(event_date_str)
                    if event_dt.tzinfo is None:
                        event_dt = event_dt.replace(tzinfo=UTC)
                    if not (now <= event_dt <= cutoff):
                        continue
                except Exception:
                    continue
            else:
                continue

            # Search for related prep materials in canonical + experience
            results = search_all(
                event_title,
                limit=3,
                sources=["rag", "canonical"],
                collections=["canonical", "experience", "knowledge"],
            )
            result_list = (
                results.get("results", [])
                if isinstance(results, dict)
                else results
                if isinstance(results, list)
                else []
            )
            has_prep = any(r.get("score", 0) > 50 for r in result_list)

            if not has_prep:
                insight_id = _make_id("schedule", event_title, event_date_str)
                insights.append(
                    ProactiveInsight(
                        id=insight_id,
                        category="schedule",
                        severity="warning",
                        summary=f"No prep materials found for: {event_title[:60]}",
                        detail=(
                            f"Event '{event_title}' is scheduled for {event_date_str}. "
                            f"No relevant canonical or experience documents were found. "
                            f"Consider preparing notes or reviewing related materials."
                        ),
                        evidence=[{"collection": "calendar", "event": event_title, "date": event_date_str}],
                        generated_at=_now_iso(),
                        expires_at=_expires_iso(48),
                    )
                )
    except Exception as e:
        log.warning("check_schedule_gaps failed: %s", e)
    return insights


def check_decision_contradictions() -> list[ProactiveInsight]:
    """Read semantic_contradictions for unresolved items, check recent memories for new ones."""
    insights = []
    try:
        points = get_vector_store().get(
            "semantic_contradictions",
            filter={"review_state": {"$eq": "pending"}},
            limit=50,
            with_payload=True,
            with_documents=True,
        )
        docs = [p.document or "" for p in points]
        metas = [p.payload for p in points]

        for doc, meta in zip(docs, metas, strict=False):
            if not doc:
                continue
            c_id = _make_id("contradiction", doc[:100])
            created = meta.get("created_at", "") if meta else ""
            insights.append(
                ProactiveInsight(
                    id=c_id,
                    category="contradiction",
                    severity="warning",
                    summary=f"Unresolved contradiction: {doc[:60]}",
                    detail=(
                        f"A knowledge contradiction has been pending review since {created or 'unknown'}. "
                        f"Content: {doc[:200]}"
                    ),
                    evidence=[{"collection": "semantic_contradictions", "created_at": created}],
                    generated_at=_now_iso(),
                    expires_at=_expires_iso(48),
                )
            )
    except Exception as e:
        log.warning("check_decision_contradictions failed: %s", e)

    # Also check recent semantic_memory for potential new contradictions
    try:
        cutoff_iso = (datetime.now(UTC) - timedelta(hours=24)).isoformat(timespec="seconds")
        # ChromaDB 1.4.1 rejects string operands in $gte/$lt — fetch unfiltered
        # and post-filter by created_at in Python. We cap at 500 to avoid
        # scanning unbounded collections.
        points = get_vector_store().get(
            "semantic_memory", limit=500, with_payload=True, with_documents=True
        )
        all_docs = [p.document or "" for p in points]
        all_metas = [p.payload for p in points]

        # Flag any that have contradiction_score in metadata AND are recent
        for doc, meta in zip(all_docs, all_metas, strict=False):
            if not doc or not meta:
                continue
            created_at = (meta.get("created_at") or "").replace("+00:00", "Z")
            if created_at and created_at < cutoff_iso.replace("+00:00", "Z"):
                continue  # older than cutoff
            c_score = meta.get("contradiction_score")
            if c_score and float(c_score) > 0.7:
                c_id = _make_id("new_contradiction", doc[:100])
                insights.append(
                    ProactiveInsight(
                        id=c_id,
                        category="contradiction",
                        severity="info",
                        summary=f"Potential new contradiction detected: {doc[:60]}",
                        detail=(
                            f"A recent memory entry has a high contradiction score ({c_score}). "
                            f"Content: {doc[:200]}"
                        ),
                        evidence=[{"collection": "semantic_memory", "score": str(c_score)}],
                        generated_at=_now_iso(),
                        expires_at=_expires_iso(24),
                    )
                )
    except Exception as e:
        log.warning("check_decision_contradictions (recent memories) failed: %s", e)

    return insights


def check_eval_trends() -> list[ProactiveInsight]:
    """Read last 7 eval-history entries, flag 3+ consecutive accuracy drops.

    Reads the stable-track history (eval-history-stable.jsonl), which is the
    canonical regression gate after the two-track migration (2026-04-13). The
    legacy single-file history (eval-history.jsonl) has been frozen since the
    two-track move so it must not be used here — it would fire forever on the
    last drop chain it captured.
    """
    insights = []
    eval_path = BRAIN_LOGS_DIR / "eval-history-stable.jsonl"
    if not eval_path.exists():
        eval_path = BRAIN_LOGS_DIR / "eval-history.jsonl"  # legacy fallback
    if not eval_path.exists():
        return []

    try:
        lines = eval_path.read_text().strip().splitlines()
        entries = []
        for line in lines[-7:]:
            try:
                entries.append(json.loads(line))
            except Exception:
                continue

        if len(entries) < 3:
            return []

        # Look for consecutive accuracy drops
        accuracies = []
        for entry in entries:
            acc = entry.get("accuracy") or entry.get("score") or entry.get("pass_rate")
            if acc is not None:
                accuracies.append(float(acc))

        if len(accuracies) < 3:
            return []

        consecutive_drops = 0
        drop_start_idx = 0
        for i in range(1, len(accuracies)):
            if accuracies[i] < accuracies[i - 1]:
                if consecutive_drops == 0:
                    drop_start_idx = i - 1
                consecutive_drops += 1
            else:
                consecutive_drops = 0

            if consecutive_drops >= 3:
                drop_start = accuracies[drop_start_idx]
                drop_end = accuracies[i]
                insight_id = _make_id("eval_trend", str(i), str(drop_end))
                insights.append(
                    ProactiveInsight(
                        id=insight_id,
                        category="trend",
                        severity="warning",
                        summary=f"RAG eval accuracy declining: {drop_start:.1%} -> {drop_end:.1%}",
                        detail=(
                            f"RAG evaluation accuracy has dropped for {consecutive_drops} consecutive runs, "
                            f"from {drop_start:.1%} to {drop_end:.1%}. "
                            f"Recent scores: {[f'{a:.1%}' for a in accuracies[-4:]]}. "
                            f"Check indexing pipeline and embedding quality."
                        ),
                        evidence=[{"file": str(eval_path), "scores": accuracies[-4:]}],
                        generated_at=_now_iso(),
                        expires_at=_expires_iso(48),
                    )
                )
                break
    except Exception as e:
        log.warning("check_eval_trends failed: %s", e)
    return insights


def check_work_patterns() -> list[ProactiveInsight]:
    """Check experience collection for late-night work clusters (after 1am PST)."""
    insights = []
    try:
        results = search_all(
            "shell_history git_activity commit push",
            limit=50,
            sources=["rag"],
            collections=["experience"],
        )
        result_list = (
            results.get("results", [])
            if isinstance(results, dict)
            else results
            if isinstance(results, list)
            else []
        )
        if not result_list:
            return []

        # Extract timestamps and check for late-night activity
        late_night_dates = set()
        for result in result_list:
            ts = (result.get("metadata") or {}).get("timestamp") or result.get("timestamp", "")
            if not ts:
                continue
            hour = _pst_hour(ts)
            if hour is not None and 1 <= hour <= 5:
                # Extract date portion for counting unique late nights
                try:
                    dt = datetime.fromisoformat(ts)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=UTC)
                    # Only count last 7 days
                    if (datetime.now(UTC) - dt).days <= 7:
                        late_night_dates.add(dt.date().isoformat())
                except Exception:
                    continue

        if len(late_night_dates) >= 3:
            insight_id = _make_id("late_nights", str(len(late_night_dates)), _now_iso()[:10])
            insights.append(
                ProactiveInsight(
                    id=insight_id,
                    category="pattern",
                    severity="warning",
                    summary=f"{len(late_night_dates)} late nights (1-5am PST) in the past week",
                    detail=(
                        f"Detected {len(late_night_dates)} nights with activity between 1-5am PST "
                        f"in the past 7 days: {sorted(late_night_dates)}. "
                        f"This pattern may indicate sleep debt accumulation."
                    ),
                    evidence=[{"dates": sorted(late_night_dates), "source": "experience"}],
                    generated_at=_now_iso(),
                    expires_at=_expires_iso(48),
                )
            )
    except Exception as e:
        log.warning("check_work_patterns failed: %s", e)
    return insights


def check_scheduler_health() -> list[ProactiveInsight]:
    """Flag jobs that failed 2+ times recently."""
    insights = []
    try:
        from scheduler import brain_scheduler

        jobs = brain_scheduler.list_jobs()

        for job in jobs:
            last_run = job.get("last_run")
            if not last_run:
                continue
            error = last_run.get("error")
            if not error:
                continue

            # Check run history for repeated failures
            name = job["name"]
            history = brain_scheduler.get_history(name)
            recent_failures = sum(1 for entry in history[-5:] if entry.get("error"))

            if recent_failures >= 2:
                severity = "urgent" if recent_failures >= 4 else "warning"
                insight_id = _make_id("job_fail", name, str(recent_failures))
                insights.append(
                    ProactiveInsight(
                        id=insight_id,
                        category="health",
                        severity=severity,
                        summary=f"Job '{name}' failed {recent_failures} of last {min(5, len(history))} runs",
                        detail=(
                            f"Scheduled job '{name}' ({job.get('description', '')}) has failed "
                            f"{recent_failures} times recently. Last error: {str(error)[:200]}. "
                            f"Next scheduled run: {job.get('next_run', 'unknown')}."
                        ),
                        evidence=[
                            {
                                "job": name,
                                "failures": recent_failures,
                                "last_error": str(error)[:200],
                            }
                        ],
                        generated_at=_now_iso(),
                        expires_at=_expires_iso(24),
                    )
                )
    except Exception as e:
        log.warning("check_scheduler_health failed: %s", e)
    return insights


def check_behavior_patterns() -> list[ProactiveInsight]:
    """Analyze outcome history for agent performance trends and recurring overrides."""
    insights = []
    try:
        from task_queue import task_queue

        accuracy = task_queue.get_domain_accuracy()

        for domain, stats in accuracy.items():
            total = stats.get("total_recommendations", 0)
            if total < 5:
                continue
            acc = stats.get("accuracy", 0)
            overrides = stats.get("override_count", 0)
            override_rate = overrides / total if total else 0

            if acc < 0.6:
                insight_id = _make_id("low_accuracy", domain, str(total))
                insights.append(
                    ProactiveInsight(
                        id=insight_id,
                        category="pattern",
                        severity="warning",
                        summary=f"Low accuracy in '{domain}': {acc:.0%} ({total} tasks)",
                        detail=(
                            f"Brain recommendations for '{domain}' have {acc:.0%} accuracy "
                            f"over {total} tasks. Override rate: {override_rate:.0%}. "
                            f"Consider reviewing delegation strategy for this domain."
                        ),
                        evidence=[
                            {"domain": domain, "accuracy": acc, "total": total, "overrides": overrides}
                        ],
                        generated_at=_now_iso(),
                        expires_at=_expires_iso(48),
                    )
                )

            if override_rate > 0.3 and overrides >= 3:
                insight_id = _make_id("high_override", domain, str(overrides))
                insights.append(
                    ProactiveInsight(
                        id=insight_id,
                        category="pattern",
                        severity="info",
                        summary=f"High override rate in '{domain}': {override_rate:.0%}",
                        detail=(
                            f"Chris has overridden {overrides} of {total} brain recommendations in '{domain}'. "
                            f"Confidence model may need recalibration for this domain."
                        ),
                        evidence=[{"domain": domain, "override_rate": override_rate, "overrides": overrides}],
                        generated_at=_now_iso(),
                        expires_at=_expires_iso(72),
                    )
                )
    except Exception as e:
        log.warning("check_behavior_patterns failed: %s", e)
    return insights


# ── Main entry points ────────────────────────────────────


def run_proactive_check() -> list[ProactiveInsight]:
    """Run all checks in parallel, deduplicate, persist, and alert on urgent items."""
    checks = [
        check_schedule_gaps,
        check_decision_contradictions,
        check_eval_trends,
        check_work_patterns,
        check_scheduler_health,
        check_behavior_patterns,
    ]

    new_insights: list[ProactiveInsight] = []

    with ThreadPoolExecutor(max_workers=len(checks)) as pool:
        futures = {pool.submit(fn): fn.__name__ for fn in checks}
        for future in as_completed(futures):
            name = futures[future]
            try:
                results = future.result(timeout=30)
                new_insights.extend(results)
                if results:
                    log.info("%s produced %d insights", name, len(results))
            except Exception as e:
                log.warning("check %s failed: %s", name, e)

    # Merge with existing insights
    existing = _read_insights()
    now = datetime.now(UTC)

    # Expire old insights
    existing = [ins for ins in existing if not _is_expired(ins, now)]

    # Build dedup index from existing
    seen_ids = {ins.id for ins in existing}

    # Add new insights that aren't duplicates
    for ins in new_insights:
        if ins.id not in seen_ids:
            existing.append(ins)
            seen_ids.add(ins.id)

    # Persist
    _write_insights(existing)

    # Deduplicate new_insights by id (two checks may produce the same insight)
    unique_new: list[ProactiveInsight] = []
    unique_new_ids: set[str] = set()
    for ins in new_insights:
        if ins.id not in unique_new_ids:
            unique_new.append(ins)
            unique_new_ids.add(ins.id)

    # Extract entities from new insights into graph (reinforces knowledge connections)
    try:
        from entity_graph import extract_and_store_entities

        for ins in unique_new[:3]:
            text = f"{ins.summary} {ins.detail[:300]}"
            if len(text) > 50:
                extract_and_store_entities(text, f"proactive_{ins.id}")
    except Exception:
        pass

    # Fire action triggers for new insights (when autopilot is on)
    try:
        from action_triggers import check_proactive_triggers

        triggered_tasks = check_proactive_triggers(unique_new)
        if triggered_tasks:
            log.info("action triggers created %d tasks from insights", len(triggered_tasks))
    except Exception as e:
        log.warning("action trigger evaluation failed: %s", e)

    # 2026-04-17 (C wiring): feed insights into attention_queue so /brain/attention
    # returns the top-priority one (urgency × novelty × valence). Previously the
    # attention_queue table was infrastructure with no feeder — now proactive
    # insights populate it automatically. Habituation (shown_count) prevents
    # the same insight from dominating across multiple /brain/attention calls.
    try:
        from attention import enqueue as _attn_enqueue

        for ins in unique_new:
            # TTL: honor existing expires_at if set, else default 48h
            try:
                ttl_hours = max(
                    1,
                    int(
                        (datetime.fromisoformat(ins.expires_at.replace("Z", "+00:00")) - now).total_seconds()
                        / 3600
                    ),
                )
            except Exception:
                ttl_hours = 48
            related_atoms = []
            for ev in (ins.evidence or [])[:3]:
                if isinstance(ev, dict):
                    aid = ev.get("id") or ev.get("atom_id") or ev.get("chroma_id")
                    if aid and isinstance(aid, str):
                        related_atoms.append(aid)
            _attn_enqueue(
                insight_id=ins.id,
                category=ins.category,
                severity=ins.severity,
                summary=ins.summary,
                detail=ins.detail[:2000],
                related_atoms=related_atoms,
                ttl_hours=ttl_hours,
            )
    except Exception as e:
        log.debug("attention enqueue failed: %s", e)

    # Dispatch urgent items to Jenna via Telegram
    acted_on_ids = {ins.id for ins in existing if ins.acted_on}
    urgent = [ins for ins in unique_new if ins.severity == "urgent" and ins.id not in acted_on_ids]
    if urgent:
        _dispatch_urgent(urgent)

    log.info(
        "proactive check complete: %d total insights, %d new, %d urgent",
        len(existing),
        len(unique_new),
        len(urgent),
    )
    return existing


def _is_expired(ins: ProactiveInsight, now: datetime) -> bool:
    try:
        expires = datetime.fromisoformat(ins.expires_at)
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=UTC)
        return now > expires
    except Exception:
        return True


def _dispatch_urgent(insights: list[ProactiveInsight]) -> None:
    """Send urgent insights to Jenna for Telegram relay."""
    summary_lines = []
    for ins in insights:
        summary_lines.append(f"[{ins.category.upper()}] {ins.summary}")
        summary_lines.append(f"  {ins.detail}")
        summary_lines.append("")

    message = (
        "PROACTIVE ALERT — the following urgent items need Chris's attention:\n\n"
        + "\n".join(summary_lines)
        + "\nPlease relay these to Chris via Telegram with appropriate context."
    )

    try:
        result = _dispatch(
            agent="jenna",
            message=message,
            thinking="low",
            timeout=60,
            degraded_placeholder="[Proactive alert dispatch failed — check logs]",
            backlog_kind="telegram",
            backlog_payload={
                "body": message,
                "severity": "urgent",
                "source": "proactive_urgent",
            },
        )
        if not result.ok:
            log.warning("urgent dispatch failed: %s", result.error)
    except Exception as e:
        log.warning("urgent dispatch error: %s", e)


def get_current_insights(
    max_age_hours: int = 24,
    severity: str | None = None,
) -> list[ProactiveInsight]:
    """Read from JSONL store, filter by severity and age. Used by boot_context and API."""
    now = datetime.now(UTC)
    cutoff = now - timedelta(hours=max_age_hours)
    insights = []

    for ins in _read_insights():
        if ins.acted_on:
            continue
        if _is_expired(ins, now):
            continue

        # Age filter
        try:
            generated = datetime.fromisoformat(ins.generated_at)
            if generated.tzinfo is None:
                generated = generated.replace(tzinfo=UTC)
            if generated < cutoff:
                continue
        except Exception:
            continue

        if severity and ins.severity != severity:
            continue
        insights.append(ins)

    return insights


def dismiss_insight(insight_id: str) -> bool:
    """Mark insight as acted_on in JSONL store. Returns True if found and updated."""
    lock_path = INSIGHTS_FILE.with_suffix(".lock")
    lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
    fcntl.flock(lock_fd, fcntl.LOCK_EX)
    try:
        insights = _read_insights()
        found = False
        for ins in insights:
            if ins.id == insight_id:
                ins.acted_on = True
                found = True
                break
        if found:
            tmp = INSIGHTS_FILE.with_suffix(".tmp")
            with tmp.open("w") as f:
                for ins in insights:
                    f.write(json.dumps(ins.to_dict()) + "\n")
            tmp.replace(INSIGHTS_FILE)
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)
    return found


if __name__ == "__main__":
    results = run_proactive_check()
    print(
        json.dumps(
            {
                "insights": len(results),
                "urgent": sum(1 for r in results if r.severity == "urgent"),
            }
        )
    )
