"""Tests for monitor.py helper functions."""

from __future__ import annotations

import pytest

from crashpilot.monitor import _extract_boot_context, _make_report_id, check_and_analyze


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



@pytest.mark.asyncio
async def test_keyless_analysis_includes_builtin_advice(monkeypatch, tmp_path):
    """No API key should still produce plain-English advice in the saved report."""
    monkeypatch.setenv("CRASHPILOT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("CRASHPILOT_DB_PATH", str(tmp_path / "crashpilot.db"))
    monkeypatch.setenv("CRASHPILOT_ANTHROPIC_API_KEY", "")
    import crashpilot.config as cfg_mod
    cfg_mod._settings = None

    telemetry = {
        "journal": {
            "boots": [
                {"boot_id": "current", "first_entry": "t1", "last_entry": "t2"},
                {"boot_id": "previous", "first_entry": "t0", "last_entry": "t1"},
            ],
            "current_boot_id": "current",
            "previous_boot_id": "previous",
            "shutdown_info": "",
            "oom_events": "Out of memory: Killed process 1234 (python3)",
            "previous_boot_errors": "",
            "previous_boot_logs_tail": "",
        },
        "dmesg": {"full_tail": "", "critical_events": [], "mce_events": ""},
        "platform": {
            "type": "bare_metal",
            "distro": "ubuntu",
            "distro_version": "24.04",
            "init": "systemd",
            "kernel": "test",
            "arch": "x86_64",
            "hostname": "test-host",
        },
    }

    async def _collect():
        return telemetry

    monkeypatch.setattr("crashpilot.monitor.collect_telemetry", _collect)

    report = await check_and_analyze(force=True)

    assert report is not None
    assert report["analysis"]["ai_analyzed"] is False
    assert "root_cause" in report["analysis"]
    assert report["analysis"]["remediation"]
    assert report["analysis"]["monitoring_suggestions"]
