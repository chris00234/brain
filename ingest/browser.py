#!/opt/homebrew/bin/python3
"""Browser history ingest — Sage filters intentional research from passive scrolling.

Reads sqlite history from any installed browsers (Chrome, Safari, Brave, Arc),
groups by day, dispatches batches to Sage agent for "intentional research vs
passive browsing" classification, writes only kept pages as schema-compliant
raw records.

Owned by Sage. LLM-using (Sage classification). Runs nightly at 02:30 PST.

Usage:
  ingest_browser.py [--dry-run] [--days-back N] [--browsers chrome,safari]
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ── Config ──────────────────────────────────────────────
INBOX_DIR = Path("/Users/chrischo/server/knowledge/raw/inbox")
STATE_FILE = Path("/Users/chrischo/.openclaw/workspace-sage/.brain_state/browser_ingest_state.json")
FAILURE_LOG = Path("/Users/chrischo/.openclaw/workspace-sage/logs/browser-ingest-failures.jsonl")

OPENCLAW_BIN = "/Users/chrischo/.local/bin/openclaw"
AGENT = "sage"
DISPATCH_TIMEOUT = 240
BATCH_SIZE = 50

CHROME_EPOCH_OFFSET = 11644473600  # seconds between 1601-01-01 and 1970-01-01
SAFARI_EPOCH_OFFSET = 978307200    # seconds between 1970-01-01 and 2001-01-01

_DEDUP_TOKEN_RE = re.compile(r'[a-z0-9_\-]{3,}')
_dedup_token_cache: dict[str, set[str]] = {}


def _is_near_duplicate(content: str, inbox_dir: Path, window: int = 50, threshold: float = 0.7) -> bool:
    """Check if content is a near-duplicate of recent raw records."""
    tokens = set(_DEDUP_TOKEN_RE.findall(content.lower()))
    if len(tokens) < 5:
        return False
    try:
        recent = sorted(inbox_dir.glob("raw_*.json"), key=lambda f: f.stat().st_mtime, reverse=True)[:window]
    except Exception:
        return False
    for f in recent:
        fkey = str(f)
        if fkey in _dedup_token_cache:
            et = _dedup_token_cache[fkey]
        else:
            try:
                existing = json.loads(f.read_text())
                et = set(_DEDUP_TOKEN_RE.findall(existing.get("content", "").lower()))
                _dedup_token_cache[fkey] = et
            except Exception:
                continue
        if et and len(tokens & et) / max(len(tokens | et), 1) > threshold:
            return True
    return False

BROWSER_PATHS = {
    "chrome": Path("/Users/chrischo/Library/Application Support/Google/Chrome/Default/History"),
    "brave":  Path("/Users/chrischo/Library/Application Support/BraveSoftware/Brave-Browser/Default/History"),
    "arc":    Path("/Users/chrischo/Library/Application Support/Arc/User Data/Default/History"),
    "safari": Path("/Users/chrischo/Library/Safari/History.db"),
}

# Domain-level noise filter — drop these before LLM ever sees them
NOISE_DOMAINS = {
    "google.com", "google.co.kr", "duckduckgo.com", "bing.com",
    "youtube.com", "youtu.be",
    "twitter.com", "x.com", "facebook.com", "instagram.com", "tiktok.com",
    "reddit.com",
    "github.com",  # too noisy — code browsing isn't research
    "stackoverflow.com",  # ditto
    "localhost", "127.0.0.1",
    "chrome://newtab", "about:blank",
}


def log_failure(reason: str) -> None:
    try:
        FAILURE_LOG.parent.mkdir(parents=True, exist_ok=True)
        with FAILURE_LOG.open("a") as f:
            f.write(json.dumps({"timestamp": datetime.now().isoformat(), "reason": reason[:500]}) + "\n")
    except Exception:
        pass


try:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "brain_core"))
    from safe_state import load_state as _safe_load, save_state as _safe_save
    def load_state() -> dict:
        return _safe_load(STATE_FILE) or {}
    def save_state(state: dict) -> None:
        _safe_save(STATE_FILE, state)
except ImportError:
    def load_state() -> dict:
        if STATE_FILE.exists():
            try:
                return json.loads(STATE_FILE.read_text())
            except Exception:
                return {}
        return {}
    def save_state(state: dict) -> None:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(state))


def domain_of(url: str) -> str:
    m = re.match(r"https?://([^/]+)", url or "")
    if m:
        return m.group(1).lower().lstrip("www.")
    return ""


def is_noise_domain(url: str) -> bool:
    dom = domain_of(url)
    if not dom:
        return True
    if dom in NOISE_DOMAINS:
        return True
    # Catch subdomains of noise domains
    parts = dom.split(".")
    for i in range(len(parts) - 1):
        candidate = ".".join(parts[i:])
        if candidate in NOISE_DOMAINS:
            return True
    return False


def read_chrome(db_path: Path, since_unix: int) -> list[dict]:
    """Read Chrome/Brave/Arc history. Returns visits since since_unix."""
    if not db_path.exists():
        return []
    with tempfile.TemporaryDirectory() as tmp:
        tmp_db = Path(tmp) / "History"
        try:
            shutil.copy2(db_path, tmp_db)
        except Exception as e:
            log_failure(f"copy chrome history: {e}")
            return []
        try:
            conn = sqlite3.connect(f"file:{tmp_db}?mode=ro", uri=True)
            cur = conn.cursor()
            # Chrome time = microseconds since 1601-01-01
            chrome_since = (since_unix + CHROME_EPOCH_OFFSET) * 1_000_000
            cur.execute("""
                SELECT urls.url, urls.title, visits.visit_time, urls.visit_count
                FROM visits
                JOIN urls ON visits.url = urls.id
                WHERE visits.visit_time > ?
                ORDER BY visits.visit_time
            """, (chrome_since,))
            rows = cur.fetchall()
            conn.close()
        except Exception as e:
            log_failure(f"chrome sqlite: {e}")
            return []
    out = []
    for url, title, vt, vcount in rows:
        unix_ts = (vt / 1_000_000) - CHROME_EPOCH_OFFSET
        if is_noise_domain(url):
            continue
        out.append({
            "url": url,
            "title": (title or "")[:200],
            "domain": domain_of(url),
            "ts": int(unix_ts),
            "iso": datetime.fromtimestamp(unix_ts, tz=timezone.utc).isoformat().replace("+00:00", "Z"),
            "visit_count": vcount or 0,
        })
    return out


def read_safari(db_path: Path, since_unix: int) -> list[dict]:
    if not db_path.exists():
        return []
    with tempfile.TemporaryDirectory() as tmp:
        tmp_db = Path(tmp) / "History.db"
        try:
            shutil.copy2(db_path, tmp_db)
        except Exception as e:
            log_failure(f"copy safari history: {e}")
            return []
        try:
            conn = sqlite3.connect(f"file:{tmp_db}?mode=ro", uri=True)
            cur = conn.cursor()
            safari_since = since_unix - SAFARI_EPOCH_OFFSET
            cur.execute("""
                SELECT history_items.url, history_visits.title, history_visits.visit_time, history_items.visit_count
                FROM history_visits
                JOIN history_items ON history_visits.history_item = history_items.id
                WHERE history_visits.visit_time > ?
                ORDER BY history_visits.visit_time
            """, (safari_since,))
            rows = cur.fetchall()
            conn.close()
        except Exception as e:
            log_failure(f"safari sqlite: {e}")
            return []
    out = []
    for url, title, vt, vcount in rows:
        unix_ts = vt + SAFARI_EPOCH_OFFSET
        if is_noise_domain(url):
            continue
        out.append({
            "url": url,
            "title": (title or "")[:200],
            "domain": domain_of(url),
            "ts": int(unix_ts),
            "iso": datetime.fromtimestamp(unix_ts, tz=timezone.utc).isoformat().replace("+00:00", "Z"),
            "visit_count": vcount or 0,
        })
    return out


READERS = {
    "chrome": read_chrome,
    "brave":  read_chrome,
    "arc":    read_chrome,
    "safari": read_safari,
}


def collect_visits(browsers: list[str], since_unix: int) -> list[dict]:
    all_visits: list[dict] = []
    seen_keys: set[tuple[str, int]] = set()
    for browser in browsers:
        path = BROWSER_PATHS.get(browser)
        if not path or not path.exists():
            continue
        reader = READERS.get(browser)
        if not reader:
            continue
        visits = reader(path, since_unix)
        for v in visits:
            v["browser"] = browser
            key = (v["url"], v["ts"] // 60)  # dedup per minute across browsers
            if key in seen_keys:
                continue
            seen_keys.add(key)
            all_visits.append(v)
    return all_visits


def build_classification_prompt(batch: list[dict]) -> str:
    lines = []
    lines.append("You are Sage, Chris's research and synthesis agent.")
    lines.append("Classify these browser visits: was this INTENTIONAL research/learning, or PASSIVE browsing?")
    lines.append("")
    lines.append("Keep examples: tutorial reading, library docs, technical articles, academic papers,")
    lines.append("comparison shopping with intent, reference lookups Chris will revisit, longform analysis.")
    lines.append("Drop examples: news scrolling, idle browsing, social media, generic search results,")
    lines.append("repeat visits to homepage, autocomplete dead-ends.")
    lines.append("")
    for i, v in enumerate(batch, 1):
        lines.append(f"[{i}] {v['domain']} — {v['title']}")
        lines.append(f"    {v['url'][:120]}")
    lines.append("")
    lines.append("OUTPUT FORMAT (return ONLY valid JSON):")
    lines.append('{"keep": [<int indices to keep>], "summaries": {"<index>": "<1 sentence why>"}}')
    lines.append("")
    lines.append("STRICT: only the JSON object. Empty keep list is allowed.")
    return "\n".join(lines)


def dispatch_classification(prompt: str) -> dict | None:
    cmd = [
        OPENCLAW_BIN, "agent", "--agent", AGENT,
        "--message", prompt, "--json",
        "--timeout", str(DISPATCH_TIMEOUT), "--thinking", "off",
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=DISPATCH_TIMEOUT + 30)
    except subprocess.TimeoutExpired:
        log_failure("openclaw agent timed out")
        sys.stderr.write(f"DISPATCH_FAIL agent={AGENT} reason=timeout\n")
        return None
    if r.returncode != 0:
        log_failure(f"openclaw agent failed: {r.stderr[:300]}")
        sys.stderr.write(f"DISPATCH_FAIL agent={AGENT} stderr={r.stderr[:200]}\n")
        return None
    try:
        response = json.loads(r.stdout)
        text = response.get("result", {}).get("payloads", [])[0].get("text", "")
        text = re.sub(r"^```(?:json)?\s*", "", text.strip())
        text = re.sub(r"\s*```$", "", text)
        return json.loads(text)
    except (json.JSONDecodeError, KeyError, IndexError) as e:
        log_failure(f"could not parse Sage reply: {e}")
        return None


def write_kept_visit(visit: dict, summary: str) -> Path | None:
    text = (
        f"Browser visit ({visit['browser']})\n"
        f"URL: {visit['url']}\n"
        f"Title: {visit['title']}\n"
        f"Domain: {visit['domain']}\n"
        f"Visited: {visit['iso']}\n"
        f"\n"
        f"Summary (Sage): {summary}"
    )
    digest = hashlib.sha256(f"{visit['url']}:{visit['ts']}".encode()).hexdigest()
    date_part = visit["iso"][:10].replace("-", "_")
    rec_id = f"raw_browser_{date_part}_{digest[:8]}"
    record = {
        "id": rec_id,
        "timestamp": visit["iso"],
        "source_type": "browser",
        "source_ref": f"browser:{visit['browser']}:{visit['url'][:80]}",
        "actor": "chris",
        "visibility": "private",
        "scrub_status": "scrubbed",
        "content": text,
        "attachments": [],
        "entities": ["Chris", visit["domain"]],
        "hash": f"sha256:{digest}",
    }
    INBOX_DIR.mkdir(parents=True, exist_ok=True)
    out = INBOX_DIR / f"{rec_id}.json"
    if out.exists():
        return None
    if _is_near_duplicate(text, INBOX_DIR):
        return None
    out.write_text(json.dumps(record, ensure_ascii=False, indent=2))
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="Browser history intelligent ingest")
    parser.add_argument("--days-back", type=int, default=2, help="Lookback window in days")
    parser.add_argument("--browsers", default="chrome,safari,brave,arc",
                        help="Comma-separated browsers (auto-detected if installed)")
    parser.add_argument("--dry-run", action="store_true", help="Print what would be classified")
    args = parser.parse_args()

    browsers = [b.strip() for b in args.browsers.split(",") if b.strip()]

    state = load_state()
    last_ts = state.get("last_ts", int((datetime.now() - timedelta(days=args.days_back)).timestamp()))
    cutoff = max(last_ts, int((datetime.now() - timedelta(days=args.days_back)).timestamp()))

    print(f"Browser ingest — browsers={browsers}, since {datetime.fromtimestamp(cutoff)}")

    print("[1/4] Reading browser histories + domain pre-filter...")
    visits = collect_visits(browsers, cutoff)
    print(f"  {len(visits)} visits after domain filter")
    if not visits:
        print("Nothing to classify.")
        return

    if args.dry_run:
        print(f"\n[DRY RUN] sample visits ({len(visits)} total):")
        for v in visits[:15]:
            print(f"  {v['domain']:30s}  {v['title'][:60]}")
        if len(visits) > 15:
            print(f"  ... ({len(visits) - 15} more)")
        return

    print("[2/4] Dispatching to Sage in batches...")
    kept_count = 0
    any_dispatch_ok = False  # any batch successfully processed (even if kept=0)
    for i in range(0, len(visits), BATCH_SIZE):
        batch = visits[i:i + BATCH_SIZE]
        prompt = build_classification_prompt(batch)
        result = dispatch_classification(prompt)
        if result is None:
            import time
            time.sleep(10)
            result = dispatch_classification(prompt)
        if not result:
            print(f"  Batch {i // BATCH_SIZE + 1}: FAILED")
            continue
        any_dispatch_ok = True
        keep_indices = set(result.get("keep", []))
        summaries = result.get("summaries", {})
        print(f"  Batch {i // BATCH_SIZE + 1}: keeping {len(keep_indices)}/{len(batch)}")
        for idx, v in enumerate(batch, 1):
            if idx in keep_indices:
                summary = summaries.get(str(idx)) or summaries.get(idx) or "(no summary)"
                if write_kept_visit(v, summary):
                    kept_count += 1

    print(f"[3/4] Wrote {kept_count} kept records")

    if kept_count == 0 and len(visits) > 0:
        sys.stderr.write(f"WARN adapter=browser visits={len(visits)} kept=0 — dispatch may have failed\n")

    # Advance watermark whenever dispatch succeeded — "processed" is separate
    # from "kept". Without this, consistently-noisy days replay the same
    # visits every run and burn LLM calls.
    if any_dispatch_ok:
        state["last_ts"] = max(v["ts"] for v in visits) if visits else cutoff
        save_state(state)
        print(f"[4/4] State updated: last_ts={state['last_ts']} (kept={kept_count})")
    else:
        print(f"[4/4] Watermark NOT advanced — all dispatches failed (will retry next run)")


if __name__ == "__main__":
    main()
