#!/opt/homebrew/bin/python3
"""Weekly purge of llm_usage.db — keeps last 90 days of dispatches."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from openclaw_dispatch import purge_old_usage

if __name__ == "__main__":
    n = purge_old_usage(days=90)
    print(f"Purged {n} llm_usage rows older than 90 days")
    sys.exit(0)
