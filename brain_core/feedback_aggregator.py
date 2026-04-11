"""brain_core/feedback_aggregator.py — Weekly feedback aggregation.

Reads search-feedback.jsonl, computes per-source usefulness rate, and
logs suggestions for SOURCE_TRUST weight adjustments. Does NOT auto-adjust
SOURCE_TRUST, but does update per-agent preference weights in autonomy.db
via agent_preferences.record_feedback() / recompute_weights().
"""
import json
import sys
from pathlib import Path
from collections import defaultdict
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(Path(__file__).parent))

FEEDBACK_LOG = Path("/Users/chrischo/server/brain/logs/search-feedback.jsonl")
REPORT_FILE = Path("/Users/chrischo/server/brain/logs/feedback-aggregate.json")
WATERMARK_FILE = Path("/Users/chrischo/server/brain/logs/feedback-aggregate.watermark")


def _load_watermark() -> str:
    """Return the ISO timestamp of the last processed feedback entry, or empty."""
    try:
        return WATERMARK_FILE.read_text().strip()
    except Exception:
        return ""


def _save_watermark(ts: str) -> None:
    try:
        WATERMARK_FILE.parent.mkdir(parents=True, exist_ok=True)
        WATERMARK_FILE.write_text(ts)
    except Exception:
        pass


def aggregate_feedback(days: int = 7) -> dict:
    """Compute per-source usefulness rate from last N days of feedback.

    Also updates per-agent source preference weights via agent_preferences.
    """
    if not FEEDBACK_LOG.exists():
        return {"error": "no feedback log", "per_source": {}}

    try:
        from agent_preferences import record_feedback, recompute_weights
        _prefs_available = True
    except Exception:
        _prefs_available = False

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    per_source: dict[str, dict] = defaultdict(lambda: {"useful": 0, "total": 0})
    per_agent_source: dict[tuple[str, str], dict] = defaultdict(lambda: {"useful": 0, "total": 0})
    # Only call record_feedback() on entries newer than the last run — otherwise
    # every weekly run re-processes the whole window and double-counts every
    # event into agent_preferences.
    high_watermark = _load_watermark()
    last_seen_ts = high_watermark

    with FEEDBACK_LOG.open() as f:
        for line in f:
            try:
                entry = json.loads(line)
                ts_raw = entry.get("timestamp", "")
                if not ts_raw:
                    continue
                ts = datetime.fromisoformat(ts_raw.rstrip("Zz"))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                if ts < cutoff:
                    continue
                source = entry.get("source") or _infer_source(entry.get("result_id", ""))
                useful = bool(entry.get("useful"))
                agent = entry.get("agent") or "system"  # forward-compat: pre-2026-04 entries lack this

                per_source[source]["total"] += 1
                if useful:
                    per_source[source]["useful"] += 1

                if source:
                    per_agent_source[(agent, source)]["total"] += 1
                    if useful:
                        per_agent_source[(agent, source)]["useful"] += 1
                    # record_feedback mutates SQLite — skip entries already
                    # processed in a prior run.
                    if _prefs_available and (not high_watermark or ts_raw > high_watermark):
                        try:
                            record_feedback(agent, source, useful)
                        except Exception:
                            pass
                if ts_raw > last_seen_ts:
                    last_seen_ts = ts_raw
            except Exception:
                continue

    if last_seen_ts and last_seen_ts != high_watermark:
        _save_watermark(last_seen_ts)

    # Compute rates
    result = {}
    for source, stats in per_source.items():
        rate = stats["useful"] / stats["total"] if stats["total"] > 0 else 0
        result[source] = {
            "useful": stats["useful"],
            "total": stats["total"],
            "rate": round(rate, 3),
        }

    per_agent_result = {}
    for (agent, source), stats in per_agent_source.items():
        rate = stats["useful"] / stats["total"] if stats["total"] > 0 else 0
        per_agent_result.setdefault(agent, {})[source] = {
            "useful": stats["useful"],
            "total": stats["total"],
            "rate": round(rate, 3),
        }

    # Generate suggestions
    suggestions = []
    for source, s in result.items():
        if s["total"] < 5:
            continue  # not enough data
        if s["rate"] < 0.3:
            suggestions.append(f"Source '{source}' has low usefulness ({s['rate']*100:.0f}%) — consider reducing SOURCE_TRUST weight")
        elif s["rate"] > 0.8:
            suggestions.append(f"Source '{source}' has high usefulness ({s['rate']*100:.0f}%) — consider increasing SOURCE_TRUST weight")

    # Recompute per-agent weights from the freshly recorded feedback
    prefs_updated = {}
    if _prefs_available:
        try:
            prefs_updated = recompute_weights()
        except Exception as e:
            prefs_updated = {"error": str(e)}

    report = {
        "days_analyzed": days,
        "timestamp": datetime.now().isoformat(),
        "per_source": result,
        "per_agent_source": per_agent_result,
        "agent_prefs_updated": prefs_updated,
        "suggestions": suggestions,
    }

    REPORT_FILE.parent.mkdir(parents=True, exist_ok=True)
    REPORT_FILE.write_text(json.dumps(report, indent=2))
    return report


def _infer_source(result_id: str) -> str:
    """Infer source type from result ID prefix."""
    if ":" in result_id:
        return result_id.split(":", 1)[0]
    return "unknown"


if __name__ == "__main__":
    import sys
    result = aggregate_feedback()
    print(json.dumps(result, indent=2))
    sys.exit(0)
