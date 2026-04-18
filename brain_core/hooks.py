"""brain_core/hooks.py — Event-driven hook system for brain lifecycle events.

Hook handlers are Python files in ~/.brain_hooks/*.py. Each file can define
functions matching event names: on_memory_stored, on_search, etc.
"""

from __future__ import annotations

import importlib.util
import json
import logging
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

log = logging.getLogger("brain.hooks")

HOOKS_DIR = Path.home() / ".brain_hooks"
HOOKS_LOG = Path("/Users/chrischo/server/brain/logs/hooks.jsonl")

_loaded_hooks: dict[str, list[Callable]] = {}
_initialized = False


def _log_event(event: str, **kwargs):
    """Append event to hooks.jsonl for debugging."""
    try:
        HOOKS_LOG.parent.mkdir(parents=True, exist_ok=True)
        with HOOKS_LOG.open("a") as f:
            f.write(
                json.dumps(
                    {
                        "timestamp": datetime.now().isoformat(),
                        "event": event,
                        **{k: str(v)[:200] for k, v in kwargs.items()},
                    }
                )
                + "\n"
            )
    except Exception:
        pass


def _load_user_hooks():
    """Load all Python files in ~/.brain_hooks/ as hook modules."""
    global _initialized
    if _initialized:
        return
    _initialized = True

    if not HOOKS_DIR.exists():
        return

    for hook_file in HOOKS_DIR.glob("*.py"):
        try:
            spec = importlib.util.spec_from_file_location(f"brain_hook_{hook_file.stem}", hook_file)
            if spec is None or spec.loader is None:
                continue
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)

            # Collect any functions starting with "on_"
            for attr in dir(module):
                if attr.startswith("on_") and callable(getattr(module, attr)):
                    _loaded_hooks.setdefault(attr, []).append(getattr(module, attr))
                    log.debug("loaded hook %s from %s", attr, hook_file.name)
        except Exception as e:
            log.warning("failed to load hook file %s: %s", hook_file.name, e)


def fire(event: str, **kwargs):
    """Fire a hook event. All registered handlers for `event` are called.

    Always logs the event to hooks.jsonl. User hooks are called best-effort.
    """
    _load_user_hooks()
    _log_event(event, **kwargs)

    handlers = _loaded_hooks.get(event, [])
    for handler in handlers:
        try:
            handler(**kwargs)
        except Exception as e:
            log.warning("hook handler %s failed: %s", handler.__name__, e)


def reload_hooks():
    """Force reload all hook files. Useful after adding new hooks."""
    global _initialized
    _initialized = False
    _loaded_hooks.clear()
    _load_user_hooks()
