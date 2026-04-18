"""brain_core/lora_embedder.py — Local sentence-transformers embedder with LoRA adapter support.

Loads base multilingual-e5-large-instruct once, applies LoRA adapter on demand.
Singleton to avoid reloading the model on every call.

Used by indexer.py and search.py when embed model name starts with 'lora:'.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any

log = logging.getLogger("brain.lora_embedder")

# Singleton state protected by _lock. load_adapter mutates globals, so
# concurrent callers must not race.
_lock = threading.Lock()
_base_model: Any = None
_active_adapter: str | None = None


def _load_base():
    """Lazy-load the base sentence-transformers model. Caller must hold _lock."""
    global _base_model
    if _base_model is not None:
        return _base_model
    try:
        from sentence_transformers import SentenceTransformer

        log.info("loading base embedding model: intfloat/multilingual-e5-large-instruct")
        _base_model = SentenceTransformer("intfloat/multilingual-e5-large-instruct")
        return _base_model
    except Exception as e:
        log.error("failed to load base model: %s", e)
        raise


def _load_adapter(adapter_path: str) -> Any:
    """Load a fine-tuned model. Returns the adapted SentenceTransformer.

    Supports three formats at ``adapter_path``:
    1. **LoRA adapter (preferred, ~5MB)** — ``adapter_config.json`` +
       ``adapter_model.safetensors``. Applied on top of a freshly-loaded
       base model via ``SentenceTransformer.add_adapter()`` +
       ``set_peft_model_state_dict()``.
    2. **Full SentenceTransformer dir** — ``modules.json`` present. Loaded
       directly via ``SentenceTransformer(path)``. This is the fallback for
       when training couldn't save an isolated adapter (~1GB).
    3. **Native load_adapter** — last-resort for any format the v3.4+ API
       can autodetect.
    """
    global _base_model, _active_adapter

    with _lock:
        if _active_adapter == adapter_path and _base_model is not None:
            return _base_model

        adapter_dir = Path(adapter_path)

        # Path 1: isolated LoRA adapter
        if (adapter_dir / "adapter_config.json").exists() and (
            adapter_dir / "adapter_model.safetensors"
        ).exists():
            try:
                import json as _json

                import torch
                from peft import LoraConfig, set_peft_model_state_dict
                from safetensors.torch import load_file
                from sentence_transformers import SentenceTransformer

                log.info("loading LoRA adapter from %s", adapter_path)
                # Resolve base model name — brain_finetune.py writes it as a
                # sibling file. Fall back to the project default.
                base_name_file = adapter_dir / "base_model.txt"
                base_name = (
                    base_name_file.read_text().strip()
                    if base_name_file.exists()
                    else "intfloat/multilingual-e5-large-instruct"
                )
                fresh = SentenceTransformer(base_name, model_kwargs={"torch_dtype": torch.float32})
                cfg_dict = _json.loads((adapter_dir / "adapter_config.json").read_text())
                # LoraConfig ignores unknown keys via ``_register_subclass`` — be defensive.
                known = {k: v for k, v in cfg_dict.items() if k in LoraConfig.__dataclass_fields__}
                cfg = LoraConfig(**known)
                fresh.add_adapter(cfg)
                state = load_file(str(adapter_dir / "adapter_model.safetensors"))
                set_peft_model_state_dict(fresh[0].auto_model, state)
                _base_model = fresh
                _active_adapter = adapter_path
                return fresh
            except Exception as e:
                log.error("failed to load LoRA adapter at %s: %s", adapter_path, e)
                raise

        # Path 2: full SentenceTransformer save (modules.json present)
        if (adapter_dir / "modules.json").exists():
            try:
                from sentence_transformers import SentenceTransformer

                log.info("loading fine-tuned SentenceTransformer from %s", adapter_path)
                _base_model = SentenceTransformer(str(adapter_dir))
                _active_adapter = adapter_path
                return _base_model
            except Exception as e:
                log.error("failed to load fine-tuned SentenceTransformer at %s: %s", adapter_path, e)
                raise

        # Path 3: native ≥3.4 load_adapter as last resort
        base = _load_base()
        if hasattr(base, "load_adapter"):
            try:
                log.info("loading via SentenceTransformer.load_adapter: %s", adapter_path)
                base.load_adapter(adapter_path)
                _active_adapter = adapter_path
                return base
            except Exception as e:
                log.error("native load_adapter failed: %s", e)
                raise

        raise RuntimeError(f"no recognized model format at {adapter_path}")


def get_lora_embedding(
    text: str, adapter_path: str, prefix: str = "passage", max_chars: int = 1000
) -> list[float]:
    """Embed text using base model + LoRA adapter.

    Args:
        text: input text to embed
        adapter_path: path to LoRA adapter directory (e.g. logs/training/lora_v1/)
        prefix: "passage" or "query" for asymmetric e5 model
        max_chars: truncate input to this many chars

    Returns:
        list of floats (1024-dim for multilingual-e5-large-instruct)
    """
    model = _load_adapter(adapter_path)
    prompted = f"{prefix}: {text[:max_chars]}" if prefix else text[:max_chars]

    try:
        emb = model.encode(prompted, convert_to_numpy=True)
        return emb.tolist() if hasattr(emb, "tolist") else list(emb)
    except Exception as e:
        log.error("embedding failed: %s", e)
        raise


def get_lora_embeddings_batch(
    texts: list[str], adapter_path: str, prefix: str = "passage", max_chars: int = 1000
) -> list[list[float]]:
    """Batch version of get_lora_embedding. Much faster for many texts."""
    if not texts:
        return []
    model = _load_adapter(adapter_path)
    prompted = [f"{prefix}: {t[:max_chars]}" if prefix else t[:max_chars] for t in texts]

    try:
        embs = model.encode(prompted, batch_size=32, convert_to_numpy=True, show_progress_bar=False)
        return [e.tolist() if hasattr(e, "tolist") else list(e) for e in embs]
    except Exception as e:
        log.error("batch embedding failed: %s", e)
        raise


def unload():
    """Release the model from memory (for testing or switching adapters)."""
    global _base_model, _active_adapter
    with _lock:
        _base_model = None
        _active_adapter = None


if __name__ == "__main__":
    # Smoke test
    import sys

    if len(sys.argv) < 2:
        print("Usage: lora_embedder.py <adapter_path>")
        sys.exit(1)
    adapter = sys.argv[1]
    emb = get_lora_embedding("docker compose setup", adapter)
    print(f"embedding dim: {len(emb)}")
    print(f"first 5 values: {emb[:5]}")
