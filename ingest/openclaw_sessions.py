#!/opt/homebrew/bin/python3
"""OpenClaw session ingest — Jenna distills decisions and preferences from agent transcripts.

Reads JSONL session files from ~/.openclaw/agents/{agent}/sessions/,
pre-filters to substantive user-assistant text exchanges, dispatches
batches to Jenna for distillation, writes kept summaries as schema-compliant
raw records to raw/inbox/.

Pipeline: JSONL → pre-filter → Jenna distillation → raw/inbox → canonical pipeline

Usage:
  openclaw_sessions.py [--dry-run] [--agents jenna,liz,ellie,sage] [--max-sessions 50]
"""

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from llm_dispatch import dispatch_json
import logging

log = logging.getLogger("brain.openclaw_sessions")


# ── Config ──────────────────────────────────────────────
AGENTS_DIR = Path.home() / ".openclaw/agents"
INBOX_DIR = Path("/Users/chrischo/server/knowledge/raw/inbox")
STATE_FILE = Path("/Users/chrischo/server/brain/logs/openclaw-sessions-state.json")
FAILURE_LOG = Path("/Users/chrischo/server/brain/logs/openclaw-sessions-failures.jsonl")
DLQ_FILE = Path("/Users/chrischo/server/brain/logs/openclaw-sessions-dlq.jsonl")
DEAD_FILE = Path("/Users/chrischo/server/brain/logs/openclaw-sessions-dead.jsonl")
MAX_DLQ_ATTEMPTS = 5
MAX_DLQ_ENTRIES = 10000

DISPATCH_AGENT = "jenna"
DISPATCH_TIMEOUT = 300
BATCH_SIZE = 8  # exchanges per dispatch (keep prompt short for quality)

ACTIVE_AGENTS = {"jenna", "liz", "ellie", "sage", "market", "claude"}
MIN_TEXT_LEN = 50
MAX_EXCHANGE_CHARS = 2000  # cap per exchange before batching

# 2026-04-17: expanded brain-machinery prompt filter.
# Root cause of the reject loop: the jenna agent session is BOTH the
# subject (ingest reads from jenna/sessions/) AND the inspector (ingest
# dispatches to Jenna for distillation). Without filtering,
# 95%+ of jenna's session is brain→Jenna mechanical dispatches — not
# real Chris↔Jenna conversations. Jenna correctly rejects them all,
# but the ingest burns LLM cycles discovering that fact every cron run.
#
# These prefixes cover every known brain-code → agent dispatch template
# so they're dropped at parse time (before LLM call).
META_INGEST_PREFIXES = (
    "You are Jenna. Review these",
    "You are Sage. Review these",
    "You are Ellie. Review these",
    "You are extracting durable memories about Chris",  # learn.py DISTILL_PROMPT
    "You are Chris's second brain.",                   # hyde.py HYDE_PROMPT
    "Rewrite the following search query",              # hyde.py EXPAND_PROMPT
    "Rewrite the query below",                         # hyde.py EXPAND_PROMPT new form
    "[brain_loop URGENT]",                             # brain_loop._telegram_alert (pre-Bot-API)
    "[BRAIN MEMORY NUDGE]",                            # memory_nudge (pre-migration)
    "[BRAIN ALERT]",                                   # scheduler._alert_failure (pre-migration)
    "[BRAIN SLO ",                                     # slos.py _alert_telegram prefix
    "[AGENT MSG]",                                     # agent_messenger._escalate
    "[brain_loop → ",                                  # brain_loop dispatch placeholder
    "Classify the following",                          # atoms_gate / ingest_classifier
    "Extract entities from",                           # entity_graph
    "Generate a dense summary",                        # synthesis prompts
    "Continue where you left off",                     # brain reasoning resume
    "Return JSON array ONLY",                          # mechanical JSON extractor prompts
    "Return JSON ONLY",                                # variant
    "You ARE Chris",                                   # jenna persona evaluator prompts
    "As Chris's second brain, answer",                 # synthesis.reflect prompts
    "Today is ",                                       # daily/weekly synthesis headers
    "Given this session transcript",                   # learn.py variant
    "Given the following facts",                       # decide / reason prompts
    "Score this atom",                                 # confidence_calibration eval
    "You are Jenna, Chris's chief of staff",           # daily_synthesis prompt
    "You are Jenna. Chris's chief of staff",           # variant
    "Read HEARTBEAT.md",                               # jenna heartbeat
    "System: [2",                                       # brain-forwarded system wrappers
    "Execute this task:",                              # task_queue dispatches
    "[BRAIN EVAL ALERT]",                              # eval_run regression alerts
    "[BRAIN ALERT]",                                   # alerts (already above but safe)
    "## Chris's Profile",                              # profile dump context prompts
    "## Identity",                                     # profile section dumps
)

# 2026-04-17: assistant-side filter. Skip exchanges whose assistant reply
# is a pure JSON extraction blob — that's the PRIOR round of this same
# ingest pipeline talking to itself. Regex catches the top-level JSON
# envelopes returned by learn.py distill / this file's own distillation.
_ASSISTANT_EXTRACTION_JSON_PATTERNS = (
    '{"keep":',           # this file's own output
    '{"memories":',       # learn.py output
    '{"corrections":',    # learn.py output
    '{"topic_key":',      # ingest_classifier
    '{"entities":',       # entity_graph
    '{"narrative":',      # daily synthesis output
    '[{"content":',       # classify bulk output
    '```json\n{"keep"',   # fenced variants
    '```json\n[',         # fenced array variants
)


# ── State (safe_state if available) ─────────────────────
try:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "brain_core"))
    from safe_state import load_state as _safe_load
    from safe_state import save_state as _safe_save

    def load_state():
        return _safe_load(STATE_FILE)

    def save_state(state):
        _safe_save(STATE_FILE, state)
except ImportError:

    def load_state():
        if STATE_FILE.exists():
            try:
                return json.loads(STATE_FILE.read_text())
            except Exception:
                return {}
        return {}

    def save_state(state):
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(state, indent=2))


def log_failure(reason: str) -> None:
    try:
        FAILURE_LOG.parent.mkdir(parents=True, exist_ok=True)
        with FAILURE_LOG.open("a") as f:
            f.write(json.dumps({"timestamp": datetime.now().isoformat(), "reason": reason[:500]}) + "\n")
    except Exception as exc:
        log.debug("openclaw_sessions: failure-log write skipped: %s", exc)


def queue_for_retry(
    agent: str,
    session_id: str,
    session_date: str,
    batch_idx: int,
    exchanges: list[dict],
    error: str,
    attempt: int = 1,
) -> None:
    """Append a failed batch to the DLQ with full payload for later retry."""
    try:
        DLQ_FILE.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "ts": datetime.now().isoformat(),
            "agent": agent,
            "session_id": session_id,
            "session_date": session_date,
            "batch_idx": batch_idx,
            "exchanges": exchanges,
            "error": error[:500],
            "attempt": attempt,
        }
        with DLQ_FILE.open("a") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:
        log_failure(f"DLQ write failed: {e}")


# ── JSONL Parsing ───────────────────────────────────────
def extract_user_text(content: list) -> str:
    """Extract actual user text from content blocks, stripping Telegram metadata."""
    texts = []
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "text":
            continue
        text = block.get("text", "")
        # Strip Telegram metadata wrappers (two variants)
        text = re.sub(
            r"^(?:Conversation info|Sender) \(untrusted metadata\):\s*```json\s*\{[^}]*\}\s*```\s*",
            "",
            text,
            flags=re.DOTALL,
        ).strip()
        # Strip cron/deliver prefix metadata
        text = re.sub(r"^\[cron:[^\]]+\]\s*", "", text).strip()
        text = re.sub(r"^\[\w+ \d{4}-\d{2}-\d{2} \d{2}:\d{2} \w+\]\s*", "", text).strip()
        if text:
            texts.append(text)
    return "\n".join(texts)


def extract_assistant_text(content: list) -> str:
    """Extract assistant text, skipping thinking/toolCall/toolResult blocks."""
    texts = []
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "text":
            continue
        text = block.get("text", "").strip()
        # Skip reply markers
        text = re.sub(r"^\[\[reply_to_\w+\]\]\s*", "", text).strip()
        if text:
            texts.append(text)
    return "\n".join(texts)


def parse_session(path: Path, offset: int = 0) -> tuple[list[dict], int, str]:
    """Parse a session JSONL, extracting substantive user-assistant exchanges.

    Returns (exchanges, new_offset, session_date).
    Each exchange = {user: str, assistant: str, timestamp: str}.
    """
    exchanges = []
    session_date = ""
    data = path.read_bytes()
    new_offset = len(data)

    if offset >= new_offset:
        return [], offset, ""

    # session metadata only lives on line 0; read it even when resuming from a saved offset
    if offset > 0:
        first_nl = data.find(b"\n")
        if first_nl > 0:
            try:
                first = json.loads(data[:first_nl].decode("utf-8", errors="replace"))
                if first.get("type") == "session":
                    ts = first.get("timestamp", "")
                    session_date = ts[:10] if ts else ""
            except Exception:
                pass

    lines = data[offset:].decode("utf-8", errors="replace").splitlines()
    pending_user = None

    for line in lines:
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue

        rtype = record.get("type", "")

        if rtype == "session" and not session_date:
            ts = record.get("timestamp", "")
            session_date = ts[:10] if ts else ""
            continue

        if rtype != "message":
            continue

        msg = record.get("message", {})
        role = msg.get("role", "")
        content = msg.get("content", [])
        if not isinstance(content, list):
            continue

        if role == "user":
            text = extract_user_text(content)
            if text.startswith(META_INGEST_PREFIXES):
                pending_user = None  # meta-ingest prompt — drop this exchange entirely
                continue
            if len(text) >= MIN_TEXT_LEN:
                pending_user = {
                    "text": text[:MAX_EXCHANGE_CHARS],
                    "timestamp": record.get("timestamp", ""),
                }
        elif role == "assistant" and pending_user:
            text = extract_assistant_text(content)
            # Skip pure-extraction JSON replies: these are prior rounds of
            # this same ingest pipeline talking to itself. They have no
            # Chris-signal even if the user prompt wasn't a meta prefix.
            # Use a whitespace-tolerant heuristic: strip ALL leading whitespace
            # + optional fence, then check if the first token resembles a
            # known extraction-schema key.
            stripped = text.lstrip()
            if stripped.startswith("```"):
                stripped = stripped.split("\n", 1)[-1].lstrip()
            if stripped.startswith("{") or stripped.startswith("["):
                # Strip optional whitespace/newlines between { and first key
                compact = "".join(stripped.split())  # removes all whitespace
                if any(compact.startswith(p) for p in _ASSISTANT_EXTRACTION_JSON_PATTERNS):
                    pending_user = None
                    continue
            if len(text) >= MIN_TEXT_LEN:
                exchanges.append(
                    {
                        "user": pending_user["text"],
                        "assistant": text[:MAX_EXCHANGE_CHARS],
                        "timestamp": pending_user["timestamp"] or record.get("timestamp", ""),
                    }
                )
                pending_user = None

    return exchanges, new_offset, session_date


# ── Agent Dispatch ──────────────────────────────────────
def build_distillation_prompt(agent_name: str, exchanges: list[dict]) -> str:
    lines = [
        f"You are Jenna. Review these {len(exchanges)} exchanges from an OpenClaw session with {agent_name}.",
        "Extract ONLY high-signal information about Chris:",
        "  (1) Decisions Chris made and why",
        "  (2) Preferences Chris stated (tools, patterns, approaches)",
        "  (3) Problems diagnosed and solutions applied",
        "  (4) Corrections Chris gave to the agent",
        "",
        "Skip: generic confirmations, routine tool usage, status updates with no insight.",
        "",
    ]
    for i, ex in enumerate(exchanges, 1):
        lines.append(f'[{i}] User: {ex["user"][:500]}')
        lines.append(f'    Agent: {ex["assistant"][:500]}')
        lines.append("")

    lines.append("OUTPUT FORMAT (return ONLY valid JSON):")
    lines.append(
        '{"keep": [{"index": <int>, "summary": "<1-3 sentences>", "signal_type": "decision|preference|diagnosis|correction", "signal_score": <1-10>}], "skip_reason": "<why others were skipped>"}'
    )
    lines.append("")
    lines.append("STRICT: only the JSON object. Empty keep list is fine if nothing is high-signal.")
    return "\n".join(lines)


def dispatch_distillation(prompt: str) -> dict | None:
    return dispatch_json(
        agent=DISPATCH_AGENT,
        prompt=prompt,
        timeout=DISPATCH_TIMEOUT,
        log_failure=log_failure,
        source="ingest.openclaw_sessions",
        thinking="off",
    )


# ── Record Writing ──────────────────────────────────────
def write_record(
    agent_name: str, session_id: str, session_date: str, item: dict, exchange: dict
) -> Path | None:
    summary = item.get("summary", "")
    signal_type = item.get("signal_type", "unknown")
    score = item.get("signal_score", 0)

    if score < 6 or not summary:
        return None

    content = (
        f'OpenClaw {agent_name} session ({session_date})\n'
        f'Signal: {signal_type} (score {score}/10)\n\n'
        f'{summary}\n\n'
        f'Context — User: {exchange["user"][:300]}\n'
        f'Agent: {exchange["assistant"][:300]}'
    )

    digest = hashlib.sha256(content.encode()).hexdigest()
    date_part = (session_date or "unknown").replace("-", "_")
    rec_id = f"raw_oc_{agent_name}_{date_part}_{digest[:8]}"

    record = {
        "id": rec_id,
        "timestamp": exchange.get("timestamp", datetime.now().isoformat()),
        "source_type": "openclaw_session",
        "source_ref": f"openclaw:{agent_name}:{session_id}",
        "actor": "chris",
        "visibility": "private",
        "scrub_status": "scrubbed",
        "content": content,
        "attachments": [],
        "entities": ["Chris", agent_name, signal_type],
        "hash": f"sha256:{digest}",
    }

    INBOX_DIR.mkdir(parents=True, exist_ok=True)
    out = INBOX_DIR / f"{rec_id}.json"
    if out.exists():
        return None
    out.write_text(json.dumps(record, ensure_ascii=False, indent=2))
    return out


# ── DLQ Retry ───────────────────────────────────────────
def process_dlq() -> tuple[int, int, int]:
    """Retry DLQ entries. Returns (succeeded, requeued, dead)."""
    if not DLQ_FILE.exists():
        return 0, 0, 0

    try:
        lines = DLQ_FILE.read_text().splitlines()
    except Exception as e:
        log_failure(f"DLQ read failed: {e}")
        return 0, 0, 0

    succeeded = 0
    requeued_entries: list[dict] = []
    dead_entries: list[dict] = []

    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue

        agent = entry.get("agent", "")
        session_id = entry.get("session_id", "")
        session_date = entry.get("session_date", "")
        exchanges = entry.get("exchanges", [])
        attempt = int(entry.get("attempt", 1))

        if not exchanges or not agent:
            continue

        prompt = build_distillation_prompt(agent, exchanges)
        result = dispatch_distillation(prompt)

        if result:
            kept = result.get("keep", [])
            for item in kept:
                idx = item.get("index", 0) - 1
                if 0 <= idx < len(exchanges):
                    write_record(agent, session_id, session_date, item, exchanges[idx])
            succeeded += 1
            continue

        next_attempt = attempt + 1
        if next_attempt > MAX_DLQ_ATTEMPTS:
            entry["attempt"] = next_attempt
            entry["dead_ts"] = datetime.now().isoformat()
            dead_entries.append(entry)
        else:
            entry["attempt"] = next_attempt
            entry["last_retry_ts"] = datetime.now().isoformat()
            requeued_entries.append(entry)

    # Enforce size cap — drop oldest if over limit
    if len(requeued_entries) > MAX_DLQ_ENTRIES:
        dropped = len(requeued_entries) - MAX_DLQ_ENTRIES
        requeued_entries = requeued_entries[-MAX_DLQ_ENTRIES:]
        log_failure(f"DLQ over cap — dropped {dropped} oldest entries")

    try:
        if requeued_entries:
            tmp = DLQ_FILE.with_suffix(".jsonl.tmp")
            tmp.write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in requeued_entries) + "\n")
            tmp.replace(DLQ_FILE)
        else:
            DLQ_FILE.unlink(missing_ok=True)
    except Exception as e:
        log_failure(f"DLQ rewrite failed: {e}")

    if dead_entries:
        try:
            DEAD_FILE.parent.mkdir(parents=True, exist_ok=True)
            with DEAD_FILE.open("a") as f:
                for e in dead_entries:
                    f.write(json.dumps(e, ensure_ascii=False) + "\n")
        except Exception as e:
            log_failure(f"DLQ dead write failed: {e}")

    return succeeded, len(requeued_entries), len(dead_entries)


# ── Main ────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(description="OpenClaw session ingest via Jenna distillation")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be processed")
    parser.add_argument(
        "--agents", default=",".join(sorted(ACTIVE_AGENTS)), help="Comma-separated agent names"
    )
    parser.add_argument("--max-sessions", type=int, default=50, help="Cap sessions per run")
    args = parser.parse_args()

    agents = [a.strip() for a in args.agents.split(",") if a.strip() in ACTIVE_AGENTS]
    state = load_state()
    total_written = 0
    total_exchanges = 0

    print(f"OpenClaw session ingest — agents={agents}")

    if not args.dry_run:
        dlq_s, dlq_r, dlq_d = process_dlq()
        if dlq_s or dlq_r or dlq_d:
            print(f"DLQ: {dlq_s} succeeded, {dlq_r} requeued, {dlq_d} dead")

    for agent_name in agents:
        sessions_dir = AGENTS_DIR / agent_name / "sessions"
        if not sessions_dir.exists():
            continue

        agent_state = state.get(agent_name, {})
        session_files = sorted(sessions_dir.glob("*.jsonl"))
        # Skip deleted sessions
        session_files = [f for f in session_files if ".deleted" not in f.name]
        processed = 0

        for session_file in session_files:
            if processed >= args.max_sessions:
                break

            session_id = session_file.stem
            offset = agent_state.get(session_id, 0)

            try:
                exchanges, new_offset, session_date = parse_session(session_file, offset)
            except FileNotFoundError:
                continue  # Session deleted between glob and read
            if not exchanges:
                agent_state[session_id] = new_offset
                continue

            total_exchanges += len(exchanges)
            processed += 1

            print(f"  [{agent_name}] {session_id[:8]}... {len(exchanges)} exchanges, date={session_date}")

            if args.dry_run:
                for ex in exchanges[:3]:
                    print(f'    U: {ex["user"][:100]}')
                    print(f'    A: {ex["assistant"][:100]}')
                agent_state[session_id] = new_offset
                continue

            # Dispatch in batches
            for i in range(0, len(exchanges), BATCH_SIZE):
                batch_idx = i // BATCH_SIZE + 1
                batch = exchanges[i : i + BATCH_SIZE]
                prompt = build_distillation_prompt(agent_name, batch)
                result = dispatch_distillation(prompt)
                if result is None:
                    import time

                    time.sleep(10)
                    result = dispatch_distillation(prompt)

                if not result:
                    print(f"    Batch {batch_idx}: DISPATCH FAILED — queued to DLQ")
                    queue_for_retry(
                        agent=agent_name,
                        session_id=session_id,
                        session_date=session_date,
                        batch_idx=batch_idx,
                        exchanges=batch,
                        error="dispatch failed after 2 attempts",
                    )
                    continue

                kept = result.get("keep", [])
                print(f"    Batch {batch_idx}: {len(kept)}/{len(batch)} kept")

                for item in kept:
                    idx = item.get("index", 0) - 1  # 1-indexed in prompt
                    if 0 <= idx < len(batch):
                        path = write_record(agent_name, session_id, session_date, item, batch[idx])
                        if path:
                            total_written += 1

            agent_state[session_id] = new_offset

        state[agent_name] = agent_state

    if not args.dry_run:
        state["last_run"] = datetime.now().isoformat()
        save_state(state)

    print(f"\nDone — {total_exchanges} exchanges processed, {total_written} records written")


if __name__ == "__main__":
    main()
