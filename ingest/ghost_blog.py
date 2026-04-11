#!/opt/homebrew/bin/python3
"""Ghost blog ingest — pulls Chris's Ghost posts into ChromaDB.

Owner: Market agent.

Auth: Ghost Admin API key from ~/.openclaw/credentials/ghost-admin.json.
      Format: {"url": "https://blog.chrischodev.com", "key": "<id>:<secret>"}

The Admin API key format is `<id>:<secret>` where secret is a 64-char hex
string. The script mints a short-lived JWT signed with the secret to
authenticate requests (no library deps — pure stdlib + hmac).

Writes to the `knowledge` collection with service=ghost metadata so search
results are filterable via the existing /recall?service=ghost path.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

# Reuse brain_core helpers (ChromaDB HTTP + Ollama embed).
sys.path.insert(0, "/Users/chrischo/server/brain/brain_core")
from indexer import chroma_api, get_embedding, ensure_collection, _get_collection_id  # noqa: E402

CREDENTIALS = Path("/Users/chrischo/.openclaw/credentials/ghost-admin.json")
COLLECTION = "knowledge"
SERVICE = "ghost"
MAX_CHUNK_CHARS = 1800
MIN_POST_LEN = 100
FAILURE_LOG = Path("/Users/chrischo/server/brain/logs/ghost-ingest-failures.jsonl")


def _log_failure(error: str) -> None:
    try:
        FAILURE_LOG.parent.mkdir(parents=True, exist_ok=True)
        entry = {"timestamp": datetime.now().isoformat(), "adapter": "ghost", "error": error[:500]}
        with FAILURE_LOG.open("a") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


# ── Ghost Admin API JWT minting ─────────────────────────
def _b64(data: bytes) -> str:
    """URL-safe base64 without padding, as used in JWTs."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _mint_jwt(key_id: str, key_secret_hex: str) -> str:
    """Mint a short-lived HS256 JWT for the Ghost Admin API.

    Ghost v5 expects `kid` header = key id, signed with the secret (hex-decoded).
    iat=now, exp=now+5min, aud="/admin/".
    """
    now = int(time.time())
    header = {"alg": "HS256", "typ": "JWT", "kid": key_id}
    payload = {"iat": now, "exp": now + 300, "aud": "/admin/"}
    signing_input = (
        _b64(json.dumps(header, separators=(",", ":")).encode())
        + "."
        + _b64(json.dumps(payload, separators=(",", ":")).encode())
    )
    secret_bytes = bytes.fromhex(key_secret_hex)
    sig = hmac.new(secret_bytes, signing_input.encode(), hashlib.sha256).digest()
    return signing_input + "." + _b64(sig)


def _load_credentials() -> tuple[str, str, str]:
    creds = json.loads(CREDENTIALS.read_text())
    url = creds["url"].rstrip("/")
    key = creds["key"]
    if ":" not in key:
        raise ValueError(f"Malformed Ghost Admin key — expected 'id:secret' format, got {key[:10]}...")
    key_id, key_secret = key.split(":", 1)
    return url, key_id, key_secret


def _http_get(url: str, token: str) -> dict:
    # Cloudflare in front of the blog rejects requests without a real
    # User-Agent (error 1010 "browser integrity check"). Python's default UA
    # trips this, so spoof a sensible one.
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Ghost {token}",
            "User-Agent": "Mozilla/5.0 (compatible; brain-ghost-ingest/1.0)",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


# ── Post normalization ───────────────────────────────────
_HTML_TAG = re.compile(r"<[^>]+>")
_HTML_ENTITY = re.compile(r"&(?:nbsp|amp|lt|gt|quot|#\d+);")
_ENTITY_MAP = {"&nbsp;": " ", "&amp;": "&", "&lt;": "<", "&gt;": ">", "&quot;": '"'}


def _strip_html(text: str) -> str:
    text = _HTML_TAG.sub("\n", text or "")
    for ent, repl in _ENTITY_MAP.items():
        text = text.replace(ent, repl)
    text = _HTML_ENTITY.sub(" ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _fetch_all_posts(url: str, key_id: str, key_secret: str) -> list[dict]:
    """Paginate through the Ghost Admin API posts endpoint.

    Re-mints the JWT each page to avoid 401 on long pagination runs (5-min TTL).
    """
    posts: list[dict] = []
    page = 1
    while True:
        jwt = _mint_jwt(key_id, key_secret)
        endpoint = (
            f"{url}/ghost/api/admin/posts/"
            f"?limit=50&page={page}&include=tags,authors&formats=html"
        )
        try:
            data = _http_get(endpoint, jwt)
        except urllib.error.HTTPError as e:
            _log_failure(f"Admin API {e.code}: {e.read().decode()[:200]}")
            return posts
        except Exception as e:
            _log_failure(f"Admin API error: {e}")
            return posts

        batch = data.get("posts") or []
        posts.extend(batch)
        pagination = data.get("meta", {}).get("pagination", {})
        total_pages = pagination.get("pages", 1)
        if page >= total_pages:
            break
        page += 1

    return posts


def _chunk_post(title: str, html_body: str) -> list[str]:
    body = _strip_html(html_body)
    if len(body) < MIN_POST_LEN:
        return []

    header = f"Ghost blog post: {title}\n"
    if len(body) + len(header) <= MAX_CHUNK_CHARS:
        return [header + body]

    chunks: list[str] = []
    # Split on paragraph boundaries first, then pack into chunks.
    paragraphs = [p.strip() for p in body.split("\n\n") if p.strip()]
    current = header
    for p in paragraphs:
        if len(current) + len(p) + 2 > MAX_CHUNK_CHARS:
            chunks.append(current.rstrip())
            current = header + p + "\n\n"
        else:
            current += p + "\n\n"
    if current.strip() != header.strip():
        chunks.append(current.rstrip())
    return chunks


# ── ChromaDB upsert ──────────────────────────────────────
def _upsert(chunks: list[dict]) -> int:
    if not chunks:
        return 0
    ensure_collection(COLLECTION)
    col_id = _get_collection_id(COLLECTION)
    if not col_id:
        return 0

    ids, embeddings, documents, metadatas = [], [], [], []
    for c in chunks:
        content = c["content"]
        emb = get_embedding(content[:8000])
        if not emb:
            continue
        doc_id = f"ghost:{c['slug']}:{c['chunk_index']}"
        ids.append(doc_id)
        embeddings.append(emb)
        documents.append(content)
        metadatas.append({
            "source": c["url"],
            "service": SERVICE,
            "type": "blog_post",
            "title": c["title"],
            "slug": c["slug"],
            "published_at": c["published_at"],
            "updated_at": c["updated_at"],
            "status": c["status"],
            "tags": ",".join(c["tags"]),
            "agent": "market",
            "created_at": datetime.now().isoformat(),
        })

    if not ids:
        return 0

    BATCH = 20
    for start in range(0, len(ids), BATCH):
        end = min(start + BATCH, len(ids))
        chroma_api(
            "POST",
            f"/api/v2/tenants/default_tenant/databases/default_database/collections/{col_id}/upsert",
            {
                "ids": ids[start:end],
                "embeddings": embeddings[start:end],
                "documents": documents[start:end],
                "metadatas": metadatas[start:end],
            },
        )
    return len(ids)


# ── Main ─────────────────────────────────────────────────
def main() -> int:
    print("=" * 60)
    print(f"Ghost Blog Ingest — {datetime.now().isoformat()}")
    print("=" * 60)

    if not CREDENTIALS.exists():
        msg = f"Missing credentials at {CREDENTIALS}"
        print(f"  ERROR: {msg}")
        _log_failure(msg)
        return 1

    try:
        url, key_id, key_secret = _load_credentials()
    except Exception as e:
        msg = f"Credential parse error: {e}"
        print(f"  ERROR: {msg}")
        _log_failure(msg)
        return 1

    print(f"[1/3] Fetching posts from {url}/ghost/api/admin/posts/")
    posts = _fetch_all_posts(url, key_id, key_secret)
    print(f"  Found {len(posts)} posts")

    if not posts:
        print("  No posts to ingest (or Admin API unreachable).")
        return 0 if not FAILURE_LOG.exists() or FAILURE_LOG.stat().st_size == 0 else 1

    print("[2/3] Chunking posts...")
    chunks: list[dict] = []
    for post in posts:
        title = post.get("title") or "(untitled)"
        slug = post.get("slug") or post.get("id") or ""
        post_chunks = _chunk_post(title, post.get("html") or "")
        for idx, content in enumerate(post_chunks):
            chunks.append({
                "content": content,
                "title": title,
                "slug": slug,
                "chunk_index": idx,
                "url": f"{url}/{slug}" if slug else url,
                "published_at": post.get("published_at") or "",
                "updated_at": post.get("updated_at") or "",
                "status": post.get("status") or "",
                "tags": [t.get("name", "") for t in (post.get("tags") or []) if isinstance(t, dict)],
            })
    print(f"  Produced {len(chunks)} chunks")

    print("[3/3] Embedding + upserting into ChromaDB...")
    count = _upsert(chunks)
    print(f"  Upserted {count} chunks into '{COLLECTION}' (service=ghost)")

    print("=" * 60)
    print(f"DONE — {count} chunks indexed")
    print("=" * 60)
    return 0 if count > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
