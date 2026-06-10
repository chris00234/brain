# Ruff debt taxonomy — 2026-06-09 (Contract 13)

Ruff 0.8.4 (pinned in `pyproject.toml`/`uv.lock`). All counts from `ruff check`
with the committed `ruff.toml` on branch `fix/brain-suite-reliability`.

## Baseline (before this contract)

- **Visible debt** (`ruff check .`): **49 violations** across 19 files, 16 rules.
- **Hidden debt** (the 68 `extend-exclude` legacy paths, checked explicitly):
  **1,592 violations**.
- CI linted only a 5-path "hardened scope".

## Taxonomy of the 49 visible violations and their dispositions

| Category | Count | Disposition |
| --- | --- | --- |
| Archived snapshot not excluded (`archived/openclaw-2026-05-23/`, T201 x4) | 4 | Added `archived/` to `extend-exclude` — frozen snapshots of retired services are never linted, same policy as `cli/_archive/`. |
| `print` in cron/CLI `main()` blocks of 8 `brain_core` modules (T201 x19) | 19 | Per-file `T20` ignores, matching the existing documented convention (prints are the stable subprocess/CLI interface; 6 of 8 modules are in JOB_REGISTRY, all 8 have `__main__` entrypoints). |
| Mechanical code fixes (RET504 x2, UP038 x2, SIM105+S110 same block, SIM108, ANN201 x3, ANN202, ANN002, F841, RUF002 x2, RUF003) | 16 | Fixed in code. No behavior change: inline returns, `int \| float` isinstance, `contextlib.suppress`, ternary, return-type/`*args` annotations, removed unused variable, `×`→`x` in docstrings/comments. |
| Non-crypto SHA1 task signature (S324) | 1 | `usedforsecurity=False` in `eval_persistent_failures_triage.py`. |
| Intentional best-effort `except: continue` loops (S112 x3) | 3 | Inline `# noqa: S112 — reason`, matching the `atom_recall_quality.py:101` precedent. |
| SQL built from internal identifiers (S608 x4: migrations table/col lists, report window literal) | 4 | Inline `# noqa: S608 — reason`, matching the `atom_deboost.py` precedent. |
| `urlopen` to local brain service with bearer auth (S310 x2, `cli/personal_webhook.py`) | 2 | Per-file `S310` ignore in the documented trusted-local-endpoints block. |

Result: `ruff check .` → **0 violations**. CI lint widened from the 5-path
hardened scope to repo-wide `uv run ruff check .` — every non-excluded file now
stays clean or CI fails.

## Residual debt (intentionally bounded, not hidden)

**1,592 violations** remain behind the `ruff.toml` `extend-exclude` legacy
list (policy in the config: "kept lint-clean opportunistically as each phase
touches them"). By area:

| Area | Violations |
| --- | --- |
| `brain_core/` legacy modules (41 files) | 651 |
| `ingest/` | 330 |
| `brain_core/pipeline/` | 158 |
| `pipeline/` | 148 |
| `synthesis/` | 127 |
| `cli/` legacy scripts | 94 |
| `server.py` | 84 |

Top rules: T201 (552), ANN001 (197), S110 (158), ANN201 (145), ANN202 (76),
S112 (62), F401 (51), E402 (49), SIM105 (43).

Reproduce: `ruff check $(python -c "import tomllib; print(' '.join(e for e in
tomllib.load(open('ruff.toml','rb'))['extend-exclude'] if e not in
('.venv','logs','**/__pycache__')))") --statistics`

**Format drift (pre-existing, untouched):** `ruff format --check .` flags 4
files never re-formatted after edits that bypassed pre-commit:
`brain_core/skill_materializer.py`, `brain_core/skill_promotion_audit.py`,
`tests/unit/test_skill_security_audit.py`, `tests/unit/test_skill_sync.py`.
Left alone per the no-mass-formatting rule; the pre-commit hook will format
each on next touch.

## Next steps

1. **Opportunistic legacy cleanup** (existing policy): when a phase touches an
   excluded module, lint-clean it and remove it from `extend-exclude` in the
   same PR. Best first targets by debt density: `ingest/` (330), `server.py` (84).
2. **Ruff upgrade decision** (separate, per the note in `ruff.toml`): 0.8.4 →
   current adds rules that flag existing code; budget a re-baseline pass.
3. **F401 quick win**: 51 unused imports in excluded files are auto-fixable and
   behavior-safe if a dedicated pass is ever wanted.
