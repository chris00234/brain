"""brain outbox drain — replays SessionEnd transcripts that didn't reach /learn.

Triggered by:
  - SessionEnd hook (post_session_v2.sh) right after enqueue
  - SessionStart hook (manually via brain-outbox-drain.sh)
  - APScheduler `outbox_drain` job every 5 minutes (Phase 2D)

Layout:
    ~/.openclaw/outbox/brain-learn/
        pending/    <sid>.jsonl     (waiting for retry)
        inflight/   <sid>.jsonl     (during drain — crash-safe via atomic rename)
        done/       <sid>.jsonl     (7-day retention for audit)
        quarantine/ <sid>.jsonl     (after MAX_RETRIES)

Each envelope is a single JSON line:
    {"session_id":"...","transcript_path":"...","enqueued_ts":...,
     "retries":0,"next_attempt_ts":...,"schema_version":1}

Idempotent — safe to spawn concurrently from multiple triggers; uses atomic
rename + per-file mtime check to avoid double-processing.
"""

from __future__ import annotations

import json
import shutil
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

OUTBOX_ROOT = Path("/Users/chrischo/.openclaw/outbox/brain-learn")
PENDING = OUTBOX_ROOT / "pending"
INFLIGHT = OUTBOX_ROOT / "inflight"
DONE = OUTBOX_ROOT / "done"
QUARANTINE = OUTBOX_ROOT / "quarantine"

SECRET_FILE = Path("/Users/chrischo/.openclaw/credentials/.personal_webhook_secret")
LEARN_URL = "http://127.0.0.1:8791/learn"
TASKS_URL = "http://127.0.0.1:8791/brain/tasks"
LOG = Path("/Users/chrischo/.openclaw/logs/brain-outbox-drain.log")

MAX_RETRIES = 8
BACKOFF_S = [30, 60, 120, 300, 600, 1200, 2400, 3600]
DONE_RETENTION_DAYS = 7
TOOL_CALL_THRESHOLD = 10  # min tool calls to record as a task outcome


def _ensure_dirs() -> None:
    for d in (PENDING, INFLIGHT, DONE, QUARANTINE, LOG.parent):
        d.mkdir(parents=True, exist_ok=True)


def _log(line: str) -> None:
    ts = time.strftime("%Y-%m-%dT%H:%M:%S")
    msg = f"[{ts}] {line}\n"
    try:
        with LOG.open("a") as f:
            f.write(msg)
    except OSError:
        pass


def _read_secret() -> str:
    try:
        return SECRET_FILE.read_text().strip()
    except OSError:
        return ""


def _post_json(url: str, body: dict, secret: str, timeout: float = 15.0) -> tuple[int, str]:
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method="POST")  # noqa: S310
    req.add_header("Authorization", f"Bearer {secret}")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode() if e.fp else str(e)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return 0, str(e)


def _extract_transcript(path: str, max_messages: int = 20) -> tuple[str, int]:
    """Return (transcript_text, tool_call_count). Empty string on parse failure."""
    if not path or not Path(path).exists():
        return "", 0
    lines: list[str] = []
    tool_count = 0
    try:
        with Path(path).open() as f:
            for raw in f:
                try:
                    rec = json.loads(raw)
                except (json.JSONDecodeError, ValueError):
                    continue
                role = rec.get("type") or rec.get("role")
                msg = rec.get("message") or {}
                text = ""
                if isinstance(msg, dict):
                    content = msg.get("content")
                    if isinstance(content, list):
                        text_parts = []
                        for c in content:
                            if not isinstance(c, dict):
                                continue
                            if c.get("type") == "text":
                                text_parts.append(c.get("text") or "")
                            elif c.get("type") == "tool_use":
                                tool_count += 1
                        text = " ".join(text_parts)
                    elif isinstance(content, str):
                        text = content
                else:
                    text = str(msg)
                if role and text and len(text.strip()) > 5:
                    lines.append(f"{role}: {text.strip()[:1500]}")
    except OSError:
        return "", 0
    return "\n\n".join(lines[-max_messages:]), tool_count


def _extract_summary(path: str) -> str:
    """Return the last meaningful assistant text, capped at 200 chars."""
    if not path or not Path(path).exists():
        return ""
    last = ""
    try:
        with Path(path).open() as f:
            for raw in f:
                try:
                    rec = json.loads(raw)
                except (json.JSONDecodeError, ValueError):
                    continue
                if rec.get("type") != "assistant":
                    continue
                msg = rec.get("message") or {}
                content = msg.get("content") if isinstance(msg, dict) else []
                if isinstance(content, list):
                    for c in content:
                        if isinstance(c, dict) and c.get("type") == "text" and len(c.get("text", "")) > 20:
                            last = c["text"][:200]
    except OSError:
        return ""
    return last.replace("\n", " ").strip()


def _record_task_outcome(session_id: str, summary: str, tool_count: int, secret: str) -> bool:
    """Create + lifecycle a task representing this session. Best-effort."""
    title = f"Claude Code session: {session_id}"[:80]
    desc = (summary or f"Session with {tool_count} tool calls")[:200]
    code, body = _post_json(
        TASKS_URL,
        {
            "title": title,
            "description": desc,
            "priority": 5,
            "confidence": 0.8,
            "assigned_agent": "claude",
        },
        secret,
        timeout=5,
    )
    if code != 200:
        _log(f"task create failed: {code} {body[:120]}")
        return False
    try:
        task = json.loads(body)
        task_id = task.get("id")
    except Exception:
        return False
    if not task_id:
        return False

    for action in ("approve", "start"):
        _post_json(f"{TASKS_URL}/{task_id}/{action}", {}, secret, timeout=3)
    _post_json(
        f"{TASKS_URL}/{task_id}/complete",
        {"result": (summary or "completed")[:500]},
        secret,
        timeout=3,
    )
    _log(f"task lifecycle ok task={task_id} tools={tool_count}")
    return True


def _drain_one(envelope_path: Path, secret: str) -> str:
    """Process a single outbox envelope. Returns status: ok|skip_short|retry|quarantine|deferred."""
    try:
        envelope = json.loads(envelope_path.read_text())
    except Exception as e:
        _log(f"corrupt envelope {envelope_path.name}: {e}")
        envelope_path.replace(QUARANTINE / envelope_path.name)
        return "quarantine"

    if envelope.get("next_attempt_ts", 0) > time.time():
        return "deferred"

    inflight_path = INFLIGHT / envelope_path.name
    try:
        envelope_path.rename(inflight_path)
    except FileNotFoundError:
        return "deferred"  # raced with another drainer

    transcript, tool_count = _extract_transcript(envelope.get("transcript_path", ""))
    if len(transcript) < 50:
        _log(f"skip_short {envelope.get('session_id')}")
        inflight_path.replace(DONE / envelope_path.name)
        return "skip_short"

    code, body = _post_json(
        LEARN_URL,
        {"transcript": transcript, "source": "claude_code", "agent": "claude"},
        secret,
        timeout=15,
    )
    if code == 200:
        _log(f"ok {envelope.get('session_id')} len={len(transcript)} tools={tool_count}")
        if tool_count >= TOOL_CALL_THRESHOLD:
            summary = _extract_summary(envelope.get("transcript_path", ""))
            _record_task_outcome(envelope["session_id"], summary, tool_count, secret)
        inflight_path.replace(DONE / envelope_path.name)
        return "ok"

    # Failure → bump retries, schedule next attempt
    envelope["retries"] = envelope.get("retries", 0) + 1
    envelope["last_error"] = f"HTTP {code}: {body[:200]}"
    if envelope["retries"] >= MAX_RETRIES:
        _log(f"quarantine {envelope.get('session_id')} after {MAX_RETRIES} retries")
        inflight_path.write_text(json.dumps(envelope) + "\n")
        inflight_path.replace(QUARANTINE / envelope_path.name)
        return "quarantine"

    backoff = BACKOFF_S[min(envelope["retries"] - 1, len(BACKOFF_S) - 1)]
    envelope["next_attempt_ts"] = time.time() + backoff
    inflight_path.write_text(json.dumps(envelope) + "\n")
    inflight_path.replace(PENDING / envelope_path.name)
    _log(f"retry {envelope.get('session_id')} #{envelope['retries']} in {backoff}s " f"(reason: HTTP {code})")
    return "retry"


def _prune_done() -> None:
    """Delete done/ entries older than DONE_RETENTION_DAYS."""
    cutoff = time.time() - DONE_RETENTION_DAYS * 86400
    for f in DONE.glob("*.jsonl"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
        except OSError:
            pass


def _recover_orphan_inflight() -> None:
    """Move stale inflight/ envelopes back to pending/ — handles crash recovery."""
    cutoff = time.time() - 600  # 10 min stale
    for f in INFLIGHT.glob("*.jsonl"):
        try:
            if f.stat().st_mtime < cutoff:
                shutil.move(str(f), PENDING / f.name)
                _log(f"recovered orphan inflight: {f.name}")
        except OSError:
            pass


def main() -> int:
    _ensure_dirs()
    secret = _read_secret()
    if not secret:
        _log("no secret file — aborting")
        return 1

    _recover_orphan_inflight()

    counts: dict[str, int] = {}
    pending_files = sorted(PENDING.glob("*.jsonl"))
    for envelope_path in pending_files:
        status = _drain_one(envelope_path, secret)
        counts[status] = counts.get(status, 0) + 1

    _prune_done()

    if counts:
        summary = " ".join(f"{k}={v}" for k, v in sorted(counts.items()))
        _log(f"drain run: {summary}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
