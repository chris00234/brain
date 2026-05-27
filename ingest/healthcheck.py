#!/Users/chrischo/server/brain/.venv/bin/python
"""Daily healthcheck for the brain system.

Verifies every signal that could silently destroy data or degrade quality:

  1. Collection counts + shrinkage + minimum floors (15 collections)
  2. Content integrity — random sample metadata looks sane
  3. Personal ingest failure log (last 24h)
  4. Adapter watermark staleness (10 adapters)
  5. Scheduled-job staleness (last-success < expected cadence)
  6. Apple Full Disk Access regression detector
  7. MinIO backup freshness (via S3 API — not `mc`)
  8. Disk free space
  9. Ollama embedding probe
 10. Vector-store write probe
 11. Recall vector search probe
 12. Eval regression (reads eval-history.jsonl)

Runs daily at 9 AM via the brain's APScheduler. Posts a single Telegram DM only
if there's something wrong; stays silent on green. Also writes a machine-readable
daily report to logs/healthcheck-YYYY-MM-DD.json.

Usage:
  healthcheck.py [--force]    # --force always sends a status DM
"""

import argparse
import json
import logging
import random
import shutil
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

# ── Config ──────────────────────────────────────────────
BRAIN_DIR = Path("/Users/chrischo/server/brain")
STATE_FILE = BRAIN_DIR / "logs" / ".healthcheck_state.json"
FAILURE_LOG = BRAIN_DIR / "logs" / "personal-ingest-failures.jsonl"
EVAL_HISTORY = BRAIN_DIR / "logs" / "eval-history.jsonl"
SCHEDULER_DB = BRAIN_DIR / "logs" / "scheduler_history.db"
DAILY_REPORT_DIR = BRAIN_DIR / "logs"

# Collections we actively monitor for count + shrinkage + min_docs floor.
# Updated 2026-04-12 to reflect production reality:
#   - knowledge floor lowered to 300 after Apr 10 SOUL.md dedup (419→334, expected)
#   - `personal` min raised from 10 to 80 (was masking today's 63-doc broken state)
#   - dropped legacy notes/messages/calendar/tasks (deleted 2026-04-12, content
#     lives in `personal`; the 80-doc floor on `personal` already monitors
#     the aggregate of all 4 source types)
# Intentionally NOT monitored: experience_compressed (transient),
# semantic_contradictions (0 is a valid/desired state after resolution).
MONITORED_COLLECTIONS = {
    "knowledge": {"min_docs": 300, "source": "reindex"},
    "experience": {"min_docs": 1500, "source": "reindex"},
    "canonical": {"min_docs": 3000, "source": "canonical_pipeline"},
    # Legacy Qdrant aliases folded into `knowledge` with origin filters.
    "context": {"min_docs": 400, "source": "reindex"},
    "semantic_memory": {"min_docs": 150, "source": "memory_store"},
    "obsidian": {"min_docs": 900, "source": "obsidian_sync"},
    "personal": {"min_docs": 80, "source": "personal_ingest"},
    "code": {"min_docs": 3500, "source": "code_index_refresh"},
    "patterns": {"min_docs": 5, "source": "pattern_detector"},
}

# Collections to sample for content integrity (metadata sanity check)
SAMPLED_COLLECTIONS = ("personal", "knowledge", "canonical", "semantic_memory")
SAMPLE_SIZE = 5

# How stale a scheduled job can be before we alert (in hours).
# Source: scheduler_history.db most-recent successful run.
EXPECTED_JOB_CADENCE_HOURS = {
    "personal_ingest": 8,
    "canonical_pipeline": 26,
    "reindex": 14,
    "eval_run": 26,
    "backup": 26,
    "neo4j_backup": 26,
    "memory_consolidation": 26,
    "gmail_ingest": 26,
    "code_index_refresh": 26,
    "memory_leak_detector": 192,  # weekly
    "near_dedup": 192,
    "lint_memory": 192,
}

MINIO_BUCKET = "rag-backups"
BACKUP_STALENESS_HOURS = 36
DISK_WARN_GB = 20
DISK_CRIT_GB = 5
OLLAMA_URL = "http://127.0.0.1:11434"
BRAIN_URL = "http://127.0.0.1:8791"

# Add brain_core + cli to sys.path so we can reuse _minio and config
sys.path.insert(0, str(BRAIN_DIR / "brain_core"))
sys.path.insert(0, str(BRAIN_DIR / "cli"))

try:
    from config import EMBED_MODEL_VERSION
except ImportError:
    EMBED_MODEL_VERSION = "multilingual-e5-large-instruct:v1"

log = logging.getLogger("brain.healthcheck")


# Collections small + stable enough that per-ID shrinkage diffs are useful.
# For large churny collections (code, experience) per-ID diff is noisy and slow.
ID_DIFF_COLLECTIONS = frozenset({"knowledge", "personal", "patterns", "semantic_memory"})


def get_collection_ids(name: str, col_id: str, limit: int = 2000) -> dict[str, str]:
    """Return {doc_id: human-readable source} for a collection. Best-effort.

    Tries common metadata keys (source_path / path / source / title). If none
    present, falls back to the raw id. Cost is a single /get call; caller
    should only invoke for small collections (see ID_DIFF_COLLECTIONS).
    """
    from vector_store import get_vector_store

    try:
        points = get_vector_store().get(
            name,  # VectorStore addresses by name; col_id is the same string now
            limit=limit,
            with_payload=True,
            with_documents=False,
        )
    except Exception:
        return {}

    out: dict[str, str] = {}
    for p in points:
        meta = p.payload or {}
        label = (
            meta.get("source_path")
            or meta.get("path")
            or meta.get("source")
            or meta.get("title")
            or meta.get("file")
            or p.id
        )
        out[p.id] = str(label)[:180]
    return out


# ── Collection counts ──────────────────────────────────
def get_collection_counts() -> dict[str, int | str]:
    """Live counts via the vector store abstraction."""
    from vector_store import get_vector_store

    try:
        store = get_vector_store()
        names = store.list_collections()
    except Exception as e:
        return {"_error": str(e)}

    counts: dict[str, int] = {}
    # Qdrant migration collapsed legacy `context` and `patterns` into
    # `knowledge` aliases. They will not appear in list_collections(), but
    # store.count(alias) still returns the filtered count.
    for name in sorted(set(names) | set(MONITORED_COLLECTIONS)):
        try:
            counts[name] = store.count(name)
        except Exception:
            counts[name] = -1
    return counts


def get_collection_id(name: str) -> str | None:
    """Resolve a collection name to its opaque backend identifier.

    Under VectorStore the "identifier" is just the name itself — we
    preserve the function signature (and its name) so call sites in
    check_chroma_write() and external tooling keep compiling without
    touching.
    """
    from vector_store import get_vector_store

    try:
        names = set(get_vector_store().list_collections())
        return name if name in names else None
    except Exception:
        return None


# ── State (uses safe_state for file locking) ──────────────
try:
    from safe_state import load_state as _safe_load
    from safe_state import save_state as _safe_save

    def load_state() -> dict:
        return _safe_load(STATE_FILE)

    def save_state(state: dict) -> None:
        state["last_check"] = datetime.now(UTC).isoformat()
        _safe_save(STATE_FILE, state)
except ImportError:

    def load_state() -> dict:
        if STATE_FILE.exists():
            try:
                return json.loads(STATE_FILE.read_text())
            except Exception:
                return {}
        return {}

    def save_state(state: dict) -> None:
        state["last_check"] = datetime.now(UTC).isoformat()
        STATE_FILE.write_text(json.dumps(state, indent=2))


# ── Failure log inspection ──────────────────────────────
def recent_failures(hours: int = 24) -> list[dict]:
    """Return failure entries newer than `hours` ago (UTC-aware comparison)."""
    if not FAILURE_LOG.exists():
        return []
    cutoff = datetime.now(UTC) - timedelta(hours=hours)
    out = []
    try:
        for line in FAILURE_LOG.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                ts_raw = entry.get("timestamp", "")
                if not ts_raw:
                    continue
                ts = datetime.fromisoformat(ts_raw.rstrip("Zz"))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=UTC)
                if ts >= cutoff:
                    out.append(entry)
            except Exception as exc:
                log.debug("failed to parse failure log line: %s", exc)
                continue
    except Exception as exc:
        log.debug("failed to read failure log: %s", exc)
    return out


def check_fda_regression() -> list[str]:
    """Distinct alert if any recent ingest failure contains 'Operation not permitted'.
    This is the macOS Full Disk Access revocation pattern that happens after
    Homebrew upgrades Python (3.13 → 3.14 path change).
    """
    issues = []
    fails = recent_failures(hours=24)
    fda_fails = [f for f in fails if "Operation not permitted" in (f.get("error") or "")]
    if fda_fails:
        adapters = sorted({f.get("adapter", "?") for f in fda_fails})
        issues.append(
            f"❌ Full Disk Access revoked — {len(fda_fails)} failures in 24h (adapters: {', '.join(adapters)}). "
            f"Fix: System Settings → Privacy & Security → Full Disk Access → re-add python3."
        )
    return issues


# ── MinIO backup freshness (via S3 API, not `mc`) ──────
def latest_backup_age_hours() -> tuple[float | None, str]:
    """Return (age_hours, reason).

    reason is 'ok' | 'boto_missing' | 'unreachable' | 'no_qdrant_backups'.

    Uses _minio.s3_client() to query MinIO via boto3. Parses LastModified on the
    newest Qdrant backup object — does NOT rely on filename parsing.
    """
    try:
        from _minio import s3_client
    except Exception as e:
        return None, f"boto_missing: {e}"
    try:
        s3 = s3_client()
        resp = s3.list_objects_v2(Bucket=MINIO_BUCKET, Prefix="qdrant-backup-")
    except Exception as e:
        return None, f"unreachable: {e}"
    tarballs = [o for o in resp.get("Contents", []) if o["Key"].endswith(".tar.gz")]
    if not tarballs:
        return None, "no_qdrant_backups"
    newest = max(tarballs, key=lambda o: o["LastModified"])
    age = (datetime.now(UTC) - newest["LastModified"]).total_seconds() / 3600
    return age, "ok"


# ── Disk space ──────────────────────────────────────────
def check_disk_space() -> tuple[list[str], float]:
    issues = []
    free_gb = shutil.disk_usage("/").free / 1e9
    if free_gb < DISK_CRIT_GB:
        issues.append(f"❌ Disk CRITICAL: {free_gb:.1f} GB free — ingest will fail imminently")
    elif free_gb < DISK_WARN_GB:
        issues.append(f"⚠️ Disk free: {free_gb:.1f} GB (warn threshold {DISK_WARN_GB} GB)")
    return issues, free_gb


# ── Ollama embedding probe ─────────────────────────────
def check_ollama_embedding() -> list[str]:
    """Verify Ollama can produce an embedding. Catches model unload / service death."""
    import urllib.request

    try:
        req = urllib.request.Request(  # noqa: S310
            f"{OLLAMA_URL}/api/embeddings",
            data=json.dumps(
                {
                    "model": "blaifa/multilingual-e5-large-instruct",
                    "prompt": "healthcheck probe",
                }
            ).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=20) as resp:  # noqa: S310
            body = json.loads(resp.read())
        emb = body.get("embedding") or []
        if not emb or len(emb) < 100:
            return [f"❌ Ollama embedding returned {len(emb)} dims (expected 1024)"]
        return []
    except Exception as e:
        return [f"❌ Ollama embedding probe failed: {e}"]


# ── Vector-store write probe ───────────────────────────
def check_chroma_write() -> list[str]:
    """Round-trip create/upsert/delete against a dedicated probe collection.
    Catches: vector backend down, write path broken. Name kept for back-
    compat with existing call sites + telemetry dashboards."""
    from vector_store import get_vector_store

    probe_name = "healthcheck_probe"
    try:
        store = get_vector_store()
        store.create_collection(probe_name, {"purpose": "healthcheck"})
        probe_id = f"probe:{datetime.now(UTC).isoformat()}"
        probe_emb = [0.1] * 1024
        store.upsert(
            probe_name,
            ids=[probe_id],
            vectors=[probe_emb],
            documents=["healthcheck probe"],
            payloads=[{"type": "probe", "created_at": datetime.now(UTC).isoformat()}],
        )
        store.delete(probe_name, ids=[probe_id])
        return []
    except Exception as e:
        return [f"❌ vector store write probe failed: {e}"]


def _bearer_secret() -> str | None:
    """Lazy load the brain bearer secret via the shared helper. Returns None if absent."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "brain_core"))
        from config import SECRET_FILE, load_bearer_secret

        if not SECRET_FILE.exists():
            return None
        return load_bearer_secret()
    except Exception:
        return None


# ── Recall vector search probe ─────────────────────────
def check_recall_vector() -> list[str]:
    """Hit /recall with a simple query. Catches the ChromaDB 1.4.1 `where` bug and
    any other silent recall-path breakage.
    """
    import urllib.request

    secret = _bearer_secret()
    if not secret:
        return []  # can't probe without bearer
    try:
        req = urllib.request.Request(  # noqa: S310
            f"{BRAIN_URL}/recall?q=homelab&n=3",
            headers={"Authorization": f"Bearer {secret}"},
        )
        with urllib.request.urlopen(req, timeout=20) as resp:  # noqa: S310
            body = json.loads(resp.read())
        if not body.get("results"):
            return ["⚠️ /recall returned 0 results for 'homelab' probe"]
        return []
    except Exception as e:
        return [f"❌ /recall probe failed: {e}"]


def check_recall_vector_temporal() -> list[str]:
    """Hit /recall with since/until — specifically catches the string-operand bug."""
    import urllib.request

    secret = _bearer_secret()
    if not secret:
        return []
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    week_ago = (datetime.now(UTC) - timedelta(days=7)).strftime("%Y-%m-%d")
    try:
        req = urllib.request.Request(  # noqa: S310
            f"{BRAIN_URL}/recall?q=homelab&since={week_ago}&until={today}&n=3",
            headers={"Authorization": f"Bearer {secret}"},
        )
        with urllib.request.urlopen(req, timeout=20):  # noqa: S310
            pass
        return []
    except urllib.error.HTTPError as e:
        return [f"❌ /recall with since/until returned HTTP {e.code} (ChromaDB where bug?)"]
    except Exception as e:
        return [f"❌ /recall temporal probe failed: {e}"]


# ── Content integrity sample ───────────────────────────
def check_content_integrity() -> list[str]:
    """Random-sample a few docs from each critical collection. Assert non-empty
    document, non-empty source, and recognizable embed_model_version.

    Rewritten 2026-04-21 to use the VectorStore abstraction — the prior
    implementation referenced an undefined ``CHROMA_API`` constant and had
    been silently ``NameError``-ing for every run since the Qdrant cutover
    (swallowed by the outer except, so the probe stayed green while it was
    actually not checking anything).
    """
    issues = []
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "brain_core"))
        from vector_store import get_vector_store

        store = get_vector_store()
    except Exception as e:
        return [f"⚠️ content integrity: vector_store unavailable: {e}"]

    for coll in SAMPLED_COLLECTIONS:
        try:
            total = store.count(coll)
            if total == 0:
                continue
            limit = min(SAMPLE_SIZE * 5, total)
            points = store.get(
                coll,
                limit=limit,
                with_payload=True,
                with_documents=True,
            )
            if not points:
                continue
            random.shuffle(points)
            sample = points[:SAMPLE_SIZE]
            empty_docs = sum(1 for p in sample if not (p.document or "").strip())
            if empty_docs:
                issues.append(f"⚠️ {coll}: {empty_docs}/{SAMPLE_SIZE} sampled docs have empty content")
            missing_source = sum(1 for p in sample if not (p.payload or {}).get("source"))
            if missing_source > SAMPLE_SIZE // 2:
                # Some collections (semantic_memory) don't use 'source'; tolerate up to 50%.
                issues.append(
                    f"⚠️ {coll}: {missing_source}/{SAMPLE_SIZE} sampled docs missing payload.source"
                )
        except Exception as e:
            issues.append(f"⚠️ content integrity probe failed for {coll}: {e}")
    return issues


# ── Eval regression detector ───────────────────────────
def check_eval_regression() -> list[str]:
    """Read the last 3 entries of eval-history.jsonl. Alert if:
    - latest hit_content@5 < 85, OR
    - 2+ consecutive entries show delta <= -5 vs baseline
    """
    issues = []
    if not EVAL_HISTORY.exists():
        return issues
    try:
        lines = [line.strip() for line in EVAL_HISTORY.read_text().splitlines() if line.strip()]
        if not lines:
            return issues
        recent = [json.loads(line) for line in lines[-5:]]
        # Last entry check
        latest = recent[-1]
        hit5 = latest.get("hit_content@5") or latest.get("metrics", {}).get("hit_content@5", 100)
        if isinstance(hit5, int | float) and hit5 < 85:
            issues.append(f"❌ Eval hit_content@5 = {hit5:.1f}% (floor 85%)")
        # Consecutive regression check
        regressions = 0
        for entry in recent[-3:]:
            err = entry.get("error", "") or ""
            if "REGRESSION" in err and "hit_content@5" in err:
                regressions += 1
        if regressions >= 2:
            issues.append(
                f"⚠️ Eval regression: {regressions} consecutive hit_content@5 drops (check eval-history.jsonl)"
            )
    except Exception as e:
        issues.append(f"⚠️ Eval regression probe failed: {e}")
    return issues


# ── Job staleness monitor ──────────────────────────────
def check_job_staleness() -> list[str]:
    """For each critical job, confirm its most recent success is within expected cadence.
    Reads scheduler_history.db for last_success times.
    """
    issues = []
    if not SCHEDULER_DB.exists():
        return [f"⚠️ scheduler_history.db missing at {SCHEDULER_DB}"]
    try:
        conn = sqlite3.connect(f"file:{SCHEDULER_DB}?mode=ro", uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        for job_name, max_hours in EXPECTED_JOB_CADENCE_HOURS.items():
            try:
                cur.execute(
                    """SELECT started_at FROM runs
                       WHERE name=? AND (error IS NULL OR error='')
                       ORDER BY started_at DESC LIMIT 1""",
                    (job_name,),
                )
                row = cur.fetchone()
                if not row:
                    issues.append(f"⚠️ Job `{job_name}` has no successful runs on record")
                    continue
                started = row["started_at"]
                if isinstance(started, str):
                    # Try to parse ISO or assume local time with no tz
                    try:
                        dt = datetime.fromisoformat(started.replace("Z", "+00:00"))
                    except ValueError:
                        continue
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=UTC)
                    age_h = (datetime.now(UTC) - dt).total_seconds() / 3600
                    if age_h > max_hours:
                        issues.append(f"⚠️ Job `{job_name}` last success {age_h:.0f}h ago (max {max_hours}h)")
            except sqlite3.OperationalError:
                continue
        conn.close()
    except Exception as e:
        issues.append(f"⚠️ Job staleness probe failed: {e}")
    return issues


# ── Telegram ────────────────────────────────────────────
def send_telegram(text: str) -> bool:
    try:
        from telegram_alert import send_chris_telegram

        return send_chris_telegram(
            text,
            source="ingest.healthcheck",
            severity="warn",
        )
    except Exception as exc:
        log.warning("telegram dispatch failed: %s", exc)
        return False


# ── Main check ──────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(description="Daily brain healthcheck")
    parser.add_argument("--force", action="store_true", help="Always send a status DM")
    parser.add_argument("--dry-run", action="store_true", help="Print but don't Telegram")
    args = parser.parse_args()

    issues: list[str] = []
    report: dict = {"timestamp": datetime.now(UTC).isoformat(), "checks": {}}

    # 1. Collection growth + floors
    counts = get_collection_counts()
    state = load_state()
    yesterday_counts = state.get("counts", {})
    yesterday_ids = state.get("collection_ids", {})

    # Snapshot today's IDs for diff-worthy collections before we compare.
    # Even when there's no shrinkage today, we need this to diff tomorrow.
    today_ids: dict[str, dict[str, str]] = {}
    if "_error" not in counts:
        for name in ID_DIFF_COLLECTIONS:
            if name not in MONITORED_COLLECTIONS:
                continue
            cid = get_collection_id(name)
            if not cid:
                continue
            ids_map = get_collection_ids(name, cid)
            if ids_map:
                today_ids[name] = ids_map

    shrinkage_details: dict[str, dict] = {}

    if "_error" in counts:
        issues.append(f"❌ ChromaDB unreachable: {counts['_error']}")
    else:
        for name, spec in MONITORED_COLLECTIONS.items():
            today = counts.get(name, -1)
            yesterday = yesterday_counts.get(name, -1)
            if today < 0:
                issues.append(f"❌ Collection `{name}` count unreadable")
                continue
            if today == 0:
                issues.append(f"❌ Collection `{name}` is EMPTY (source: {spec['source']})")
                continue
            if yesterday < 0:
                continue  # no baseline yet
            if today < spec["min_docs"]:
                issues.append(
                    f"⚠️ Collection `{name}` = {today} (below min {spec['min_docs']}, source: {spec['source']})"
                )
            if today < yesterday:
                dropped_info = ""
                if name in today_ids and name in yesterday_ids:
                    prev_map = yesterday_ids[name] or {}
                    curr_map = today_ids[name] or {}
                    dropped = sorted(set(prev_map.keys()) - set(curr_map.keys()))
                    if dropped:
                        sample = [prev_map.get(d, d) for d in dropped[:5]]
                        dropped_info = f" — dropped: {', '.join(sample)}"
                        if len(dropped) > 5:
                            dropped_info += f" (+{len(dropped) - 5} more)"
                        shrinkage_details[name] = {
                            "dropped_count": len(dropped),
                            "dropped_ids": dropped[:20],
                            "dropped_sources": [prev_map.get(d, d) for d in dropped[:20]],
                        }
                issues.append(f"⚠️ Collection `{name}` shrank ({yesterday} → {today}){dropped_info}")
    report["checks"]["collection_counts"] = counts
    if shrinkage_details:
        report["checks"]["shrinkage_details"] = shrinkage_details

    # 2. Content integrity sampling
    integrity_issues = check_content_integrity()
    issues.extend(integrity_issues)
    report["checks"]["content_integrity_issues"] = integrity_issues

    # 3. Recent ingest failures
    failures = recent_failures(hours=24)
    if failures:
        adapters = sorted({f.get("adapter", "?") for f in failures})
        issues.append(f"⚠️ {len(failures)} ingest failures in last 24h (adapters: {', '.join(adapters)})")
    report["checks"]["recent_failures_24h"] = len(failures)

    # 4. Full Disk Access regression
    fda_issues = check_fda_regression()
    issues.extend(fda_issues)
    report["checks"]["fda_issues"] = fda_issues

    # 5. Adapter watermark staleness (legacy check, kept for compat)
    ADAPTER_STATES = {
        "git_activity": (BRAIN_DIR / "logs" / "git-activity-state.json", 48),
        # 2026-05-20: screen_time adapter removed — Chris no longer accumulates
        # screen-time data, so the ingester + cron + state file are all gone.
        # Keeping this freshness check would have started failing immediately
        # once the 192h window elapsed past the last (Apr 26) write.
        "gmail": (Path("/Users/chrischo/server/brain/state/ingest/jenna/gmail_ingest_state.json"), 48),
        "browser": (
            Path("/Users/chrischo/server/brain/state/ingest/sage/browser_ingest_state.json"),
            48,
        ),
        "obsidian_sync": (Path("/Users/chrischo/server/brain/state/ingest/jenna/obsidian_sync_state.json"), 6),
        "shell": (BRAIN_DIR / "logs" / "shell-ingest-state.json", 48),
        "claude_code_sessions": (BRAIN_DIR / "logs" / "claude-code-sessions-state.json", 48),
        "code_index": (BRAIN_DIR / "logs" / "code-index-state.json", 48),
    }
    for adapter, (state_path, max_hours) in ADAPTER_STATES.items():
        try:
            if not state_path.exists():
                continue
            state_data = json.loads(state_path.read_text())
            last_run = state_data.get("last_run", "") or state_data.get("last_ingest", "")
            if last_run:
                last_dt = datetime.fromisoformat(last_run.replace("Z", "+00:00"))
                if last_dt.tzinfo is None:
                    last_dt = last_dt.replace(tzinfo=UTC)
                age_hours = (datetime.now(UTC) - last_dt).total_seconds() / 3600
                if age_hours > max_hours:
                    issues.append(
                        f"⚠️ Adapter `{adapter}` stale — last run {age_hours:.0f}h ago (max: {max_hours}h)"
                    )
        except Exception as exc:
            log.debug("adapter staleness check failed for %s: %s", adapter, exc)

    # 6. Job staleness via scheduler_history.db
    stale_jobs = check_job_staleness()
    issues.extend(stale_jobs)
    report["checks"]["stale_jobs"] = stale_jobs

    # 7. Eval regression
    eval_issues = check_eval_regression()
    issues.extend(eval_issues)
    report["checks"]["eval_issues"] = eval_issues

    # 8. MinIO backup freshness
    age, reason = latest_backup_age_hours()
    if age is None:
        if reason == "boto_missing":
            issues.append(f"❌ MinIO check: boto3 import failed ({reason}) — check BRAIN_PYTHON")
        elif reason == "unreachable":
            issues.append(f"❌ MinIO unreachable: {reason}")
        elif reason == "no_qdrant_backups":
            issues.append(f"❌ MinIO bucket `{MINIO_BUCKET}` has no vector-store backups")
        else:
            issues.append(f"⚠️ MinIO backup check failed: {reason}")
    elif age > BACKUP_STALENESS_HOURS:
        issues.append(f"⚠️ Latest MinIO backup is {age:.1f}h old (threshold {BACKUP_STALENESS_HOURS}h)")
    report["checks"]["minio_backup"] = {"age_hours": age, "reason": reason}

    # 9. Disk space
    disk_issues, free_gb = check_disk_space()
    issues.extend(disk_issues)
    report["checks"]["disk_free_gb"] = round(free_gb, 1)

    # 10. Ollama embedding probe
    ollama_issues = check_ollama_embedding()
    issues.extend(ollama_issues)
    report["checks"]["ollama_issues"] = ollama_issues

    # 11. ChromaDB write probe
    chroma_write_issues = check_chroma_write()
    issues.extend(chroma_write_issues)
    report["checks"]["chroma_write_issues"] = chroma_write_issues

    # 12. Recall probes
    recall_issues = check_recall_vector() + check_recall_vector_temporal()
    issues.extend(recall_issues)
    report["checks"]["recall_issues"] = recall_issues

    # 13. Pending contradictions queue depth — ingest-time near-duplicate
    # gate (learn.py) clears most of these automatically, so a large backlog
    # means something is slipping through and a human should look.
    pending_contradictions = counts.get("semantic_contradictions")
    if isinstance(pending_contradictions, int) and pending_contradictions >= 50:
        issues.append(
            f"⚠️ {pending_contradictions} pending contradictions — run `auto_resolve_contradictions` "
            f"or review at /memory/contradictions"
        )
    report["checks"]["pending_contradictions"] = pending_contradictions

    # Persist baseline for tomorrow
    if "_error" not in counts:
        save_state({"counts": counts, "collection_ids": today_ids})

    report["issues"] = issues
    report["issue_count"] = len(issues)

    # Write daily JSON report
    try:
        day_path = DAILY_REPORT_DIR / f"healthcheck-{datetime.now(UTC).strftime('%Y-%m-%d')}.json"
        day_path.write_text(json.dumps(report, indent=2))
    except Exception as e:
        sys.stdout.write(f"WARNING: failed to write daily report: {e}\n")

    # Print summary
    sys.stdout.write(f"Healthcheck — {report['timestamp']}\n")
    sys.stdout.write(f"Counts: {counts}\n")
    sys.stdout.write(f"Disk free: {free_gb:.1f} GB\n")
    sys.stdout.write(f"Issues: {len(issues)}\n")
    for i in issues:
        sys.stdout.write(f"  {i}\n")

    # Telegram
    if args.dry_run:
        return
    if issues:
        msg = (
            "🧠 Brain healthcheck — issues detected:\n\n"
            + "\n".join(issues)
            + "\n\nFull report: ~/server/brain/logs/healthcheck-"
            + datetime.now(UTC).strftime("%Y-%m-%d")
            + ".json"
        )
        send_telegram(msg)
    elif args.force:
        msg = (
            "🧠 Brain healthcheck — all green ✅\n\n"
            + f"Disk: {free_gb:.1f} GB free\n"
            + "\n".join(f"  {k}: {v}" for k, v in counts.items() if not str(k).startswith("_"))
        )
        send_telegram(msg)


if __name__ == "__main__":
    main()
