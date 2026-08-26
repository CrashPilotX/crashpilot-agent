"""Tests for rolling snapshots, attribution, and forecasting."""

from __future__ import annotations

from crashpilot import flight_recorder


def _sample(timestamp: str, memory: float, disk: float, rss: float) -> dict:
    return {
        "captured_at": timestamp,
        "memory": {"used_pct": memory},
        "disk": {"used_pct": disk},
        "processes": {
            "memory": [{"pid": 42, "name": "worker", "rss_mb": rss, "cpu_pct": 10}],
        },
        "failed_services": [{"unit": "worker.service"}],
        "package_changes": ["Upgrade: worker (1.0, 1.1)"],
    }


def test_summarize_window_forecasts_and_attributes_growth(monkeypatch):
    samples = [
        _sample("2026-06-18T00:00:00+00:00", 60, 70, 100),
        _sample("2026-06-18T00:30:00+00:00", 65, 72.5, 115),
        _sample("2026-06-18T01:00:00+00:00", 70, 75, 140),
        _sample("2026-06-18T01:30:00+00:00", 75, 77.5, 160),
        _sample("2026-06-18T02:00:00+00:00", 80, 80, 175),
        _sample("2026-06-18T02:30:00+00:00", 85, 82.5, 190),
    ]
    monkeypatch.setattr(flight_recorder, "list_flight_snapshots", lambda hours: samples)

    summary = flight_recorder.summarize_window(hours=6)

    assert summary["sample_count"] == 6
    assert summary["forecasts"]["memory"]["hours_to_threshold"] == 1.0
    assert summary["forecasts"]["memory"]["confidence"] == "high"
    assert summary["forecasts"]["disk"]["hours_to_threshold"] == 2.5
    assert summary["process_memory_growth"][0]["name"] == "worker"
    assert summary["process_memory_growth"][0]["growth_mb"] == 90
    assert summary["failed_services"][0]["unit"] == "worker.service"
    assert summary["recent_package_changes"] == ["Upgrade: worker (1.0, 1.1)"]


def test_process_memory_growth_detects_a_process_that_enters_the_top8_partway_through(monkeypatch):
    """Regression: comparing only the first and latest snapshot missed any
    process that started small (outside the top-8 by memory) and grew into
    the top-8 partway through the window - exactly the "quiet leak that
    eventually OOMs the box" case this feature exists to catch. "leaker"
    isn't in the first snapshot at all (it wasn't yet big enough to rank),
    first appears at t=1h, and is the biggest process by t=2h (latest)."""
    def _sample_with(timestamp: str, rows: list[dict]) -> dict:
        return {
            "captured_at": timestamp,
            "memory": {"used_pct": 50},
            "disk": {"used_pct": 50},
            "processes": {"memory": rows},
            "failed_services": [],
            "package_changes": [],
        }

    samples = [
        _sample_with("2026-06-18T00:00:00+00:00", [
            {"pid": 1, "name": "steady", "rss_mb": 500, "cpu_pct": 5},
        ]),
        _sample_with("2026-06-18T01:00:00+00:00", [
            {"pid": 1, "name": "steady", "rss_mb": 500, "cpu_pct": 5},
            {"pid": 99, "name": "leaker", "rss_mb": 50, "cpu_pct": 20},
        ]),
        _sample_with("2026-06-18T02:00:00+00:00", [
            {"pid": 99, "name": "leaker", "rss_mb": 300, "cpu_pct": 30},
            {"pid": 1, "name": "steady", "rss_mb": 500, "cpu_pct": 5},
        ]),
    ]
    monkeypatch.setattr(flight_recorder, "list_flight_snapshots", lambda hours: samples)

    summary = flight_recorder.summarize_window(hours=3)

    growth_by_name = {row["name"]: row["growth_mb"] for row in summary["process_memory_growth"]}
    assert growth_by_name.get("leaker") == 250
    assert "steady" not in growth_by_name  # flat, below the 10 MB reporting threshold


def test_forecast_ignores_flat_or_sparse_series():
    assert flight_recorder._forecast([], ("memory", "used_pct"), 95) is None
    samples = [
        {"captured_at": "2026-06-18T00:00:00Z", "memory": {"used_pct": 50}},
        {"captured_at": "2026-06-18T01:00:00Z", "memory": {"used_pct": 50}},
        {"captured_at": "2026-06-18T02:00:00Z", "memory": {"used_pct": 50}},
    ]
    assert flight_recorder._forecast(samples, ("memory", "used_pct"), 95) is None


def test_record_snapshot_persists_capture(monkeypatch):
    captured = {"captured_at": "2026-06-18T00:00:00+00:00"}
    saved: list[dict] = []
    monkeypatch.setattr(flight_recorder, "init_db", lambda: None)
    monkeypatch.setattr(flight_recorder, "capture_snapshot", lambda deep=False: captured)
    monkeypatch.setattr(flight_recorder, "save_flight_snapshot", saved.append)

    assert flight_recorder.record_snapshot(deep=True) == captured
    assert saved == [captured]
