# Brain v2 Chunking, Tagging, and Entry Storage Policy

Brain ingest uses **one entry contract** across all sources, but **not one
universal chunking algorithm**.

## Non-negotiable entry contract

Every vector payload written through the Brain vector-store boundary must carry:

- `schema_version` / `entry_schema_version`: currently `brain-entry-v2`
- `chunk_version` / `chunk_policy_version`: currently `source-aware-v2`
- `tag_policy_version`: currently `normalized-tags-v1`
- `content_hash`
- `source_kind`: `file`, `url`, `manual`, `named_source`, or `unknown`
- `source_type`: normalized source/type/subtype/kind
- `chunk_strategy`: `semantic`, `paragraph`, `structured`, `atomic`, `ast`, or `turn_based`
- `tags` and `context_tags`
- document/chunk provenance when available: `document_id`, `source_document_id`,
  `source_path`, `source_name`, `chunk_id`, `chunk_index`, `chunk_count`,
  `parent_id`, `is_parent`

The central enforcement point is `brain_core/source_policy.py`; the final safety
net is `brain_core/qdrant_store.py`, which enriches every future upsert before it
lands in Qdrant.

## Regression gates

- `cli/audit_qdrant_writes.py`: source-code gate. Fails when production code
  adds raw `qdrant_client` mutating writes outside the approved vector-store /
  maintenance boundary.
- `cli/entry_contract_audit.py`: live-data gate. Samples Qdrant payloads and
  fails when any sampled point is missing v2 entry-contract fields.
- `brain_core/slos.py::entry_contract_missing_pct`: production SLO for the same
  live-data contract. Target is exactly `0.0%`.

These gates are wired into `cli/ci_runner.py` and the scheduler jobs
`qdrant_write_audit` / `entry_contract_audit`, so future data keeps entering
through the same source-aware pattern instead of silently drifting.

## Chunking rule

- Apply normalized tags/context tags to every source.
- Use semantic chunking only for long natural-language documents when
  `BRAIN_SEMANTIC_CHUNKING=1`.
- Preserve source-native boundaries for atomic events, code, metrics, and
  turn/session records.
- Use structure-aware chunking for config/Markdown/code-like sources before any
  fallback text splitting. Canonical/distilled truth notes are treated as
  structured notes, not sentence-semantic prose.

## Why not semantic chunking for everything?

Semantic chunking finds boundaries by embedding adjacent sentences and splitting
where meaning changes. That can improve PDFs, notes, blog posts, and long prose.
It is the wrong boundary for sources whose meaning is already atomic or
structured:

- Calendar/reminder/health/uptime records: one event is the semantic unit.
- Code: AST/function boundaries are more accurate than sentence boundaries.
- Shell/git/screen-time metrics: splitting prose can separate timestamp,
  command, and outcome.
- Chat/session records: turn/event boundaries preserve speaker and chronology.
- Config/JSON/YAML: field hierarchy matters more than sentence distance.

For those sources, retrieval quality comes from tags, source metadata, filters,
and canonical atoms — not from additional semantic splitting.

## Storage layers

1. **Raw source**: append-only/source-native record where available.
2. **Vector payload**: Qdrant searchable chunk with the v2 entry contract.
3. **Entry manifest**: SQLite `brain.db` tables `entry_documents` and
   `entry_chunks`, maintained best-effort by `brain_core/entry_manifest.py`.
4. **Canonical atoms/relations**: the governed truth layer; not replaced by raw
   RAG chunks.

The manifest lets us audit legacy coverage, backfill metadata without re-embed,
and later decide which documents need selective re-chunk/re-embed.

## Qdrant payload indexes

`cli/qdrant_bootstrap.py` maintains hot filter indexes for v2 fields:

- version fields: `schema_version`, `entry_schema_version`, `chunk_version`,
  `chunk_policy_version`, `tag_policy_version`
- source/provenance: `source_kind`, `source_type`, `document_id`,
  `source_document_id`, `content_hash`
- retrieval filters: `chunk_strategy`, `tags`, `context_tags`

Do not index every metadata field. Only index fields used in filters, audits, or
routing.

## Connected paths

- `brain_core/qdrant_store.py`: final enforcement for all future vector writes.
- `brain_core/indexer.py`: source-aware chunking and chunk metadata stamping.
  Canonical/distilled notes preserve heading/truth-note boundaries; Obsidian and
  other long prose can use semantic boundaries when enabled.
- `brain_core/entry_manifest.py`: DB manifest for document/chunk/vector lineage.
- `ingest/pdfs.py`: PDF chunks receive common tags and strategy metadata.
- `ingest/ghost_blog.py`: Ghost posts can use semantic chunking when enabled.
- `ingest/personal.py`: personal records receive common tags; atomic records
  remain atomic.
- `brain_core/routes/knowledge.py`: manual ingest preserves request tags and
  writes policy metadata.
- `brain_core/routes/ingest.py`: image captions are atomic entries with source
  and tags.

## Legacy rollout guidance

Do **not** blindly semantic-rechunk every collection in place.

1. Apply schema/index changes and the future-write safety net.
2. Metadata-only backfill old Qdrant payloads to `brain-entry-v2` where the
   document text is unchanged.
3. Populate `entry_documents`/`entry_chunks` from backfilled payloads.
4. Selectively shadow-rechunk/re-embed long natural-language sources only.
5. Promote shadow collections only after recall evals pass.

This keeps the entry point consistent without destroying source-native meaning
or spending embeddings on low-value rechunking.
