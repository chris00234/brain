#!/opt/homebrew/bin/python3
"""Temporal expression parser for the brain's recall layer.

Used by `search_unified.py`, `search.py`, and `search_memory.py` to translate
human-friendly date expressions into ISO datetime ranges that ChromaDB can
filter on via `where` clauses.

Supported forms:
  ISO              "2026-04-07", "2026-04-07T15:30:00"
  Shorthand        "1d", "7d", "2w", "1m", "1y"          (N days/weeks/months/years ago)
  Named single     "today", "yesterday", "tomorrow"
  Named ago        "5 days ago", "2 weeks ago", "1 month ago"
  Named weekday    "monday", "last monday", "last tuesday", ...
  Named period     "this week", "last week", "this month", "last month",
                   "this year", "last year"

Returns: a single datetime, OR a (start, end) tuple for period expressions.

The CLI surface is intentionally tiny — anything not parseable returns None
and the caller falls back to no filter rather than crashing.
"""

import re
from datetime import UTC, datetime, timedelta

# ── Constants ────────────────────────────────────────────
WEEKDAYS = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
    "mon": 0,
    "tue": 1,
    "wed": 2,
    "thu": 3,
    "fri": 4,
    "sat": 5,
    "sun": 6,
}

UNIT_DAYS = {
    "d": 1,
    "day": 1,
    "days": 1,
    "w": 7,
    "wk": 7,
    "week": 7,
    "weeks": 7,
    "m": 30,
    "mo": 30,
    "month": 30,
    "months": 30,
    "y": 365,
    "yr": 365,
    "year": 365,
    "years": 365,
}


# ── Public API ───────────────────────────────────────────
def parse(expression: str, now: datetime | None = None) -> datetime | tuple[datetime, datetime] | None:
    """Parse a temporal expression. Returns datetime, (start, end), or None."""
    if expression is None:
        return None
    s = expression.strip().lower()
    if not s:
        return None
    if now is None:
        now = datetime.now(UTC)

    # ISO date / datetime
    iso = _try_iso(s)
    if iso is not None:
        return iso

    # Shorthand: "7d", "2w", "1m", "1y"
    m = re.fullmatch(r"(\d+)\s*([a-z]+)", s)
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        if unit in UNIT_DAYS:
            return now - timedelta(days=n * UNIT_DAYS[unit])

    # "N days ago", "N weeks ago"
    m = re.fullmatch(r"(\d+)\s+([a-z]+)\s+ago", s)
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        if unit in UNIT_DAYS:
            return now - timedelta(days=n * UNIT_DAYS[unit])

    # Single named days
    if s == "now":
        return now
    if s == "today":
        return _start_of_day(now)
    if s == "yesterday":
        return _start_of_day(now - timedelta(days=1))
    if s == "tomorrow":
        return _start_of_day(now + timedelta(days=1))

    # Period ranges
    if s == "this week":
        start = _start_of_day(now - timedelta(days=now.weekday()))
        return (start, start + timedelta(days=7))
    if s == "last week":
        start = _start_of_day(now - timedelta(days=now.weekday() + 7))
        return (start, start + timedelta(days=7))
    if s == "this month":
        start = _start_of_day(now.replace(day=1))
        # next month start
        if now.month == 12:
            end = start.replace(year=start.year + 1, month=1)
        else:
            end = start.replace(month=start.month + 1)
        return (start, end)
    if s == "last month":
        first_this = now.replace(day=1)
        if first_this.month == 1:
            start = first_this.replace(year=first_this.year - 1, month=12, day=1)
        else:
            start = first_this.replace(month=first_this.month - 1, day=1)
        return (_start_of_day(start), _start_of_day(first_this))
    if s == "this year":
        start = _start_of_day(now.replace(month=1, day=1))
        return (start, start.replace(year=start.year + 1))
    if s == "last year":
        start = _start_of_day(now.replace(year=now.year - 1, month=1, day=1))
        return (start, start.replace(year=start.year + 1))

    # Bare weekday → most recent occurrence (today if today matches, else last week's)
    if s in WEEKDAYS:
        return _last_weekday(now, WEEKDAYS[s])

    # "last <weekday>"
    m = re.fullmatch(r"last\s+([a-z]+)", s)
    if m and m.group(1) in WEEKDAYS:
        return _last_weekday(now, WEEKDAYS[m.group(1)], force_previous=True)

    return None


def parse_range(
    since: str | None, until: str | None, now: datetime | None = None
) -> tuple[datetime | None, datetime | None]:
    """Parse --since/--until pair, normalizing period expressions to (start, end)."""
    if now is None:
        now = datetime.now(UTC)

    start: datetime | None = None
    end: datetime | None = None

    if since:
        result = parse(since, now=now)
        if isinstance(result, tuple):
            start = result[0]
            if end is None:
                end = result[1]
        elif isinstance(result, datetime):
            start = result

    if until:
        result = parse(until, now=now)
        if isinstance(result, tuple):
            end = result[1]
        elif isinstance(result, datetime):
            end = result

    return start, end


def to_chroma_where(start: datetime | None, end: datetime | None, field: str = "created_at") -> dict | None:
    """DEPRECATED after ChromaDB 1.4.1.

    ChromaDB 1.4.1 tightened validate_where to require isinstance(operand, (int, float))
    for $gt/$gte/$lt/$lte. All brain writers store `created_at` / `valid_until` as
    ISO-8601 strings, so passing them as range operands now returns HTTP 400.

    Until we migrate every writer to numeric timestamps, date filtering must happen
    Python-side. This function always returns None — callers should capture the
    (start, end) datetimes and call `filter_by_created_at()` on the result list.
    """
    return None


def filter_by_created_at(
    rows: list,
    start: datetime | None,
    end: datetime | None,
    field: str = "created_at",
    meta_accessor=None,
) -> list:
    """Python-side post-filter for ISO-string datetime metadata fields.

    Args:
        rows: list of items to filter
        start: inclusive lower bound (or None)
        end: exclusive upper bound (or None)
        field: metadata key to read (default "created_at")
        meta_accessor: callable(row) -> metadata dict. Defaults to row itself if it's
                       a dict with the field, else row.get("metadata", {}).

    Rows whose metadata timestamp is missing or unparseable are KEPT (fail-open —
    dropping would silently destroy unsourced data).
    """
    if not start and not end:
        return rows
    start_iso = start.isoformat().replace("+00:00", "Z") if start else None
    end_iso = end.isoformat().replace("+00:00", "Z") if end else None

    def _get_ts(row) -> str:
        if meta_accessor is not None:
            meta = meta_accessor(row) or {}
        elif isinstance(row, dict):
            if field in row and isinstance(row[field], str):
                return row[field]
            meta = row.get("metadata") or {}
        else:
            return ""
        return (meta or {}).get(field, "") or ""

    kept = []
    for row in rows:
        ts = _get_ts(row)
        if not ts:
            kept.append(row)  # fail-open
            continue
        # Normalize comparison format — both sides as Z-suffix strings
        ts_norm = ts.replace("+00:00", "Z")
        if start_iso and ts_norm < start_iso:
            continue
        if end_iso and ts_norm >= end_iso:
            continue
        kept.append(row)
    return kept


# ── Helpers ──────────────────────────────────────────────
def _start_of_day(dt: datetime) -> datetime:
    return dt.replace(hour=0, minute=0, second=0, microsecond=0)


def _try_iso(s: str) -> datetime | None:
    # Strip trailing Z (UTC indicator) before parsing. `parse()` lower-cases
    # the string first, so we must match the lowercase form ("z") as well as
    # the uppercase form for any direct callers.
    s = s.rstrip("Zz")
    # Accept date or datetime — always return UTC-aware
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


def _last_weekday(now: datetime, target_weekday: int, force_previous: bool = False) -> datetime:
    today_weekday = now.weekday()
    if force_previous:
        days_back = (today_weekday - target_weekday) % 7
        if days_back == 0:
            days_back = 7
    else:
        days_back = (today_weekday - target_weekday) % 7
    return _start_of_day(now - timedelta(days=days_back))


# ── CLI smoke test ───────────────────────────────────────
def _smoke() -> None:
    cases = [
        "2026-04-07",
        "2026-04-07T15:30:00",
        "7d",
        "2w",
        "1m",
        "today",
        "yesterday",
        "this week",
        "last week",
        "last month",
        "monday",
        "last tuesday",
        "5 days ago",
        "garbage input",
    ]
    for c in cases:
        result = parse(c)
        print(f"  {c!r:30s} → {result}")


if __name__ == "__main__":
    _smoke()
