#!/opt/homebrew/bin/python3
"""Weekly memory leak detector — spots collections growing abnormally.

Compares per-collection counts against last week's snapshot. If any collection
grew >20% WoW without a corresponding ingest event, emits a healing signal.
"""

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from vector_store import get_vector_store

HISTORY_FILE = Path("/Users/chrischo/server/brain/logs/collection_size_history.jsonl")
GROWTH_THRESHOLD_PCT = 20.0


def get_counts() -> dict[str, int]:
    """Get current doc counts per collection via the vector store."""
    store = get_vector_store()
    counts: dict[str, int] = {}
    for name in store.list_collections():
        try:
            counts[name] = store.count(name)
        except Exception as e:
            print(f"  {name}: count failed: {e}")
            counts[name] = -1
    return counts


def get_last_snapshot() -> dict[str, int] | None:
    if not HISTORY_FILE.exists():
        return None
    try:
        with HISTORY_FILE.open() as f:
            lines = f.readlines()
        if not lines:
            return None
        last = json.loads(lines[-1])
        return last.get("counts", {})
    except Exception:
        return None


HISTORY_MAX_ENTRIES = 104  # ~2 years at weekly cadence


def append_snapshot(counts: dict[str, int]) -> None:
    """Atomically append a snapshot entry, capped at HISTORY_MAX_ENTRIES.

    Writes the new full file to a .tmp sibling and os.replace() it into place,
    so a crash mid-write can't truncate or corrupt the history. Trims old
    entries so the file never exceeds HISTORY_MAX_ENTRIES lines.
    """
    import os

    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "timestamp": datetime.now(UTC).isoformat(),
        "counts": counts,
    }
    existing_lines: list[str] = []
    if HISTORY_FILE.exists():
        try:
            existing_lines = [ln for ln in HISTORY_FILE.read_text().splitlines() if ln.strip()]
        except Exception:
            existing_lines = []
    # Keep the most recent N-1 entries, then append the new one.
    if len(existing_lines) >= HISTORY_MAX_ENTRIES:
        existing_lines = existing_lines[-(HISTORY_MAX_ENTRIES - 1) :]
    tmp = HISTORY_FILE.with_suffix(HISTORY_FILE.suffix + ".tmp")
    with tmp.open("w") as f:
        for ln in existing_lines:
            f.write(ln + "\n")
        f.write(json.dumps(entry) + "\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, HISTORY_FILE)


def detect_leaks() -> dict:
    """Compare current counts to last week's. Return growth report."""
    current = get_counts()
    previous = get_last_snapshot()

    result: dict = {
        "timestamp": datetime.now(UTC).isoformat(),
        "current_counts": current,
        "leaks": [],
        "normal_growth": [],
    }

    if not previous:
        result["status"] = "first_run_baseline"
        append_snapshot(current)
        return result

    for col_name, current_count in current.items():
        if current_count < 0:
            continue
        prev_count = previous.get(col_name, 0)
        if prev_count <= 0:
            # First sighting of this collection — not a leak
            continue

        growth_pct = ((current_count - prev_count) / prev_count) * 100
        entry = {
            "collection": col_name,
            "previous": prev_count,
            "current": current_count,
            "growth_pct": round(growth_pct, 1),
        }

        if growth_pct > GROWTH_THRESHOLD_PCT:
            result["leaks"].append(entry)
        else:
            result["normal_growth"].append(entry)

    # Emit healing signal if leaks detected
    if result["leaks"]:
        try:
            from self_heal import HealingSignal, dispatch

            for leak in result["leaks"]:
                dispatch(
                    HealingSignal(
                        source="memory_leak_detector",
                        signal_type="memory_growth",
                        severity="high" if leak["growth_pct"] > 50 else "medium",
                        metric="weekly_growth_pct",
                        value=leak["growth_pct"],
                        baseline=GROWTH_THRESHOLD_PCT,
                        target=leak["collection"],
                        context=leak,
                    )
                )
        except Exception as e:
            print(f"self_heal dispatch failed: {e}")

    append_snapshot(current)
    return result


if __name__ == "__main__":
    result = detect_leaks()
    print(json.dumps(result, indent=2))
    sys.exit(0)
