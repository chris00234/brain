#!/opt/homebrew/bin/python3
"""RAG Indexer — Phase 2: Initial data indexing into ChromaDB via Ollama embeddings."""

import hashlib
import json
import logging
import os
import re
from datetime import UTC, datetime
from pathlib import Path

log = logging.getLogger("brain.indexer")

# ── Config ──────────────────────────────────────────────
# Direct host-port access (chromadb + ollama expose 127.0.0.1:8000 and 11434
# via docker-compose). Eliminates the ~50-100ms subprocess penalty per query
# that the old `docker exec nginx curl ...` path incurred.
try:
    from config import BRAIN_HOME, CHROMA_URL, EMBED_MODEL, EMBED_MODEL_VERSION, OLLAMA_URL, OPENCLAW_DIR
except ImportError:
    CHROMA_URL = "http://127.0.0.1:8000"
    OLLAMA_URL = "http://127.0.0.1:11434"
    EMBED_MODEL = "blaifa/multilingual-e5-large-instruct"
    EMBED_MODEL_VERSION = "multilingual-e5-large-instruct:v1"
    OPENCLAW_DIR = Path("/Users/chrischo/.openclaw")
    BRAIN_HOME = Path("/Users/chrischo/server")


import sys as _sys

_sys.path.insert(0, str(Path(__file__).parent))
from http_pool import http_json as _http_json


def chroma_api(method, path, data=None):
    """Call ChromaDB directly via localhost HTTP with keep-alive."""
    return _http_json(method, f"{CHROMA_URL}{path}", payload=data, timeout=120)


# ── Embedding Cache ────────────────────────────────────
# Shared SQLite cache: md5(embed_text) → embedding vector.
try:
    from embed_cache import cache_get as _cache_get
    from embed_cache import cache_put as _cache_put
except ImportError:
    import sqlite3 as _sqlite3

    _EMBED_CACHE_PATH = Path("/Users/chrischo/server/brain/logs/embedding_cache.db")
    _embed_cache_conn = None

    def _embed_cache():
        global _embed_cache_conn
        if _embed_cache_conn is None:
            _embed_cache_conn = _sqlite3.connect(str(_EMBED_CACHE_PATH), timeout=10)
            _embed_cache_conn.execute("PRAGMA journal_mode=WAL")
            _embed_cache_conn.execute(
                "CREATE TABLE IF NOT EXISTS embeddings (hash TEXT PRIMARY KEY, embedding BLOB)"
            )
        return _embed_cache_conn

    def _cache_get(text_hash):
        try:
            row = (
                _embed_cache()
                .execute("SELECT embedding FROM embeddings WHERE hash=?", (text_hash,))
                .fetchone()
            )
            if row:
                return json.loads(row[0])
        except Exception:
            pass
        return None

    def _cache_put(text_hash, embedding):
        try:
            _embed_cache().execute(
                "INSERT OR REPLACE INTO embeddings (hash, embedding) VALUES (?, ?)",
                (text_hash, json.dumps(embedding)),
            )
            _embed_cache().commit()
        except Exception:
            pass


_lora_embedder = None  # (adapter_path: str, st_model: SentenceTransformer) when active
_lora_lock = None


def _lock():
    global _lora_lock
    if _lora_lock is None:
        import threading

        _lora_lock = threading.Lock()
    return _lora_lock


def set_lora_adapter(adapter_path: str | None) -> dict:
    """Load (or unload) a LoRA adapter over the base e5 model for embeddings.

    The adapter was saved via SentenceTransformer.get_adapter_state_dict(),
    whose keys are scoped to the ST model's internal structure
    ('base_model.model.encoder...'). Loading must match that — using
    PeftModel.from_pretrained on the raw auto_model has a key mismatch
    and silently loads zero weights (warn: "Found missing adapter keys").

    Correct flow: build SentenceTransformer → add_adapter with the saved
    config → manually load_state_dict from adapter_model.safetensors.
    """
    global _lora_embedder
    with _lock():
        if adapter_path is None:
            _lora_embedder = None
            return {"status": "cleared"}
        if _lora_embedder and _lora_embedder[0] == adapter_path:
            return {"status": "unchanged", "adapter": adapter_path}
        try:
            import torch
            from peft import LoraConfig
            from safetensors.torch import load_file
            from sentence_transformers import SentenceTransformer
        except ImportError as e:
            return {"status": "error", "reason": f"deps missing: {e}"}
        adapter_dir = Path(adapter_path)
        base_model_path = adapter_dir / "base_model.txt"
        base_name = (
            base_model_path.read_text().strip()
            if base_model_path.exists()
            else "intfloat/multilingual-e5-large-instruct"
        )
        cfg_path = adapter_dir / "adapter_config.json"
        weights_path = adapter_dir / "adapter_model.safetensors"
        if not cfg_path.exists() or not weights_path.exists():
            return {"status": "error", "reason": f"missing adapter files in {adapter_dir}"}
        try:
            model = SentenceTransformer(base_name, model_kwargs={"torch_dtype": torch.float32})
            cfg_dict = json.loads(cfg_path.read_text())
            lora_config = LoraConfig(
                **{
                    k: v
                    for k, v in cfg_dict.items()
                    if k
                    in {
                        "r",
                        "lora_alpha",
                        "target_modules",
                        "lora_dropout",
                        "bias",
                        "task_type",
                        "fan_in_fan_out",
                        "init_lora_weights",
                        "layers_to_transform",
                        "layers_pattern",
                        "rank_pattern",
                        "alpha_pattern",
                        "megatron_config",
                        "megatron_core",
                        "exclude_modules",
                        "inference_mode",
                    }
                }
            )
            # add_adapter matches how brain_finetune.py saved; state_dict keys align
            model.add_adapter(lora_config)
            raw_state = load_file(str(weights_path))
            # Key remap: brain_finetune.py saved via get_adapter_state_dict()
            # which produces RAW transformer keys ('encoder.layer.N.attn...').
            # Loading back into the wrapped ST model needs '0.model.' prefix
            # (Sequential position 0 = Transformer, .model = auto_model) and
            # '.default' adapter-name segment before '.weight'.
            state_dict: dict = {}
            for k, v in raw_state.items():
                # Insert .default before final .weight
                if k.endswith(".weight"):
                    new_key = "0.model." + k[: -len(".weight")] + ".default.weight"
                else:
                    new_key = "0.model." + k
                state_dict[new_key] = v
            missing, unexpected = model.load_state_dict(state_dict, strict=False)
            real_missing = [k for k in missing if "lora_" in k]
            if real_missing:
                return {
                    "status": "error",
                    "reason": f"adapter keys not matched: {len(real_missing)} missing (first: {real_missing[0]})",
                }
            model.eval()
            _lora_embedder = (adapter_path, model)
            return {
                "status": "loaded",
                "adapter": adapter_path,
                "base": base_name,
                "adapter_keys_loaded": len(state_dict),
                "unexpected_keys": len(unexpected),
            }
        except Exception as e:
            return {"status": "error", "reason": f"adapter load failed: {str(e)[:400]}"}


def _embed_via_lora(text: str, prefix: str) -> list[float] | None:
    """In-process embedding through the currently-loaded LoRA adapter."""
    global _lora_embedder
    if _lora_embedder is None:
        return None
    _, model = _lora_embedder
    prompted = f"{prefix}: {text[:1000]}" if prefix else text[:1000]
    try:
        vec = model.encode([prompted], normalize_embeddings=True, show_progress_bar=False)
        return vec[0].tolist()
    except Exception:
        return None


def get_embedding(text, _retries=5, use_cache=True, prefix="passage"):
    """Get embedding. Uses LoRA adapter in-process when active, else Ollama HTTP.

    When a LoRA adapter is loaded via set_lora_adapter(path), the cache key
    incorporates the adapter path so base-embedder and adapter-embedder
    vectors don't collide.
    """
    import time as _time

    max_chars = 1000
    truncated = text[:max_chars]
    prompted = f"{prefix}: {truncated}" if prefix else truncated

    # LoRA-aware cache key: mix adapter path into the hash so the same text
    # gets a different cache entry under each adapter.
    adapter_marker = ""
    if _lora_embedder is not None:
        adapter_marker = f"|lora:{_lora_embedder[0]}"

    if use_cache:
        text_hash = hashlib.md5(f"{EMBED_MODEL}{adapter_marker}:{prompted}".encode()).hexdigest()
        cached = _cache_get(text_hash)
        if cached:
            return cached

    # LoRA path: in-process sentence-transformers
    if _lora_embedder is not None:
        emb = _embed_via_lora(text, prefix)
        if emb:
            if use_cache:
                _cache_put(text_hash, emb)
            return emb
        # Fall through to Ollama on any LoRA failure — degrade rather than crash

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
                    text_hash = hashlib.md5(f"{EMBED_MODEL}:{prompted}".encode()).hexdigest()
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
                    text_hash = hashlib.md5(f"{EMBED_MODEL}:{prompted}".encode()).hexdigest()
                last_err = f"context overflow (4xx), retrying at {max_chars} chars"
                continue
            last_err = str(e)
        if attempt < _retries - 1:
            wait = 2 * (attempt + 1)
            _time.sleep(wait)
    raise RuntimeError(f"Ollama failed after {_retries} retries for text[:50]={text[:50]!r}: {last_err}")


def get_embeddings_batch(
    texts: list[str], prefix: str = "passage", batch_size: int = 50, use_cache: bool = True
) -> list[list[float]]:
    """Batch embed multiple texts via Ollama /api/embed. Falls back to serial on error.

    Returns embeddings in the same order as input texts.
    """
    if not texts:
        return []

    prompted = [f"{prefix}: {(t or '')[:1000]}" if prefix else (t or "")[:1000] for t in texts]
    embeddings: list[list[float]] = []

    # Check cache first for each text
    cache_hits: dict[int, list[float]] = {}
    to_embed: list[tuple[int, str]] = []
    if use_cache:
        for i, p in enumerate(prompted):
            text_hash = hashlib.md5(f"{EMBED_MODEL}:{p}".encode()).hexdigest()
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
        batch = to_embed[batch_start : batch_start + batch_size]
        batch_indices = [i for i, _ in batch]
        batch_texts = [t for _, t in batch]

        try:
            payload = {"model": EMBED_MODEL, "input": batch_texts}
            data = _http_json("POST", f"{OLLAMA_URL}/api/embed", payload=payload, timeout=120)
            batch_embs = data.get("embeddings") or []

            if len(batch_embs) == len(batch_texts):
                for idx, emb in zip(batch_indices, batch_embs, strict=False):
                    batch_results[idx] = emb
                    if use_cache:
                        text_hash = hashlib.md5(f"{EMBED_MODEL}:{prompted[idx]}".encode()).hexdigest()
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
    re.compile(r"(?i)(api.?key|token|secret|password|passwd)\s*[:=]\s*\S+"),
    re.compile(r"(?i)CLOUDFLARE_API_TOKEN\S*"),
    re.compile(r"ghp_[A-Za-z0-9]{36}"),
    re.compile(r"sk-[A-Za-z0-9]{48}"),
    re.compile(r"[A-Za-z0-9+/]{60,}={0,2}"),  # long base64
    re.compile(r"(?i)(ssh|rsa|ed25519).?key"),
]
SKIP_FILES = {".env", ".htpasswd", ".htpasswd_tesla"}


def filter_secrets(text):
    for pat in SECRET_PATTERNS:
        text = pat.sub("[REDACTED]", text)
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
        return [{"content": text, "section": "full"}]
    chunks = []
    paragraphs = re.split(r"\n\n+", text)
    # Split long paragraphs at sentence boundaries
    expanded = []
    for para in paragraphs:
        if len(para) <= max_size:
            expanded.append(para)
        else:
            sentences = re.split(r"(?<=[.!?])\s+", para)
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
            chunks.append({"content": current.strip(), "section": f"part {len(chunks)+1}"})
            overlap_text = current[-overlap:] if overlap else ""
            current = overlap_text + "\n\n" + para if overlap_text else para
            # If overlap pushed us over, drop it
            if len(current) > max_size:
                current = para
        else:
            current = (current + "\n\n" + para).strip() if current else para
    if current.strip():
        chunks.append({"content": current.strip(), "section": f"part {len(chunks)+1}"})
    return chunks


def enforce_max_chunk_size(chunks, max_size=MAX_CHUNK_SIZE, overlap=CHUNK_OVERLAP):
    """Post-process: recursively split oversized chunks.

    Propagates the parent section heading into every sub-part's content so the
    vector representation includes the topic anchor. Previously, parts 2+ lost
    the header from the embed text (it was only kept in metadata as a string),
    which made them un-retrievable for queries that only matched the header.
    Bug fix 2026-04-12: ~54% of `context` collection was affected.
    """
    result = []
    for chunk in chunks:
        if len(chunk["content"]) <= max_size:
            result.append(chunk)
        else:
            parent_section = (chunk.get("section") or "").strip()
            subs = chunk_text(chunk["content"], max_size, overlap)
            for i, sub in enumerate(subs):
                new = dict(chunk)
                # For parts 2+, prepend the parent section heading so the
                # embedding captures which topic this fragment belongs to.
                # Part 1 already contains the header (it was part of the
                # original content that got sliced).
                if parent_section and i > 0:
                    # Don't duplicate if the sub-content already starts with it
                    body_head = sub["content"][:200].lower()
                    if parent_section.lower() not in body_head:
                        new["content"] = f"## {parent_section}\n\n{sub['content']}"
                    else:
                        new["content"] = sub["content"]
                else:
                    new["content"] = sub["content"]
                new["section"] = f"{parent_section} (part {i+1})" if parent_section else f"part {i+1}"
                result.append(new)
    return result


def chunk_docker_compose(text, source):
    """Chunk by service blocks using a real YAML parser.

    Previously used a 2-space-indent regex which missed Docker Compose v3.9+
    specs with 4-space indent and produced `service='unknown'` chunks for any
    file with mixed indentation. Now parses the YAML structurally.
    Bug fix 2026-04-12.
    """
    try:
        import yaml
    except ImportError:
        yaml = None

    if yaml is not None:
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError:
            data = None
        if isinstance(data, dict):
            chunks = []
            services = data.get("services") or {}
            if isinstance(services, dict):
                for service_name, service_config in services.items():
                    try:
                        body = yaml.dump(
                            {service_name: service_config},
                            default_flow_style=False,
                            allow_unicode=True,
                            sort_keys=False,
                        )
                    except Exception:
                        body = f"{service_name}: {service_config}"
                    chunks.append({"content": body, "service": service_name})

            # Also emit top-level blocks (networks/volumes/secrets) so queries
            # like "which network do services share" can find them.
            for top_key in ("networks", "volumes", "secrets", "configs"):
                if data.get(top_key):
                    try:
                        body = yaml.dump(
                            {top_key: data[top_key]},
                            default_flow_style=False,
                            allow_unicode=True,
                            sort_keys=False,
                        )
                    except Exception:
                        body = f"{top_key}: {data[top_key]}"
                    chunks.append({"content": body, "service": f"__{top_key}__"})

            if chunks:
                return chunks

    # Fallback: legacy regex chunker (kept for safety on non-parseable files)
    chunks = []
    lines = text.split("\n")
    current = []
    service_name = None

    for line in lines:
        if re.match(r"^  \w+:", line) and not line.strip().startswith("#"):
            if current and service_name:
                chunks.append(
                    {
                        "content": "\n".join(current),
                        "service": service_name,
                    }
                )
            service_name = line.strip().rstrip(":")
            current = [line]
        else:
            current.append(line)

    if current and service_name:
        chunks.append({"content": "\n".join(current), "service": service_name})

    if not chunks:
        chunks = [{"content": text, "service": "unknown"}]

    return chunks


def _nginx_split_blocks(text):
    """Parse nginx config into top-level blocks using brace matching.

    Yields (directive_line, full_block_text) for every top-level `directive { ... }`
    construct. Skips file-level comments and simple `directive ...;` directives
    that don't have a body. Handles nested braces and comments inside blocks.
    """
    blocks = []
    i = 0
    n = len(text)
    while i < n:
        # Skip whitespace
        while i < n and text[i] in " \t\r\n":
            i += 1
        if i >= n:
            break
        # Skip full-line comments
        if text[i] == "#":
            while i < n and text[i] != "\n":
                i += 1
            continue
        # Read directive up to { or ;
        directive_start = i
        while i < n and text[i] not in "{;":
            if text[i] == "#":
                while i < n and text[i] != "\n":
                    i += 1
                continue
            i += 1
        if i >= n:
            break
        directive = text[directive_start:i].strip()
        if text[i] == ";":
            # top-level simple directive (include, user, events outside brace) — skip
            i += 1
            continue
        # text[i] == '{' — find matching closing brace
        i += 1
        depth = 1
        while i < n and depth > 0:
            ch = text[i]
            if ch == "#":
                while i < n and text[i] != "\n":
                    i += 1
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
            i += 1
        body_end = i  # just past the closing }
        full = text[directive_start:body_end].strip()
        if full:
            blocks.append((directive, full))
    return blocks


def chunk_nginx_conf(text, source):
    """Chunk nginx config into one chunk per top-level block.

    Uses a brace-matching state machine to split on `server`, `map`, `upstream`,
    and other top-level blocks. Previously used a naive `server {` regex which:
      - Concatenated multi-hostname `server_name a b c;` into one label
      - Captured file-level comments as a fake `unknown` chunk
      - Couldn't see `map`/`upstream` blocks as their own units
      - Failed on default servers without `server_name`
    Bug fix 2026-04-12.
    """
    blocks = _nginx_split_blocks(text)
    chunks = []
    for directive, block_text in blocks:
        dtype = directive.split()[0] if directive else ""

        if dtype == "server":
            # Extract primary hostname from server_name directive; use first
            # name only so filter queries like `?collection=nginx&service=<host>`
            # match predictably.
            m = re.search(r"server_name\s+([^;]+);", block_text)
            if m:
                names = m.group(1).split()
                service = names[0] if names else "default-server"
            else:
                service = "default-server"
            chunks.append({"content": block_text, "service": service})

        elif dtype == "map":
            # map $source $result { ... }
            m = re.match(r"map\s+\S+\s+(\S+)", directive)
            service = f"map_{m.group(1)}" if m else "map"
            chunks.append({"content": block_text, "service": service})

        elif dtype == "upstream":
            # upstream <name> { ... }
            m = re.match(r"upstream\s+(\S+)", directive)
            service = f"upstream_{m.group(1)}" if m else "upstream"
            chunks.append({"content": block_text, "service": service})

        elif dtype in ("http", "events", "stream", "mail"):
            # Rare at /etc/nginx/conf.d/ level but handle gracefully
            chunks.append({"content": block_text, "service": f"{dtype}_global"})

        else:
            # Unknown top-level block — label by its directive type
            chunks.append({"content": block_text, "service": dtype or "unknown"})

    if not chunks:
        # File has no top-level blocks at all (pure comments, or odd format).
        # Return the whole file as a fallback chunk keyed by filename.
        from pathlib import Path as _P

        fname = _P(source).stem if source else "unknown"
        chunks = [{"content": text.strip(), "service": fname}]

    return chunks


def _strip_frontmatter(text):
    """Strip YAML/JSON frontmatter block from the top of a markdown doc.

    Handles both `---\n...\n---` and `---json\n...\n---` forms. Previously,
    `chunk_markdown` would emit frontmatter as its own `intro` chunk which
    polluted retrieval with JSON metadata vectors (~32% of `canonical`).
    Bug fix 2026-04-12.
    """
    stripped = text.lstrip()
    if not stripped.startswith("---"):
        return text
    m = re.match(r"^---(?:json)?\s*\n.*?\n---\s*\n", stripped, re.DOTALL)
    if m:
        return stripped[m.end() :]
    return text


def _chunk_markdown_with_lib(text):
    """Structure-aware markdown chunker using markdown_it. Returns list of
    {content, section} dicts. Falls back to regex chunker on parse failure.

    Uses the actual markdown AST so it correctly handles:
      - H1-H6 headings (not just H1-H3)
      - Collapsed single-line markdown (the regex chunker's main bug)
      - Setext headings (underline-style)
      - Code fences, lists, blockquotes — kept inside their parent section
    """
    try:
        from markdown_it import MarkdownIt
    except ImportError:
        return None  # fall back to regex

    md = MarkdownIt("commonmark")
    tokens = md.parse(text)

    chunks = []
    current_heading = "intro"
    current_heading_line = ""
    current_body = []

    def _flush():
        if not current_body:
            return
        body_text = "\n".join(current_body).strip()
        if len(body_text) < 50:  # skip tiny sections
            return
        content = f"{current_heading_line}\n\n{body_text}" if current_heading_line else body_text
        chunks.append({"content": content, "section": current_heading})

    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok.type == "heading_open":
            _flush()
            level = int(tok.tag.lstrip("h") or "1")
            # The next token is an `inline` with the heading text
            inline = tokens[i + 1] if i + 1 < len(tokens) else None
            heading_text = inline.content.strip() if inline and inline.type == "inline" else ""
            current_heading = heading_text
            current_heading_line = f"{'#' * level} {heading_text}"
            current_body = []
            i += 3  # heading_open, inline, heading_close
            continue
        if tok.type == "inline":
            current_body.append(tok.content)
        elif tok.type == "fence":
            info = tok.info or ""
            current_body.append(f"```{info}\n{tok.content}```")
        elif tok.type == "code_block":
            current_body.append(tok.content.rstrip())
        elif tok.type == "paragraph_close":
            current_body.append("")  # blank line between paragraphs
        elif tok.type == "bullet_list_close" or tok.type == "ordered_list_close":
            current_body.append("")
        i += 1

    _flush()
    return chunks


def _merge_small_sections(chunks, target_size=700, max_size=MAX_CHUNK_SIZE):
    """Merge consecutive small chunks into denser ones.

    When a markdown doc has many short H2/H3 sections, the default chunker
    emits each as its own chunk, leaving them thinly-populated. The eval's
    ``hit_content@5`` substring match then often fails because the expected
    text lands in a tiny chunk that doesn't win top-5.

    Merge strategy: walk the chunk list, accumulating content until we hit
    ``target_size`` or would exceed ``max_size``. On overflow, flush and
    start a new chunk. Single chunks bigger than target_size pass through
    unchanged (``enforce_max_chunk_size`` handles too-big ones downstream).
    """
    if not chunks:
        return chunks
    merged = []
    buf_content: list[str] = []
    buf_section: str = ""
    buf_len = 0
    for c in chunks:
        content = c.get("content", "")
        if not content:
            continue
        # Flush if adding this would overflow max_size
        if buf_len + len(content) + 2 > max_size and buf_content:
            merged.append({"content": "\n\n".join(buf_content), "section": buf_section})
            buf_content = []
            buf_section = ""
            buf_len = 0
        buf_content.append(content)
        if not buf_section:
            buf_section = c.get("section", "")
        else:
            buf_section = f"{buf_section} + {c.get('section', '')}"
        buf_len += len(content) + 2
        # Flush on reaching target size
        if buf_len >= target_size:
            merged.append({"content": "\n\n".join(buf_content), "section": buf_section})
            buf_content = []
            buf_section = ""
            buf_len = 0
    if buf_content:
        merged.append({"content": "\n\n".join(buf_content), "section": buf_section})
    return merged


def chunk_markdown(text, source):
    """Chunk a markdown document by headings with structural awareness.

    Pipeline:
      1. Strip YAML/JSON frontmatter (don't emit it as its own chunk)
      2. Try markdown_it token-stream walk (handles collapsed / Setext / H4+)
      3. Fall back to regex split if markdown_it isn't available or fails
      4. Merge consecutive small sections so embed/retrieve windows have
         enough content for the eval substring match (2026-04-13 fix)
    """
    text = _strip_frontmatter(text)

    # Try the structure-aware chunker first
    lib_chunks = _chunk_markdown_with_lib(text)
    if lib_chunks is not None and lib_chunks:
        return lib_chunks

    # Fallback: regex-based split (legacy behavior)
    sections = re.split(r"^(#{1,3}\s+.+)$", text, flags=re.MULTILINE)
    chunks = []
    current_heading = "intro"
    current_content = []

    for part in sections:
        if re.match(r"^#{1,3}\s+", part):
            if current_content:
                content = "\n".join(current_content).strip()
                if len(content) > 50:  # skip tiny sections
                    chunks.append({"content": content, "section": current_heading})
            current_heading = part.strip("# \n")
            current_content = [part]
        else:
            current_content.append(part)

    if current_content:
        content = "\n".join(current_content).strip()
        if len(content) > 50:
            chunks.append({"content": content, "section": current_heading})

    if not chunks:
        chunks = [{"content": text, "section": "full"}]

    return chunks


def chunk_learnings(text, source):
    """Chunk by entry (date/title blocks)."""
    entries = re.split(r"\n(?=\d{4}-\d{2}-\d{2}|#{1,3}\s+)", text)
    chunks = []
    for entry in entries:
        entry = entry.strip()
        if len(entry) > 30:
            chunks.append({"content": entry, "section": entry[:80]})
    if not chunks:
        chunks = [{"content": text, "section": "full"}]
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
                    if c.get("name") == name:
                        log.debug("collection %r exists: %s", name, c["id"])
                        return c["id"]

            result = chroma_api(
                "POST",
                "/api/v2/tenants/default_tenant/databases/default_database/collections",
                {"name": name, "metadata": {"hnsw:space": "cosine"}},
            )
            log.info("collection %r created: %s", name, result.get("id", "unknown"))
            return result.get("id")
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
                if c.get("name") and c.get("id"):
                    _collection_ids[c["name"]] = c["id"]
        return _collection_ids.get(name)


INCREMENTAL_REINDEX_COLLECTIONS = frozenset({"knowledge"})


def add_documents(collection_name, docs, skip_stale_cleanup=False, force_incremental=False):
    """Add documents to a collection with content-hash dedup and optional stale cleanup.

    When `force_incremental` is True (or the collection is in
    INCREMENTAL_REINDEX_COLLECTIONS), docs whose (id, mtime, embed model)
    already match the collection are left untouched — no re-embed, no upsert,
    and stale-cleanup preserves them. Safe to enable for collections whose
    source files have trustworthy mtimes (docker-compose, nginx, AGENTS.md).
    """
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
        content_hash = hashlib.md5(doc["content"].encode()).hexdigest()
        if content_hash in seen_content:
            continue
        seen_content.add(content_hash)
        deduped_docs.append(doc)
    if len(deduped_docs) < len(docs):
        print(
            f"    Dedup: {len(docs)} → {len(deduped_docs)} ({len(docs) - len(deduped_docs)} content duplicates removed)"
        )
    docs = deduped_docs

    # Phase 1: Prepare all documents (metadata, content, embed text)
    prepared = []
    for i, doc in enumerate(docs):
        content = filter_secrets(doc["content"])
        stripped = content.strip()
        if len(stripped) < 30:
            continue
        # Skip boilerplate canonical/distilled proposal stub chunks
        if stripped.startswith("## Statement") and "Review this proposed" in stripped[:80]:
            continue
        if stripped.startswith("## Observations") and "Derived from raw evidence" in stripped[:80]:
            continue
        # 2026-04-17: skip ALL `## Source Summary` chunks regardless of length.
        # Previously only short ones were skipped, so long proposal notes leaked
        # their raw_event JSON dumps into the embedding space — these surface as
        # mid-JSON snippets in /recall/v2 content fields. The source events are
        # already indexed as raw-* collection entries, so re-embedding the dump
        # here is pure noise.
        if stripped.startswith("## Source Summary"):
            continue
        # Skip JSON-only frontmatter chunks (no searchable content)
        if stripped.startswith("---json") and len(stripped) < 200:
            continue
        # 2026-04-17: chunks that start mid-JSON are debris from long Source
        # Summary sections split after the `## Source Summary` header was in
        # a previous chunk. Target the obvious cases directly rather than a
        # noisy punctuation-density heuristic.
        if stripped.startswith(('",\n', '", "', '":', '": "', '": 0.', '": null', '": true', '": false')):
            continue

        # Build semantic header for embedding — gives short/structured chunks
        # (YAML configs, nginx server blocks) natural-language anchor text so
        # vector search can match them on intent, not just literal tokens.
        source = str(doc.get("source", ""))
        service = doc.get("service", "")
        doc_type = doc.get("type", "")
        section = doc.get("section", "")

        header_parts = []
        if doc_type == "docker-compose":
            header_parts.append(f"Docker Compose configuration for service '{service}'")
        elif doc_type == "nginx-conf":
            header_parts.append(f"Nginx reverse proxy configuration for '{service}'")
        elif doc_type == "agent-config":
            header_parts.append(f"Agent {doc.get('agent', '')} configuration: {section}")
        elif doc_type == "learning":
            header_parts.append(f"Agent {doc.get('agent', '')} learning notes")
        elif doc_type == "session-memory":
            header_parts.append(f"Agent {doc.get('agent', '')} session memory")
        elif doc_type == "obsidian-note":
            header_parts.append(f"Personal note in Obsidian vault ({doc.get('vault_subdir', '')})")
        elif doc_type in ("canonical-note", "distilled-note"):
            source_stem = Path(source).stem.replace("-", " ").replace("_", " ")
            label = "Canonical knowledge" if doc_type == "canonical-note" else "Distilled summary"
            section_suffix = f" — {section}" if section else ""
            header_parts.append(f"{label}: {source_stem}{section_suffix}")

        embed_text = ("\n".join(header_parts) + "\n\n" + content) if header_parts else content
        # ID = hash of (source + content) — avoids collisions from long path truncation
        doc_id = hashlib.md5(f"{source}:{content}".encode()).hexdigest()
        # mtime: prefer explicit, else stat source file, else empty string
        mtime_val = doc.get("mtime") or ""
        if not mtime_val and source:
            try:
                _src_path = Path(source)
                if _src_path.exists() and _src_path.is_file():
                    mtime_val = f"{_src_path.stat().st_mtime:.6f}"
            except Exception:
                mtime_val = ""
        meta = {
            "source": source,
            "type": doc_type,
            "service": service,
            "agent": doc.get("agent", ""),
            "section": section,
            "vault_subdir": doc.get("vault_subdir", ""),
            "created_at": doc.get("event_time")
            or doc.get("timestamp")
            or datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "mtime": mtime_val,
            "embed_model": EMBED_MODEL,
            "embed_model_version": EMBED_MODEL_VERSION,
        }
        prepared.append((doc_id, content, meta, embed_text))

    if not prepared:
        return 0

    # Phase 1.5: Incremental reindex — skip re-embedding docs whose mtime matches.
    # Gated behind BRAIN_INCREMENTAL_REINDEX (default off) because it changes
    # semantics: docs that mutate WITHOUT a corresponding mtime change would be
    # silently skipped. Enable only when source mtimes are trustworthy.
    skipped_ids: set[str] = set()
    incremental_on = (
        force_incremental
        or collection_name in INCREMENTAL_REINDEX_COLLECTIONS
        or os.getenv("BRAIN_INCREMENTAL_REINDEX", "").lower() in ("1", "true", "yes")
    )
    if incremental_on:
        prepared_ids = [p[0] for p in prepared]
        try:
            # Batch fetch existing metadata in chunks of 200 to keep request small
            _existing: dict[str, dict] = {}
            for _start in range(0, len(prepared_ids), 200):
                _chunk = prepared_ids[_start : _start + 200]
                _resp = chroma_api(
                    "POST",
                    f"/api/v2/tenants/default_tenant/databases/default_database/collections/{col_id}/get",
                    {"ids": _chunk, "include": ["metadatas"]},
                )
                _rids = _resp.get("ids") or []
                _rmetas = _resp.get("metadatas") or []
                for _rid, _rmeta in zip(_rids, _rmetas, strict=False):
                    _existing[_rid] = _rmeta or {}
            for _doc_id, _content, _meta, _etxt in prepared:
                _prev = _existing.get(_doc_id)
                if not _prev:
                    continue
                _prev_mtime = _prev.get("mtime") or ""
                _new_mtime = _meta.get("mtime") or ""
                # Strict equality on non-empty mtime + same embed model
                if (
                    _prev_mtime
                    and _new_mtime
                    and _prev_mtime == _new_mtime
                    and _prev.get("embed_model_version") == _meta.get("embed_model_version")
                ):
                    skipped_ids.add(_doc_id)
            if skipped_ids:
                print(f"    Incremental: skipping {len(skipped_ids)}/{len(prepared)} docs (unchanged mtime)")
        except Exception as _e:
            print(f"    WARNING: incremental skip check failed: {_e}")
            skipped_ids = set()

    # Phase 2: Batched embedding via Ollama /api/embed (falls back to serial on error)
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

    for (doc_id, content, meta, _), emb in zip(to_embed, emb_results, strict=False):
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
        print(f"    Batch {start//BATCH + 1}: upserted {end - start} chunks")

    # Phase 3: Delete stale docs — IDs in collection but not in current upsert set
    # Skip when running a targeted/partial reindex to avoid deleting docs from other sources
    if skip_stale_cleanup:
        _kept = len(ids) + len(skipped_ids)
        print(
            f"    Total: {_kept} chunks in '{collection_name}' (stale cleanup skipped, {len(skipped_ids)} reused)"
        )
        return _kept
    # Include incrementally-skipped IDs so they aren't treated as stale and deleted.
    upserted_ids = set(ids) | skipped_ids
    try:
        # Get actual collection count to avoid unbounded 1M limit (scales to 100K+)
        count_resp = chroma_api(
            "GET", f"/api/v2/tenants/default_tenant/databases/default_database/collections/{col_id}/count"
        )
        total_count = int(count_resp) if isinstance(count_resp, (int, str)) else 100000
        resp = chroma_api(
            "POST",
            f"/api/v2/tenants/default_tenant/databases/default_database/collections/{col_id}/get",
            {
                "limit": max(total_count, len(ids)),
                "include": [],
            },
        )
        existing_ids = set(resp.get("ids", []))
        stale_ids = list(existing_ids - upserted_ids)
        if stale_ids:
            # Delete in batches
            for start in range(0, len(stale_ids), BATCH):
                end = min(start + BATCH, len(stale_ids))
                chroma_api(
                    "POST",
                    f"/api/v2/tenants/default_tenant/databases/default_database/collections/{col_id}/delete",
                    {
                        "ids": stale_ids[start:end],
                    },
                )
            print(f"    Cleaned {len(stale_ids)} stale docs from '{collection_name}'")
    except Exception as e:
        print(f"    WARNING: Stale cleanup failed: {e}")

    _total_kept = len(ids) + len(skipped_ids)
    print(
        f"    Total: {_total_kept} chunks in '{collection_name}' ({len(ids)} embedded, {len(skipped_ids)} reused)"
    )
    return _total_kept


# ── Data Sources ────────────────────────────────────────
def collect_knowledge():
    """Collect infrastructure config files."""
    docs = []
    server_dir = BRAIN_HOME

    # Docker compose files
    for dc in server_dir.glob("*/docker-compose.yml"):
        if dc.parent.name.startswith("."):
            continue
        text = dc.read_text()
        chunks = chunk_docker_compose(text, str(dc))
        for chunk in chunks:
            docs.append(
                {
                    "content": chunk["content"],
                    "source": str(dc),
                    "type": "docker-compose",
                    "service": chunk.get("service", dc.parent.name),
                    "agent": "",
                }
            )

    # Nginx confs
    nginx_dir = server_dir / "nginx/conf.d"
    if nginx_dir.exists():
        for conf in nginx_dir.glob("*.conf"):
            text = conf.read_text()
            chunks = chunk_nginx_conf(text, str(conf))
            for chunk in chunks:
                docs.append(
                    {
                        "content": chunk["content"],
                        "source": str(conf),
                        "type": "nginx-conf",
                        "service": chunk.get("service", conf.stem),
                        "agent": "",
                    }
                )

    # Agent config files (only active agents — skip inactive/legacy workspaces)
    # SHARED_FILES are identical across workspaces — only index from the first found
    ACTIVE_AGENTS = {"liz", "ellie", "jenna", "sage", "claude", "market"}
    SHARED_FILES = {"SOUL.md"}
    PER_AGENT_FILES = {"MEMORY.md", "IDENTITY.md", "AGENTS.md", "TOOLS.md"}
    shared_indexed = set()  # track which shared files we already indexed
    agents_dir = OPENCLAW_DIR
    for agent_dir in sorted((agents_dir / "agents").iterdir()):
        if not agent_dir.is_dir():
            continue
        agent_name = agent_dir.name
        if agent_name not in ACTIVE_AGENTS:
            continue
        ws_dir = agents_dir / f"workspace-{agent_name}"
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
                    docs.append(
                        {
                            "content": chunk["content"],
                            "source": str(f),
                            "type": "agent-config",
                            "service": "",
                            "agent": agent_name if md_file in PER_AGENT_FILES else "shared",
                            "section": chunk.get("section", ""),
                        }
                    )
                if md_file in SHARED_FILES:
                    shared_indexed.add(md_file)

    return docs


def collect_experience():
    """Collect learnings, raw inbox records (browser/shell), and canonical notes."""
    docs = []
    ACTIVE_AGENTS = {"liz", "ellie", "jenna", "sage", "claude", "market"}
    agents_dir = OPENCLAW_DIR

    # 1. Agent .learnings files (original source)
    for agent_dir in (agents_dir / "agents").iterdir():
        if not agent_dir.is_dir():
            continue
        agent_name = agent_dir.name
        if agent_name not in ACTIVE_AGENTS:
            continue
        ws_dir = agents_dir / f"workspace-{agent_name}"
        if not ws_dir.exists():
            continue
        learnings_dir = ws_dir / ".learnings"
        if learnings_dir.exists():
            for f in learnings_dir.glob("*.md"):
                text = f.read_text()
                if len(text.strip()) < 200:
                    continue  # skip near-empty template files
                chunks = enforce_max_chunk_size(chunk_learnings(text, str(f)))
                for chunk in chunks:
                    docs.append(
                        {
                            "content": chunk["content"],
                            "source": str(f),
                            "type": "learning",
                            "service": "",
                            "agent": agent_name,
                            "section": chunk.get("section", ""),
                        }
                    )

    # 2. Raw inbox records (all agent-distilled records from ingest pipeline)
    INBOX_TYPES = {
        "browser",
        "shell",
        "git_activity",
        "screen_time",
        "openclaw_session",
        "claude_code_session",
    }
    HEADER_MAP = {
        "browser": lambda r: f"Browser research: {r.get('source_ref', '')}",
        "shell": lambda r: "Shell session activity",
        "git_activity": lambda r: f"Git activity: {r.get('source_ref', '')}",
        "screen_time": lambda r: f"Screen time pattern: {r.get('source_ref', '')}",
        "openclaw_session": lambda r: f"OpenClaw agent session: {r.get('source_ref', '')}",
        "claude_code_session": lambda r: f"Claude Code session: {r.get('source_ref', '')}",
    }
    inbox_dir = BRAIN_HOME / "knowledge" / "raw" / "inbox"
    if inbox_dir.exists():
        for f in inbox_dir.glob("*.json"):
            try:
                record = json.loads(f.read_text())
            except Exception:
                continue
            source_type = record.get("source_type", "")
            if source_type not in INBOX_TYPES:
                continue
            content = record.get("content", "").strip()
            if len(content) < 50:
                continue
            header_fn = HEADER_MAP.get(source_type, lambda r: source_type)
            full_content = f"{header_fn(record)}\n\n{content}"
            event_time = record.get("timestamp", "")
            if len(full_content) > MAX_CHUNK_SIZE:
                sub_chunks = chunk_text(full_content)
                for sc in sub_chunks:
                    docs.append(
                        {
                            "content": sc["content"],
                            "source": str(f),
                            "type": f"raw-{source_type}",
                            "service": "",
                            "agent": record.get("actor", ""),
                            "section": sc.get("section", ""),
                            "event_time": event_time,
                        }
                    )
            else:
                docs.append(
                    {
                        "content": full_content,
                        "source": str(f),
                        "type": f"raw-{source_type}",
                        "service": "",
                        "agent": record.get("actor", ""),
                        "section": "",
                        "event_time": event_time,
                    }
                )

    return docs


def collect_canonical():
    """Collect canonical + distilled notes for dedicated vector search."""
    docs = []
    knowledge_dir = BRAIN_HOME / "knowledge"
    canonical_stems = set()
    for subdir in ("canonical", "distilled"):
        notes_dir = knowledge_dir / subdir
        if not notes_dir.exists():
            continue
        for md_file in notes_dir.rglob("*.md"):
            if subdir == "distilled" and md_file.stem in canonical_stems:
                continue
            try:
                text = md_file.read_text(errors="replace")
            except Exception:
                continue
            if len(text.strip()) < 100:
                continue
            chunks = enforce_max_chunk_size(chunk_markdown(text, str(md_file)))
            for chunk in chunks:
                docs.append(
                    {
                        "content": chunk["content"],
                        "source": str(md_file),
                        "type": f"{subdir}-note",
                        "service": "",
                        "agent": "",
                        "section": chunk.get("section", ""),
                    }
                )
            if subdir == "canonical":
                canonical_stems.add(md_file.stem)
    return docs


def collect_context():
    """Collect recent session summaries from memory files."""
    docs = []
    ACTIVE_AGENTS = {"liz", "ellie", "jenna", "sage", "claude", "market"}
    agents_dir = OPENCLAW_DIR

    for agent_dir in (agents_dir / "agents").iterdir():
        if not agent_dir.is_dir():
            continue
        agent_name = agent_dir.name
        if agent_name not in ACTIVE_AGENTS:
            continue
        ws_dir = agents_dir / f"workspace-{agent_name}"
        if not ws_dir.exists():
            continue
        memory_dir = ws_dir / "memory"
        if memory_dir.exists():
            for f in sorted(memory_dir.glob("*.md"), reverse=True)[:30]:  # last 30 files
                text = f.read_text()
                if len(text.strip()) < 50:
                    continue
                chunks = enforce_max_chunk_size(chunk_markdown(text, str(f)))
                for chunk in chunks:
                    docs.append(
                        {
                            "content": chunk["content"],
                            "source": str(f),
                            "type": "session-memory",
                            "service": "",
                            "agent": agent_name,
                            "section": chunk.get("section", ""),
                        }
                    )

    return docs


def collect_obsidian():
    """Collect markdown notes from local Obsidian vault mirror."""
    docs = []
    try:
        from config import OBSIDIAN_VAULT_ICLOUD
    except ImportError:
        OBSIDIAN_VAULT_ICLOUD = (
            Path.home()
            / "Library"
            / "Mobile Documents"
            / "iCloud~md~obsidian"
            / "Documents"
            / "Obsidian-vault"
        )
    vault_dir = OBSIDIAN_VAULT_ICLOUD
    if not vault_dir.exists():
        return docs

    for md_file in vault_dir.rglob("*.md"):
        # Skip hidden files/dirs
        if any(part.startswith(".") for part in md_file.relative_to(vault_dir).parts):
            continue
        try:
            text = md_file.read_text(errors="replace")
        except Exception:
            continue
        if len(text.strip()) < 50:
            continue

        rel_parts = md_file.relative_to(vault_dir).parts
        vault_subdir = rel_parts[0] if len(rel_parts) > 1 else ""

        chunks = enforce_max_chunk_size(chunk_markdown(text, str(md_file)))
        for chunk in chunks:
            docs.append(
                {
                    "content": chunk["content"],
                    "source": str(md_file),
                    "type": "obsidian-note",
                    "service": "",
                    "agent": "",
                    "section": chunk.get("section", ""),
                    "vault_subdir": vault_subdir,
                }
            )

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


if __name__ == "__main__":
    import argparse
    import sys as _sys

    _sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

    parser = argparse.ArgumentParser()
    parser.add_argument("--collection", help="Index only this collection")
    parser.add_argument("--fresh", action="store_true", help="Ignore checkpoint, re-index everything")
    args = parser.parse_args()

    print("=" * 60)
    print("RAG Indexer — Phase 2")
    print("=" * 60)

    ALL_COLLECTIONS = [
        "knowledge",
        "experience",
        "context",
        "semantic_memory",
        "obsidian",
        "canonical",
        "personal",
    ]

    print("\n[setup] Ensuring collections...")
    for col in ALL_COLLECTIONS:
        ensure_collection(col)

    done = set() if args.fresh else _load_checkpoint()
    if done:
        print(f"  Resuming — already done: {', '.join(sorted(done))}")

    STEPS = [
        ("knowledge", "configs, agent files", collect_knowledge),
        ("experience", "learnings + raw inbox", collect_experience),
        ("canonical", "canonical + distilled notes", collect_canonical),
        ("context", "session memories", collect_context),
        ("obsidian", "Obsidian vault", collect_obsidian),
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
            print("  ERROR: ChromaDB/Ollama not ready. Saving checkpoint.")
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
