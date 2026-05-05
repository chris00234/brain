# World-level Brain research refresh — 2026-05-05

Purpose: strengthen the active world-level Brain audit with current primary sources for agent memory, graph RAG, long-term-memory benchmarks, and production memory layers. This is not an adoption plan; it is an evidence map for what to compare against before claiming world-level readiness.

## Source selection rules

- Prefer papers, official docs, and official GitHub repos over commentary.
- Treat benchmark claims as design signals, not proof to copy a dependency.
- Preserve Chris's constraints: local-first where possible, no new usage-based LLM billing, CLI/subscription LLM dispatch, high-value ingestion, low pollution, explicit provenance, UI/readiness gates.

## Current source map

| System / paper | Primary sources | Relevant idea | Brain fit / action |
| --- | --- | --- | --- |
| Mem0 | Paper: https://arxiv.org/abs/2504.19413 · Repo: https://github.com/mem0ai/mem0 | Scalable long-term conversational memory with graph-memory variant and latency/token-cost claims versus full-context baselines. | Compare against Brain's canonical/semantic/episodic split and `llm_usage` cost telemetry. Do not adopt as service dependency without security and local-first review. |
| Zep / Graphiti | Paper/PDF: https://blog.getzep.com/content/files/2025/01/ZEP__USING_KNOWLEDGE_GRAPHS_TO_POWER_LLM_AGENT_MEMORY_2025011700.pdf · Docs: https://help.getzep.com/v2/understanding-the-graph · Repo: https://github.com/getzep/graphiti | Temporal knowledge graph with episodes, entities, and time-valid relationships for agent memory and Graph RAG. | Strong comparison point for Brain's atoms supersession, Neo4j entity graph, and source-validity fields. Add temporal-validity eval cases before copying implementation. |
| A-MEM | Paper: https://arxiv.org/abs/2502.12110 · Repo: https://github.com/WujiangXu/A-mem · Eval/system repos referenced by paper: https://github.com/WujiangXu/AgenticMemory and https://github.com/agiresearch/A-mem | Agentic memory organization using Zettelkasten-style linking, dynamic indexing, and memory evolution. | Compare to Brain procedure/lesson promotion and canonical memory evolution. Useful for outcome-linked memory-update tests. |
| MemoryOS | Paper: https://arxiv.org/abs/2506.06326 · Repo: https://github.com/BAI-LAB/MemoryOS | Memory operating-system framing for personalized agents. | Useful conceptual benchmark for tiering, consolidation, forgetting, and MCP-facing memory operations. Keep Brain's existing FastAPI/MCP substrate. |
| HippoRAG | Paper: https://arxiv.org/abs/2405.14831 · NeurIPS PDF: https://papers.nips.cc/paper_files/paper/2024/file/6ddc001d07ca4f319af96a3024f6dbd1-Paper-Conference.pdf · Repo: https://github.com/OSU-NLP-Group/HippoRAG | Graph/PPR-style retrieval inspired by long-term associative memory for multi-hop reasoning. | Compare to Brain's CRAG correction bridge, Neo4j graph, and source-term rewrites. Good candidate for learned alias/graph traversal evals, not a drop-in. |
| TERAG | Paper: https://arxiv.org/abs/2509.18667 | Token-efficient graph RAG using Personalized PageRank ideas while reducing output-token use. | Relevant to Chris's resource-efficiency constraint; compare token budget before adding graph retrieval context. |
| Hindsight | Paper: https://arxiv.org/abs/2512.12818 · Repo: https://github.com/vectorize-io/hindsight · Product/docs entry: https://vectorize.io/features/agent-memory | Retain/recall/reflect framing for agent memory that learns from experience, with LongMemEval/BEAM claims. | Strong comparison point for Brain's failure lessons, procedure outcomes, and reflection loops. Treat claims as external benchmark targets; verify locally before architectural adoption. |
| H²R | Paper: https://arxiv.org/abs/2509.12810 | Hierarchical hindsight reflection separates high-level planning memory from low-level execution memory. | Fits Brain's skill/procedure/failure-lesson split; suggests separate outcome metrics for plan memories versus execution memories. |
| Agent memory survey | Paper: https://arxiv.org/abs/2512.13564 | Scope clarification for agent memory versus RAG/context engineering/LLM memory. | Use as taxonomy guardrail so Brain does not call retrieval-only behavior “memory learning.” |

## Immediate implications for this Brain

1. **Completion criteria need benchmarks, not anecdotes.** World-level readiness should compare Brain against at least one long-horizon memory benchmark shape: temporal questions, multi-hop personal facts, stale-fact resistance, and correction recovery.
2. **Temporal validity is a recurring differentiator.** Zep/Graphiti and Brain's atoms supersession point in the same direction; readiness should keep blocking stale-current-truth regressions.
3. **Graph retrieval must be budgeted.** HippoRAG/TERAG-style graph expansion is useful only if it improves hard multi-hop rows without increasing prompt/token cost beyond the CLI/subscription budget contract.
4. **Learning quality needs outcome lift.** A-MEM/Hindsight/H²R reinforce that stored lessons/procedures must improve later behavior. Brain's current instrumentation is correct but still needs enough linked post-use outcomes before claiming lift.
5. **Dependency adoption is not implied.** The local Brain already has FastAPI, Qdrant, Neo4j, SQLite, MCP, CLI-first LLM dispatch, and direct Telegram alerts. External systems are comparison baselines and source of test ideas unless a future dependency audit proves a replacement is safer and better.

## Next eval rows to add

- Temporal-validity personal-memory cases: old preference superseded by newer preference.
- Multi-hop agent-execution cases: handoff → dispatch attempt → outcome → failure lesson/procedure reuse.
- Graph alias cases: entity name variants and source-term bridges learned from query logs.
- Long-horizon correction cases: user correction captured, stale claim forbidden, canonical answer preferred.
- Cost/latency guard rows: graph or reflection improvements must not exceed agreed p95 latency/token budgets.
