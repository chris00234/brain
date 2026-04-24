#!/Users/chrischo/server/brain/.venv/bin/python
"""cli/skill_sync.py — reconcile ~/.openclaw/skills disk state with
~/.openclaw/openclaw.json registry + track per-skill telemetry in a
brain-owned sidecar.

Problem the script solves:
  - `atoms_to_skills` and `skill_extractor` generate brain-learned-* skill
    directories on disk, but none of them land in `skills.entries` — the
    registry openclaw reads to decide which skills are enabled.
  - No brain-side telemetry for whether generated skills actually get
    exercised by any agent.

Why two files:
  - ``~/.openclaw/openclaw.json`` — openclaw's config. Its `skills.entries`
    schema only accepts ``{enabled: bool}``. We stay inside that schema to
    avoid "Config invalid" on every openclaw invocation.
  - ``~/server/brain/logs/skill_telemetry.json`` — brain-owned sidecar
    keyed by skill name. Stores description, path, registered_at,
    last_used_at, use_count. Free-schema, safe to extend.

Runs weekly (scheduled after `skill_extract` so newly generated skills
auto-register) and can also be invoked manually after `atoms_to_skills`.
Idempotent.

Usage:
  skill_sync.py                 # reconcile registry + telemetry + attach
  skill_sync.py --dry-run       # show what would change
  skill_sync.py --bump-agent jenna   # stamp last_used_at + use_count for
                                     # every brain-learned-* skill
                                     # currently attached to jenna
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

OPENCLAW_ROOT = Path("/Users/chrischo/.openclaw")
SKILLS_DIR = OPENCLAW_ROOT / "skills"
CONFIG_PATH = OPENCLAW_ROOT / "openclaw.json"

BRAIN_LOGS = Path("/Users/chrischo/server/brain/logs")
TELEMETRY_PATH = BRAIN_LOGS / "skill_telemetry.json"

# Brain-generated skills share this prefix; auto-attached to every agent.
BRAIN_PREFIX = "brain-learned-"

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_FIELD_RE = re.compile(r"^(\w[\w-]*):\s*(.+?)\s*$", re.MULTILINE)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _read_skill_meta(skill_dir: Path) -> dict | None:
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.exists():
        return None
    try:
        text = skill_md.read_text(encoding="utf-8")
    except OSError:
        return None
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return None
    fields = dict(_FIELD_RE.findall(m.group(1)))
    name = fields.get("name") or skill_dir.name
    description = fields.get("description", "").strip()
    if description.startswith('"') and description.endswith('"'):
        description = description[1:-1]
    return {
        "name": name,
        "description": description[:400],
        "path": str(skill_dir),
    }


def _load_config() -> dict:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


# Brain-owned telemetry keys that must NEVER land in openclaw.json's
# skills.entries — they'd trip openclaw's strict schema validator. Every
# write scrubs them defensively. Keys like ``env`` or ``mcp`` that
# openclaw itself accepts pass through unchanged.
_BRAIN_TELEMETRY_KEYS = {"description", "path", "registered_at", "last_used_at", "use_count"}


def _scrub_brain_keys(entries: dict) -> int:
    """Remove keys this script previously (wrongly) stamped into
    skills.entries. Returns count of entries modified."""
    modified = 0
    for _name, v in entries.items():
        if not isinstance(v, dict):
            continue
        leaked = _BRAIN_TELEMETRY_KEYS & set(v.keys())
        if leaked:
            for k in leaked:
                v.pop(k, None)
            modified += 1
    return modified


def _atomic_write_config(data: dict) -> None:
    entries = data.get("skills", {}).get("entries", {})
    _scrub_brain_keys(entries)
    rendered = json.dumps(data, indent=2, ensure_ascii=False)
    parsed = json.loads(rendered)
    required = {"meta", "agents", "skills"}
    if not required.issubset(parsed):
        raise RuntimeError(f"refusing to write config missing keys: {required - set(parsed)}")
    tmp = CONFIG_PATH.with_suffix(".tmp.skill_sync")
    tmp.write_text(rendered + "\n", encoding="utf-8")
    tmp.replace(CONFIG_PATH)


def _load_telemetry() -> dict:
    if not TELEMETRY_PATH.exists():
        return {}
    try:
        return json.loads(TELEMETRY_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_telemetry(data: dict) -> None:
    BRAIN_LOGS.mkdir(parents=True, exist_ok=True)
    rendered = json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True)
    tmp = TELEMETRY_PATH.with_suffix(".tmp.skill_sync")
    tmp.write_text(rendered + "\n", encoding="utf-8")
    tmp.replace(TELEMETRY_PATH)


def reconcile_registry(*, dry_run: bool = False) -> dict:
    """Walk the skills dir and:
    (a) ensure every on-disk skill has ``{enabled: true}`` in openclaw.json
        skills.entries (schema-compliant minimal entry)
    (b) populate the brain telemetry sidecar with name/description/path +
        seeded last_used_at=None, use_count=0
    """
    cfg = _load_config()
    entries: dict = cfg.setdefault("skills", {}).setdefault("entries", {})
    telemetry = _load_telemetry()

    disk_skills: dict[str, dict] = {}
    for child in sorted(SKILLS_DIR.iterdir()):
        if not child.is_dir() or child.name.startswith("_"):
            continue
        meta = _read_skill_meta(child)
        if meta is None:
            continue
        disk_skills[meta["name"]] = meta

    added_reg = 0
    added_tel = 0
    updated_tel = 0
    # Always scrub brain-owned telemetry leakage from prior buggy runs of
    # this script. _atomic_write_config also scrubs but doing it up-front
    # keeps the "what changed" summary honest.
    scrubbed = _scrub_brain_keys(entries)
    for name, meta in disk_skills.items():
        # Registry side — schema-compliant minimal entry. Leave openclaw-
        # native keys (env, mcp, etc.) on existing entries untouched.
        if name not in entries or not isinstance(entries[name], dict):
            entries[name] = {"enabled": True}
            added_reg += 1
        else:
            entries[name].setdefault("enabled", True)

        # Telemetry side — full metadata + counters.
        t = telemetry.get(name)
        if t is None:
            telemetry[name] = {
                "description": meta["description"],
                "path": meta["path"],
                "registered_at": _now_iso(),
                "last_used_at": None,
                "use_count": 0,
            }
            added_tel += 1
        else:
            changed = False
            if meta["description"] and t.get("description") != meta["description"]:
                t["description"] = meta["description"]
                changed = True
            if t.get("path") != meta["path"]:
                t["path"] = meta["path"]
                changed = True
            t.setdefault("registered_at", _now_iso())
            t.setdefault("last_used_at", None)
            t.setdefault("use_count", 0)
            if changed:
                updated_tel += 1

    # Drop registry entries that point at deleted skill dirs. Preserve
    # marketplace installs (no disk counterpart under SKILLS_DIR).
    removed = 0
    for name in list(entries.keys()):
        t = telemetry.get(name, {})
        path_claim = t.get("path", "")
        if path_claim.startswith(str(SKILLS_DIR)) and not Path(path_claim).exists():
            del entries[name]
            telemetry.pop(name, None)
            removed += 1

    if not dry_run:
        _atomic_write_config(cfg)
        _save_telemetry(telemetry)

    return {
        "registry_added": added_reg,
        "telemetry_added": added_tel,
        "telemetry_updated": updated_tel,
        "scrubbed_legacy": scrubbed,
        "removed": removed,
        "total_on_disk": len(disk_skills),
        "total_entries": len(entries),
        "dry_run": dry_run,
    }


def attach_brain_skills(*, dry_run: bool = False) -> dict:
    """Ensure every agent in `agents.list` has every brain-learned-* skill
    on disk (resolved via telemetry file, not the registry flag)."""
    cfg = _load_config()
    telemetry = _load_telemetry()
    brain_skill_names = sorted(n for n in telemetry if n.startswith(BRAIN_PREFIX))

    attached = 0
    touched_agents: list[str] = []
    for agent in cfg.get("agents", {}).get("list", []):
        if not isinstance(agent, dict):
            continue
        skills = list(agent.get("skills") or [])
        changed = False
        for name in brain_skill_names:
            if name not in skills:
                skills.append(name)
                changed = True
                attached += 1
        if changed:
            agent["skills"] = skills
            touched_agents.append(agent.get("name", agent.get("id", "?")))

    if not dry_run and attached:
        _atomic_write_config(cfg)

    return {
        "attached": attached,
        "agents_touched": touched_agents,
        "brain_skills_registered": len(brain_skill_names),
        "dry_run": dry_run,
    }


def bump_agent_usage(agent_name: str) -> dict:
    """Stamp last_used_at + increment use_count on every brain-learned-*
    skill attached to the named agent. Brain-side telemetry only — never
    touches openclaw.json."""
    try:
        cfg = _load_config()
    except Exception:
        return {"ok": False, "reason": "config_unreadable"}
    telemetry = _load_telemetry()
    now = _now_iso()

    matching = None
    for agent in cfg.get("agents", {}).get("list", []):
        if not isinstance(agent, dict):
            continue
        if str(agent.get("name", "")).lower() == agent_name.lower():
            matching = agent
            break
    if matching is None:
        return {"ok": False, "reason": "agent_not_found", "agent": agent_name}

    bumped = 0
    for skill_name in matching.get("skills") or []:
        if not skill_name.startswith(BRAIN_PREFIX):
            continue
        t = telemetry.get(skill_name)
        if t is None:
            continue
        t["last_used_at"] = now
        t["use_count"] = int(t.get("use_count") or 0) + 1
        bumped += 1

    if bumped:
        try:
            _save_telemetry(telemetry)
        except Exception as exc:
            return {"ok": False, "reason": f"write_failed: {exc}", "bumped": bumped}
    return {"ok": True, "bumped": bumped, "agent": agent_name}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--bump-agent", help="stamp last_used_at for every brain-learned-* skill on one agent"
    )
    args = parser.parse_args()

    if args.bump_agent:
        out = bump_agent_usage(args.bump_agent)
        print(json.dumps(out, indent=2))
        return 0 if out.get("ok") else 1

    print("[1/2] Reconciling registry (openclaw.json) + telemetry sidecar...")
    reg = reconcile_registry(dry_run=args.dry_run)
    print(json.dumps(reg, indent=2))

    print("\n[2/2] Attaching brain-learned-* to all agents...")
    att = attach_brain_skills(dry_run=args.dry_run)
    print(json.dumps(att, indent=2))

    return 0


if __name__ == "__main__":
    sys.exit(main())
