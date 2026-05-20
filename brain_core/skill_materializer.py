"""brain_core/skill_materializer.py — Voyager/Hermes-style skill materialization.

When a procedure in autonomy.db reaches a reuse threshold, write it out as a
SKILL.md file that Claude Code (~/.claude/skills/auto-<slug>/), Codex
(~/.codex/skills/auto-<slug>/), and OpenClaw (~/.openclaw/skills/auto-<slug>/)
can discover and invoke.

Design principles:
  - Brain is source of truth. Files are materializations.
  - Auto-generated skills are prefixed `auto-` so humans + tooling can tell.
  - Regeneration is idempotent. Writing over an existing auto- skill is safe.
  - Failure to materialize is non-fatal. Log and continue. Brain writes must not
    block on filesystem permissions etc.
  - Hot-path-aware. Materialization is called from a background pool post-commit,
    not inline in the dispatch loop.

Trigger policy:
  - procedure.success_count >= 2 (proven reuse, not a fluke)
  - len(steps) >= 3 (multi-step, not a one-liner)
  - tier in {"extraction", "awm_session:*"} — extracted from real work

Archival (future): a nightly job can walk ~/.claude/skills/auto-*/,
~/.codex/skills/auto-*/, and ~/.openclaw/skills/auto-*/ and mark archived=true
in their frontmatter when the backing procedure is deleted or has gone stale
(last_used > 180 days).
"""

from __future__ import annotations

import importlib.util
import json
import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

log = logging.getLogger("brain.skill_materializer")

CLAUDE_SKILLS_DIR = Path.home() / ".claude" / "skills"
CODEX_SKILLS_DIR = Path.home() / ".codex" / "skills"
OPENCLAW_SKILLS_DIR = Path.home() / ".openclaw" / "skills"
AUTO_PREFIX = "auto-"
MIN_SUCCESS_COUNT = 2
MIN_STEPS = 3

# Staleness + overload thresholds
STALE_DAYS = 90  # skills backing procedures last_used > N days are archived
MAX_AUTO_SKILLS = 50  # global cap per skill dir; lowest-success skills evicted first
USAGE_FILE = ".brain_auto_skill_usage.json"
PROMOTION_CONTRACT_VERSION = "skill-promotion-contract-v1"


# Hermes-inspired guardrail: auto-generated skills are loaded into agent context,
# so content extracted from logs/procedures must not be allowed to smuggle prompt
# injection or credential exfiltration instructions into SKILL.md.
_SKILL_THREAT_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"ignore\s+(?:\w+\s+)*(?:previous|all|above|prior)\s+(?:\w+\s+)*instructions", "prompt_injection"),
    (r"do\s+not\s+(?:\w+\s+)*tell\s+(?:\w+\s+)*the\s+user", "deception_hide"),
    (r"system\s+prompt\s+override", "sys_prompt_override"),
    (
        r"disregard\s+(?:\w+\s+)*(?:your|all|any)\s+(?:\w+\s+)*(?:instructions|rules|guidelines)",
        "disregard_rules",
    ),
    (r"curl\s+[^\n]*\$\{?\w*(?:KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|API)", "exfil_curl"),
    (r"wget\s+[^\n]*\$\{?\w*(?:KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|API)", "exfil_wget"),
    (r"cat\s+[^\n]*(?:\.env|credentials|\.netrc|\.pgpass|\.npmrc|\.pypirc)", "read_secrets"),
    (r"authorized_keys", "ssh_backdoor"),
    (r"rm\s+-rf\s+/", "destructive_root_rm"),
)

_INVISIBLE_CHARS = {
    "\u200b",
    "\u200c",
    "\u200d",
    "\u2060",
    "\ufeff",
    "\u202a",
    "\u202b",
    "\u202c",
    "\u202d",
    "\u202e",
}


def _sync_openclaw_registry() -> dict[str, Any]:
    """Best-effort central OpenClaw registry/allowlist sync.

    skill_materializer writes SKILL.md files for Claude, Codex, and OpenClaw.
    Claude/Codex discover the files directly, but OpenClaw also has a strict
    registry plus per-agent skill allowlists. Delegate that write path to the
    single owner (`cli/skill_sync.py`) instead of duplicating config mutation
    logic here.
    """
    sync_path = Path(__file__).resolve().parents[1] / "cli" / "skill_sync.py"
    spec = importlib.util.spec_from_file_location("brain_skill_sync", sync_path)
    if spec is None or spec.loader is None:
        return {"ok": False, "reason": f"load_failed:{sync_path}"}
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    registry = module.reconcile_registry(dry_run=False)
    attach = module.attach_generated_skills(dry_run=False)
    return {"ok": True, "registry": registry, "attach": attach}


def _slug(task_type: str) -> str:
    """Convert task_type to a filesystem-safe slug prefixed with `auto-`."""
    base = re.sub(r"[^a-z0-9]+", "-", (task_type or "").lower()).strip("-")
    if not base:
        return ""
    return f"{AUTO_PREFIX}{base}"[:60]


def _fetch_related_lessons(title: str, limit: int = 2) -> list[dict[str, Any]]:
    """Best-effort: pull top similar lessons for the pitfalls section.

    Fails silently — lessons are nice-to-have, not required.
    """
    try:
        import failure_memory

        return failure_memory.get_similar_lessons(title, agent_id="system", limit=limit) or []
    except Exception:
        return []


def _scan_generated_skill_content(text: str) -> str:
    """Return a threat id when generated SKILL.md content should be blocked.

    Auto skills are procedural memory. If a bad transcript/procedure is promoted
    into a skill, every future agent that loads it can inherit the payload. Keep
    the gate lightweight and deterministic so the background materializer stays
    cheap.
    """
    for char in _INVISIBLE_CHARS:
        if char in text:
            return f"invisible_unicode:U+{ord(char):04X}"
    for pattern, threat_id in _SKILL_THREAT_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            return threat_id
    return ""


def _usage_path(root: Path) -> Path:
    return root / USAGE_FILE


def _load_usage(root: Path) -> dict[str, dict[str, Any]]:
    path = _usage_path(root)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): v for k, v in data.items() if isinstance(v, dict)}


def _save_usage(root: Path, data: dict[str, dict[str, Any]]) -> None:
    path = _usage_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    tmp.replace(path)


def _latest_usage_activity(record: dict[str, Any]) -> str:
    """Newest actual skill activity timestamp from the sidecar record."""
    latest_dt = None
    latest_raw = ""
    for key in ("last_used_at", "last_viewed_at", "last_patched_at"):
        raw = record.get(key)
        if not raw:
            continue
        try:
            parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except Exception:  # noqa: S112 - skip malformed sidecar timestamps
            continue
        if latest_dt is None or parsed > latest_dt:
            latest_dt = parsed
            latest_raw = str(raw)
    return latest_raw


def _upsert_usage_record(root: Path, slug: str, proc: dict[str, Any]) -> None:
    """Track provenance/activity for generated skills, Hermes-curator style."""
    try:
        data = _load_usage(root)
        now_iso = datetime.now(UTC).isoformat(timespec="seconds")
        existing = data.get(slug) or {}
        existing.setdefault("created_at", now_iso)
        existing.update(
            {
                "brain_procedure_id": proc.get("id") or "",
                "task_type": proc.get("task_type") or "",
                "success_count": int(proc.get("success_count") or 1),
                "last_materialized_at": now_iso,
                "promotion_contract_version": PROMOTION_CONTRACT_VERSION,
                "source_episode_count": int(proc.get("success_count") or 1),
                "validation_status": "materialized_after_threshold_and_threat_scan",
                "rollback_strategy": "archive_or_delete_auto_skill_dir_then_regenerate_from_brain_procedure",
                "state": existing.get("state") or "active",
                "pinned": bool(existing.get("pinned", False)),
            }
        )
        data[slug] = existing
        _save_usage(root, data)
    except Exception as exc:
        log.debug("auto skill usage update failed for %s/%s: %s", root, slug, exc)


def _render_claude_skill_md(proc: dict[str, Any], lessons: list[dict[str, Any]]) -> str:
    """Render Claude Code SKILL.md — YAML frontmatter + body."""
    task_type = proc.get("task_type", "")
    title = proc.get("title", task_type)
    success_count = int(proc.get("success_count") or 1)
    steps = proc.get("steps") or []
    tools = proc.get("tools") or []
    preconditions = (proc.get("preconditions") or "").strip()
    proc_id = proc.get("id") or ""
    source = proc.get("source") or "extraction"
    last_used = proc.get("last_used") or proc.get("created_at") or ""
    created_at = proc.get("created_at") or ""

    slug = _slug(task_type)
    description = (
        f"Auto-materialized from brain procedure ({source}). "
        f"Used {success_count}x for task type: {task_type}. "
        f"Load this when encountering a similar multi-step task."
    )

    steps_md = "\n".join(f"{i+1}. {s}" for i, s in enumerate(steps[:10]))
    tools_md = ", ".join(str(t) for t in tools[:10]) if tools else "none recorded"
    preconds_md = preconditions or "No specific preconditions recorded."

    pitfalls_md = "None recorded."
    if lessons:
        lines = []
        for lesson in lessons[:2]:
            avoid = (lesson.get("avoid") or "").strip()
            try_next = (lesson.get("try_next") or "").strip()
            if avoid or try_next:
                frag = (lesson.get("reflection") or lesson.get("task") or "")[:120]
                line = f"- {frag}"
                if avoid:
                    line += f"\n  - **Avoid**: {avoid[:180]}"
                if try_next:
                    line += f"\n  - **Try instead**: {try_next[:180]}"
                lines.append(line)
        if lines:
            pitfalls_md = "\n".join(lines)

    now_iso = datetime.now(UTC).isoformat(timespec="seconds")

    frontmatter = (
        "---\n"
        f"name: {slug}\n"
        f"version: 0.{success_count}.0\n"
        f"description: |\n"
        f"  {description}\n"
        "allowed-tools:\n"
        "  - Bash\n"
        "  - Read\n"
        "  - Grep\n"
        "  - Edit\n"
        "auto_generated: true\n"
        f"brain_procedure_id: {proc_id}\n"
        f"brain_source: {source}\n"
        f"generated_at: {now_iso}\n"
        f"success_count: {success_count}\n"
        f"promotion_contract_version: {PROMOTION_CONTRACT_VERSION}\n"
        f"source_episode_count: {success_count}\n"
        "rollback_strategy: archive_generated_auto_skill_dir\n"
        "---\n"
    )

    promotion_md = (
        "## Promotion contract\n\n"
        f"- **Source episodes**: {success_count} successful uses from `{source}`.\n"
        f"- **Outcome gate**: materialized only after `success_count >= {MIN_SUCCESS_COUNT}` "
        f"and at least {MIN_STEPS} recorded steps.\n"
        "- **Validation gate**: generated content passed deterministic unsafe-content scan before writing.\n"
        "- **Runtime parity**: materialized for Claude, Codex, and OpenClaw; OpenClaw registry sync runs after write.\n"
        "- **Rollback**: archive or delete the generated `auto-*` skill directory; the backing Brain procedure "
        "remains the source of truth and can regenerate a clean copy.\n"
    )

    body = (
        f"\n# /{slug} — {title}\n\n"
        f"> Auto-materialized by brain from procedure `{proc_id}` after "
        f"{success_count} successful uses. **Do not edit** — edits are "
        f"overwritten on regeneration. To change behavior, edit the procedure "
        f"via brain (`/brain/procedures`) or record a new outcome.\n\n"
        f"## When to use\n\nThis skill encapsulates a proven workflow for tasks "
        f"matching `task_type = {task_type}`. Load it when the current task looks "
        f"similar to:\n\n> {title}\n\n"
        f"## Preconditions\n\n{preconds_md}\n\n"
        f"## Steps\n\n{steps_md}\n\n"
        f"## Tools used\n\n{tools_md}\n\n"
        f"## Pitfalls (from related brain lessons)\n\n{pitfalls_md}\n\n"
        f"{promotion_md}\n\n"
        f"## Source\n\n"
        f"- Procedure ID: `{proc_id}`\n"
        f"- Source: `{source}`\n"
        f"- Used: {success_count}x\n"
        f"- First recorded: {created_at}\n"
        f"- Last used: {last_used}\n"
    )
    return frontmatter + body


def _render_openclaw_meta(proc: dict[str, Any]) -> str:
    """Render _meta.json for ClawhubRegistry compatibility."""
    success_count = int(proc.get("success_count") or 1)
    slug = _slug(proc.get("task_type", ""))
    return (
        json.dumps(
            {
                "ownerId": "chrischo",
                "slug": slug,
                "version": f"0.{success_count}.0",
                "publishedAt": int(datetime.now(UTC).timestamp() * 1000),
                "autoGenerated": True,
                "brainProcedureId": proc.get("id") or "",
            },
            indent=2,
        )
        + "\n"
    )


def materialize(proc: dict[str, Any], *, min_success: int = MIN_SUCCESS_COUNT) -> dict[str, Any]:
    """Write SKILL.md + _meta.json for Claude, Codex, and OpenClaw if threshold met.

    Returns {materialized: bool, slug: str, paths: [...], reason: str}.
    Fail-open: any exception is logged and swallowed so brain writes don't
    regress on filesystem issues. `min_success` lets callers (batch mode)
    override the default reuse threshold.
    """
    result: dict[str, Any] = {"materialized": False, "slug": "", "paths": [], "reason": ""}
    try:
        task_type = (proc.get("task_type") or "").strip()
        success_count = int(proc.get("success_count") or 1)
        steps = proc.get("steps") or []

        if not task_type:
            result["reason"] = "empty_task_type"
            return result
        if success_count < min_success:
            result["reason"] = f"below_threshold:{success_count}<{min_success}"
            return result
        if not isinstance(steps, list) or len(steps) < MIN_STEPS:
            result["reason"] = f"too_few_steps:{len(steps) if isinstance(steps, list) else 0}"
            return result

        slug = _slug(task_type)
        if not slug:
            result["reason"] = "slug_empty"
            return result
        result["slug"] = slug

        # Enrich with lessons (best-effort)
        lessons = _fetch_related_lessons(proc.get("title") or task_type)

        skill_md = _render_claude_skill_md(proc, lessons)
        threat = _scan_generated_skill_content(skill_md)
        if threat:
            result["reason"] = f"blocked_unsafe_skill_content:{threat}"
            return result
        meta_json = _render_openclaw_meta(proc)

        # Write to each runtime skill dir. OpenClaw additionally needs _meta.json.
        cc_dir = CLAUDE_SKILLS_DIR / slug
        codex_dir = CODEX_SKILLS_DIR / slug
        oc_dir = OPENCLAW_SKILLS_DIR / slug
        for d in (cc_dir, codex_dir, oc_dir):
            d.mkdir(parents=True, exist_ok=True)

        cc_path = cc_dir / "SKILL.md"
        codex_path = codex_dir / "SKILL.md"
        oc_path = oc_dir / "SKILL.md"
        oc_meta_path = oc_dir / "_meta.json"

        cc_path.write_text(skill_md)
        codex_path.write_text(skill_md)
        oc_path.write_text(skill_md)
        oc_meta_path.write_text(meta_json)
        for root in (CLAUDE_SKILLS_DIR, CODEX_SKILLS_DIR, OPENCLAW_SKILLS_DIR):
            _upsert_usage_record(root, slug, proc)

        openclaw_sync = _sync_openclaw_registry()

        result["materialized"] = True
        result["paths"] = [str(cc_path), str(codex_path), str(oc_path), str(oc_meta_path)]
        result["reason"] = "ok"
        result["openclaw_sync"] = openclaw_sync
        result["promotion_contract_version"] = PROMOTION_CONTRACT_VERSION
        log.info(
            "materialized skill %s (success_count=%d, steps=%d)",
            slug,
            success_count,
            len(steps),
        )
    except Exception as exc:
        log.warning("skill materialization failed: %s", exc)
        result["reason"] = f"error:{exc}"[:200]
    return result


def materialize_all_procedures(min_success: int = MIN_SUCCESS_COUNT) -> dict[str, Any]:
    """Batch pass: materialize every qualifying procedure in autonomy.db.

    Suitable for a scheduled job. Returns {total: n, materialized: n, skipped: n}.
    """
    summary = {"total": 0, "materialized": 0, "skipped": 0, "slugs": []}
    try:
        from task_queue import task_queue

        rows = task_queue.get_procedures(limit=500)
        summary["total"] = len(rows)
        for proc in rows:
            if int(proc.get("success_count") or 1) < min_success:
                summary["skipped"] += 1
                continue
            r = materialize(proc, min_success=min_success)
            if r["materialized"]:
                summary["materialized"] += 1
                summary["slugs"].append(r["slug"])
            else:
                summary["skipped"] += 1
    except Exception as exc:
        log.warning("batch materialization failed: %s", exc)
        summary["error"] = str(exc)[:200]
    return summary


def _parse_frontmatter(skill_md_path: Path) -> dict[str, Any]:
    """Parse minimal YAML frontmatter (just the fields we care about).

    Avoids a yaml dep — skills we wrote have a known layout. Returns {}
    on any parse failure.
    """
    try:
        text = skill_md_path.read_text()
        if not text.startswith("---"):
            return {}
        end = text.find("\n---\n", 3)
        if end < 0:
            return {}
        block = text[3:end]
        out: dict[str, Any] = {}
        for line in block.splitlines():
            line = line.rstrip()
            if not line or line.startswith(" ") or ":" not in line:
                continue
            key, _, val = line.partition(":")
            key = key.strip()
            val = val.strip()
            # strip wrapping quotes
            if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
                val = val[1:-1]
            if key in (
                "name",
                "brain_procedure_id",
                "brain_source",
                "success_count",
                "auto_generated",
                "generated_at",
                "version",
                "promotion_contract_version",
                "source_episode_count",
                "rollback_strategy",
            ):
                out[key] = val
        return out
    except Exception:
        return {}


def _list_auto_skill_dirs() -> list[Path]:
    """Return all auto-* skill directories across Claude, Codex, and OpenClaw."""
    dirs: list[Path] = []
    for root in (CLAUDE_SKILLS_DIR, CODEX_SKILLS_DIR, OPENCLAW_SKILLS_DIR):
        if not root.exists():
            continue
        for p in root.iterdir():
            if p.is_dir() and p.name.startswith(AUTO_PREFIX):
                dirs.append(p)
    return dirs


def _archive_skill_dir(skill_dir: Path, reason: str) -> bool:
    """Move a skill dir to a sibling .archived-auto-<yyyymmdd>/ with reason log."""
    try:
        today = datetime.now(UTC).strftime("%Y%m%d")
        archive_root = skill_dir.parent / f".archived-auto-{today}"
        archive_root.mkdir(parents=True, exist_ok=True)
        dest = archive_root / skill_dir.name
        # Handle name collision on re-run same day
        if dest.exists():
            dest = archive_root / f"{skill_dir.name}-{int(datetime.now(UTC).timestamp())}"
        skill_dir.rename(dest)
        # Drop a marker file with the archival reason (for auditability)
        (archive_root / f"{dest.name}.reason.txt").write_text(f"{reason}\n{datetime.now(UTC).isoformat()}\n")
        log.info("archived skill %s: %s", skill_dir.name, reason)
        return True
    except Exception as exc:
        log.warning("archive failed for %s: %s", skill_dir, exc)
        return False


def cleanup_stale_auto_skills(
    *,
    stale_days: int = STALE_DAYS,
    max_skills: int = MAX_AUTO_SKILLS,
    soft_stale_days: int = 45,
    success_grace_threshold: int = 5,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Walk auto-* skills, archive stale/orphaned/over-cap entries.

    Archival rules (in order):
      1. Backing procedure missing from autonomy.db → archive (reason: orphan)
      2. Soft-stale (last_used > soft_stale_days AND success_count <= 2 AND
         no recent usage sidecar activity) → archive (reason: soft_stale)
      3. Hard-stale (last_used > stale_days) → archive UNLESS the procedure has
         success_count >= success_grace_threshold, which earns a stay of execution
      4. Count per root > max_skills → archive lowest-success-count overages
         (reason: overload_cap)

    2026-05-20 W3: added soft-stale tier + success-count grace + ``dry_run``
    mode so the curator can be exercised without mutating disk. dry_run reports
    what WOULD be archived under ``would_archive`` without invoking
    ``_archive_skill_dir`` — matches Hermes Curator's preview semantics so the
    cron job can ship a journal entry before live archival.

    Returns a summary suitable for the scheduler log.
    """
    summary: dict[str, Any] = {
        "scanned": 0,
        "orphaned": 0,
        "soft_stale": 0,
        "stale": 0,
        "overload": 0,
        "grace_granted": 0,
        "archived": 0,
        "would_archive": 0,
        "kept": 0,
        "dry_run": bool(dry_run),
        "errors": [],
    }

    def _try_archive(path: Path, reason: str) -> bool:
        if dry_run:
            summary["would_archive"] += 1
            return True
        return bool(_archive_skill_dir(path, reason=reason))

    try:
        import sqlite3

        from config import BRAIN_LOGS_DIR

        db_path = BRAIN_LOGS_DIR / "autonomy.db"
        if not db_path.exists():
            summary["errors"].append("autonomy.db missing")
            return summary
        conn = sqlite3.connect(str(db_path))
        try:
            rows = conn.execute("SELECT id, success_count, last_used FROM procedures").fetchall()
        finally:
            conn.close()
        proc_map = {r[0]: {"success_count": int(r[1] or 1), "last_used": r[2]} for r in rows}

        now = datetime.now(UTC)
        stale_cutoff_ts = now.timestamp() - (stale_days * 86400)
        soft_stale_cutoff_ts = now.timestamp() - (soft_stale_days * 86400)

        dirs = _list_auto_skill_dirs()
        summary["scanned"] = len(dirs)
        # Group by parent dir so cap applies per-root (CC and OC separately)
        by_root: dict[Path, list[tuple[Path, dict]]] = {}
        for d in dirs:
            skill_md = d / "SKILL.md"
            fm = _parse_frontmatter(skill_md) if skill_md.exists() else {}
            by_root.setdefault(d.parent, []).append((d, fm))

        survivors_per_root: dict[Path, list[tuple[Path, dict]]] = {}

        for root, items in by_root.items():
            usage = _load_usage(root)
            surviving: list[tuple[Path, dict]] = []
            for d, fm in items:
                # SAFETY GATE: only touch skills we materialized. Human-authored
                # skills that happen to share the `auto-` prefix (e.g. auto-updater)
                # must NEVER be archived. Require explicit auto_generated marker.
                auto_flag = str(fm.get("auto_generated", "")).strip().lower()
                if auto_flag not in ("true", "1", "yes"):
                    summary["kept"] += 1
                    continue

                proc_id = fm.get("brain_procedure_id", "")
                proc = proc_map.get(proc_id) if proc_id else None
                usage_record = usage.get(d.name) or {}

                # Hermes-style curator pin: an explicit sidecar pin is a hard
                # fence against automatic archival, even when the backing
                # procedure looks stale or temporarily missing.
                if usage_record.get("pinned"):
                    surviving.append((d, fm))
                    continue

                # Rule 1: orphan
                if not proc:
                    if _try_archive(d, reason=f"orphan:missing_procedure:{proc_id or 'empty'}"):
                        summary["orphaned"] += 1
                        if not dry_run:
                            summary["archived"] += 1
                    continue

                # Compose effective last_used from procedure + usage sidecar
                last_used = proc.get("last_used") or ""
                try:
                    last_ts = datetime.fromisoformat(last_used.replace("Z", "+00:00")).timestamp()
                except Exception:
                    last_ts = 0
                usage_activity = _latest_usage_activity(usage_record)
                has_recent_usage = False
                if usage_activity:
                    try:
                        usage_ts = datetime.fromisoformat(usage_activity.replace("Z", "+00:00")).timestamp()
                        # "Recent" = within soft-stale window
                        has_recent_usage = usage_ts > soft_stale_cutoff_ts
                        last_ts = max(last_ts, usage_ts)
                    except Exception:  # noqa: S110 - ignore malformed optional usage timestamps
                        pass

                success_count = int(proc.get("success_count", 1) or 1)

                # Rule 2 (NEW): soft-stale — between soft_stale_days and stale_days,
                # archive only if success_count<=2 AND no recent usage sidecar
                # activity. This matches Hermes Curator's "active→stale→archived"
                # progression and rescues low-traffic-but-recently-touched skills.
                if (
                    last_ts
                    and last_ts < soft_stale_cutoff_ts
                    and last_ts >= stale_cutoff_ts
                    and success_count <= 2
                    and not has_recent_usage
                ):
                    if _try_archive(d, reason=f"soft_stale:last_used={last_used}:success={success_count}"):
                        summary["soft_stale"] += 1
                        if not dry_run:
                            summary["archived"] += 1
                    continue

                # Rule 3: hard-stale — grant grace if procedure has earned a
                # strong success record (>= success_grace_threshold).
                if last_ts and last_ts < stale_cutoff_ts:
                    if success_count >= success_grace_threshold:
                        summary["grace_granted"] += 1
                        surviving.append((d, fm))
                        continue
                    if _try_archive(d, reason=f"stale:last_used={last_used}:success={success_count}"):
                        summary["stale"] += 1
                        if not dry_run:
                            summary["archived"] += 1
                    continue

                surviving.append((d, fm))
            survivors_per_root[root] = surviving

        # Rule 3: overload cap per root
        for _root, items in survivors_per_root.items():
            if len(items) <= max_skills:
                summary["kept"] += len(items)
                continue

            # Sort ascending by success_count (from procedure row), evict low first
            def _sc(pair: tuple[Path, dict[str, Any]]) -> int:
                _, fm = pair
                try:
                    return int(fm.get("success_count", 1))
                except ValueError:
                    return 1

            items.sort(key=_sc)
            evict_count = len(items) - max_skills
            for d, _fm in items[:evict_count]:
                if _try_archive(d, reason="overload_cap"):
                    summary["overload"] += 1
                    if not dry_run:
                        summary["archived"] += 1
            summary["kept"] += max_skills
    except Exception as exc:
        summary["errors"].append(str(exc)[:200])
    return summary


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "batch":
        print(json.dumps(materialize_all_procedures(), indent=2))
    elif len(sys.argv) > 1 and sys.argv[1] == "cleanup":
        print(json.dumps(cleanup_stale_auto_skills(), indent=2))
    else:
        # Dry-run: render for a test procedure, no file writes
        test_proc = {
            "id": "proc_test",
            "task_type": "test_materialize",
            "title": "Smoke test materialization",
            "steps": ["step 1", "step 2", "step 3"],
            "tools": ["bash", "curl"],
            "preconditions": "brain API available",
            "success_count": 3,
            "created_at": "2026-04-17T00:00:00Z",
            "last_used": "2026-04-17T00:00:00Z",
            "source": "extraction",
        }
        skill_md = _render_claude_skill_md(test_proc, [])
        print(skill_md[:2000])
        print("---")
        print(_render_openclaw_meta(test_proc))
