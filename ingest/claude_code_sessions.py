#!/opt/homebrew/bin/python3
"""Claude Code session ingest — Jenna distills decisions from coding sessions.

Reads JSONL session transcripts from ~/.claude/projects/*/,
pre-filters to substantive assistant text (reasoning, decisions, explanations),
dispatches batches to Jenna for distillation, writes kept summaries to raw/inbox/.

Claude Code sessions are assistant-heavy: Chris gives brief instructions,
assistant produces long chains of tool use + reasoning. The signal is in:
  1. Chris's initial request (user text — rare but important)
  2. Assistant explanations, decisions, and reasoning (text blocks)

Pipeline: JSONL → pre-filter → Jenna distillation → raw/inbox → canonical pipeline

Usage:
  claude_code_sessions.py [--dry-run] [--max-sessions 30]
"""

import argparse
import hashlib
import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

# ── Config ──────────────────────────────────────────────
PROJECTS_DIR = Path.home() / '.claude/projects'
INBOX_DIR = Path('/Users/chrischo/server/knowledge/raw/inbox')
STATE_FILE = Path('/Users/chrischo/server/brain/logs/claude-code-sessions-state.json')
FAILURE_LOG = Path('/Users/chrischo/server/brain/logs/claude-code-sessions-failures.jsonl')
DLQ_FILE = Path('/Users/chrischo/server/brain/logs/claude-code-sessions-dlq.jsonl')
DEAD_FILE = Path('/Users/chrischo/server/brain/logs/claude-code-sessions-dead.jsonl')
MAX_DLQ_ATTEMPTS = 5
MAX_DLQ_ENTRIES = 10000

OPENCLAW_BIN = '/Users/chrischo/.local/bin/openclaw'
DISPATCH_AGENT = 'jenna'
DISPATCH_TIMEOUT = 300
BATCH_SIZE = 10  # text segments per dispatch

MIN_TEXT_LEN = 60
MAX_SEGMENT_CHARS = 1500
# Skip projects that are OpenClaw agent workspaces (already handled by openclaw_sessions.py)
SKIP_PROJECT_PATTERNS = ['-openclaw-workspace-']


# ── State ───────────────────────────────────────────────
try:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'brain_core'))
    from safe_state import load_state as _safe_load, save_state as _safe_save
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
        with FAILURE_LOG.open('a') as f:
            f.write(json.dumps({'timestamp': datetime.now().isoformat(), 'reason': reason[:500]}) + '\n')
    except Exception:
        pass


def queue_for_retry(project: str, session_id: str, session_date: str,
                    batch_idx: int, groups: list[dict], error: str,
                    attempt: int = 1) -> None:
    """Append a failed batch to the DLQ with full payload for later retry."""
    try:
        DLQ_FILE.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            'ts': datetime.now().isoformat(),
            'project': project,
            'session_id': session_id,
            'session_date': session_date,
            'batch_idx': batch_idx,
            'groups': groups,
            'error': error[:500],
            'attempt': attempt,
        }
        with DLQ_FILE.open('a') as f:
            f.write(json.dumps(entry, ensure_ascii=False) + '\n')
    except Exception as e:
        log_failure(f'DLQ write failed: {e}')


# ── Project Name Extraction ─────────────────────────────
def project_name_from_dir(dirname: str) -> str:
    """Convert Claude Code project dir name to human-readable project name.
    e.g. '-Users-chrischo-server-brain' -> 'server/brain'
    """
    # Strip leading -Users-chrischo- prefix
    name = re.sub(r'^-Users-chrischo-?', '', dirname)
    if not name:
        return 'home'
    return name.replace('-', '/')


# ── JSONL Parsing ───────────────────────────────────────
def parse_session(path: Path, offset: int = 0) -> tuple[list[dict], int, str]:
    """Parse a Claude Code session JSONL.

    Returns (segments, new_offset, session_date).
    Each segment = {role: str, text: str, timestamp: str}.
    """
    segments = []
    session_date = ''
    data = path.read_bytes()
    new_offset = len(data)

    if offset >= new_offset:
        return [], offset, ''

    lines = data[offset:].decode('utf-8', errors='replace').splitlines()

    for line in lines:
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue

        rtype = record.get('type', '')

        # Skip noise line types
        if rtype in ('progress', 'file-history-snapshot', 'system', 'queue-operation', 'last-prompt'):
            continue

        if rtype not in ('user', 'assistant'):
            continue

        msg = record.get('message', {})
        content = msg.get('content', [])
        if not isinstance(content, list):
            continue

        ts = record.get('timestamp', '')
        if not session_date and ts:
            session_date = ts[:10]

        for block in content:
            if not isinstance(block, dict) or block.get('type') != 'text':
                continue
            text = block.get('text', '').strip()

            # Skip noise patterns
            if text.startswith('[Request interrupted'):
                continue
            if text.startswith('Base directory for this skill'):
                continue
            if len(text) < MIN_TEXT_LEN:
                continue

            segments.append({
                'role': rtype,
                'text': text[:MAX_SEGMENT_CHARS],
                'timestamp': ts,
            })

    return segments, new_offset, session_date


def group_segments(segments: list[dict], max_chars: int = 3000) -> list[dict]:
    """Group consecutive segments into chunks for distillation.

    Each group captures a logical unit of work: user request + assistant reasoning.
    """
    if not segments:
        return []

    groups = []
    current_texts = []
    current_chars = 0
    first_ts = segments[0].get('timestamp', '')

    for seg in segments:
        seg_text = f"{'User' if seg['role'] == 'user' else 'Claude'}: {seg['text']}"

        if current_chars + len(seg_text) > max_chars and current_texts:
            groups.append({
                'text': '\n\n'.join(current_texts),
                'timestamp': first_ts,
            })
            current_texts = []
            current_chars = 0
            first_ts = seg.get('timestamp', '')

        current_texts.append(seg_text)
        current_chars += len(seg_text)

    if current_texts:
        groups.append({
            'text': '\n\n'.join(current_texts),
            'timestamp': first_ts,
        })

    return groups


# ── Agent Dispatch ──────────────────────────────────────
def build_distillation_prompt(project: str, groups: list[dict]) -> str:
    lines = [
        f'You are Jenna. Review these {len(groups)} segments from a Claude Code session in project "{project}".',
        'Extract ONLY high-signal information about Chris:',
        '  (1) Architecture and design decisions',
        '  (2) Debugging insights and root causes discovered',
        '  (3) Code style/tooling preferences',
        '  (4) Corrections Chris gave to Claude',
        '  (5) Technical trade-offs and why one approach was chosen',
        '',
        'Skip: routine tool output, file reading, standard operations, status updates.',
        '',
    ]
    for i, group in enumerate(groups, 1):
        lines.append(f'[{i}] {group["text"][:800]}')
        lines.append('')

    lines.append('OUTPUT FORMAT (return ONLY valid JSON):')
    lines.append('{"keep": [{"index": <int>, "summary": "<1-3 sentences>", "signal_type": "decision|preference|diagnosis|correction|architecture", "signal_score": <1-10>}], "skip_reason": "<why others were skipped>"}')
    lines.append('')
    lines.append('STRICT: only the JSON object. Empty keep list is fine.')
    return '\n'.join(lines)


def dispatch_distillation(prompt: str) -> dict | None:
    cmd = [
        OPENCLAW_BIN, 'agent', '--agent', DISPATCH_AGENT,
        '--message', prompt, '--json',
        '--timeout', str(DISPATCH_TIMEOUT), '--thinking', 'off',
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=DISPATCH_TIMEOUT + 30)
    except subprocess.TimeoutExpired:
        log_failure('jenna dispatch timed out')
        return None
    if r.returncode != 0:
        log_failure(f'jenna dispatch failed: {r.stderr[:300]}')
        return None
    try:
        response = json.loads(r.stdout)
        text = response.get('result', {}).get('payloads', [])[0].get('text', '')
        text = re.sub(r'^```(?:json)?\s*', '', text.strip())
        text = re.sub(r'\s*```$', '', text)
        return json.loads(text)
    except (json.JSONDecodeError, KeyError, IndexError) as e:
        log_failure(f'could not parse Jenna reply: {e}')
        return None


# ── Record Writing ──────────────────────────────────────
def write_record(project: str, session_id: str, session_date: str,
                 item: dict, group: dict) -> Path | None:
    summary = item.get('summary', '')
    signal_type = item.get('signal_type', 'unknown')
    score = item.get('signal_score', 0)

    if score < 6 or not summary:
        return None

    content = (
        f'Claude Code session in {project} ({session_date})\n'
        f'Signal: {signal_type} (score {score}/10)\n\n'
        f'{summary}\n\n'
        f'Context: {group["text"][:500]}'
    )

    digest = hashlib.sha256(content.encode()).hexdigest()
    date_part = (session_date or 'unknown').replace('-', '_')
    proj_slug = re.sub(r'[^a-z0-9]', '_', project.lower())[:20]
    rec_id = f'raw_cc_{proj_slug}_{date_part}_{digest[:8]}'

    record = {
        'id': rec_id,
        'timestamp': group.get('timestamp', datetime.now().isoformat()),
        'source_type': 'claude_code_session',
        'source_ref': f'claude_code:{project}:{session_id}',
        'actor': 'chris',
        'visibility': 'private',
        'scrub_status': 'scrubbed',
        'content': content,
        'attachments': [],
        'entities': ['Chris', project, signal_type],
        'hash': f'sha256:{digest}',
    }

    INBOX_DIR.mkdir(parents=True, exist_ok=True)
    out = INBOX_DIR / f'{rec_id}.json'
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
        log_failure(f'DLQ read failed: {e}')
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

        project = entry.get('project', '')
        session_id = entry.get('session_id', '')
        session_date = entry.get('session_date', '')
        groups = entry.get('groups', [])
        attempt = int(entry.get('attempt', 1))

        if not groups or not project:
            continue

        prompt = build_distillation_prompt(project, groups)
        result = dispatch_distillation(prompt)

        if result:
            kept = result.get('keep', [])
            for item in kept:
                idx = item.get('index', 0) - 1
                if 0 <= idx < len(groups):
                    write_record(project, session_id, session_date, item, groups[idx])
            succeeded += 1
            continue

        next_attempt = attempt + 1
        if next_attempt > MAX_DLQ_ATTEMPTS:
            entry['attempt'] = next_attempt
            entry['dead_ts'] = datetime.now().isoformat()
            dead_entries.append(entry)
        else:
            entry['attempt'] = next_attempt
            entry['last_retry_ts'] = datetime.now().isoformat()
            requeued_entries.append(entry)

    # Enforce size cap — drop oldest if over limit
    if len(requeued_entries) > MAX_DLQ_ENTRIES:
        dropped = len(requeued_entries) - MAX_DLQ_ENTRIES
        requeued_entries = requeued_entries[-MAX_DLQ_ENTRIES:]
        log_failure(f'DLQ over cap — dropped {dropped} oldest entries')

    try:
        if requeued_entries:
            tmp = DLQ_FILE.with_suffix('.jsonl.tmp')
            tmp.write_text('\n'.join(json.dumps(e, ensure_ascii=False) for e in requeued_entries) + '\n')
            tmp.replace(DLQ_FILE)
        else:
            DLQ_FILE.unlink(missing_ok=True)
    except Exception as e:
        log_failure(f'DLQ rewrite failed: {e}')

    if dead_entries:
        try:
            DEAD_FILE.parent.mkdir(parents=True, exist_ok=True)
            with DEAD_FILE.open('a') as f:
                for e in dead_entries:
                    f.write(json.dumps(e, ensure_ascii=False) + '\n')
        except Exception as e:
            log_failure(f'DLQ dead write failed: {e}')

    return succeeded, len(requeued_entries), len(dead_entries)


# ── Main ────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(description='Claude Code session ingest via Jenna distillation')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--max-sessions', type=int, default=30)
    args = parser.parse_args()

    state = load_state()
    total_written = 0
    total_segments = 0

    print(f'Claude Code session ingest — scanning {PROJECTS_DIR}')

    if not args.dry_run:
        dlq_s, dlq_r, dlq_d = process_dlq()
        if dlq_s or dlq_r or dlq_d:
            print(f'DLQ: {dlq_s} succeeded, {dlq_r} requeued, {dlq_d} dead')

    for proj_dir in sorted(PROJECTS_DIR.iterdir()):
        if not proj_dir.is_dir():
            continue
        dirname = proj_dir.name
        if any(pat in dirname for pat in SKIP_PROJECT_PATTERNS):
            continue

        project = project_name_from_dir(dirname)
        proj_state = state.get(dirname, {})
        session_files = sorted(proj_dir.glob('*.jsonl'))
        processed = 0

        for session_file in session_files:
            if processed >= args.max_sessions:
                break

            session_id = session_file.stem
            offset = proj_state.get(session_id, 0)

            segments, new_offset, session_date = parse_session(session_file, offset)
            if not segments:
                proj_state[session_id] = new_offset
                continue

            groups = group_segments(segments)
            if not groups:
                proj_state[session_id] = new_offset
                continue

            total_segments += len(groups)
            processed += 1

            print(f'  [{project}] {session_id[:8]}... {len(segments)} segments -> {len(groups)} groups, date={session_date}')

            if args.dry_run:
                for g in groups[:2]:
                    print(f'    {g["text"][:120]}')
                proj_state[session_id] = new_offset
                continue

            # Dispatch in batches
            for i in range(0, len(groups), BATCH_SIZE):
                batch_idx = i // BATCH_SIZE + 1
                batch = groups[i:i + BATCH_SIZE]
                prompt = build_distillation_prompt(project, batch)
                result = dispatch_distillation(prompt)
                if result is None:
                    import time
                    time.sleep(10)
                    result = dispatch_distillation(prompt)

                if not result:
                    print(f'    Batch {batch_idx}: DISPATCH FAILED — queued to DLQ')
                    queue_for_retry(
                        project=project,
                        session_id=session_id,
                        session_date=session_date,
                        batch_idx=batch_idx,
                        groups=batch,
                        error='dispatch failed after 2 attempts',
                    )
                    continue

                kept = result.get('keep', [])
                print(f'    Batch {batch_idx}: {len(kept)}/{len(batch)} kept')

                for item in kept:
                    idx = item.get('index', 0) - 1
                    if 0 <= idx < len(batch):
                        path = write_record(project, session_id, session_date, item, batch[idx])
                        if path:
                            total_written += 1

            proj_state[session_id] = new_offset

        state[dirname] = proj_state

    if not args.dry_run:
        state['last_run'] = datetime.now().isoformat()
        save_state(state)

    print(f'\nDone — {total_segments} groups processed, {total_written} records written')


if __name__ == '__main__':
    main()
