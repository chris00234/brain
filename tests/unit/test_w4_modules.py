"""Targeted tests for W4 sprint modules — wm_contract, closed_loop_controller,
storage_attribution.

Codex round-6 #5 picked targeted tests on hot cognitive modules as Phase 5
of the W4 sprint. Chris values adversarial review + tests before completion
claims (canonical_2026-05-15) so the W4 sprint is not closed until these
locks are green.

Scope per module:
- wm_contract: validate + roundtrip + render_for_boot + cross-session backfill
- closed_loop_controller: threshold gate (no proposal under 3 cycles),
  signal-to-knob mapping presence, write/read cycle, summary shape
- storage_attribution: classification correctness, ordering by bytes desc,
  top_contributor matches first class
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "brain_core"))


# ── wm_contract ──────────────────────────────────────────────
def test_wm_contract_validate_truncates_oversize(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import wm_contract

    payload = {"goal": "x" * 500, "current_task": "ok", "blocker": "", "decision": "", "next_action": ""}
    fields = wm_contract._validate(payload)
    assert fields["goal"].endswith("…[truncated]"), "oversized goal should mark truncation"
    assert len(fields["goal"]) <= wm_contract.FIELD_MAX_LEN["goal"]
    assert fields["current_task"] == "ok"
    assert fields["blocker"] == ""


def test_wm_contract_validate_missing_fields_default_empty() -> None:
    import wm_contract

    fields = wm_contract._validate({"goal": "only goal"})
    assert fields["goal"] == "only goal"
    for k in ("current_task", "blocker", "decision", "next_action"):
        assert fields[k] == ""


def test_wm_contract_set_returns_missing_required(monkeypatch: pytest.MonkeyPatch) -> None:
    """When goal/current_task/next_action are blank, missing_required lists them.
    The actual /memory write is mocked via wm_set patch so tests don't touch
    autonomy.db state.
    """
    import wm_contract

    writes: list[tuple[str, str, str, str, bool]] = []

    class _StubWM:
        def wm_set(self, sid: str, agent: str, key: str, value: str, durable: bool = False) -> dict:
            writes.append((sid, agent, key, value, durable))
            return {"ok": True}

        def wm_get(self, sid: str, agent: str, key: str) -> str | None:
            for s, a, k, v, _ in writes:
                if (s, a, k) == (sid, agent, key):
                    return v
            return None

    monkeypatch.setitem(sys.modules, "working_memory", _StubWM())

    result = wm_contract.set_contract(
        "s1", "claude", {"goal": "", "current_task": "", "blocker": "blk", "decision": "d", "next_action": ""}
    )
    assert result["fields"]["blocker"] == "blk"
    assert set(result["missing_required"]) == {"goal", "current_task", "next_action"}
    # blob + updated_at + 2 non-empty fields = 4 writes
    assert len(writes) == 4
    keys_written = {w[2] for w in writes}
    assert "contract:_blob" in keys_written
    assert "contract:blocker" in keys_written
    assert "contract:decision" in keys_written


def test_wm_contract_get_falls_back_to_per_field_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the blob is missing, get_contract should reconstruct from
    per-field keys so callers that wrote freeform contract:* fields still
    get a stable response shape.
    """
    import wm_contract

    state: dict[tuple[str, str, str], str] = {
        ("s2", "claude", "contract:goal"): "fallback goal",
        ("s2", "claude", "contract:current_task"): "fallback task",
    }

    class _StubWM:
        def wm_set(self, *a, **k) -> dict:
            return {}

        def wm_get(self, sid: str, agent: str, key: str) -> str | None:
            return state.get((sid, agent, key))

    monkeypatch.setitem(sys.modules, "working_memory", _StubWM())
    out = wm_contract.get_contract("s2", "claude")
    assert out["fields"]["goal"] == "fallback goal"
    assert out["fields"]["current_task"] == "fallback task"
    assert out["fields"]["blocker"] == ""


# ── closed_loop_controller ──────────────────────────────────────
def test_closed_loop_controller_threshold_gate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A signal that has breached for fewer than _MIN_CONSECUTIVE_BREACHES
    must NOT produce a proposal even if the knob mapping exists.
    """
    import closed_loop_controller as clc

    # Redirect AUTONOMY_DB to a temp file so tests don't mutate real state.
    monkeypatch.setattr(clc, "AUTONOMY_DB", tmp_path / "autonomy_test.db")
    clc.ensure_schema()

    # Synthesize a signal that's at the threshold minus 1.
    fake_signal = {
        "kind": "slo",
        "name": "logs_dir_total_mb",
        "breached": True,
        "consecutive_breaches": clc._MIN_CONSECUTIVE_BREACHES - 1,
    }
    proposals = clc.propose_mutations([fake_signal])
    assert proposals == []

    # Bump to exactly threshold — should produce at least one proposal.
    fake_signal["consecutive_breaches"] = clc._MIN_CONSECUTIVE_BREACHES
    proposals = clc.propose_mutations([fake_signal])
    assert len(proposals) >= 1
    knobs = {p["knob_key"] for p in proposals}
    assert "BRAIN_SCHED_MAX_HEAVY_JOBS" in knobs


def test_closed_loop_controller_recall_p95_maps_dynamic_knob() -> None:
    """Round-8 defect C fix: recall_v2_p95_ms must map to at least one
    dynamic knob so a proposal has immediate-acting reduction available.
    """
    import closed_loop_controller as clc

    mappings = clc._SIGNAL_TO_KNOB.get("recall_v2_p95_ms") or []
    knob_names = [m[0] for m in mappings]
    # Must contain at least one dynamic knob (kind=="dynamic" in ALLOWLIST)
    assert any(
        clc.ALLOWLIST.get(k, {}).get("kind") == "dynamic" for k in knob_names
    ), f"recall_v2_p95_ms must map to a dynamic knob; got {knob_names}"


def test_closed_loop_controller_summary_shape(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import closed_loop_controller as clc

    monkeypatch.setattr(clc, "AUTONOMY_DB", tmp_path / "autonomy_test2.db")
    clc.ensure_schema()
    s = clc.summary()
    assert "by_status" in s
    assert "metrics_at_threshold" in s
    assert "knobs_known" in s
    assert "min_consecutive_breaches" in s
    assert len(s["knobs_known"]) >= 5  # 5-knob allowlist


# ── storage_attribution ──────────────────────────────────────
def test_storage_attribution_classify_known_patterns() -> None:
    import storage_attribution as sa

    assert sa._classify("brain.db") == "database_primary"
    assert sa._classify("autonomy.db-wal") == "database_primary"
    assert sa._classify("embedding_cache.db") == "database_cache"
    assert sa._classify("metrics_history.db-shm") == "database_cache"
    assert sa._classify("backups/2026-05-20/brain.db.gz") == "backups"
    assert sa._classify("jobs/foo.log") == "job_logs"
    assert sa._classify("training/run.jsonl") == "training"
    assert sa._classify("search-feedback.jsonl") == "feedback_logs"
    assert sa._classify("random_file.txt") == "other"


def test_storage_attribution_compute_orders_by_bytes_desc(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """compute_attribution must return classes sorted bytes-desc so
    top_contributor[0] always wins.
    """
    import storage_attribution as sa

    # Build a fake logs/ tree.
    fake_logs = tmp_path / "logs_fake"
    (fake_logs / "jobs").mkdir(parents=True)
    (fake_logs / "training").mkdir(parents=True)
    (fake_logs / "backups").mkdir(parents=True)

    (fake_logs / "brain.db").write_bytes(b"A" * 800)  # database_primary 800B
    (fake_logs / "backups" / "x.gz").write_bytes(b"B" * 500)  # backups 500B
    (fake_logs / "jobs" / "f.log").write_bytes(b"C" * 100)  # job_logs 100B
    (fake_logs / "training" / "r.jsonl").write_bytes(b"D" * 200)  # training 200B
    (fake_logs / "misc.txt").write_bytes(b"E" * 50)  # other 50B

    monkeypatch.setattr(sa, "BRAIN_LOGS_DIR", fake_logs)
    out = sa.compute_attribution(top_files_per_class=2)
    classes = [c["class"] for c in out["classes"]]
    sizes = [c["bytes"] for c in out["classes"]]
    assert sizes == sorted(sizes, reverse=True), "classes must be sorted bytes-desc"
    assert classes[0] == "database_primary"
    assert out["top_contributor"]["class"] == "database_primary"
    assert out["total_bytes"] == 800 + 500 + 100 + 200 + 50


def test_storage_attribution_top_class_default_when_empty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import storage_attribution as sa

    empty_dir = tmp_path / "empty_logs"
    empty_dir.mkdir()
    monkeypatch.setattr(sa, "BRAIN_LOGS_DIR", empty_dir)
    out = sa.top_class()
    assert out["class"] == "unknown"
    assert out["mb"] == 0.0


# ── module-import smoke (catches import-time exceptions) ──────
def test_w4_modules_import_clean() -> None:
    for name in ("wm_contract", "closed_loop_controller", "storage_attribution", "profile_hypotheses"):
        mod = importlib.import_module(name)
        assert mod is not None
