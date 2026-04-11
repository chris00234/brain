#!/opt/homebrew/bin/python3
"""Personal data ingestion — Apple Notes, iMessage, Calendar, Reminders → ChromaDB.

Pulls Chris's personal data sources nightly and indexes them so all agents can search
his actual life (notes, conversations, schedule, todos) — not just tech configs.

Each adapter:
  1. Pulls data from its source (osascript or sqlite)
  2. Filters secrets and PII
  3. Chunks appropriately
  4. Embeds via Ollama
  5. Upserts to its dedicated ChromaDB collection
  6. Tracks state in .personal_ingest_state.json

Usage:
  ingest_personal.py
  ingest_personal.py --days 60     # iMessage lookback window
  ingest_personal.py --notes-only  # run only one adapter
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
import time
from datetime import datetime, timedelta
from pathlib import Path

# Reuse helpers from brain_core.indexer (single source of truth).
sys.path.insert(0, "/Users/chrischo/server/brain/brain_core")
from indexer import (
    chroma_api,
    get_embedding,
    ensure_collection,
    filter_secrets,
    _get_collection_id,
    EMBED_MODEL,
)

# ── Config ──────────────────────────────────────────────
STATE_FILE = Path('/Users/chrischo/server/brain/logs/personal-ingest-state.json')
FAILURE_LOG = Path('/Users/chrischo/server/brain/logs/personal-ingest-failures.jsonl')

# Direct SQLite paths — bypass osascript entirely.
# Requires Full Disk Access on the Python binary that runs this script.
MESSAGES_DB = Path.home() / 'Library' / 'Messages' / 'chat.db'
NOTES_DB = Path.home() / 'Library' / 'Group Containers' / 'group.com.apple.notes' / 'NoteStore.sqlite'
CALENDAR_DB = Path.home() / 'Library' / 'Group Containers' / 'group.com.apple.calendar' / 'Calendar.sqlitedb'
REMINDERS_STORES_DIR = Path.home() / 'Library' / 'Group Containers' / 'group.com.apple.reminders' / 'Container_v1' / 'Stores'

APPLE_EPOCH_OFFSET = 978307200  # seconds between unix epoch and 2001-01-01


def _log_failure(adapter: str, error: str) -> None:
    """Append a structured failure record. Critical for catching regressions."""
    try:
        FAILURE_LOG.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "timestamp": datetime.now().isoformat(),
            "adapter": adapter,
            "error": str(error)[:500],
        }
        with FAILURE_LOG.open("a") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass  # never let logging break the run

MIN_NOTE_LEN = 50
MIN_MESSAGE_LEN = 4  # Korean text is info-dense at shorter char counts
MAX_GROUP_PARTICIPANTS = 10
MAX_CHUNK_CHARS = 1500
MAX_REMINDER_LEN = 5  # title-only is fine, just skip empty

# Extra PII regexes for personal data (in addition to indexer.filter_secrets)
PII_PATTERNS = [
    re.compile(r'\b\d{3}-\d{2}-\d{4}\b'),                    # SSN
    re.compile(r'\b\d{4}[\s-]?\d{4}[\s-]?\d{4}[\s-]?\d{4}\b'),  # credit card
    re.compile(r'\b\d{3}\s?\d{6}\b'),                          # bank-ish
]

def filter_pii(text):
    text = filter_secrets(text)
    for pat in PII_PATTERNS:
        if pat.search(text):
            return None  # drop entirely — don't even redact
    return text


# ── State (uses safe_state for file locking) ──────────────
try:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "brain_core"))
    from safe_state import load_state as _safe_load, save_state as _safe_save
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
        STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False))


# ── Custom add_documents (carries personal metadata) ────
def add_personal_documents(collection_name, docs, skip_stale_cleanup=False):
    """Like indexer.add_documents but uses personal metadata fields."""
    if not docs:
        return 0

    col_id = _get_collection_id(collection_name)
    if not col_id:
        print(f"    ERROR: Collection '{collection_name}' not found")
        return 0

    ids = []
    embeddings = []
    documents = []
    metadatas = []

    for i, doc in enumerate(docs):
        content = filter_pii(doc['content'])
        if content is None:
            continue
        if len(content.strip()) < 30:
            continue
        if len(content) > MAX_CHUNK_CHARS:
            content = content[:MAX_CHUNK_CHARS] + "\n[...truncated]"

        source = str(doc.get('source', ''))
        doc_type = doc.get('type', '')
        service = doc.get('service', '')

        # Semantic header for embedding (vector search anchor)
        header_parts = []
        if doc_type == 'note':
            header_parts.append(f"Apple Note titled '{doc.get('title', '')}' in folder '{service}'")
        elif doc_type == 'message':
            header_parts.append(f"iMessage conversation with {service} on {doc.get('date', '')}")
        elif doc_type == 'event':
            header_parts.append(f"Calendar event '{doc.get('title', '')}' in '{service}' on {doc.get('event_date', '')}")
        elif doc_type == 'reminder':
            header_parts.append(f"Reminder in list '{service}' status {doc.get('status', '')}")

        embed_text = ("\n".join(header_parts) + "\n\n" + content) if header_parts else content

        # Stable ID — let updated content overwrite previous version
        id_seed = doc.get('stable_id') or f"{source}:{content}"
        doc_id = f"{collection_name}:{hashlib.md5(id_seed.encode()).hexdigest()}"[:63]

        print(f"    Embedding {i+1}/{len(docs)}: {doc_id[:50]}...", end='\r')
        try:
            emb = get_embedding(embed_text)
        except RuntimeError as e:
            print(f"\n    WARNING: embedding failed for {doc_id[:40]}, skipping: {e}")
            continue
        if not emb:
            continue

        ids.append(doc_id)
        documents.append(content)
        embeddings.append(emb)
        metadatas.append({
            'source': doc.get('source', ''),
            'type': doc_type,
            'service': service,
            'title': doc.get('title', ''),
            'date': doc.get('date', ''),
            'event_date': doc.get('event_date', ''),
            'modified_at': doc.get('modified_at', ''),
            'status': doc.get('status', ''),
            'due': doc.get('due', ''),
            'is_past': str(doc.get('is_past', '')),
            'participant_count': str(doc.get('participant_count', '')),
            'note_id': doc.get('note_id', ''),
            'created_at': datetime.now().isoformat(),
            'embed_model': EMBED_MODEL,
        })

    if not ids:
        print(f"    No valid docs after filtering for '{collection_name}'")
        return 0

    BATCH = 20
    for start in range(0, len(ids), BATCH):
        end = min(start + BATCH, len(ids))
        chroma_api("POST",
            f"/api/v2/tenants/default_tenant/databases/default_database/collections/{col_id}/upsert",
            {
                "ids": ids[start:end],
                "embeddings": embeddings[start:end],
                "documents": documents[start:end],
                "metadatas": metadatas[start:end],
            })
        print(f"    Batch {start//BATCH + 1}: upserted {end - start} chunks       ")

    upserted_ids = set(ids)
    if skip_stale_cleanup:
        print(f"    Total: {len(ids)} chunks in '{collection_name}' (stale cleanup skipped — partial run)")
        return len(ids)
    try:
        resp = chroma_api("POST",
            f"/api/v2/tenants/default_tenant/databases/default_database/collections/{col_id}/get",
            {"limit": 1_000_000, "include": []})
        existing_ids = set(resp.get("ids", []))
        if len(upserted_ids) < len(existing_ids) * 0.5 and len(existing_ids) > 20:
            print(f"    WARNING: Skipping stale cleanup — current set ({len(upserted_ids)}) < 50% of existing ({len(existing_ids)})")
        else:
            stale_ids = list(existing_ids - upserted_ids)
            if stale_ids:
                for s in range(0, len(stale_ids), BATCH):
                    e = min(s + BATCH, len(stale_ids))
                    chroma_api("POST",
                        f"/api/v2/tenants/default_tenant/databases/default_database/collections/{col_id}/delete",
                        {"ids": stale_ids[s:e]})
                print(f"    Cleaned {len(stale_ids)} stale docs from '{collection_name}'")
    except Exception as ex:
        print(f"    WARNING: Stale cleanup failed: {ex}")

    print(f"    Total: {len(ids)} chunks in '{collection_name}'")
    return len(ids)


# ── Adapter 1: Apple Notes (direct SQLite read from NoteStore.sqlite) ──
def _copy_sqlite_live(db_path: Path, tmpdir: str, suffixes: tuple[str, ...] = ('-shm', '-wal')) -> Path:
    """Safely copy a live SQLite file and its WAL/SHM so queries don't block the app."""
    tmp_db = Path(tmpdir) / db_path.name
    shutil.copy2(db_path, tmp_db)
    for sfx in suffixes:
        side = db_path.parent / f"{db_path.name}{sfx}"
        if side.exists():
            shutil.copy2(side, Path(tmpdir) / f"{db_path.name}{sfx}")
    return tmp_db


def _apple_timestamp_to_iso(ts: float | int | None) -> str:
    if not ts or ts <= 0:
        return ""
    try:
        return datetime.fromtimestamp(ts + APPLE_EPOCH_OFFSET).strftime("%Y-%m-%dT%H:%M:%S")
    except (ValueError, OSError):
        return ""


def _extract_text_from_zdata(blob):
    """Extract readable text from gzipped protobuf ZDATA blob."""
    import gzip as _gzip
    if not blob or blob[:2] != b'\x1f\x8b':
        return ""
    try:
        raw = _gzip.decompress(blob)
    except Exception:
        return ""
    segments = re.findall(rb'[\x20-\x7e\xc0-\xff][\x20-\x7e\x80-\xff]{8,}', raw)
    texts = []
    for seg in segments:
        try:
            decoded = seg.decode('utf-8', errors='ignore').strip()
        except Exception:
            continue
        if len(decoded) < 10:
            continue
        if ' ' not in decoded and '\t' not in decoded:
            continue
        if re.match(r'^[A-F0-9\-]{20,}$', decoded):
            continue
        if decoded.startswith('com.apple.'):
            continue
        texts.append(decoded)
    return "\n".join(texts)


def collect_notes():
    """Pull Apple Notes via direct SQLite read with body text extraction."""
    docs = []
    if not NOTES_DB.exists():
        msg = f"{NOTES_DB} not found"
        print(f"    ERROR: {msg}")
        _log_failure("notes", msg)
        return docs

    with tempfile.TemporaryDirectory() as tmpdir:
        try:
            tmp_db = _copy_sqlite_live(NOTES_DB, tmpdir)
        except PermissionError as e:
            msg = f"Cannot copy NoteStore.sqlite: {e}. Grant Full Disk Access to /opt/homebrew/bin/python3 in System Settings → Privacy & Security."
            print(f"    ERROR: {msg}")
            _log_failure("notes", msg)
            return docs
        except Exception as e:
            msg = f"NoteStore copy failed: {e}"
            print(f"    ERROR: {msg}")
            _log_failure("notes", msg)
            return docs

        try:
            conn = sqlite3.connect(f"file:{tmp_db}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute("""
                SELECT
                    n.Z_PK,
                    n.ZTITLE1 AS title,
                    n.ZMODIFICATIONDATE1 AS modified,
                    n.ZCREATIONDATE1 AS created,
                    COALESCE(f.ZTITLE2, 'Notes') AS folder,
                    n.ZSNIPPET AS snippet,
                    nd.ZDATA AS body_blob
                FROM ZICCLOUDSYNCINGOBJECT n
                LEFT JOIN ZICCLOUDSYNCINGOBJECT f ON f.Z_PK = n.ZFOLDER
                LEFT JOIN ZICNOTEDATA nd ON nd.ZNOTE = n.Z_PK
                WHERE n.ZTITLE1 IS NOT NULL
                  AND n.ZMARKEDFORDELETION IS NOT 1
                ORDER BY n.ZMODIFICATIONDATE1 DESC
            """)
            rows = cur.fetchall()
            conn.close()
        except Exception as e:
            msg = f"NoteStore query failed: {e}"
            print(f"    ERROR: {msg}")
            _log_failure("notes", msg)
            return docs

    for row in rows:
        title = (row["title"] or "").strip()
        if not title:
            continue
        folder = (row["folder"] or "Notes").strip()
        modified = _apple_timestamp_to_iso(row["modified"])
        note_pk = row["Z_PK"]
        body = (row["snippet"] or "").strip()
        if not body and row["body_blob"]:
            body = _extract_text_from_zdata(row["body_blob"])
        if body and len(body) < MIN_NOTE_LEN:
            body = ""
        content = f"Note: {title}\nFolder: {folder}\nModified: {modified}"
        if body:
            content += f"\n\n{body}"
        docs.append({
            'content': content,
            'source': f"apple-notes://{note_pk}",
            'type': 'note',
            'service': folder,
            'title': title,
            'note_id': str(note_pk),
            'modified_at': modified,
            'stable_id': f"note:{note_pk}",
        })
    return docs


# ── Adapter 2: iMessage ─────────────────────────────────
def collect_messages(days_back=30):
    """Pull iMessage from chat.db. Group by (contact, day). Skip junk + group chats."""
    if not MESSAGES_DB.exists():
        msg = f"{MESSAGES_DB} not found"
        print(f"    ERROR: {msg}")
        _log_failure("messages", msg)
        return []

    # Copy db to temp because chat.db may be locked by Messages.app
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_db = Path(tmpdir) / 'chat.db'
        try:
            shutil.copy2(MESSAGES_DB, tmp_db)
            for suffix in ('-shm', '-wal'):
                src = MESSAGES_DB.parent / f'chat.db{suffix}'
                if src.exists():
                    shutil.copy2(src, Path(tmpdir) / f'chat.db{suffix}')
        except Exception as e:
            msg = f"Cannot copy chat.db: {e}. Grant Full Disk Access to /bin/bash and /opt/homebrew/bin/python3 in System Settings."
            print(f"    ERROR: {msg}")
            _log_failure("messages", msg)
            return []

        cutoff_unix = (datetime.now() - timedelta(days=days_back)).timestamp()
        cutoff_apple = int((cutoff_unix - APPLE_EPOCH_OFFSET) * 1_000_000_000)

        try:
            conn = sqlite3.connect(f'file:{tmp_db}?mode=ro', uri=True)
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()

            # Pre-compute participant counts per chat
            cur.execute("""
                SELECT chat.ROWID as chat_id,
                       COUNT(DISTINCT chat_handle_join.handle_id) as participant_count
                FROM chat
                LEFT JOIN chat_handle_join ON chat_handle_join.chat_id = chat.ROWID
                GROUP BY chat.ROWID
            """)
            chat_participants = {row['chat_id']: row['participant_count'] for row in cur.fetchall()}

            cur.execute("""
                SELECT
                    message.text,
                    message.date,
                    message.is_from_me,
                    handle.id as handle_id,
                    chat.ROWID as chat_id,
                    chat.display_name as chat_name,
                    chat.chat_identifier
                FROM message
                LEFT JOIN handle ON message.handle_id = handle.ROWID
                LEFT JOIN chat_message_join ON chat_message_join.message_id = message.ROWID
                LEFT JOIN chat ON chat.ROWID = chat_message_join.chat_id
                WHERE message.date > ?
                  AND message.text IS NOT NULL
                  AND message.text != ''
                  AND message.is_system_message = 0
                  AND message.associated_message_type = 0
                ORDER BY message.date ASC
            """, (cutoff_apple,))
            rows = cur.fetchall()
            conn.close()
        except Exception as e:
            msg = f"sqlite query failed: {e}"
            print(f"    ERROR: {msg}")
            _log_failure("messages", msg)
            return []

    # Group by (chat_id, day)
    EMOJI_ONLY_RE = re.compile(r'^[\W_\u2600-\u27BF\U0001F300-\U0001FAFF\s]+$')
    grouped = {}
    chat_meta = {}

    for row in rows:
        text = row['text'] or ''
        text = text.strip()
        if len(text) < MIN_MESSAGE_LEN:
            continue
        if EMOJI_ONLY_RE.match(text):
            continue

        chat_id = row['chat_id']
        if chat_id is None:
            continue

        participants = chat_participants.get(chat_id, 1)
        if participants > MAX_GROUP_PARTICIPANTS:
            continue

        # Convert Apple date to unix
        unix_ts = (row['date'] / 1_000_000_000) + APPLE_EPOCH_OFFSET
        dt = datetime.fromtimestamp(unix_ts)
        day = dt.strftime('%Y-%m-%d')

        contact = row['chat_name'] or row['chat_identifier'] or row['handle_id'] or 'unknown'
        # Skip messages from purely numeric handles that look like spam shortcodes
        if contact and re.match(r'^\d{3,6}$', str(contact)):
            continue

        key = (chat_id, day)
        grouped.setdefault(key, []).append({
            'time': dt.strftime('%H:%M'),
            'is_from_me': row['is_from_me'],
            'text': text,
            'sender': row['handle_id'] or 'me',
        })
        chat_meta[chat_id] = {
            'contact': contact,
            'participants': participants,
        }

    # Build chunks
    docs = []
    for (chat_id, day), msgs in grouped.items():
        meta = chat_meta[chat_id]
        contact = meta['contact']
        lines = [f"Conversation with {contact} on {day}:"]
        for m in msgs:
            speaker = "Me" if m['is_from_me'] else "Them"
            lines.append(f"{speaker}: {m['text']}")
        content = "\n".join(lines)

        # Hard cap — split if too long
        if len(content) > MAX_CHUNK_CHARS:
            # Take last N messages that fit
            head = lines[0] + "\n"
            body_lines = lines[1:]
            current = head
            chunk_parts = []
            for ln in body_lines:
                if len(current) + len(ln) + 1 > MAX_CHUNK_CHARS:
                    chunk_parts.append(current)
                    current = head + ln + "\n"
                else:
                    current += ln + "\n"
            if current.strip() != head.strip():
                chunk_parts.append(current)
            for idx, part in enumerate(chunk_parts):
                docs.append({
                    'content': part,
                    'source': f"imessage://{chat_id}/{day}/{idx}",
                    'type': 'message',
                    'service': contact,
                    'date': day,
                    'participant_count': meta['participants'],
                    'stable_id': f"msg:{chat_id}:{day}:{idx}",
                })
        else:
            docs.append({
                'content': content,
                'source': f"imessage://{chat_id}/{day}",
                'type': 'message',
                'service': contact,
                'date': day,
                'participant_count': meta['participants'],
                'stable_id': f"msg:{chat_id}:{day}",
            })
    return docs


# ── Adapter 3: Calendar (direct SQLite read from Calendar.sqlitedb) ──
def collect_calendar(days_back=30, days_forward=60):
    """Pull Calendar events from Calendar.sqlitedb (no osascript, no Calendar.app)."""
    docs = []
    if not CALENDAR_DB.exists():
        msg = f"{CALENDAR_DB} not found"
        print(f"    ERROR: {msg}")
        _log_failure("calendar", msg)
        return docs

    now = datetime.now()
    cutoff_start = (now - timedelta(days=days_back)).timestamp() - APPLE_EPOCH_OFFSET
    cutoff_end = (now + timedelta(days=days_forward)).timestamp() - APPLE_EPOCH_OFFSET

    with tempfile.TemporaryDirectory() as tmpdir:
        try:
            tmp_db = _copy_sqlite_live(CALENDAR_DB, tmpdir)
        except PermissionError as e:
            msg = f"Cannot copy Calendar.sqlitedb: {e}. Grant Full Disk Access to python3 in System Settings → Privacy & Security."
            print(f"    ERROR: {msg}")
            _log_failure("calendar", msg)
            return docs
        except Exception as e:
            msg = f"Calendar.sqlitedb copy failed: {e}"
            print(f"    ERROR: {msg}")
            _log_failure("calendar", msg)
            return docs

        try:
            conn = sqlite3.connect(f"file:{tmp_db}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute("""
                SELECT
                    ci.ROWID,
                    ci.summary,
                    ci.description,
                    ci.start_date,
                    ci.end_date,
                    ci.all_day,
                    ci.UUID,
                    ci.status,
                    l.title AS location_title,
                    c.title AS calendar_name
                FROM CalendarItem ci
                LEFT JOIN Location l ON l.ROWID = ci.location_id
                LEFT JOIN Calendar c ON c.ROWID = ci.calendar_id
                WHERE ci.summary IS NOT NULL
                  AND ci.start_date BETWEEN ? AND ?
                  AND (ci.hidden IS NULL OR ci.hidden = 0)
                ORDER BY ci.start_date DESC
            """, (cutoff_start, cutoff_end))
            rows = cur.fetchall()
            conn.close()
        except Exception as e:
            msg = f"Calendar query failed: {e}"
            print(f"    ERROR: {msg}")
            _log_failure("calendar", msg)
            return docs

    for row in rows:
        title = (row["summary"] or "").strip() or "(no title)"
        cal_name = (row["calendar_name"] or "Calendar").strip()
        uid = row["UUID"] or str(row["ROWID"])
        start_iso = _apple_timestamp_to_iso(row["start_date"])
        end_iso = _apple_timestamp_to_iso(row["end_date"])
        event_date = start_iso[:10] if start_iso else ""
        is_past = False
        try:
            if start_iso:
                is_past = datetime.fromisoformat(start_iso) < now
        except ValueError:
            pass

        content_lines = [f"Event: {title}", f"Calendar: {cal_name}", f"Start: {start_iso}", f"End: {end_iso}"]
        loc = (row["location_title"] or "").strip()
        if loc:
            content_lines.append(f"Location: {loc}")
        desc = (row["description"] or "").strip()
        if desc:
            content_lines.append(f"Notes: {desc[:500]}")
        content = "\n".join(content_lines)

        docs.append({
            'content': content,
            'source': f"calendar://{cal_name}/{uid}",
            'type': 'event',
            'service': cal_name,
            'title': title,
            'event_date': event_date,
            'is_past': is_past,
            'stable_id': f"event:{uid}",
        })
    return docs


# ── Adapter 4: Reminders (direct SQLite Core Data read) ──
def _find_active_reminders_db() -> Path | None:
    """Find the iCloud Reminders store with actual data (skips Data-local.sqlite)."""
    if not REMINDERS_STORES_DIR.exists():
        return None
    candidates = sorted(REMINDERS_STORES_DIR.glob("Data-*.sqlite"))
    best: tuple[int, Path] | None = None
    for db in candidates:
        if db.name == "Data-local.sqlite":
            continue
        with tempfile.TemporaryDirectory() as tmpdir:
            try:
                tmp = _copy_sqlite_live(db, tmpdir)
                conn = sqlite3.connect(f"file:{tmp}?mode=ro", uri=True)
                cur = conn.cursor()
                cur.execute("SELECT COUNT(*) FROM ZREMCDREMINDER")
                count = cur.fetchone()[0]
                conn.close()
                if count > 0 and (best is None or count > best[0]):
                    best = (count, db)
            except Exception:
                continue
    return best[1] if best else None


def collect_reminders():
    """Pull Apple Reminders from Core Data SQLite store. One chunk per reminder."""
    docs = []
    db_path = _find_active_reminders_db()
    if db_path is None:
        # Fall back to the local store even if empty — at least we don't crash.
        fallback = REMINDERS_STORES_DIR / "Data-local.sqlite"
        if not fallback.exists():
            msg = f"No Reminders store found in {REMINDERS_STORES_DIR}"
            print(f"    ERROR: {msg}")
            _log_failure("reminders", msg)
            return docs
        db_path = fallback

    with tempfile.TemporaryDirectory() as tmpdir:
        try:
            tmp_db = _copy_sqlite_live(db_path, tmpdir)
        except PermissionError as e:
            msg = f"Cannot copy {db_path.name}: {e}. Grant Full Disk Access to python3 in System Settings."
            print(f"    ERROR: {msg}")
            _log_failure("reminders", msg)
            return docs
        except Exception as e:
            msg = f"Reminders store copy failed: {e}"
            print(f"    ERROR: {msg}")
            _log_failure("reminders", msg)
            return docs

        try:
            conn = sqlite3.connect(f"file:{tmp_db}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            # Join reminders to their parent list (Core Data naming).
            cur.execute("""
                SELECT
                    r.Z_PK,
                    r.ZTITLE AS title,
                    r.ZNOTES AS notes,
                    r.ZCOMPLETED AS completed,
                    r.ZDUEDATE AS due_date,
                    r.ZCREATIONDATE AS created,
                    r.ZFLAGGED AS flagged,
                    r.ZPRIORITY AS priority,
                    l.ZNAME AS list_name
                FROM ZREMCDREMINDER r
                LEFT JOIN ZREMCDBASELIST l ON l.Z_PK = r.ZLIST
                WHERE r.ZTITLE IS NOT NULL
                  AND (r.ZMARKEDFORDELETION IS NULL OR r.ZMARKEDFORDELETION = 0)
                ORDER BY r.ZCREATIONDATE DESC
            """)
            rows = cur.fetchall()
            conn.close()
        except Exception as e:
            msg = f"Reminders query failed: {e}"
            print(f"    ERROR: {msg}")
            _log_failure("reminders", msg)
            return docs

    for row in rows:
        name = (row["title"] or "").strip()
        if not name:
            continue
        list_name = (row["list_name"] or "Reminders").strip()
        status = "completed" if (row["completed"] or 0) else "pending"
        due_iso = _apple_timestamp_to_iso(row["due_date"])[:10] if row["due_date"] else ""
        notes = (row["notes"] or "").strip()

        content_lines = [
            f"Reminder: {name}",
            f"List: {list_name}",
            f"Status: {status}",
            f"Due: {due_iso or 'none'}",
        ]
        if row["flagged"]:
            content_lines.append("Flagged: yes")
        if notes:
            content_lines.append(f"Notes: {notes[:500]}")
        content = "\n".join(content_lines)

        rem_id = str(row["Z_PK"])
        docs.append({
            'content': content,
            'source': f"reminders://{list_name}/{rem_id}",
            'type': 'reminder',
            'service': list_name,
            'title': name,
            'status': status,
            'due': due_iso,
            'stable_id': f"reminder:{rem_id}",
        })
    return docs


# ── Main ────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Personal data ingestion (Notes/iMessage/Calendar/Reminders)")
    parser.add_argument('--days', type=int, default=90, help='iMessage lookback days')
    parser.add_argument('--cal-back', type=int, default=30)
    parser.add_argument('--cal-forward', type=int, default=60)
    parser.add_argument('--notes-only', action='store_true')
    parser.add_argument('--messages-only', action='store_true')
    parser.add_argument('--calendar-only', action='store_true')
    parser.add_argument('--reminders-only', action='store_true')
    args = parser.parse_args()

    print("=" * 60)
    print(f"Personal Data Ingestion — {datetime.now().isoformat()}")
    print("=" * 60)

    print("\n[setup] Ensuring collections...")
    ensure_collection('personal')

    state = load_state()
    state['last_run'] = datetime.now().isoformat()

    n_count = m_count = c_count = t_count = 0
    only_flags = (args.notes_only, args.messages_only, args.calendar_only, args.reminders_only)
    run_all = not any(only_flags)
    # Skip stale cleanup when running a single adapter — the consolidated 'personal'
    # collection contains all 4 data types, so partial upserts would delete other types.
    partial = not run_all

    if run_all or args.notes_only:
        print("\n[1/4] Apple Notes...")
        notes = collect_notes()
        print(f"  Collected {len(notes)} notes")
        n_count = add_personal_documents("personal", notes, skip_stale_cleanup=partial)
        state['notes_last_count'] = n_count

    if run_all or args.messages_only:
        print(f"\n[2/4] iMessage (last {args.days} days)...")
        messages = collect_messages(days_back=args.days)
        print(f"  Collected {len(messages)} message chunks")
        m_count = add_personal_documents("personal", messages, skip_stale_cleanup=partial)
        state['messages_last_count'] = m_count

    if run_all or args.calendar_only:
        print(f"\n[3/4] Calendar (-{args.cal_back}/+{args.cal_forward} days)...")
        events = collect_calendar(days_back=args.cal_back, days_forward=args.cal_forward)
        print(f"  Collected {len(events)} events")
        c_count = add_personal_documents("personal", events, skip_stale_cleanup=partial)
        state['calendar_last_count'] = c_count

    if run_all or args.reminders_only:
        print("\n[4/4] Reminders...")
        tasks = collect_reminders()
        print(f"  Collected {len(tasks)} reminders")
        t_count = add_personal_documents("personal", tasks, skip_stale_cleanup=partial)
        state['tasks_last_count'] = t_count

    save_state(state)

    print("\n" + "=" * 60)
    print(f"DONE — notes: {n_count}, messages: {m_count}, calendar: {c_count}, tasks: {t_count}")
    print(f"Total: {n_count + m_count + c_count + t_count} chunks indexed")
    print("=" * 60)

    # Fail loud: exit non-zero if any requested adapter ingested nothing.
    # This makes launchd's last-exit-status reflect reality and prevents silent regressions.
    failed = []
    if (run_all or args.notes_only) and n_count == 0:
        failed.append("notes")
    if (run_all or args.messages_only) and m_count == 0:
        failed.append("messages")
    if (run_all or args.calendar_only) and c_count == 0:
        failed.append("calendar")
    if (run_all or args.reminders_only) and t_count == 0:
        failed.append("tasks")

    if failed:
        sys.stderr.write(f"PERSONAL_INGEST_FAIL adapters={','.join(failed)}\n")
        sys.exit(1)


if __name__ == '__main__':
    import sys as _sys
    _sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from brain_core.batch_lock import batch_lock
    print("  Acquiring batch lock (ensures no concurrent heavy jobs)...")
    with batch_lock("personal_ingest"):
        main()
