#!/opt/homebrew/bin/python3
"""RAG Indexer — Phase 2: Initial data indexing into ChromaDB via Ollama embeddings."""

import hashlib
import json
import os
import re
import urllib.request
from pathlib import Path
from datetime import datetime, timezone

# ── Config ──────────────────────────────────────────────
# Direct host-port access (chromadb + ollama expose 127.0.0.1:8000 and 11434
# via docker-compose). Eliminates the ~50-100ms subprocess penalty per query
# that the old `docker exec nginx curl ...` path incurred.
try:
    from config import CHROMA_URL, OLLAMA_URL, EMBED_MODEL, EMBED_MODEL_VERSION, OPENCLAW_DIR, BRAIN_HOME
except ImportError:
    CHROMA_URL = "http://127.0.0.1:8000"
    OLLAMA_URL = "http://127.0.0.1:11434"
    EMBED_MODEL = "blaifa/multilingual-e5-large-instruct"
    EMBED_MODEL_VERSION = "multilingual-e5-large-instruct:v1"
    OPENCLAW_DIR = Path("/Users/chrischo/.openclaw")
    BRAIN_HOME = Path("/Users/chrischo/server")

import urllib.error


import sys as _sys
_sys.path.insert(0, str(Path(__file__).parent))
from http_pool import http_json as _http_json  # noqa: E402


def chroma_api(method, path, data=None):
    """Call ChromaDB directly via localhost HTTP with keep-alive."""
    return _http_json(method, f"{CHROMA_URL}{path}", payload=data, timeout=120)


# ── Embedding Cache ────────────────────────────────────
# Shared SQLite cache: md5(embed_text) → embedding vector.
try:
    from embed_cache import cache_get as _cache_get, cache_put as _cache_put
except ImportError:
    import sqlite3 as _sqlite3
    _EMBED_CACHE_PATH = Path("/Users/chrischo/server/brain/logs/embedding_cache.db")
    _embed_cache_conn = None
    def _embed_cache():
        global _embed_cache_conn
        if _embed_cache_conn is None:
            _embed_cache_conn = _sqlite3.connect(str(_EMBED_CACHE_PATH), timeout=10)
            _embed_cache_conn.execute("PRAGMA journal_mode=WAL")
            _embed_cache_conn.execute("CREATE TABLE IF NOT EXISTS embeddings (hash TEXT PRIMARY KEY, embedding BLOB)")
        return _embed_cache_conn
    def _cache_get(text_hash):
        try:
            row = _embed_cache().execute("SELECT embedding FROM embeddings WHERE hash=?", (text_hash,)).fetchone()
            if row: return json.loads(row[0])
        except Exception: pass
        return None
    def _cache_put(text_hash, embedding):
        try:
            _embed_cache().execute("INSERT OR REPLACE INTO embeddings (hash, embedding) VALUES (?, ?)", (text_hash, json.dumps(embedding)))
            _embed_cache().commit()
        except Exception: pass


def get_embedding(text, _retries=5, use_cache=True, prefix="passage"):
    """Get embedding from Ollama via localhost HTTP with retry on empty/error response."""
    import time as _time
    max_chars = 1000
    truncated = text[:max_chars]
    prompted = f"{prefix}: {truncated}" if prefix else truncated

    if use_cache:
        text_hash = hashlib.md5(f'{EMBED_MODEL}:{prompted}'.encode()).hexdigest()
        cached = _cache_get(text_hash)
        if cached:
            return cached

    last_err = None
    for attempt in range(_retries):
        try:
            payload = {"model": EMBED_MODEL, "prompt": prompted}
            data = _http_json("POST", f"{OLLAMA_URL}/api/embeddings", payload=payload, timeout=120)
            err_msg = data.get("error", "")
            if "context length" in err_msg or "input length" in err_msg:
                max_chars = max_chars // 2
                truncated = text[:max_chars]
                prompted = f"{prefix}: {truncated}" if prefix else truncated
                if use_cache:
                    text_hash = hashlib.md5(f'{EMBED_MODEL}:{prompted}'.encode()).hexdigest()
                last_err = f"context overflow, retrying at {max_chars} chars"
                continue
            emb = data.get("embedding", data.get("embeddings", [[]])[0])
            if emb:
                if use_cache:
                    _cache_put(text_hash, emb)
                return emb
            last_err = f"empty response: {str(data)[:200]}"
        except Exception as e:
            err_str = str(e).lower()
            # Ollama may return 4xx for context overflow — handle like the in-body error
            if "context length" in err_str or "input length" in err_str or "too long" in err_str:
                max_chars = max_chars // 2
                truncated = text[:max_chars]
                prompted = f"{prefix}: {truncated}" if prefix else truncated
                if use_cache:
                    text_hash = hashlib.md5(f'{EMBED_MODEL}:{prompted}'.encode()).hexdigest()
                last_err = f"context overflow (4xx), retrying at {max_chars} chars"
                continue
            last_err = str(e)
        if attempt < _retries - 1:
            wait = 2 * (attempt + 1)
            _time.sleep(wait)
    raise RuntimeError(f"Ollama failed after {_retries} retries for text[:50]={text[:50]!r}: {last_err}")


def get_embeddings_batch(texts: list[str], prefix: str = "passage", batch_size: int = 50, use_cache: bool = True) -> list[list[float]]:
    """Batch embed multiple texts via Ollama /api/embed. Falls back to serial on error.

    Returns embeddings in the same order as input texts.
    """
    if not texts:
        return []

    prompted = [f"{prefix}: {(t or '')[:1000]}" if prefix else (t or '')[:1000] for t in texts]
    embeddings: list[list[float]] = []

    # Check cache first for each text
    cache_hits: dict[int, list[float]] = {}
    to_embed: list[tuple[int, str]] = []
    if use_cache:
        for i, p in enumerate(prompted):
            text_hash = hashlib.md5(f'{EMBED_MODEL}:{p}'.encode()).hexdigest()
            cached = _cache_get(text_hash)
            if cached:
                cache_hits[i] = cached
            else:
                to_embed.append((i, p))
    else:
        to_embed = list(enumerate(prompted))

    # Embed uncached texts in batches
    batch_results: dict[int, list[float]] = {}
    for batch_start in range(0, len(to_embed), batch_size):
        batch = to_embed[batch_start:batch_start + batch_size]
        batch_indices = [i for i, _ in batch]
        batch_texts = [t for _, t in batch]

        try:
            payload = {"model": EMBED_MODEL, "input": batch_texts}
            data = _http_json("POST", f"{OLLAMA_URL}/api/embed", payload=payload, timeout=120)
            batch_embs = data.get("embeddings") or []

            if len(batch_embs) == len(batch_texts):
                for idx, emb in zip(batch_indices, batch_embs):
                    batch_results[idx] = emb
                    if use_cache:
                        text_hash = hashlib.md5(f'{EMBED_MODEL}:{prompted[idx]}'.encode()).hexdigest()
                        _cache_put(text_hash, emb)
            else:
                # Fallback to serial for this batch
                for idx, t in batch:
                    try:
                        emb = get_embedding(texts[idx], prefix=prefix, use_cache=use_cache)
                        batch_results[idx] = emb
                    except Exception:
                        batch_results[idx] = []
        except Exception:
            # Fallback to serial on any error
            for idx, t in batch:
                try:
                    emb = get_embedding(texts[idx], prefix=prefix, use_cache=use_cache)
                    batch_results[idx] = emb
                except Exception:
                    batch_results[idx] = []

    # Merge cache hits and new results in original order
    for i in range(len(prompted)):
        if i in cache_hits:
            embeddings.append(cache_hits[i])
        elif i in batch_results:
            embeddings.append(batch_results[i])
        else:
            embeddings.append([])  # should not happen

    return embeddings


# ── Secret Filtering ────────────────────────────────────
SECRET_PATTERNS = [
    re.compile(r'(?i)(api.?key|token|secret|password|passwd)\s*[:=]\s*\S+'),
    re.compile(r'(?i)CLOUDFLARE_API_TOKEN\S*'),
    re.compile(r'ghp_[A-Za-z0-9]{36}'),
    re.compile(r'sk-[A-Za-z0-9]{48}'),
    re.compile(r'[A-Za-z0-9+/]{60,}={0,2}'),  # long base64
    re.compile(r'(?i)(ssh|rsa|ed25519).?key'),
]
SKIP_FILES = {'.env', '.htpasswd', '.htpasswd_tesla'}

def filter_secrets(text):
    for pat in SECRET_PATTERNS:
        text = pat.sub('[REDACTED]', text)
    return text

def file_hash(content):
    return hashlib.md5(content.encode()).hexdigest()

# ── Chunking ────────────────────────────────────────────
MAX_CHUNK_SIZE = 1000
CHUNK_OVERLAP = 200
MIN_CHUNK_SIZE = 50


def chunk_text(text, max_size=MAX_CHUNK_SIZE, overlap=CHUNK_OVERLAP):
    """Generic chunker: split at paragraph > sentence > char boundaries."""
    if len(text) <= max_size:
        return [{'content': text, 'section': 'full'}]
    chunks = []
    paragraphs = re.split(r'\n\n+', text)
    # Split long paragraphs at sentence boundaries
    expanded = []
    for para in paragraphs:
        if len(para) <= max_size:
            expanded.append(para)
        else:
            sentences = re.split(r'(?<=[.!?])\s+', para)
            buf = ""
            for sent in sentences:
                if len(buf) + len(sent) + 1 > max_size and buf:
                    expanded.append(buf)
                    buf = sent
                else:
                    buf = (buf + " " + sent).strip() if buf else sent
            if buf:
                expanded.append(buf)
    current = ""
    for para in expanded:
        if len(current) + len(para) + 2 > max_size and current:
            chunks.append({'content': current.strip(), 'section': f'part {len(chunks)+1}'})
            overlap_text = current[-overlap:] if overlap else ""
            current = overlap_text + "\n\n" + para if overlap_text else para
            # If overlap pushed us over, drop it
            if len(current) > max_size:
                current = para
        else:
            current = (current + "\n\n" + para).strip() if current else para
    if current.strip():
        chunks.append({'content': current.strip(), 'section': f'part {len(chunks)+1}'})
    return chunks


def enforce_max_chunk_size(chunks, max_size=MAX_CHUNK_SIZE, overlap=CHUNK_OVERLAP):
    """Post-process: recursively split oversized chunks."""
    result = []
    for chunk in chunks:
        if len(chunk['content']) <= max_size:
            result.append(chunk)
        else:
            subs = chunk_text(chunk['content'], max_size, overlap)
            for i, sub in enumerate(subs):
                new = dict(chunk)
                new['content'] = sub['content']
                new['section'] = f"{chunk.get('section', '')} (part {i+1})"
                result.append(new)
    return result


def chunk_docker_compose(text, source):
    """Chunk by service blocks."""
    chunks = []
    lines = text.split('\n')
    current = []
    service_name = None

    for line in lines:
        if re.match(r'^  \w+:', line) and not line.strip().startswith('#'):
            if current and service_name:
                chunks.append({
                    'content': '\n'.join(current),
                    'service': service_name,
                })
            service_name = line.strip().rstrip(':')
            current = [line]
        else:
            current.append(line)

    if current and service_name:
        chunks.append({'content': '\n'.join(current), 'service': service_name})

    # If no services found, return whole file as one chunk
    if not chunks:
        chunks = [{'content': text, 'service': 'unknown'}]

    return chunks

def chunk_nginx_conf(text, source):
    """Chunk by server blocks."""
    blocks = re.split(r'(?=server\s*\{)', text)
    chunks = []
    for block in blocks:
        block = block.strip()
        if not block:
            continue
        server_name = 'unknown'
        m = re.search(r'server_name\s+([^;]+);', block)
        if m:
            server_name = m.group(1).strip()
        chunks.append({'content': block, 'service': server_name})
    return chunks

def chunk_markdown(text, source):
    """Chunk by headings."""
    sections = re.split(r'^(#{1,3}\s+.+)$', text, flags=re.MULTILINE)
    chunks = []
    current_heading = 'intro'
    current_content = []

    for part in sections:
        if re.match(r'^#{1,3}\s+', part):
            if current_content:
                content = '\n'.join(current_content).strip()
                if len(content) > 50:  # skip tiny sections
                    chunks.append({'content': content, 'section': current_heading})
            current_heading = part.strip('# \n')
            current_content = [part]
        else:
            current_content.append(part)

    if current_content:
        content = '\n'.join(current_content).strip()
        if len(content) > 50:
            chunks.append({'content': content, 'section': current_heading})

    if not chunks:
        chunks = [{'content': text, 'section': 'full'}]

    return chunks

def chunk_learnings(text, source):
    """Chunk by entry (date/title blocks)."""
    entries = re.split(r'\n(?=\d{4}-\d{2}-\d{2}|#{1,3}\s+)', text)
    chunks = []
    for entry in entries:
        entry = entry.strip()
        if len(entry) > 30:
            chunks.append({'content': entry, 'section': entry[:80]})
    if not chunks:
        chunks = [{'content': text, 'section': 'full'}]
    return chunks

# ── Collection Setup ────────────────────────────────────
def ensure_collection(name, _retries=5):
    """Get or create collection with retry on transient ChromaDB failures."""
    import time as _time
    last_err = None
    for attempt in range(_retries):
        try:
            cols = chroma_api("GET", "/api/v2/tenants/default_tenant/databases/default_database/collections")
            if isinstance(cols, list):
                for c in cols:
                    if c.get('name') == name:
                        print(f"  Collection '{name}' exists: {c['id']}")
                        return c['id']

            result = chroma_api("POST", "/api/v2/tenants/default_tenant/databases/default_database/collections", {
                "name": name,
                "metadata": {"hnsw:space": "cosine"}
            })
            print(f"  Collection '{name}' created: {result.get('id', 'unknown')}")
            return result.get('id')
        except Exception as e:
            last_err = str(e)
            if attempt < _retries - 1:
                wait = 3 * (attempt + 1)
                print(f"  ChromaDB unavailable (attempt {attempt + 1}/{_retries}), retrying in {wait}s...")
                _time.sleep(wait)
    raise RuntimeError(f"ensure_collection('{name}') failed after {_retries} retries: {last_err}")

# Cache collection name -> ID mapping (thread-safe, re-fetches on miss)
import threading as _threading
_collection_ids: dict[str, str] = {}
_collection_ids_lock = _threading.Lock()

def _get_collection_id(name):
    with _collection_ids_lock:
        cached = _collection_ids.get(name)
        if cached:
            return cached
    # Re-fetch from ChromaDB — collection may have been created since last cache fill
    try:
        cols = chroma_api("GET", "/api/v2/tenants/default_tenant/databases/default_database/collections")
    except Exception:
        return None
    with _collection_ids_lock:
        if isinstance(cols, list):
            for c in cols:
                if c.get('name') and c.get('id'):
                    _collection_ids[c['name']] = c['id']
        return _collection_ids.get(name)

def add_documents(collection_name, docs, skip_stale_cleanup=False):
    """Add documents to a collection with content-hash dedup and optional stale cleanup."""
    if not docs:
        return 0

    col_id = _get_collection_id(collection_name)
    if not col_id:
        print(f"    ERROR: Collection '{collection_name}' not found")
        return 0

    # Phase 0: Content-hash dedup — first occurrence of identical content wins
    seen_content = set()
    deduped_docs = []
    for doc in docs:
        content_hash = hashlib.md5(doc['content'].encode()).hexdigest()
        if content_hash in seen_content:
            continue
        seen_content.add(content_hash)
        deduped_docs.append(doc)
    if len(deduped_docs) < len(docs):
        print(f"    Dedup: {len(docs)} → {len(deduped_docs)} ({len(docs) - len(deduped_docs)} content duplicates removed)")
    docs = deduped_docs

    # Phase 1: Prepare all documents (metadata, content, embed text)
    prepared = []
    for i, doc in enumerate(docs):
        content = filter_secrets(doc['content'])
        stripped = content.strip()
        if len(stripped) < 30:
            continue
        # Skip boilerplate canonical/distilled proposal stub chunks
        if stripped.startswith('## Statement') and 'Review this proposed' in stripped[:80]:
            continue
        if stripped.startswith('## Observations') and 'Derived from raw evidence' in stripped[:80]:
            continue
        if stripped.startswith('## Source Summary') and len(stripped) < 80:
            continue
        # Skip JSON-only frontmatter chunks (no searchable content)
        if stripped.startswith('---json') and len(stripped) < 200:
            continue

        # Build semantic header for embedding — gives short/structured chunks
        # (YAML configs, nginx server blocks) natural-language anchor text so
        # vector search can match them on intent, not just literal tokens.
        source = str(doc.get('source', ''))
        service = doc.get('service', '')
        doc_type = doc.get('type', '')
        section = doc.get('section', '')

        header_parts = []
        if doc_type == 'docker-compose':
            header_parts.append(f"Docker Compose configuration for service '{service}'")
        elif doc_type == 'nginx-conf':
            header_parts.append(f"Nginx reverse proxy configuration for '{service}'")
        elif doc_type == 'agent-config':
            header_parts.append(f"Agent {doc.get('agent', '')} configuration: {section}")
        elif doc_type == 'learning':
            header_parts.append(f"Agent {doc.get('agent', '')} learning notes")
        elif doc_type == 'session-memory':
            header_parts.append(f"Agent {doc.get('agent', '')} session memory")
        elif doc_type == 'obsidian-note':
            header_parts.append(f"Personal note in Obsidian vault ({doc.get('vault_subdir', '')})")
        elif doc_type in ('canonical-note', 'distilled-note'):
            source_stem = Path(source).stem.replace('-', ' ').replace('_', ' ')
            label = 'Canonical knowledge' if doc_type == 'canonical-note' else 'Distilled summary'
            section_suffix = f" — {section}" if section else ''
            header_parts.append(f"{label}: {source_stem}{section_suffix}")

        embed_text = ("\n".join(header_parts) + "\n\n" + content) if header_parts else content
        # ID = hash of (source + content) — avoids collisions from long path truncation
        doc_id = hashlib.md5(f"{source}:{content}".encode()).hexdigest()
        # mtime: prefer explicit, else stat source file, else empty string
        mtime_val = doc.get('mtime') or ''
        if not mtime_val and source:
            try:
                _src_path = Path(source)
                if _src_path.exists() and _src_path.is_file():
                    mtime_val = f"{_src_path.stat().st_mtime:.6f}"
            except Exception:
                mtime_val = ''
        meta = {
            'source': source,
            'type': doc_type,
            'service': service,
            'agent': doc.get('agent', ''),
            'section': section,
            'vault_subdir': doc.get('vault_subdir', ''),
            'created_at': doc.get('event_time') or doc.get('timestamp') or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            'mtime': mtime_val,
            'embed_model': EMBED_MODEL,
            'embed_model_version': EMBED_MODEL_VERSION,
        }
        prepared.append((doc_id, content, meta, embed_text))

    if not prepared:
        return 0

    # Phase 1.5: Incremental reindex — skip re-embedding docs whose mtime matches.
    # Gated behind BRAIN_INCREMENTAL_REINDEX (default off) because it changes
    # semantics: docs that mutate WITHOUT a corresponding mtime change would be
    # silently skipped. Enable only when source mtimes are trustworthy.
    skipped_ids: set[str] = set()
    if os.getenv('BRAIN_INCREMENTAL_REINDEX', '').lower() in ('1', 'true', 'yes'):
        prepared_ids = [p[0] for p in prepared]
        try:
            # Batch fetch existing metadata in chunks of 200 to keep request small
            _existing: dict[str, dict] = {}
            for _start in range(0, len(prepared_ids), 200):
                _chunk = prepared_ids[_start:_start + 200]
                _resp = chroma_api(
                    "POST",
                    f"/api/v2/tenants/default_tenant/databases/default_database/collections/{col_id}/get",
                    {"ids": _chunk, "include": ["metadatas"]},
                )
                _rids = _resp.get("ids") or []
                _rmetas = _resp.get("metadatas") or []
                for _rid, _rmeta in zip(_rids, _rmetas):
                    _existing[_rid] = _rmeta or {}
            for _doc_id, _content, _meta, _etxt in prepared:
                _prev = _existing.get(_doc_id)
                if not _prev:
                    continue
                _prev_mtime = _prev.get('mtime') or ''
                _new_mtime = _meta.get('mtime') or ''
                # Strict equality on non-empty mtime + same embed model
                if (_prev_mtime and _new_mtime and _prev_mtime == _new_mtime
                        and _prev.get('embed_model_version') == _meta.get('embed_model_version')):
                    skipped_ids.add(_doc_id)
            if skipped_ids:
                print(f"    Incremental: skipping {len(skipped_ids)}/{len(prepared)} docs (unchanged mtime)")
        except Exception as _e:
            print(f"    WARNING: incremental skip check failed: {_e}")
            skipped_ids = set()

    # Phase 2: Batched embedding via Ollama /api/embed (falls back to serial on error)
    import time as _time
    ids = []
    documents = []
    metadatas = []
    embeddings = []

    # Only embed docs that weren't skipped above. Skipped docs stay in collection unchanged.
    to_embed = [p for p in prepared if p[0] not in skipped_ids]
    embed_texts = [p[3] for p in to_embed]
    total = len(embed_texts)
    print(f"    Embedding {total} docs (batched, cache-enabled)...")
    emb_results = get_embeddings_batch(embed_texts, prefix="passage", use_cache=True) if embed_texts else []
    skipped = sum(1 for e in emb_results if not e)
    print(f"    Embedding done: {total - skipped} succeeded, {skipped} skipped")

    for (doc_id, content, meta, _), emb in zip(to_embed, emb_results):
        if not emb:
            continue
        ids.append(doc_id)
        documents.append(content)
        metadatas.append(meta)
        embeddings.append(emb)

    # Upsert in batches of 20
    BATCH = 20
    for start in range(0, len(ids), BATCH):
        end = min(start + BATCH, len(ids))
        chroma_api("POST", f"/api/v2/tenants/default_tenant/databases/default_database/collections/{col_id}/upsert", {
            "ids": ids[start:end],
            "embeddings": embeddings[start:end],
            "documents": documents[start:end],
            "metadatas": metadatas[start:end],
        })
        print(f"    Batch {start//BATCH + 1}: upserted {end - start} chunks")

    # Phase 3: Delete stale docs — IDs in collection but not in current upsert set
    # Skip when running a targeted/partial reindex to avoid deleting docs from other sources
    if skip_stale_cleanup:
        _kept = len(ids) + len(skipped_ids)
        print(f"    Total: {_kept} chunks in '{collection_name}' (stale cleanup skipped, {len(skipped_ids)} reused)")
        return _kept
    # Include incrementally-skipped IDs so they aren't treated as stale and deleted.
    upserted_ids = set(ids) | skipped_ids
    try:
        # Get actual collection count to avoid unbounded 1M limit (scales to 100K+)
        count_resp = chroma_api("GET", f"/api/v2/tenants/default_tenant/databases/default_database/collections/{col_id}/count")
        total_count = int(count_resp) if isinstance(count_resp, (int, str)) else 100000
        resp = chroma_api("POST", f"/api/v2/tenants/default_tenant/databases/default_database/collections/{col_id}/get", {
            "limit": max(total_count, len(ids)),
            "include": [],
        })
        existing_ids = set(resp.get("ids", []))
        stale_ids = list(existing_ids - upserted_ids)
        if stale_ids:
            # Delete in batches
            for start in range(0, len(stale_ids), BATCH):
                end = min(start + BATCH, len(stale_ids))
                chroma_api("POST", f"/api/v2/tenants/default_tenant/databases/default_database/collections/{col_id}/delete", {
                    "ids": stale_ids[start:end],
                })
            print(f"    Cleaned {len(stale_ids)} stale docs from '{collection_name}'")
    except Exception as e:
        print(f"    WARNING: Stale cleanup failed: {e}")

    _total_kept = len(ids) + len(skipped_ids)
    print(f"    Total: {_total_kept} chunks in '{collection_name}' ({len(ids)} embedded, {len(skipped_ids)} reused)")
    return _total_kept

# ── Data Sources ────────────────────────────────────────
def collect_knowledge():
    """Collect infrastructure config files."""
    docs = []
    server_dir = BRAIN_HOME

    # Docker compose files
    for dc in server_dir.glob('*/docker-compose.yml'):
        if dc.parent.name.startswith('.'):
            continue
        text = dc.read_text()
        chunks = chunk_docker_compose(text, str(dc))
        for chunk in chunks:
            docs.append({
                'content': chunk['content'],
                'source': str(dc),
                'type': 'docker-compose',
                'service': chunk.get('service', dc.parent.name),
                'agent': '',
            })

    # Nginx confs
    nginx_dir = server_dir / 'nginx/conf.d'
    if nginx_dir.exists():
        for conf in nginx_dir.glob('*.conf'):
            text = conf.read_text()
            chunks = chunk_nginx_conf(text, str(conf))
            for chunk in chunks:
                docs.append({
                    'content': chunk['content'],
                    'source': str(conf),
                    'type': 'nginx-conf',
                    'service': chunk.get('service', conf.stem),
                    'agent': '',
                })

    # Agent config files (only active agents — skip inactive/legacy workspaces)
    # SHARED_FILES are identical across workspaces — only index from the first found
    ACTIVE_AGENTS = {'liz', 'ellie', 'jenna', 'sage', 'claude', 'market'}
    SHARED_FILES = {'SOUL.md'}
    PER_AGENT_FILES = {'MEMORY.md', 'IDENTITY.md', 'AGENTS.md', 'TOOLS.md'}
    shared_indexed = set()  # track which shared files we already indexed
    agents_dir = OPENCLAW_DIR
    for agent_dir in sorted((agents_dir / 'agents').iterdir()):
        if not agent_dir.is_dir():
            continue
        agent_name = agent_dir.name
        if agent_name not in ACTIVE_AGENTS:
            continue
        ws_dir = agents_dir / f'workspace-{agent_name}'
        if not ws_dir.exists():
            continue
        for md_file in sorted(SHARED_FILES | PER_AGENT_FILES):
            # Shared files: only index once (first workspace alphabetically)
            if md_file in SHARED_FILES and md_file in shared_indexed:
                continue
            f = ws_dir / md_file
            if f.exists():
                text = f.read_text()
                chunks = chunk_markdown(text, str(f))
                for chunk in chunks:
                    docs.append({
                        'content': chunk['content'],
                        'source': str(f),
                        'type': 'agent-config',
                        'service': '',
                        'agent': agent_name if md_file in PER_AGENT_FILES else 'shared',
                        'section': chunk.get('section', ''),
                    })
                if md_file in SHARED_FILES:
                    shared_indexed.add(md_file)

    return docs

def collect_experience():
    """Collect learnings, raw inbox records (browser/shell), and canonical notes."""
    docs = []
    ACTIVE_AGENTS = {'liz', 'ellie', 'jenna', 'sage', 'claude', 'market'}
    agents_dir = OPENCLAW_DIR

    # 1. Agent .learnings files (original source)
    for agent_dir in (agents_dir / 'agents').iterdir():
        if not agent_dir.is_dir():
            continue
        agent_name = agent_dir.name
        if agent_name not in ACTIVE_AGENTS:
            continue
        ws_dir = agents_dir / f'workspace-{agent_name}'
        if not ws_dir.exists():
            continue
        learnings_dir = ws_dir / '.learnings'
        if learnings_dir.exists():
            for f in learnings_dir.glob('*.md'):
                text = f.read_text()
                if len(text.strip()) < 200:
                    continue  # skip near-empty template files
                chunks = enforce_max_chunk_size(chunk_learnings(text, str(f)))
                for chunk in chunks:
                    docs.append({
                        'content': chunk['content'],
                        'source': str(f),
                        'type': 'learning',
                        'service': '',
                        'agent': agent_name,
                        'section': chunk.get('section', ''),
                    })

    # 2. Raw inbox records (all agent-distilled records from ingest pipeline)
    INBOX_TYPES = {'browser', 'shell', 'git_activity', 'screen_time',
                   'openclaw_session', 'claude_code_session'}
    HEADER_MAP = {
        'browser': lambda r: f"Browser research: {r.get('source_ref', '')}",
        'shell': lambda r: "Shell session activity",
        'git_activity': lambda r: f"Git activity: {r.get('source_ref', '')}",
        'screen_time': lambda r: f"Screen time pattern: {r.get('source_ref', '')}",
        'openclaw_session': lambda r: f"OpenClaw agent session: {r.get('source_ref', '')}",
        'claude_code_session': lambda r: f"Claude Code session: {r.get('source_ref', '')}",
    }
    inbox_dir = BRAIN_HOME / 'knowledge' / 'raw' / 'inbox'
    if inbox_dir.exists():
        for f in inbox_dir.glob('*.json'):
            try:
                record = json.loads(f.read_text())
            except Exception:
                continue
            source_type = record.get('source_type', '')
            if source_type not in INBOX_TYPES:
                continue
            content = record.get('content', '').strip()
            if len(content) < 50:
                continue
            header_fn = HEADER_MAP.get(source_type, lambda r: source_type)
            full_content = f"{header_fn(record)}\n\n{content}"
            event_time = record.get('timestamp', '')
            if len(full_content) > MAX_CHUNK_SIZE:
                sub_chunks = chunk_text(full_content)
                for sc in sub_chunks:
                    docs.append({
                        'content': sc['content'],
                        'source': str(f),
                        'type': f'raw-{source_type}',
                        'service': '',
                        'agent': record.get('actor', ''),
                        'section': sc.get('section', ''),
                        'event_time': event_time,
                    })
            else:
                docs.append({
                    'content': full_content,
                    'source': str(f),
                    'type': f'raw-{source_type}',
                    'service': '',
                    'agent': record.get('actor', ''),
                    'section': '',
                    'event_time': event_time,
                })

    return docs


def collect_canonical():
    """Collect canonical + distilled notes for dedicated vector search."""
    docs = []
    knowledge_dir = BRAIN_HOME / 'knowledge'
    canonical_stems = set()
    for subdir in ('canonical', 'distilled'):
        notes_dir = knowledge_dir / subdir
        if not notes_dir.exists():
            continue
        for md_file in notes_dir.rglob('*.md'):
            if subdir == 'distilled' and md_file.stem in canonical_stems:
                continue
            try:
                text = md_file.read_text(errors='replace')
            except Exception:
                continue
            if len(text.strip()) < 100:
                continue
            chunks = enforce_max_chunk_size(chunk_markdown(text, str(md_file)))
            for chunk in chunks:
                docs.append({
                    'content': chunk['content'],
                    'source': str(md_file),
                    'type': f'{subdir}-note',
                    'service': '',
                    'agent': '',
                    'section': chunk.get('section', ''),
                })
            if subdir == 'canonical':
                canonical_stems.add(md_file.stem)
    return docs

def collect_context():
    """Collect recent session summaries from memory files."""
    docs = []
    ACTIVE_AGENTS = {'liz', 'ellie', 'jenna', 'sage', 'claude', 'market'}
    agents_dir = OPENCLAW_DIR

    for agent_dir in (agents_dir / 'agents').iterdir():
        if not agent_dir.is_dir():
            continue
        agent_name = agent_dir.name
        if agent_name not in ACTIVE_AGENTS:
            continue
        ws_dir = agents_dir / f'workspace-{agent_name}'
        if not ws_dir.exists():
            continue
        memory_dir = ws_dir / 'memory'
        if memory_dir.exists():
            for f in sorted(memory_dir.glob('*.md'), reverse=True)[:30]:  # last 30 files
                text = f.read_text()
                if len(text.strip()) < 50:
                    continue
                chunks = enforce_max_chunk_size(chunk_markdown(text, str(f)))
                for chunk in chunks:
                    docs.append({
                        'content': chunk['content'],
                        'source': str(f),
                        'type': 'session-memory',
                        'service': '',
                        'agent': agent_name,
                        'section': chunk.get('section', ''),
                    })

    return docs

def collect_obsidian():
    """Collect markdown notes from local Obsidian vault mirror."""
    docs = []
    try:
        from config import OBSIDIAN_VAULT_ICLOUD
    except ImportError:
        OBSIDIAN_VAULT_ICLOUD = Path.home() / "Library" / "Mobile Documents" / "iCloud~md~obsidian" / "Documents" / "Obsidian-vault"
    vault_dir = OBSIDIAN_VAULT_ICLOUD
    if not vault_dir.exists():
        return docs

    for md_file in vault_dir.rglob('*.md'):
        # Skip hidden files/dirs
        if any(part.startswith('.') for part in md_file.relative_to(vault_dir).parts):
            continue
        try:
            text = md_file.read_text(errors='replace')
        except Exception:
            continue
        if len(text.strip()) < 50:
            continue

        rel_parts = md_file.relative_to(vault_dir).parts
        vault_subdir = rel_parts[0] if len(rel_parts) > 1 else ''

        chunks = enforce_max_chunk_size(chunk_markdown(text, str(md_file)))
        for chunk in chunks:
            docs.append({
                'content': chunk['content'],
                'source': str(md_file),
                'type': 'obsidian-note',
                'service': '',
                'agent': '',
                'section': chunk.get('section', ''),
                'vault_subdir': vault_subdir,
            })

    return docs

# ── Main ────────────────────────────────────────────────
CHECKPOINT_FILE = Path("/tmp/.reindex-checkpoint.json")

def _wait_for_services(timeout=120):
    """Wait for ChromaDB + Ollama to be healthy."""
    import time as _t
    for _ in range(timeout // 2):
        try:
            chroma_api("GET", "/api/v2/tenants/default_tenant/databases/default_database/collections")
            get_embedding("health check")
            return True
        except Exception:
            _t.sleep(2)
    return False

def _load_checkpoint() -> set:
    if CHECKPOINT_FILE.exists():
        return set(json.loads(CHECKPOINT_FILE.read_text()))
    return set()

def _save_checkpoint(done: set):
    CHECKPOINT_FILE.write_text(json.dumps(sorted(done)))

if __name__ == '__main__':
    import argparse, sys as _sys
    _sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

    parser = argparse.ArgumentParser()
    parser.add_argument('--collection', help='Index only this collection')
    parser.add_argument('--fresh', action='store_true', help='Ignore checkpoint, re-index everything')
    args = parser.parse_args()

    print("=" * 60)
    print("RAG Indexer — Phase 2")
    print("=" * 60)

    ALL_COLLECTIONS = ["knowledge", "experience", "context", "semantic_memory",
                       "obsidian", "canonical", "personal"]

    print("\n[setup] Ensuring collections...")
    for col in ALL_COLLECTIONS:
        ensure_collection(col)

    done = set() if args.fresh else _load_checkpoint()
    if done:
        print(f"  Resuming — already done: {', '.join(sorted(done))}")

    STEPS = [
        ("knowledge",  "configs, agent files",       collect_knowledge),
        ("experience", "learnings + raw inbox",      collect_experience),
        ("canonical",  "canonical + distilled notes", collect_canonical),
        ("context",    "session memories",            collect_context),
        ("obsidian",   "Obsidian vault",              collect_obsidian),
    ]

    counts = {}
    for name, label, collector in STEPS:
        if args.collection and args.collection != name:
            continue
        if name in done:
            print(f"\n[skip] {name} (already checkpointed)")
            continue

        # Health check before each collection
        print(f"\n[index] {name} ({label})...")
        if not _wait_for_services(30):
            print(f"  ERROR: ChromaDB/Ollama not ready. Saving checkpoint.")
            _save_checkpoint(done)
            _sys.exit(1)

        docs = collector()
        print(f"  Found {len(docs)} chunks")
        partial = bool(args.collection)
        counts[name] = add_documents(name, docs, skip_stale_cleanup=partial)
        done.add(name)
        _save_checkpoint(done)
        print(f"  Checkpointed {name} ({counts[name]} chunks)")

    # semantic_memory is managed by memory_store.py
    if not args.collection or args.collection == "semantic_memory":
        print("\n[skip] semantic_memory (managed by memory_store.py)")

    # Cleanup checkpoint on full success
    if not args.collection:
        CHECKPOINT_FILE.unlink(missing_ok=True)

    total = sum(counts.values())
    parts = ", ".join(f"{k}: {v}" for k, v in counts.items())
    print(f"\n{'=' * 60}")
    print(f"DONE — {parts}")
    print(f"Total: {total} chunks indexed")
    print("=" * 60)
