#!/opt/homebrew/bin/python3
"""Screen Time ingest — Sage analyzes daily app usage patterns.

Reads knowledgeC.db (Apple Screen Time), aggregates per-day app usage,
dispatches daily summaries to Sage for pattern analysis, writes to raw/inbox/.

Pipeline: SQLite → aggregate by day → Sage analysis → raw/inbox → canonical pipeline

Usage:
  screen_time.py [--dry-run] [--days-back 30]
"""

import argparse
import hashlib
import json
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

# ── Config ──────────────────────────────────────────────
KNOWLEDGE_DB = Path.home() / 'Library/Application Support/Knowledge/knowledgeC.db'
INBOX_DIR = Path('/Users/chrischo/server/knowledge/raw/inbox')
STATE_FILE = Path('/Users/chrischo/server/brain/logs/screen-time-state.json')
FAILURE_LOG = Path('/Users/chrischo/server/brain/logs/screen-time-failures.jsonl')

OPENCLAW_BIN = '/Users/chrischo/.local/bin/openclaw'
DISPATCH_AGENT = 'sage'
DISPATCH_TIMEOUT = 240
BATCH_SIZE = 7  # days per dispatch

APPLE_EPOCH = 978307200  # 2001-01-01
MIN_SECONDS = 300  # 5 minutes minimum per app per day
MIN_APPS_PER_DAY = 3

# Bundle ID → friendly name mapping
APP_NAMES = {
    'com.apple.Terminal': 'Terminal',
    'com.google.Chrome': 'Chrome',
    'com.microsoft.VSCode': 'VS Code',
    'com.apple.finder': 'Finder',
    'com.apple.Safari': 'Safari',
    'com.googlecode.iterm2': 'iTerm2',
    'com.apple.dt.Xcode': 'Xcode',
    'com.tinyspeck.slackmacgap': 'Slack',
    'com.apple.mail': 'Mail',
    'com.apple.MobileSMS': 'Messages',
    'com.apple.Notes': 'Notes',
    'us.zoom.xos': 'Zoom',
    'com.spotify.client': 'Spotify',
    'com.apple.Music': 'Music',
    'com.brave.Browser': 'Brave',
    'company.thebrowser.Browser': 'Arc',
    'com.apple.systempreferences': 'System Settings',
    'com.figma.Desktop': 'Figma',
    'com.notion.id': 'Notion',
    'md.obsidian': 'Obsidian',
    'org.orbstack.Orbstack': 'OrbStack',
}

# System apps to skip (always noise)
SKIP_APPS = {
    'com.apple.loginwindow',
    'com.apple.SystemUIServer',
    'com.apple.dock',
    'com.apple.WindowManager',
    'com.apple.Spotlight',
    'com.apple.controlcenter',
    'com.apple.notificationcenterui',
}


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


def friendly_name(bundle_id: str) -> str:
    if bundle_id in APP_NAMES:
        return APP_NAMES[bundle_id]
    # Extract last component: com.example.MyApp -> MyApp
    parts = bundle_id.split('.')
    return parts[-1] if parts else bundle_id


def format_duration(seconds: int) -> str:
    if seconds >= 3600:
        h = seconds // 3600
        m = (seconds % 3600) // 60
        return f'{h}h{m:02d}m'
    return f'{seconds // 60}m'


# ── Data Collection ─────────────────────────────────────
def collect_usage(days_back: int) -> dict[str, list[tuple[str, int]]]:
    """Read app usage from knowledgeC.db, return {date: [(app_name, seconds)]}."""
    if not KNOWLEDGE_DB.exists():
        log_failure('knowledgeC.db not found')
        return {}

    with tempfile.TemporaryDirectory() as tmp:
        tmp_db = Path(tmp) / 'knowledgeC.db'
        try:
            shutil.copy2(KNOWLEDGE_DB, tmp_db)
        except Exception as e:
            log_failure(f'cannot copy knowledgeC.db: {e}')
            return {}

        cutoff = datetime.now().timestamp() - (days_back * 86400)
        cutoff_apple = cutoff - APPLE_EPOCH

        try:
            conn = sqlite3.connect(f'file:{tmp_db}?mode=ro', uri=True)
            cur = conn.cursor()
            cur.execute('''
                SELECT ZVALUESTRING, ZSTARTDATE, ZENDDATE
                FROM ZOBJECT
                WHERE ZSTREAMNAME = '/app/usage'
                  AND ZSTARTDATE > ?
                  AND ZVALUESTRING IS NOT NULL
                ORDER BY ZSTARTDATE
            ''', (cutoff_apple,))
            rows = cur.fetchall()
            conn.close()
        except Exception as e:
            log_failure(f'knowledgeC query failed: {e}')
            return {}

    # Aggregate by (date, app)
    day_app_seconds: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))

    for bundle_id, start_apple, end_apple in rows:
        if bundle_id in SKIP_APPS:
            continue
        if not start_apple or not end_apple:
            continue
        duration = int(end_apple - start_apple)
        if duration <= 0 or duration > 86400:
            continue

        start_unix = start_apple + APPLE_EPOCH
        date = datetime.fromtimestamp(start_unix).strftime('%Y-%m-%d')
        app_name = friendly_name(bundle_id)
        day_app_seconds[date][app_name] += duration

    # Filter and sort
    result: dict[str, list[tuple[str, int]]] = {}
    for date, apps in sorted(day_app_seconds.items()):
        filtered = [(app, secs) for app, secs in apps.items() if secs >= MIN_SECONDS]
        if len(filtered) < MIN_APPS_PER_DAY:
            continue
        filtered.sort(key=lambda x: x[1], reverse=True)
        result[date] = filtered

    return result


# ── Agent Dispatch ──────────────────────────────────────
def build_analysis_prompt(daily_summaries: list[dict]) -> str:
    lines = [
        f'You are Sage. Analyze these {len(daily_summaries)} days of app usage for Chris.',
        'What patterns emerge? What was Chris focused on each day?',
        '',
        'Look for: deep work sessions, tool preferences, workflow shifts, unusual patterns.',
        'Skip: generic observations like "Chris used his computer".',
        '',
    ]
    for i, ds in enumerate(daily_summaries, 1):
        lines.append(f'[{i}] {ds["date"]}: {ds["usage_line"]}')
        lines.append('')

    lines.append('OUTPUT FORMAT (return ONLY valid JSON):')
    lines.append('{"keep": [{"index": <int>, "summary": "<1-2 sentences about focus/pattern>", "focus_areas": ["area1", "area2"], "signal_score": <1-10>}], "skip_reason": "<why others were skipped>"}')
    lines.append('')
    lines.append('STRICT: only the JSON object. Empty keep list is fine.')
    return '\n'.join(lines)


def dispatch_analysis(prompt: str) -> dict | None:
    cmd = [
        OPENCLAW_BIN, 'agent', '--agent', DISPATCH_AGENT,
        '--message', prompt, '--json',
        '--timeout', str(DISPATCH_TIMEOUT), '--thinking', 'off',
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=DISPATCH_TIMEOUT + 30)
    except subprocess.TimeoutExpired:
        log_failure('sage dispatch timed out')
        return None
    if r.returncode != 0:
        log_failure(f'sage dispatch failed: {r.stderr[:300]}')
        return None
    try:
        response = json.loads(r.stdout)
        text = response.get('result', {}).get('payloads', [])[0].get('text', '')
        text = re.sub(r'^```(?:json)?\s*', '', text.strip())
        text = re.sub(r'\s*```$', '', text)
        return json.loads(text)
    except (json.JSONDecodeError, KeyError, IndexError) as e:
        log_failure(f'could not parse Sage reply: {e}')
        return None


# ── Record Writing ──────────────────────────────────────
def write_record(summary_item: dict, daily: dict) -> Path | None:
    summary = summary_item.get('summary', '')
    focus = summary_item.get('focus_areas', [])
    score = summary_item.get('signal_score', 0)

    if score < 6 or not summary:
        return None

    date = daily['date']
    content = (
        f'Screen time pattern — {date}\n'
        f'Focus: {", ".join(focus) if focus else "general"}\n\n'
        f'{summary}\n\n'
        f'Usage: {daily["usage_line"]}'
    )

    digest = hashlib.sha256(content.encode()).hexdigest()
    date_part = date.replace('-', '_')
    rec_id = f'raw_screentime_{date_part}_{digest[:8]}'

    record = {
        'id': rec_id,
        'timestamp': f'{date}T00:00:00Z',
        'source_type': 'screen_time',
        'source_ref': f'screentime:{date}',
        'actor': 'chris',
        'visibility': 'private',
        'scrub_status': 'scrubbed',
        'content': content,
        'attachments': [],
        'entities': ['Chris'] + focus[:5],
        'hash': f'sha256:{digest}',
    }

    INBOX_DIR.mkdir(parents=True, exist_ok=True)
    out = INBOX_DIR / f'{rec_id}.json'
    if out.exists():
        return None
    out.write_text(json.dumps(record, ensure_ascii=False, indent=2))
    return out


# ── Main ────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(description='Screen Time ingest via Sage analysis')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--days-back', type=int, default=30)
    args = parser.parse_args()

    state = load_state()
    last_date = state.get('last_date', '')

    print(f'Screen Time ingest — last {args.days_back} days')

    usage = collect_usage(args.days_back)
    print(f'  {len(usage)} days with sufficient app usage')

    # Build daily summaries
    daily_summaries = []
    for date, apps in sorted(usage.items()):
        if date <= last_date:
            continue
        usage_line = ', '.join(f'{app} {format_duration(secs)}' for app, secs in apps[:8])
        daily_summaries.append({
            'date': date,
            'usage_line': usage_line,
            'apps': apps,
        })

    print(f'  {len(daily_summaries)} new days to process')

    if not daily_summaries:
        print('Nothing to process.')
        return

    if args.dry_run:
        for ds in daily_summaries[:10]:
            print(f'  {ds["date"]}: {ds["usage_line"]}')
        print(f'\nDone — {len(daily_summaries)} days, 0 records written (dry run)')
        return

    total_written = 0
    last_successful_date = state.get('last_date')
    for i in range(0, len(daily_summaries), BATCH_SIZE):
        batch = daily_summaries[i:i + BATCH_SIZE]
        prompt = build_analysis_prompt(batch)
        result = dispatch_analysis(prompt)
        if result is None:
            import time
            time.sleep(10)
            result = dispatch_analysis(prompt)

        if not result:
            print(f'  Batch {i // BATCH_SIZE + 1}: DISPATCH FAILED')
            continue

        kept = result.get('keep', [])
        print(f'  Batch {i // BATCH_SIZE + 1}: {len(kept)}/{len(batch)} kept')

        for item in kept:
            idx = item.get('index', 0) - 1
            if 0 <= idx < len(batch):
                path = write_record(item, batch[idx])
                if path:
                    total_written += 1

        last_successful_date = batch[-1]['date']

    if last_successful_date:
        state['last_date'] = last_successful_date
    state['last_run'] = datetime.now().isoformat()
    save_state(state)

    print(f'\nDone — {len(daily_summaries)} days processed, {total_written} records written')


if __name__ == '__main__':
    main()
