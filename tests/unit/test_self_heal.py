"""Unit tests for brain_core/self_heal.py — the self-healing dispatcher
that reacts to SLO breaches. Previously zero coverage."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "brain_core"))


def _reset_rate_limit():
    """Clear the rate-limit table between tests so they don't leak state."""
    import sqlite3
    from config import AUTONOMY_DB

    try:
        conn = sqlite3.connect(str(AUTONOMY_DB))
        conn.execute("DELETE FROM heal_rate_limit WHERE signal_type LIKE 'test.%'")
        conn.commit()
        conn.close()
    except Exception:
        pass


def test_healing_signal_dataclass():
    from self_heal import HealingSignal

    sig = HealingSignal(
        source="test",
        signal_type="test.alert",
        severity="medium",
        metric="foo",
        value=100.0,
        baseline=50.0,
        target="recall",
    )
    assert sig.signal_type == "test.alert"
    assert sig.severity == "medium"


def test_is_rate_limited_fresh_signal():
    """A signal never seen before must not be rate-limited."""
    _reset_rate_limit()
    from self_heal import _is_rate_limited

    assert _is_rate_limited("test.never_seen_1", "some_target") is False


def test_heal_kind_mapping():
    from self_heal import HealingSignal, _heal_kind

    sig = HealingSignal(
        source="slo_monitor",
        signal_type="slo_latency_breach",
        severity="high",
        metric="recall_v2_p95_ms",
        value=600,
        baseline=500,
        target="recall",
    )
    kind = _heal_kind(sig)
    # Should return a non-empty string mapping to an autonomy kind
    assert isinstance(kind, str)
    assert len(kind) > 0


def test_dispatch_returns_dict_for_unmapped_signal():
    """Dispatch always returns a dict. An unmapped signal type should
    not raise; it either no-ops or returns a status indicating that."""
    from self_heal import HealingSignal, dispatch

    sig = HealingSignal(
        source="test",
        signal_type="nonsense.signal_type.no_match",
        severity="low",
        metric="test_metric",
        value=10,
        baseline=5,
        target="recall",
    )

    result = dispatch(sig)
    assert isinstance(result, dict)


def test_recent_actions_returns_list():
    from self_heal import recent_actions

    out = recent_actions(limit=5)
    assert isinstance(out, list)
    # Each entry should be a dict
    for row in out:
        assert isinstance(row, dict)
