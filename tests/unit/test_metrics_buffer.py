from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "brain_core"))

from metrics_buffer import MIN_WINDOW_SAMPLES, MetricsBuffer


def test_metrics_publish_low_sample_latency_with_floor_metadata():
    mb = MetricsBuffer()
    mb.record_request("/recall/v2", 350.0, status_code=200)
    mb.record_search_latency(350, {"search_ms": 180, "cross_encoder_ms": 167})

    snap = mb.snapshot()
    route = snap["routes"]["/recall/v2"]
    phase = snap["phase_latency"]["cross_encoder_ms"]

    assert route["window_count"] == 1
    assert route["min_window_samples"] == MIN_WINDOW_SAMPLES
    assert route["sample_floor_met"] is False
    assert route["p95_ms"] == 350.0
    assert phase["sample_floor_met"] is False
    assert phase["p95_ms"] == 167.0


def test_metrics_marks_sample_floor_met_after_minimum_window_samples():
    mb = MetricsBuffer()
    for _ in range(MIN_WINDOW_SAMPLES):
        mb.record_request("/recall/v2", 120.0, status_code=200)
        mb.record_search_latency(120, {"cross_encoder_ms": 80})

    snap = mb.snapshot()

    assert snap["routes"]["/recall/v2"]["sample_floor_met"] is True
    assert snap["phase_latency"]["cross_encoder_ms"]["sample_floor_met"] is True
