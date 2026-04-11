# Brain Data Sources Guide

How to add a new data source to the brain system.

## Architecture

```
[Data Source] → [Ingest Adapter] → raw/inbox/*.json → [Canonical Pipeline] → ChromaDB
                                                    → [Reindex] → ChromaDB
```

Every data source follows this pattern:
1. An **ingest adapter** pulls data from the source
2. Writes **schema-compliant JSON records** to `~/server/knowledge/raw/inbox/`
3. The **canonical pipeline** (daily 2am) distills and promotes high-signal records
4. The **reindex job** (2x daily) indexes raw records into the `experience` collection

## Anatomy of an Ingest Adapter

Reference: `ingest/ghost_blog.py` (simplest), `ingest/browser.py` (with state tracking)

```python
#!/opt/homebrew/bin/python3
import json, hashlib, sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "brain_core"))
from safe_state import load_state, save_state

STATE_FILE = Path("/Users/chrischo/server/brain/logs/my-source-state.json")
INBOX_DIR = Path("/Users/chrischo/server/knowledge/raw/inbox")

def collect():
    state = load_state(STATE_FILE)
    # ... pull data from source, track watermark in state ...
    records = []
    for item in source_data:
        content = f"Description of item\n\n{item['text']}"
        digest = hashlib.sha256(content.encode()).hexdigest()
        records.append({
            "id": f"raw_mysource_{datetime.now().strftime('%Y_%m_%d')}_{digest[:8]}",
            "timestamp": datetime.now().isoformat(),
            "source_type": "my_source",        # must be unique across adapters
            "source_ref": f"my_source:{item['id']}",
            "actor": "chris",
            "visibility": "private",
            "scrub_status": "scrubbed",
            "content": content,
            "attachments": [],
            "entities": ["Chris"],
            "hash": f"sha256:{digest}",
        })
    # Write records
    INBOX_DIR.mkdir(parents=True, exist_ok=True)
    for rec in records:
        out = INBOX_DIR / f"{rec['id']}.json"
        if not out.exists():
            out.write_text(json.dumps(rec, ensure_ascii=False, indent=2))
    save_state(STATE_FILE, state)
    return len(records)

if __name__ == "__main__":
    from brain_core.batch_lock import batch_lock
    with batch_lock("my_source_ingest"):
        n = collect()
        print(f"Ingested {n} records")
```

## Required Record Fields

| Field | Type | Description |
|-------|------|-------------|
| `id` | str | Unique ID: `raw_{source}_{date}_{hash8}` |
| `timestamp` | str | ISO 8601 datetime |
| `source_type` | str | Adapter name (used for filtering in indexer) |
| `source_ref` | str | Link back to original source |
| `actor` | str | Who generated this (`chris`, agent name) |
| `visibility` | str | `private` or `public` |
| `scrub_status` | str | `scrubbed` (PII removed) or `raw` |
| `content` | str | The actual text content |
| `attachments` | list | Empty list or file references |
| `entities` | list | Named entities mentioned |
| `hash` | str | `sha256:{hex}` of content |

## Registering with the Scheduler

### 1. Add to JOB_REGISTRY in `server.py`:
```python
"my_source_ingest": [_py, f"{_bd}/ingest/my_source.py"],
```

### 2. Add to JOB_SCHEDULE in `brain_core/scheduler.py`:
```python
ScheduledJob(
    name="my_source_ingest",
    description="My source ingest (daily 3am)",
    trigger=CronTrigger(hour=3, minute=0),
    agent="system",
),
```

### 3. Add source_type to indexer's INBOX_TYPES

In `brain_core/indexer.py`, find `collect_experience()` and add your `source_type` to `INBOX_TYPES`:
```python
INBOX_TYPES = {'browser', 'shell', 'git_activity', 'screen_time',
               'openclaw_session', 'claude_code_session', 'my_source'}
```

Add a header template to `HEADER_MAP`:
```python
'my_source': lambda r: f"My source data: {r.get('source_ref', '')}",
```

## Testing

```bash
# 1. Run adapter manually
cd ~/server/brain && .venv/bin/python ingest/my_source.py

# 2. Check inbox
ls ~/server/knowledge/raw/inbox/raw_mysource_*.json | head -5

# 3. Trigger canonical pipeline
curl -X POST -H "Authorization: Bearer $(cat ~/.openclaw/credentials/.personal_webhook_secret)" \
  http://127.0.0.1:8791/jobs/canonical_pipeline

# 4. Trigger reindex to pick up in experience collection
curl -X POST -H "Authorization: Bearer $(cat ~/.openclaw/credentials/.personal_webhook_secret)" \
  http://127.0.0.1:8791/jobs/reindex

# 5. Search for your data
curl -H "Authorization: Bearer $(cat ~/.openclaw/credentials/.personal_webhook_secret)" \
  "http://127.0.0.1:8791/recall?q=my+source+query&n=5"
```

## Existing Adapters

| Adapter | Source | Schedule | Owner Agent |
|---------|--------|----------|-------------|
| `personal.py` | Apple Notes/iMessage/Calendar/Reminders | 3x daily | jenna |
| `gmail.py` | Gmail IMAP | daily 1:30am | jenna |
| `browser.py` | Chrome history | daily 2:30am | sage |
| `shell_history.py` | zsh history | daily 2:15am | ellie |
| `obsidian.py` | Obsidian vault | hourly | jenna |
| `ghost_blog.py` | Ghost CMS posts | daily 5am | market |
| `openclaw_sessions.py` | Agent session JSONL | daily 1am | jenna |
| `claude_code_sessions.py` | Claude Code sessions | daily 1:15am | jenna |
| `git_activity.py` | Git commit history | daily 3am | ellie |
| `screen_time.py` | macOS Screen Time | weekly Sun 3:30am | sage |
| `active_contacts.py` | iMessage contacts | monthly 1st 4am | jenna |
