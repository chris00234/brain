"""brain — Chris's unified second-brain package.

Package layout:
  brain_core/  — in-process modules imported by server.py (search, learn, indexer, ...)
  cli/         — thin CLI wrappers that preserve legacy argparse signatures
  ingest/      — data ingestion scripts (personal, gmail, browser, shell, obsidian)
  synthesis/   — daily/weekly/monthly narrative + reflection jobs
  tests/       — pytest suite
  server.py    — the FastAPI (launchd entrypoint)
"""
