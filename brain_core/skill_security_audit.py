"""brain_core/skill_security_audit.py — supply-chain audit for auto-skills.

Hermes Curator + tirith_security inspired: every materialized SKILL.md
carries a ``content_sha256`` attestation in its usage sidecar. This module
walks the on-disk auto-* directories, compares each file's hash against the
recorded value, and quarantines any drift so a tampered/replaced skill
doesn't silently load into an agent's context next turn.

Triggers a quarantine when:
  1. ``content_sha256`` is recorded in the sidecar AND on-disk hash differs.
  2. The SKILL.md is missing from disk while the sidecar still tracks it.
  3. The threat scanner (``_scan_generated_skill_content``) flags pattern
     drift introduced by a manual edit after materialization.

Quarantine writes ``quarantined: true`` + ``quarantine_reason`` into the
usage sidecar; ``skill_materializer.cleanup_stale_auto_skills`` honors that
flag and stops touching the dir until an operator inspects it.

2026-05-20 W3.5 round 3 (codex gap 4): closes the supply-chain risk window.
Previously the pin gate + threat scanner only fired at materialization; a
post-write edit could swap content and stay live.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

log = logging.getLogger("brain.skill_security_audit")

sys.path.insert(0, str(Path(__file__).resolve().parent))

from skill_materializer import (  # noqa: E402 — sys.path inject required
    AUTO_PREFIX,
    CLAUDE_SKILLS_DIR,
    CODEX_SKILLS_DIR,
    _load_usage,
    _save_usage,
    _scan_generated_skill_content,
    hermes_skill_roots,
)


def _hash_file(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except Exception as exc:
        log.debug("hash failed for %s: %s", path, exc)
        return None


def _quarantine(usage: dict[str, dict[str, Any]], slug: str, reason: str) -> None:
    now_iso = datetime.now(UTC).isoformat(timespec="seconds")
    entry = usage.get(slug) or {}
    entry["quarantined"] = True
    entry["quarantine_reason"] = reason[:200]
    entry["quarantined_at"] = now_iso
    usage[slug] = entry


def audit_root(root: Path) -> dict[str, Any]:
    """Audit one skill root (Claude/Codex/Hermes). Updates the sidecar
    in-place when drift is detected; returns a per-skill summary.
    """
    summary: dict[str, Any] = {
        "root": str(root),
        "scanned": 0,
        "ok": 0,
        "drift": 0,
        "missing": 0,
        "threat_drift": 0,
        "no_attestation": 0,
        "quarantined_now": [],
        "errors": [],
    }
    if not root.exists():
        return summary
    usage = _load_usage(root)
    mutated = False
    for dir_path in root.iterdir():
        if not dir_path.is_dir():
            continue
        if not dir_path.name.startswith(AUTO_PREFIX):
            continue
        slug = dir_path.name
        summary["scanned"] += 1
        skill_md = dir_path / "SKILL.md"
        record = usage.get(slug) or {}

        recorded_hash = record.get("content_sha256")
        if not skill_md.exists():
            if recorded_hash:
                summary["missing"] += 1
                _quarantine(usage, slug, "skill_md_missing")
                summary["quarantined_now"].append(slug)
                mutated = True
            continue

        actual_hash = _hash_file(skill_md)
        if not actual_hash:
            summary["errors"].append(f"hash_fail:{slug}")
            continue

        if not recorded_hash:
            # Legacy skill — no attestation. Stamp the current hash so
            # future audits have a reference. Codex round-4 defect E1: the
            # original backfill path skipped the threat scanner via
            # ``continue``, so a malicious legacy skill could be attested
            # AS-IS and only get quarantined on the next cycle (a 24h
            # window where the SKILL.md happily loads into agent context).
            # Run the scanner inline before stamping so anything dangerous
            # is quarantined on the very first audit.
            try:
                text = skill_md.read_text()
            except Exception as exc:
                summary["errors"].append(f"read_fail:{slug}:{exc}")
                continue
            threat = _scan_generated_skill_content(text)
            if threat:
                summary["threat_drift"] += 1
                _quarantine(usage, slug, f"legacy_threat_pattern:{threat}")
                summary["quarantined_now"].append(slug)
                mutated = True
                continue
            record["content_sha256"] = actual_hash
            record["content_sha256_at"] = datetime.now(UTC).isoformat(timespec="seconds")
            record["content_sha256_origin"] = "audit_backfill"
            usage[slug] = record
            mutated = True
            summary["no_attestation"] += 1
            continue

        if actual_hash != recorded_hash:
            # Disk drift — content changed since materialization.
            summary["drift"] += 1
            _quarantine(
                usage,
                slug,
                f"content_sha256_drift:expected={recorded_hash[:12]}:actual={actual_hash[:12]}",
            )
            summary["quarantined_now"].append(slug)
            mutated = True
            continue

        # Hash matches — but did the threat pattern surface? (Defense in depth:
        # a sanctioned re-write could match an attestation generated AFTER
        # the malicious edit — only matters when no_attestation backfill ran
        # before the operator noticed.)
        try:
            text = skill_md.read_text()
        except Exception as exc:
            summary["errors"].append(f"read_fail:{slug}:{exc}")
            continue
        threat = _scan_generated_skill_content(text)
        if threat:
            summary["threat_drift"] += 1
            _quarantine(usage, slug, f"threat_pattern:{threat}")
            summary["quarantined_now"].append(slug)
            mutated = True
            continue

        summary["ok"] += 1

    if mutated:
        try:
            _save_usage(root, usage)
        except Exception as exc:
            summary["errors"].append(f"sidecar_save_fail:{exc}")

    return summary


def run_audit() -> dict[str, Any]:
    """Walk all three runtime roots and report. Suitable for the daily cron."""
    results = {
        "ts": datetime.now(UTC).isoformat(),
        "per_root": {},
        "totals": {
            "scanned": 0,
            "ok": 0,
            "drift": 0,
            "missing": 0,
            "threat_drift": 0,
            "no_attestation": 0,
            "quarantined_now": [],
        },
    }
    for root in (CLAUDE_SKILLS_DIR, CODEX_SKILLS_DIR, *hermes_skill_roots().values()):
        r = audit_root(root)
        results["per_root"][str(root)] = r
        for key in ("scanned", "ok", "drift", "missing", "threat_drift", "no_attestation"):
            results["totals"][key] += r.get(key, 0)
        results["totals"]["quarantined_now"].extend(r.get("quarantined_now", []))
    return results


def list_quarantined() -> dict[str, list[dict[str, Any]]]:
    """Report current quarantines across all roots — for operator review."""
    out: dict[str, list[dict[str, Any]]] = {}
    for root in (CLAUDE_SKILLS_DIR, CODEX_SKILLS_DIR, *hermes_skill_roots().values()):
        if not root.exists():
            continue
        usage = _load_usage(root)
        quarantined = [
            {
                "slug": k,
                **{
                    kk: vv
                    for kk, vv in v.items()
                    if kk in {"quarantine_reason", "quarantined_at", "brain_procedure_id"}
                },
            }
            for k, v in usage.items()
            if v.get("quarantined")
        ]
        if quarantined:
            out[str(root)] = quarantined
    return out


def clear_quarantine(slug: str) -> dict[str, Any]:
    """Operator action: remove the quarantine flag once a skill is reviewed."""
    cleared = []
    for root in (CLAUDE_SKILLS_DIR, CODEX_SKILLS_DIR, *hermes_skill_roots().values()):
        usage = _load_usage(root)
        if slug in usage and usage[slug].get("quarantined"):
            usage[slug]["quarantined"] = False
            usage[slug]["quarantine_reason"] = ""
            usage[slug]["quarantine_cleared_at"] = datetime.now(UTC).isoformat(timespec="seconds")
            _save_usage(root, usage)
            cleared.append(str(root))
    return {"slug": slug, "cleared_in": cleared}


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="auto-skill supply-chain audit")
    parser.add_argument("action", choices=["audit", "list_quarantined", "clear"], default="audit", nargs="?")
    parser.add_argument("--slug", default=None, help="Slug to clear when action=clear")
    args = parser.parse_args()
    if args.action == "audit":
        print(json.dumps(run_audit(), indent=2, ensure_ascii=False))  # noqa: T201
    elif args.action == "list_quarantined":
        print(json.dumps(list_quarantined(), indent=2, ensure_ascii=False))  # noqa: T201
    elif args.action == "clear":
        if not args.slug:
            print(json.dumps({"error": "--slug required for clear"}, ensure_ascii=False))  # noqa: T201
            return 2
        print(json.dumps(clear_quarantine(args.slug), indent=2, ensure_ascii=False))  # noqa: T201
    return 0


if __name__ == "__main__":
    sys.exit(main())
