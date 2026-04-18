"""brain_core/self_modify.py — the self-modification pathway.

Gives brain the ability to edit its own intent_routes.yaml (and later scheduler
and autonomy levels) within the autonomy gate. Every mutation goes through
autonomy.authorize() + a deterministic audit trail + atomic .tmp→rename writes
so a partial failure can't corrupt the routing table.

Today this module exposes one public mutation:

  apply_intent_route_patch(patch: dict, actor: str = "brain_loop") -> dict

The patch format:
  {
    "op": "add_keyword" | "add_intent" | "remove_intent",
    "intent": "frontend_design",
    "keywords_en": ["new_kw1"],  # for add_keyword
    "keywords_ko": [],            # optional
    "canonical_paths": [],        # for add_intent
    ...
  }

Gated by autonomy kind "brain_loop.self_modify_route" (default L1 = propose-only
until Chris promotes). If gate returns L0/L1, the patch is written to
eval_proposals as a candidate rather than applied.

Audit trail: every attempted mutation appends to logs/self_modify_audit.jsonl
and writes an action_audit row with route='brain_loop/self_modify_route'.
"""

from __future__ import annotations

import json
import logging
import shutil
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

try:
    from config import BRAIN_CORE_DIR, BRAIN_LOGS_DIR
except ImportError:
    BRAIN_CORE_DIR = Path(__file__).parent
    BRAIN_LOGS_DIR = BRAIN_CORE_DIR.parent / "logs"

try:
    import yaml
except ImportError:
    yaml = None

log = logging.getLogger("brain.self_modify")

INTENT_ROUTES_PATH = BRAIN_CORE_DIR / "intent_routes.yaml"
BACKUP_DIR = BRAIN_LOGS_DIR / "self_modify_backups"
AUDIT_LOG = BRAIN_LOGS_DIR / "self_modify_audit.jsonl"


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _load_routes() -> dict:
    if yaml is None or not INTENT_ROUTES_PATH.exists():
        return {}
    try:
        return yaml.safe_load(INTENT_ROUTES_PATH.read_text()) or {}
    except Exception as e:
        log.warning("load_routes failed: %s", e)
        return {}


def _backup_before_write() -> Path | None:
    """Copy current intent_routes.yaml to logs/self_modify_backups/ with a
    timestamped name so we can roll back deterministically."""
    if not INTENT_ROUTES_PATH.exists():
        return None
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    target = BACKUP_DIR / f"intent_routes_{ts}.yaml"
    try:
        shutil.copy2(INTENT_ROUTES_PATH, target)
        return target
    except OSError as e:
        log.warning("backup failed: %s", e)
        return None


def _atomic_write(path: Path, content: str) -> bool:
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text(content)
        tmp.rename(path)
        return True
    except OSError as e:
        log.warning("atomic write failed for %s: %s", path, e)
        return False


def _audit(event: dict) -> None:
    BRAIN_LOGS_DIR.mkdir(parents=True, exist_ok=True)
    entry = {"ts": _now_iso(), **event}
    try:
        with AUDIT_LOG.open("a") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass
    try:
        from atoms_store import insert_action_audit

        insert_action_audit(
            route="brain_loop/self_modify_route",
            query_text=json.dumps(event)[:2000],
            tool="self_modify",
            actor=event.get("actor", "brain_loop"),
        )
    except Exception:
        pass


def _apply_patch(routes: dict, patch: dict) -> tuple[bool, str, dict]:
    """Apply a patch dict to the routes tree. Returns (ok, message, new_routes).

    Does NOT write to disk — caller handles persistence after validation."""
    op = patch.get("op")
    intent = patch.get("intent")
    if not op or not intent:
        return False, "missing op or intent field", routes

    intents = routes.get("intents") or {}
    if op == "add_intent":
        if intent in intents:
            return False, f"intent '{intent}' already exists (use add_keyword)", routes
        intents[intent] = {
            "keywords_en": list(patch.get("keywords_en") or []),
            "keywords_ko": list(patch.get("keywords_ko") or []),
            "canonical_paths": list(patch.get("canonical_paths") or []),
            "always_push_queries": list(patch.get("always_push_queries") or []),
            "priority": patch.get("priority", "medium"),
            "max_tokens": int(patch.get("max_tokens", 500)),
        }
        routes["intents"] = intents
        return True, f"added intent '{intent}'", routes

    if op == "add_keyword":
        existing = intents.get(intent)
        if not existing:
            return False, f"intent '{intent}' not found", routes
        added: list[str] = []
        for lang_key in ("keywords_en", "keywords_ko"):
            new_kws = list(patch.get(lang_key) or [])
            if not new_kws:
                continue
            cur = list(existing.get(lang_key) or [])
            for kw in new_kws:
                if kw and kw not in cur:
                    cur.append(kw)
                    added.append(f"{lang_key}:{kw}")
            existing[lang_key] = cur
        intents[intent] = existing
        routes["intents"] = intents
        return True, f"added {len(added)} keyword(s) to '{intent}': {added}", routes

    if op == "remove_intent":
        if intent not in intents:
            return False, f"intent '{intent}' not found", routes
        del intents[intent]
        routes["intents"] = intents
        return True, f"removed intent '{intent}'", routes

    return False, f"unknown op: {op}", routes


def _propose_patch(patch: dict, reason: str, actor: str) -> str | None:
    """When the autonomy gate says L1/L0, write the patch as an eval_proposals
    candidate instead of applying it. Returns the proposal id or None on failure."""
    try:
        import sqlite3

        from config import AUTONOMY_DB
    except ImportError:
        AUTONOMY_DB = BRAIN_LOGS_DIR / "autonomy.db"
        import sqlite3

    pid = f"selfmod_{int(time.time())}_{patch.get('intent','x')}"
    try:
        with sqlite3.connect(str(AUTONOMY_DB), timeout=5) as conn:
            conn.execute(
                "INSERT INTO eval_proposals "
                "(id, query, expected, expected_sources, source_event, "
                " status, confidence, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    pid,
                    f"self_modify:{patch.get('op','?')}:{patch.get('intent','?')}",
                    json.dumps({"patch": patch, "reason": reason}),
                    "[]",
                    f"self_modify_{actor}",
                    "candidate",
                    0.7,
                    _now_iso(),
                ),
            )
            conn.commit()
        return pid
    except Exception as e:
        log.warning("propose_patch failed: %s", e)
        return None


def apply_intent_route_patch(
    patch: dict,
    reason: str = "",
    actor: str = "brain_loop",
    force: bool = False,
) -> dict:
    """Attempt to apply a patch to intent_routes.yaml.

    Flow:
      1. autonomy.authorize("brain_loop.self_modify_route")
      2. If L2/L3 (or force=True): validate → backup → patch → atomic write
      3. If L0/L1: write to eval_proposals as candidate
      4. Audit every step

    Returns {"status": "applied|proposed|denied|failed", "reason": "...", "backup": "...", "proposal_id": "..."}
    """
    # Authorize
    try:
        from autonomy import authorize

        decision = authorize("brain_loop.self_modify_route")
        level = decision.level
        allowed = decision.allowed
    except Exception as e:
        level = "L0"
        allowed = False
        log.debug("authorize failed: %s", e)

    if not force and (level in ("L0", "L1") or not allowed):
        proposal_id = _propose_patch(patch, reason, actor)
        result = {
            "status": "proposed",
            "level": level,
            "reason": reason,
            "proposal_id": proposal_id,
        }
        _audit({"event": "propose", "actor": actor, "patch": patch, **result})
        return result

    # Apply
    routes = _load_routes()
    ok, msg, new_routes = _apply_patch(routes, patch)
    if not ok:
        result = {"status": "failed", "reason": msg, "level": level}
        _audit({"event": "apply_failed", "actor": actor, "patch": patch, **result})
        return result

    if yaml is None:
        result = {"status": "failed", "reason": "PyYAML unavailable", "level": level}
        _audit({"event": "apply_failed", "actor": actor, "patch": patch, **result})
        return result

    backup_path = _backup_before_write()
    try:
        yaml_text = yaml.safe_dump(new_routes, sort_keys=False, allow_unicode=True)
        if not _atomic_write(INTENT_ROUTES_PATH, yaml_text):
            raise OSError("atomic write failed")
    except Exception as e:
        result = {
            "status": "failed",
            "reason": f"write error: {e}",
            "level": level,
            "backup": str(backup_path) if backup_path else None,
        }
        _audit({"event": "apply_failed", "actor": actor, "patch": patch, **result})
        return result

    # Invalidate active_recall cache so next /recall/active sees the new routes.
    try:
        import active_recall

        active_recall._routes_cache = None
        active_recall._routes_cache_mtime = 0.0
    except Exception:
        pass

    result = {
        "status": "applied",
        "level": level,
        "reason": msg,
        "backup": str(backup_path) if backup_path else None,
    }
    _audit({"event": "applied", "actor": actor, "patch": patch, **result})
    return result


def preview_intent_route_patch(patch: dict) -> dict:
    """Dry-run: return what the routes tree would look like after applying
    the patch, without writing anything."""
    routes = _load_routes()
    ok, msg, new_routes = _apply_patch(routes, patch)
    return {
        "ok": ok,
        "message": msg,
        "preview": new_routes if ok else None,
    }


if __name__ == "__main__":
    # Minimal smoke test: preview adding a keyword to frontend_design.
    preview = preview_intent_route_patch(
        {
            "op": "add_keyword",
            "intent": "frontend_design",
            "keywords_en": ["glassmorphism"],
        }
    )
    print(json.dumps({"preview_ok": preview["ok"], "message": preview["message"]}))
