"""brain.pipeline — canonical knowledge promotion pipeline.

Operates on markdown + JSON data in /Users/chrischo/server/knowledge/ (raw → distilled
→ canonical). Originally lived at knowledge/scripts/; moved here during the brain
consolidation so all code lives under /server/brain/.

Entry points:
  pipeline_auto.py      — scans agent .learnings/memory + raw/inbox, runs the full
                           ingest → distill → propose → score → promote chain
  ingest.py             — raw file intake + schema-compliant record generation
  batch_distill.py      — LLM-driven summarization of raw records (via OpenClaw)
  batch_propose.py      — propose canonical entries from distilled notes
  score_proposals.py    — confidence scoring
  promote_canonical.py  — promote high-score proposals to canonical/
  search_memory.py      — canonical + distilled tier-weighted search
                           (imported by brain_core.search_unified)
  common.py             — shared helpers (paths, frontmatter, tokenize, ROOT)

ROOT in common.py points to /Users/chrischo/server/knowledge/ (the data directory),
not Path(__file__).parent — because code and data now live in separate trees.
"""
