"""Behavioral unit tests for slos.

Beyond the smoke import test, exercises the breach-evaluation logic and
check_one() contract without touching production databases:
  - _is_breach(): higher-is-better, lower-is-better, info-only branches
  - check_one(): SLOResult shape, breach flag, delta math
  - check_one(): unknown SLO returns None
  - check_one(): measurement failure returns None (graceful)

Measurement functions are monkeypatched via the _MEASUREMENTS registry so
tests don't depend on brain.db/autonomy.db state.
"""

from __future__ import annotations

import sys
from pathlib import Path

BRAIN_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BRAIN_ROOT / "brain_core"))


def test_is_breach_lower_is_better_latency():
    import slos

    # recall_v2_p95_ms target=1000 — lower is better
    slo = slos.SLOS["recall_v2_p95_ms"]
    assert slos._is_breach(slo, 1500.0) is True
    assert slos._is_breach(slo, 800.0) is False
    assert slos._is_breach(slo, 1000.0) is False  # equal = not breached (>)


def test_is_breach_higher_is_better_content_hit():
    import slos

    slo = slos.SLOS["recall_v2_content_hit_pct"]
    # target=96, higher is better — breach below
    assert slos._is_breach(slo, 95.0) is True
    assert slos._is_breach(slo, 97.0) is False
    assert slos._is_breach(slo, 96.0) is False  # equal = not breached (<)


def test_is_breach_pancake_confidence_stddev():
    import slos

    slo = slos.SLOS["atoms_confidence_stddev_1d"]
    # target=0.05, higher is better (stddev too low = pancake)
    assert slos._is_breach(slo, 0.01) is True
    assert slos._is_breach(slo, 0.15) is False


def test_is_breach_info_only_never_breaches():
    import slos

    slo = slos.SLOS["eval_holdout_growth_weekly"]
    # Info-only: must never breach regardless of value
    assert slos._is_breach(slo, 0.0) is False
    assert slos._is_breach(slo, 999.0) is False
    assert slos._is_breach(slo, -1.0) is False


def test_check_one_unknown_slo_returns_none():
    import slos

    assert slos.check_one("definitely_not_a_real_slo") is None


def test_check_one_uses_measurement_and_reports_breach(monkeypatch):
    import slos

    # Inject a synthetic measurement that's clearly over the 1000ms target.
    monkeypatch.setitem(slos._MEASUREMENTS, "recall_v2_p95_ms", lambda: 1500.0)

    result = slos.check_one("recall_v2_p95_ms")
    assert result is not None
    assert result.slo.name == "recall_v2_p95_ms"
    assert result.actual == 1500.0
    assert result.breached is True
    assert result.delta == 500.0


def test_check_one_no_breach_when_under_target(monkeypatch):
    import slos

    monkeypatch.setitem(slos._MEASUREMENTS, "recall_v2_p95_ms", lambda: 750.0)
    result = slos.check_one("recall_v2_p95_ms")
    assert result is not None
    assert result.breached is False
    assert result.delta == -250.0


def test_check_one_measurement_exception_returns_none(monkeypatch):
    """A throwing measurement should never crash the SLO loop."""
    import slos

    def _raises() -> float:
        raise RuntimeError("simulated upstream failure")

    monkeypatch.setitem(slos._MEASUREMENTS, "recall_v2_p95_ms", _raises)
    assert slos.check_one("recall_v2_p95_ms") is None


def test_check_one_with_missing_measurement_returns_none(monkeypatch):
    """SLO exists in SLOS but no measurement registered → None (not a crash)."""
    import slos

    # Drop the measurement temporarily; SLOS still has the def.
    monkeypatch.delitem(slos._MEASUREMENTS, "recall_v2_p95_ms", raising=False)
    assert slos.check_one("recall_v2_p95_ms") is None


def test_check_all_skips_none_and_returns_list(monkeypatch):
    """check_all aggregates check_one across the registry, omitting Nones."""
    import slos

    # Make every measurement return a safe under-target value to avoid
    # hitting production code paths. We don't assert breach state here —
    # just that the aggregator runs and returns SLOResult instances.
    monkeypatch.setattr(
        slos,
        "_MEASUREMENTS",
        {name: (lambda: 0.0) for name in slos.SLOS},
    )

    results = slos.check_all()
    assert isinstance(results, list)
    assert len(results) == len(slos.SLOS)
    assert all(isinstance(r, slos.SLOResult) for r in results)


def test_slo_registry_invariants():
    """Every SLO entry has the expected dataclass fields populated."""
    import slos

    for name, slo in slos.SLOS.items():
        assert slo.name == name, f"SLO {name} has mismatched name field"
        assert slo.severity in ("info", "warning", "critical"), f"bad severity for {name}"
        assert isinstance(slo.target, int | float)
        assert slo.consecutive_breaches_required >= 1
