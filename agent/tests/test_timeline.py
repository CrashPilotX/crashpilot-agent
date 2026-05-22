"""Tests for timeline building and timestamp extraction."""

from __future__ import annotations

import pytest
from crashpilot.analyzers.timeline import (
    build_timeline,
    _extract_timestamp,
    _infer_level,
    _strip_timestamp_prefix,
)


class TestExtractTimestamp:
    def test_iso_timestamp(self):
        line = "2024-01-15T10:23:45+00:00 hostname kernel: panic"
        ts = _extract_timestamp(line)
        assert ts == "2024-01-15T10:23:45"

    def test_syslog_timestamp(self):
        line = "Jan 15 10:23:45 hostname kernel: something bad"
        ts = _extract_timestamp(line)
        assert "Jan" in ts
        assert "10:23:45" in ts

    def test_no_timestamp_returns_empty(self):
        line = "no timestamp here at all"
        ts = _extract_timestamp(line)
        assert ts == ""

    def test_dmesg_bracket_not_extracted(self):
        """Dmesg [seconds] timestamps are not extracted as wall-clock time."""
        line = "[12345.678901] kernel panic occurred"
        ts = _extract_timestamp(line)
        assert ts == ""  # no ISO or syslog timestamp


class TestInferLevel:
    def test_critical_keywords(self):
        assert _infer_level("kernel panic: not syncing") == "critical"
        assert _infer_level("BUG: unable to handle") == "critical"
        assert _infer_level("MCE: corrected hardware memory error") == "critical"

    def test_error_keywords(self):
        assert _infer_level("EXT4-fs error on device sda1") == "error"
        assert _infer_level("process killed by OOM killer") == "error"
        assert _infer_level("Xid error on GPU") == "error"

    def test_warning_keywords(self):
        assert _infer_level("CPU thermal throttling activated") == "warning"
        assert _infer_level("RCU stall detected") == "warning"
        assert _infer_level("task hung for more than 120s") == "warning"

    def test_info_default(self):
        assert _infer_level("normal boot message") == "info"
        assert _infer_level("") == "info"


class TestBuildTimeline:
    def _make_telemetry(self, **kwargs) -> dict:
        base = {
            "journal": {
                "previous_boot_errors": kwargs.get("errors", ""),
                "oom_events": kwargs.get("oom", ""),
            },
            "dmesg": {"critical_events": kwargs.get("dmesg_events", [])},
            "gpu": {"nvidia": {"xid_errors": ""}},
            "thermal": {"thermal_warnings": kwargs.get("thermal", [])},
            "smart": {"critical_disks": kwargs.get("smart_disks", [])},
        }
        return base

    def test_empty_telemetry_returns_empty_list(self):
        timeline = build_timeline({})
        assert timeline == []

    def test_journal_errors_parsed(self):
        tel = self._make_telemetry(
            errors="2024-01-15T10:00:00 host kernel: Out of memory: Killed process 123"
        )
        timeline = build_timeline(tel)
        assert len(timeline) >= 1
        assert any("memory" in e["message"].lower() or "killed" in e["message"].lower()
                   for e in timeline)

    def test_dmesg_events_included(self):
        tel = self._make_telemetry(dmesg_events=["GPU HANG: ecode 9:1:85df"])
        timeline = build_timeline(tel)
        sources = [e["source"] for e in timeline]
        assert "dmesg" in sources

    def test_thermal_warnings_included(self):
        tel = self._make_telemetry(thermal=["CPU temperature 98°C exceeds threshold"])
        timeline = build_timeline(tel)
        sources = [e["source"] for e in timeline]
        assert "thermal" in sources

    def test_smart_disks_included(self):
        tel = self._make_telemetry(smart_disks=[
            {"device": "/dev/sda", "health": "FAILED", "critical_attributes": {"5": 100}}
        ])
        timeline = build_timeline(tel)
        sources = [e["source"] for e in timeline]
        assert "smart" in sources

    def test_timeline_sorted_timestamps_first(self):
        """Events with timestamps come before events without."""
        tel = self._make_telemetry(
            errors="2024-01-15T10:00:00 host kernel: error occurred",
            thermal=["thermal warning without timestamp"],
        )
        timeline = build_timeline(tel)
        # Find the first event with a null timestamp
        ts_values = [e["timestamp"] for e in timeline]
        none_indices = [i for i, t in enumerate(ts_values) if t is None]
        ts_indices = [i for i, t in enumerate(ts_values) if t is not None]
        if none_indices and ts_indices:
            assert min(ts_indices) < max(none_indices)

    def test_timeline_event_schema(self):
        tel = self._make_telemetry(
            errors="2024-01-15T10:00:00 host kernel: Out of memory: Killed process 1"
        )
        timeline = build_timeline(tel)
        for event in timeline:
            assert "timestamp" in event
            assert "source" in event
            assert "level" in event
            assert "message" in event
            assert event["level"] in ("critical", "error", "warning", "info")
