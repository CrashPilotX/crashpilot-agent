"""Tests for monitor.py helper functions."""

from __future__ import annotations

import pytest
from crashpilot.monitor import _extract_boot_context, _make_report_id


class TestMakeReportId:
    def test_format(self):
        rid = _make_report_id("some-boot-id")
        assert rid.startswith("crash_")
        assert len(rid) == len("crash_") + 12

    def test_deterministic(self):
        assert _make_report_id("abc") == _make_report_id("abc")

    def test_different_ids_for_different_boots(self):
        assert _make_report_id("boot1") != _make_report_id("boot2")


class TestExtractBootContext:
    def _tel(self, boots=None, shutdown="") -> dict:
        return {
            "journal": {
                "boots": boots or [],
                "current_boot_id": boots[0]["boot_id"] if boots else None,
                "previous_boot_id": boots[1]["boot_id"] if boots and len(boots) > 1 else None,
                "shutdown_info": shutdown,
            }
        }

    def test_two_boots_returns_both(self):
        tel = self._tel(boots=[
            {"boot_id": "cur", "first_entry": "t1", "last_entry": "t2"},
            {"boot_id": "prev", "first_entry": "t3", "last_entry": "t4"},
        ])
        current, previous, crash_time = _extract_boot_context(tel)
        assert current == "cur"
        assert previous == "prev"
        assert crash_time == "t4"

    def test_single_boot_no_previous(self):
        tel = self._tel(boots=[
            {"boot_id": "only", "first_entry": "t1", "last_entry": "t2"},
        ])
        current, previous, crash_time = _extract_boot_context(tel)
        assert current == "only"
        assert previous is None
        assert crash_time is None

    def test_no_boots_returns_unknown(self):
        tel = self._tel(boots=[])
        current, previous, crash_time = _extract_boot_context(tel)
        assert current == "unknown"
        assert previous is None

    def test_kubernetes_fallback(self):
        tel = {
            "journal": {"boots": [], "current_boot_id": None, "previous_boot_id": None,
                        "shutdown_info": ""},
            "kubernetes": {"available": True},
            "platform": {"container_id": "pod-abc-123"},
        }
        current, previous, crash_time = _extract_boot_context(tel)
        assert current == "pod-abc-123"
        assert previous is None
