#!/usr/bin/env python3
"""
Obsidian Live Sync ↔ Local Folder Sync
Syncs CouchDB (Obsidian Live Sync) to a local folder for OpenClaw access.
Supports reading and writing notes.
"""

import argparse
import base64
import fcntl
import json
import os
import sys
import time
import urllib.parse
import urllib.request
import uuid
from datetime import UTC, datetime
from pathlib import Path

ENV_FILE = Path(os.getenv("OBSIDIAN_SYNC_ENV_FILE", "/Users/chrischo/.openclaw/workspace/.obsidian_sync.env"))
FAILURE_LOG = Path("/Users/chrischo/server/brain/logs/obsidian-sync-failures.jsonl")
STATE_FILE = Path("/Users/chrischo/server/brain/logs/obsidian-sync-state.json")


# 2026-04-16 R-5: flock-based safe_state to prevent overlapping syncs
# from corrupting last_seq (would force a full rescan from seq=0).
def load_state() -> dict:
    if not STATE_FILE.exists():
        return {"last_seq": "0", "last_run": None}
    try:
        with STATE_FILE.open("r") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_SH)
            try:
                return json.loads(f.read())
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    except Exception:
        return {"last_seq": "0", "last_run": None}


def save_state(state: dict) -> None:
    state["last_run"] = datetime.now(UTC).isoformat()
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(STATE_FILE.suffix + ".tmp")
    with tmp.open("w") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            f.write(json.dumps(state, indent=2))
            f.flush()
            os.fsync(f.fileno())
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    os.replace(tmp, STATE_FILE)


def log_failure(stage: str, error: str = ""):
    try:
        FAILURE_LOG.parent.mkdir(parents=True, exist_ok=True)
        with FAILURE_LOG.open("a") as f:
            f.write(
                json.dumps(
                    {
                        "timestamp": datetime.now(UTC).isoformat(),
                        "stage": stage,
                        "error": str(error)[:500],
                    }
                )
                + "\n"
            )
    except Exception:
        pass


def load_env_file(path: Path):
    """Load simple KEY=VALUE lines from an env file if it exists."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


load_env_file(ENV_FILE)

COUCH_URL = os.getenv("OBSIDIAN_COUCH_URL", "http://localhost:5984")
COUCH_USER = os.getenv("OBSIDIAN_COUCH_USER")
COUCH_PASS = os.getenv("OBSIDIAN_COUCH_PASS")
DB_NAME = os.getenv("OBSIDIAN_COUCH_DB", "obsidian")
VAULT_DIR = os.getenv("OBSIDIAN_VAULT_DIR", "/Users/chrischo/.openclaw/workspace/obsidian-vault")


def _check_creds():
    if not COUCH_USER or not COUCH_PASS:
        raise RuntimeError(
            "Missing Obsidian CouchDB credentials. Set OBSIDIAN_COUCH_USER/OBSIDIAN_COUCH_PASS "
            "(or create /Users/chrischo/.openclaw/workspace/.obsidian_sync.env)."
        )


def couch_request(path, method="GET", data=None):
    """Make authenticated CouchDB request"""
    url = f"{COUCH_URL}/{path}"
    creds = base64.b64encode(f"{COUCH_USER}:{COUCH_PASS}".encode()).decode()
    headers = {"Authorization": f"Basic {creds}", "Content-Type": "application/json"}

    if data:
        req = urllib.request.Request(url, data=json.dumps(data).encode(), headers=headers, method=method)
    else:
        req = urllib.request.Request(url, headers=headers, method=method)

    # 30s timeout — matches the other external ingest calls. Without this a
    # stalled CouchDB (partition, paused container) wedges the sync job
    # forever and holds the scheduler slot.
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        log_failure("couch_request", f"{method} {path} HTTP {e.code}")
        return json.loads(e.read())
    except Exception as e:
        log_failure("couch_request", f"{method} {path} {e}")
        return {"error": str(e)}


def get_all_docs():
    """Get all document IDs from CouchDB (full scan fallback)"""
    result = couch_request(f"{DB_NAME}/_all_docs?include_docs=true&limit=10000")
    return result.get("rows", [])


def get_changes(since: str = "0", limit: int = 500):
    """Get changed docs since last sync using CouchDB _changes feed."""
    url = (
        f"{DB_NAME}/_changes?since={urllib.parse.quote(str(since), safe='')}&include_docs=true&limit={limit}"
    )
    result = couch_request(url)
    return result.get("results", []), result.get("last_seq", since)


def reconstruct_note(doc):
    """Reconstruct note content from parent + children chunks"""
    if doc.get("deleted"):
        return None

    children = doc.get("children", [])
    if not children:
        # Small doc with inline data
        return doc.get("data", "")

    # Fetch children chunks and concatenate.
    # 2026-04-18: previously silently skipped children whose fetch returned
    # an error dict (HTTPError caught by couch_request returns `{"error": ...}`
    # on 404/409). Notes ended up reconstructed with missing segments and
    # indexed that way — silent data loss. Now: abort reconstruction so the
    # caller can decide to retry next run.
    content_parts = []
    for child_id in children:
        encoded_id = urllib.parse.quote(child_id, safe="")
        child = couch_request(f"{DB_NAME}/{encoded_id}")
        if isinstance(child, dict) and "error" in child:
            log_failure("reconstruct_note", f"missing child {child_id[:40]}: {str(child.get('error'))[:120]}")
            return None
        if "data" in child:
            content_parts.append(child["data"])
        else:
            log_failure("reconstruct_note", f"child {child_id[:40]} has no data field")
            return None

    return "".join(content_parts)


def atomic_write_text(file_path, content):
    """Write text atomically to avoid transient 0-byte/truncated files."""
    tmp_path = f"{file_path}.tmp.openclaw"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, file_path)
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


def _sync_md_doc(doc: dict) -> str:
    """Sync a single markdown doc to the vault. Returns status: synced/unchanged/skipped/deleted."""
    doc_id = doc.get("_id", "")
    if not doc_id.endswith(".md"):
        return "skipped"
    vault_root = os.path.realpath(VAULT_DIR)
    rel = doc.get("path", doc_id)
    file_path = os.path.realpath(os.path.join(VAULT_DIR, rel))
    if not file_path.startswith(vault_root):
        log_failure("path_traversal", f"Blocked path outside vault: {rel}")
        return "skipped"
    # CouchDB _changes tombstone: `_deleted` on the doc (plus `deleted` fallback
    # for legacy shapes). Unlink the mirror so deletes in Obsidian propagate.
    if doc.get("_deleted") or doc.get("deleted"):
        if not os.path.exists(file_path):
            return "skipped"
        try:
            os.remove(file_path)
        except Exception as e:
            log_failure("vault_unlink", f"{file_path}: {e}")
            return "skipped"
        parent = os.path.dirname(file_path)
        while parent != vault_root and parent.startswith(vault_root):
            try:
                os.rmdir(parent)
            except OSError:
                break
            parent = os.path.dirname(parent)
        return "deleted"
    content = reconstruct_note(doc)
    if content is None:
        return "skipped"
    os.makedirs(os.path.dirname(file_path), exist_ok=True)
    if os.path.exists(file_path):
        try:
            with open(file_path, encoding="utf-8") as f:
                if f.read() == content:
                    return "unchanged"
        except Exception as e:
            log_failure("vault_read", f"{file_path}: {e}")
    try:
        atomic_write_text(file_path, content)
        return "synced"
    except Exception as e:
        log_failure("vault_write", f"{file_path}: {e}")
        return "skipped"


def pull_notes_incremental():
    """Incremental sync using CouchDB _changes feed. Only processes changed docs."""
    _check_creds()
    os.makedirs(VAULT_DIR, exist_ok=True)
    state = load_state()
    since = state.get("last_seq", "0")

    try:
        changes, new_seq = get_changes(since, limit=500)
    except Exception as e:
        log_failure("changes_fetch", str(e))
        print(f"ERROR: CouchDB _changes fetch failed: {e}")
        return

    synced = 0
    skipped = 0
    unchanged = 0
    deleted = 0

    for change in changes:
        doc = change.get("doc", {}) or {}
        doc_id = doc.get("_id", "")
        if not doc_id or doc_id.startswith("_") or doc_id.startswith("h:"):
            continue
        # CouchDB exposes the tombstone flag at the change level; mirror it onto
        # the doc so _sync_md_doc sees it even when the body is a bare stub.
        if change.get("deleted"):
            doc["_deleted"] = True
        status = _sync_md_doc(doc)
        if status == "synced":
            synced += 1
        elif status == "unchanged":
            unchanged += 1
        elif status == "deleted":
            deleted += 1
        else:
            skipped += 1

    state["last_seq"] = new_seq
    save_state(state)

    print(f"Incremental sync ({len(changes)} changes processed)")
    print(f"  Synced {synced} | Unchanged {unchanged} | Deleted {deleted} | Skipped {skipped}")
    print(f"  last_seq: {str(new_seq)[:40]}")


def pull_notes():
    """Pull all notes from CouchDB to local folder"""
    _check_creds()
    os.makedirs(VAULT_DIR, exist_ok=True)

    try:
        rows = get_all_docs()
    except Exception as e:
        log_failure("couchdb_fetch", str(e))
        print(f"ERROR: CouchDB fetch failed: {e}")
        return
    synced = 0
    skipped = 0
    unchanged = 0

    for row in rows:
        doc = row.get("doc", {})
        doc_id = doc.get("_id", "")
        doc_type = doc.get("type", "")

        # Skip internal docs, deleted docs, and non-text files
        if doc_id.startswith("_") or doc_id.startswith("h:"):
            continue
        if doc.get("deleted"):
            skipped += 1
            continue

        # Handle markdown files
        if doc_id.endswith(".md"):
            content = reconstruct_note(doc)
            if content is None:
                skipped += 1
                continue

            file_path = os.path.realpath(os.path.join(VAULT_DIR, doc.get("path", doc_id)))
            if not file_path.startswith(os.path.realpath(VAULT_DIR)):
                log_failure("path_traversal", f"Blocked path outside vault: {doc.get('path', doc_id)}")
                skipped += 1
                continue
            os.makedirs(os.path.dirname(file_path), exist_ok=True)

            # Skip rewrite when content is identical (reduces editor write conflicts)
            if os.path.exists(file_path):
                try:
                    with open(file_path, encoding="utf-8") as f:
                        if f.read() == content:
                            unchanged += 1
                            continue
                except Exception as e:
                    log_failure("vault_read", f"{file_path}: {e}")

            try:
                atomic_write_text(file_path, content)
                synced += 1
            except Exception as e:
                log_failure("vault_write", f"{file_path}: {e}")
                skipped += 1

        # Handle binary files (images, etc.)
        elif any(doc_id.endswith(ext) for ext in [".png", ".jpg", ".jpeg", ".gif", ".pdf"]):
            # Skip binary files for now
            skipped += 1

    print(f"✅ Synced {synced} notes to {VAULT_DIR}")
    print(f"🟰 Unchanged {unchanged}")
    print(f"⏭️ Skipped {skipped} (deleted/binary)")


def push_note(file_path):
    """Push a local note back to CouchDB (Live Sync-compatible parent+leaf format)."""
    rel_path = os.path.relpath(file_path, VAULT_DIR)
    doc_id = rel_path.lower()

    # Read local file
    with open(file_path, encoding="utf-8") as f:
        content = f.read()

    now_ms = int(time.time() * 1000)
    content_size = len(content.encode("utf-8"))

    # Check if parent doc exists
    encoded_id = urllib.parse.quote(doc_id, safe="")
    existing = couch_request(f"{DB_NAME}/{encoded_id}")

    if "error" in existing:
        # New parent + one leaf child
        child_id = f"h:{uuid.uuid4().hex[:28]}"
        child_doc = {
            "_id": child_id,
            "type": "leaf",
            "data": content,
            "ctime": now_ms,
            "mtime": now_ms,
            "eden": {},
        }
        encoded_child = urllib.parse.quote(child_id, safe="")
        child_result = couch_request(f"{DB_NAME}/{encoded_child}", method="PUT", data=child_doc)

        parent_doc = {
            "_id": doc_id,
            "path": rel_path,
            "ctime": now_ms,
            "mtime": now_ms,
            "size": content_size,
            "type": "plain",
            "children": [child_id],
            "eden": {},
        }
        parent_result = couch_request(f"{DB_NAME}/{encoded_id}", method="PUT", data=parent_doc)

        result = parent_result if "ok" in child_result else child_result
    else:
        # Existing parent: keep/repair to one leaf child
        children = existing.get("children", [])
        child_id = children[0] if children else f"h:{uuid.uuid4().hex[:28]}"
        encoded_child = urllib.parse.quote(child_id, safe="")

        existing_child = couch_request(f"{DB_NAME}/{encoded_child}")
        child_doc = {
            "_id": child_id,
            "type": "leaf",
            "data": content,
            "ctime": existing.get("ctime", now_ms),
            "mtime": now_ms,
            "eden": existing_child.get("eden", {}) if "error" not in existing_child else {},
        }
        if "error" not in existing_child and "_rev" in existing_child:
            child_doc["_rev"] = existing_child["_rev"]

        child_result = couch_request(f"{DB_NAME}/{encoded_child}", method="PUT", data=child_doc)

        existing.pop("data", None)  # inline format can trigger Live Sync issues
        existing["path"] = rel_path
        existing["mtime"] = now_ms
        existing["size"] = content_size
        existing["type"] = "plain"
        existing["children"] = [child_id]
        existing.setdefault("eden", {})

        parent_result = couch_request(f"{DB_NAME}/{encoded_id}", method="PUT", data=existing)
        result = parent_result if "ok" in child_result else child_result

    if "ok" in result:
        print(f"✅ Pushed: {rel_path}")
    else:
        log_failure("push_note", f"{rel_path}: {result}")
        print(f"❌ Error: {result}")


def list_notes():
    """List all notes in CouchDB"""
    rows = get_all_docs()
    notes = []
    for row in rows:
        doc = row.get("doc", {})
        doc_id = doc.get("_id", "")
        if doc_id.startswith("_") or doc_id.startswith("h:"):
            continue
        if doc.get("deleted"):
            continue
        if doc_id.endswith(".md"):
            notes.append(
                {"path": doc.get("path", doc_id), "size": doc.get("size", 0), "mtime": doc.get("mtime", 0)}
            )

    notes.sort(key=lambda x: x["mtime"], reverse=True)
    for n in notes[:30]:
        ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(n["mtime"] / 1000))
        print(f"  {ts}  {n['size']:>6}B  {n['path']}")
    print(f"\nTotal: {len(notes)} notes")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Obsidian Live Sync ↔ Local Folder")
    parser.add_argument("action", choices=["pull", "push", "list"], help="Action to perform")
    parser.add_argument("--file", help="File to push (for push action)")
    parser.add_argument("--full", action="store_true", help="Force full scan instead of incremental")

    args = parser.parse_args()

    if args.action == "pull":
        if args.full:
            pull_notes()
        else:
            pull_notes_incremental()
    elif args.action == "push":
        if not args.file:
            print("Error: --file required for push")
            sys.exit(1)
        push_note(args.file)
    elif args.action == "list":
        list_notes()
