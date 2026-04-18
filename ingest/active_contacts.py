#!/opt/homebrew/bin/python3
"""Active contacts ingest — Jenna enriches contact profiles from iMessage activity.

Cross-references iMessage chat.db handles with AddressBook to build
relationship cards for people Chris actually communicates with.
Dispatches to Jenna for enrichment, writes to raw/inbox/.

Since AddressBook-v22.abcddb may be sparse (iCloud contacts not synced
to local SQLite), this script also works with bare handle IDs from chat.db.

Pipeline: chat.db handles → AddressBook match → Jenna enrichment → raw/inbox

Usage:
  active_contacts.py [--dry-run] [--days-back 180]
"""

import argparse
import hashlib
import json
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

# ── Config ──────────────────────────────────────────────
MESSAGES_DB = Path.home() / "Library/Messages/chat.db"
ADDRESSBOOK_DB = Path.home() / "Library/Application Support/AddressBook/AddressBook-v22.abcddb"
INBOX_DIR = Path("/Users/chrischo/server/knowledge/raw/inbox")
STATE_FILE = Path("/Users/chrischo/server/brain/logs/active-contacts-state.json")
FAILURE_LOG = Path("/Users/chrischo/server/brain/logs/active-contacts-failures.jsonl")

OPENCLAW_BIN = "/Users/chrischo/.local/bin/openclaw"
DISPATCH_AGENT = "jenna"
DISPATCH_TIMEOUT = 240
BATCH_SIZE = 30

APPLE_EPOCH = 978307200

# Skip spam/service patterns
SKIP_PATTERNS = [
    re.compile(r"^\d{3,6}$"),  # shortcodes
    re.compile(r"^\+?1\d{10}$"),  # only if no messages beyond notifications
    re.compile(r"noreply|no-reply", re.I),
]


# ── State ───────────────────────────────────────────────
try:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "brain_core"))
    from safe_state import load_state as _safe_load
    from safe_state import save_state as _safe_save

    def load_state():
        return _safe_load(STATE_FILE)

    def save_state(state):
        _safe_save(STATE_FILE, state)
except ImportError:

    def load_state():
        if STATE_FILE.exists():
            try:
                return json.loads(STATE_FILE.read_text())
            except Exception:
                return {}
        return {}

    def save_state(state):
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(state, indent=2))


def log_failure(reason: str) -> None:
    try:
        FAILURE_LOG.parent.mkdir(parents=True, exist_ok=True)
        with FAILURE_LOG.open("a") as f:
            f.write(json.dumps({"timestamp": datetime.now().isoformat(), "reason": reason[:500]}) + "\n")
    except Exception:
        pass


def frequency_bucket(msg_count: int, days: int) -> str:
    rate = msg_count / max(days, 1)
    if rate >= 1:
        return "daily"
    if rate >= 0.14:
        return "weekly"
    if rate >= 0.03:
        return "monthly"
    return "rare"


# ── Data Collection ─────────────────────────────────────
def collect_active_handles(days_back: int) -> list[dict]:
    """Extract active contacts from iMessage chat.db."""
    if not MESSAGES_DB.exists():
        log_failure("chat.db not found")
        return []

    with tempfile.TemporaryDirectory() as tmp:
        tmp_db = Path(tmp) / "chat.db"
        try:
            shutil.copy2(MESSAGES_DB, tmp_db)
            for sfx in ("-shm", "-wal"):
                src = MESSAGES_DB.parent / f"chat.db{sfx}"
                if src.exists():
                    shutil.copy2(src, Path(tmp) / f"chat.db{sfx}")
        except Exception as e:
            log_failure(f"cannot copy chat.db: {e}")
            return []

        cutoff_unix = (datetime.now() - timedelta(days=days_back)).timestamp()
        cutoff_apple = int((cutoff_unix - APPLE_EPOCH) * 1_000_000_000)

        try:
            conn = sqlite3.connect(f"file:{tmp_db}?mode=ro", uri=True)
            cur = conn.cursor()

            # Get per-handle stats
            cur.execute(
                """
                SELECT
                    handle.id AS handle_id,
                    chat.display_name AS chat_name,
                    chat.chat_identifier,
                    COUNT(*) AS msg_count,
                    SUM(CASE WHEN message.is_from_me = 1 THEN 1 ELSE 0 END) AS from_me,
                    MAX(message.date) AS last_date,
                    MIN(message.date) AS first_date
                FROM message
                LEFT JOIN handle ON message.handle_id = handle.ROWID
                LEFT JOIN chat_message_join ON chat_message_join.message_id = message.ROWID
                LEFT JOIN chat ON chat.ROWID = chat_message_join.chat_id
                WHERE message.date > ?
                  AND message.text IS NOT NULL
                  AND message.text != ''
                  AND handle.id IS NOT NULL
                GROUP BY handle.id
                ORDER BY msg_count DESC
            """,
                (cutoff_apple,),
            )
            rows = cur.fetchall()
            conn.close()
        except Exception as e:
            log_failure(f"chat.db query failed: {e}")
            return []

    contacts = []
    for handle_id, chat_name, chat_ident, msg_count, from_me, last_date_apple, first_date_apple in rows:
        # Skip spam/shortcodes
        if any(pat.match(str(handle_id)) for pat in SKIP_PATTERNS):
            continue

        # Convert dates
        last_unix = (last_date_apple / 1_000_000_000) + APPLE_EPOCH
        first_unix = (first_date_apple / 1_000_000_000) + APPLE_EPOCH
        last_date = datetime.fromtimestamp(last_unix).strftime("%Y-%m-%d")
        span_days = max(1, (last_unix - first_unix) / 86400)

        contact_name = chat_name or chat_ident or handle_id
        bucket = frequency_bucket(msg_count, int(span_days))

        contacts.append(
            {
                "handle": handle_id,
                "name": contact_name,
                "msg_count": msg_count,
                "from_me": from_me,
                "from_them": msg_count - from_me,
                "last_contact": last_date,
                "frequency": bucket,
            }
        )

    return contacts


# ── AddressBook Enrichment ──────────────────────────────
def enrich_from_addressbook(contacts: list[dict]) -> list[dict]:
    """Try to match handles against local AddressBook for names/orgs."""
    if not ADDRESSBOOK_DB.exists():
        return contacts

    with tempfile.TemporaryDirectory() as tmp:
        tmp_db = Path(tmp) / "ab.db"
        try:
            shutil.copy2(ADDRESSBOOK_DB, tmp_db)
        except Exception:
            return contacts

        try:
            conn = sqlite3.connect(f"file:{tmp_db}?mode=ro", uri=True)
            cur = conn.cursor()

            # Build handle → name mapping from phone numbers and emails
            handle_map = {}
            cur.execute("""
                SELECT p.ZFULLNUMBER, r.ZFIRSTNAME, r.ZLASTNAME, r.ZORGANIZATION
                FROM ZABCDPHONENUMBER p
                JOIN ZABCDRECORD r ON r.Z_PK = p.ZOWNER
            """)
            for phone, first, last, org in cur.fetchall():
                if phone:
                    name = f'{first or ""} {last or ""}'.strip()
                    handle_map[phone] = {"name": name, "org": org or ""}

            cur.execute("""
                SELECT e.ZADDRESS, r.ZFIRSTNAME, r.ZLASTNAME, r.ZORGANIZATION
                FROM ZABCDEMAILADDRESS e
                JOIN ZABCDRECORD r ON r.Z_PK = e.ZOWNER
            """)
            for email, first, last, org in cur.fetchall():
                if email:
                    name = f'{first or ""} {last or ""}'.strip()
                    handle_map[email.lower()] = {"name": name, "org": org or ""}

            conn.close()
        except Exception:
            return contacts

    # Match
    for contact in contacts:
        handle = contact["handle"]
        match = handle_map.get(handle) or handle_map.get(handle.lower())
        if match:
            if match["name"]:
                contact["name"] = match["name"]
            if match["org"]:
                contact["org"] = match["org"]

    return contacts


# ── Agent Dispatch ──────────────────────────────────────
def build_enrichment_prompt(contacts: list[dict]) -> str:
    lines = [
        f"You are Jenna. Review these {len(contacts)} active contacts from Chris's iMessage.",
        "For each contact, provide a brief relationship context based on the available data.",
        "",
        "Skip contacts that appear to be service/notification numbers.",
        "",
    ]
    for i, c in enumerate(contacts, 1):
        org = c.get("org", "")
        lines.append(
            f'[{i}] {c["name"]} (handle: {c["handle"]})'
            + (f" at {org}" if org else "")
            + f' — {c["msg_count"]} messages ({c["from_me"]} sent, {c["from_them"]} received), '
            + f'{c["frequency"]} contact, last: {c["last_contact"]}'
        )

    lines.append("")
    lines.append("OUTPUT FORMAT (return ONLY valid JSON):")
    lines.append(
        '{"keep": [{"index": <int>, "summary": "<1 sentence relationship context>", "signal_score": <1-10>}], "skip_reason": "<why others were skipped>"}'
    )
    lines.append("")
    lines.append("STRICT: only the JSON object.")
    return "\n".join(lines)


def dispatch_enrichment(prompt: str) -> dict | None:
    cmd = [
        OPENCLAW_BIN,
        "agent",
        "--agent",
        DISPATCH_AGENT,
        "--message",
        prompt,
        "--json",
        "--timeout",
        str(DISPATCH_TIMEOUT),
        "--thinking",
        "off",
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=DISPATCH_TIMEOUT + 30)
    except subprocess.TimeoutExpired:
        log_failure("jenna dispatch timed out")
        return None
    if r.returncode != 0:
        log_failure(f"jenna dispatch failed: {r.stderr[:300]}")
        return None
    try:
        response = json.loads(r.stdout)
        text = response.get("result", {}).get("payloads", [])[0].get("text", "")
        text = re.sub(r"^```(?:json)?\s*", "", text.strip())
        text = re.sub(r"\s*```$", "", text)
        return json.loads(text)
    except (json.JSONDecodeError, KeyError, IndexError) as e:
        log_failure(f"could not parse Jenna reply: {e}")
        return None


# ── Record Writing ──────────────────────────────────────
def write_record(contact: dict, item: dict) -> Path | None:
    summary = item.get("summary", "")
    score = item.get("signal_score", 0)

    if score < 5 or not summary:  # lower threshold for contacts
        return None

    content = (
        f'Active contact: {contact["name"]}\n'
        f'Handle: {contact["handle"]}\n'
        + (f'Organization: {contact.get("org", "")}\n' if contact.get("org") else "")
        + f'Frequency: {contact["frequency"]}\n'
        f'Messages: {contact["msg_count"]} ({contact["from_me"]} sent, {contact["from_them"]} received)\n'
        f'Last contact: {contact["last_contact"]}\n\n'
        f'{summary}'
    )

    handle_hash = hashlib.md5(contact["handle"].encode()).hexdigest()[:12]
    # Include a year-month bucket so each month produces a fresh record instead
    # of the first-ever write permanently winning. Contact relationships evolve
    # (new context, frequency, nickname); stale records would never update.
    month_bucket = datetime.now().strftime("%Y%m")
    rec_id = f"raw_contact_{handle_hash}_{month_bucket}"

    record = {
        "id": rec_id,
        "timestamp": datetime.now().isoformat(),
        "source_type": "active_contact",
        "source_ref": f'contact:{contact["handle"]}',
        "actor": "chris",
        "visibility": "private",
        "scrub_status": "scrubbed",
        "content": content,
        "attachments": [],
        "entities": ["Chris", contact["name"]],
        "hash": f"sha256:{hashlib.sha256(content.encode()).hexdigest()}",
    }

    INBOX_DIR.mkdir(parents=True, exist_ok=True)
    out = INBOX_DIR / f"{rec_id}.json"
    if out.exists():
        return None
    out.write_text(json.dumps(record, ensure_ascii=False, indent=2))
    return out


# ── Main ────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(description="Active contacts ingest via Jenna enrichment")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--days-back", type=int, default=180)
    args = parser.parse_args()

    print(f"Active contacts ingest — last {args.days_back} days")

    contacts = collect_active_handles(args.days_back)
    print(f"  {len(contacts)} active handles found")

    if not contacts:
        print("No active contacts to process.")
        return

    contacts = enrich_from_addressbook(contacts)

    if args.dry_run:
        for c in contacts:
            org = c.get("org", "")
            print(
                f'  {c["name"]:30s} {c["frequency"]:8s} {c["msg_count"]:3d} msgs  last: {c["last_contact"]}'
                + (f"  ({org})" if org else "")
            )
        print(f"\nDone — {len(contacts)} contacts, 0 records written (dry run)")
        return

    # Dispatch in batches
    total_written = 0
    total_kept = 0
    for i in range(0, len(contacts), BATCH_SIZE):
        batch = contacts[i : i + BATCH_SIZE]
        prompt = build_enrichment_prompt(batch)
        result = dispatch_enrichment(prompt)
        if result is None:
            import time

            time.sleep(10)
            result = dispatch_enrichment(prompt)
        if result:
            kept = result.get("keep", [])
            total_kept += len(kept)
            print(f"  Batch {i // BATCH_SIZE + 1}: Jenna kept {len(kept)}/{len(batch)} contacts")
            for item in kept:
                idx = item.get("index", 0) - 1
                if 0 <= idx < len(batch):
                    path = write_record(batch[idx], item)
                    if path:
                        total_written += 1
        else:
            log_failure(f"batch {i // BATCH_SIZE + 1} dispatch failed")
            print(f"  Batch {i // BATCH_SIZE + 1}: DISPATCH FAILED")

    print(f"  Total kept: {total_kept}/{len(contacts)}")

    state = load_state()
    state["last_run"] = datetime.now().isoformat()
    state["contacts_processed"] = len(contacts)
    save_state(state)

    print(f"\nDone — {len(contacts)} contacts processed, {total_written} records written")


if __name__ == "__main__":
    main()
