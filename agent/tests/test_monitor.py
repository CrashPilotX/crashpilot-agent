"""Tests for monitor.py helper functions."""

from __future__ import annotations

import pytest

import crashpilot.monitor as monitor_mod
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

    def test_unknown_boot_never_yields_a_shared_id(self):
        # Report IDs are unique across every machine in the cloud. Hashing the
        # literal "unknown" gave every machine without a boot ID the same one.
        assert _make_report_id("unknown") != _make_report_id("unknown")
        assert _make_report_id("unknown").startswith("crash_")


class TestKernelBootId:
    def test_reads_proc_in_journal_format(self, tmp_path):
        proc = tmp_path / "boot_id"
        proc.write_text("ad912364-5d6a-4634-af86-0dfcc1c2efb6\n")
        # journalctl prints the same ID without dashes.
        assert monitor_mod._kernel_boot_id(proc) == "ad9123645d6a4634af860dfcc1c2efb6"

    def test_missing_or_malformed_is_none(self, tmp_path):
        assert monitor_mod._kernel_boot_id(tmp_path / "absent") is None
        bad = tmp_path / "bad"
        bad.write_text("unknown\n")
        assert monitor_mod._kernel_boot_id(bad) is None


class TestExtractBootContext:
    def _tel(self, boots=None, shutdown="") -> dict:
        # boots is oldest-first, matching journalctl --list-boots: the
        # current boot is the LAST entry, previous is second-to-last.
        return {
            "journal": {
                "boots": boots or [],
                "current_boot_id": boots[-1]["boot_id"] if boots else None,
                "previous_boot_id": boots[-2]["boot_id"] if boots and len(boots) > 1 else None,
                "shutdown_info": shutdown,
            }
        }

    def test_two_boots_returns_both(self):
        tel = self._tel(boots=[
            {"boot_id": "prev", "first_entry": "t3", "last_entry": "t4"},
            {"boot_id": "cur", "first_entry": "t1", "last_entry": "t2"},
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

    def test_no_boots_falls_back_to_the_kernel_boot_id(self, monkeypatch):
        # The journal collector failing (or no journalctl at all) must not
        # leave the report without a real boot ID.
        monkeypatch.setattr(monitor_mod, "_kernel_boot_id", lambda: "b" * 32)
        tel = self._tel(boots=[])
        current, previous, crash_time = _extract_boot_context(tel)
        assert current == "b" * 32
        assert previous is None

    def test_an_unknown_crash_time_is_none_not_empty(self):
        tel = self._tel(boots=[
            {"boot_id": "prev", "first_entry": "", "last_entry": ""},
            {"boot_id": "cur", "first_entry": "", "last_entry": ""},
        ])
        assert _extract_boot_context(tel)[2] is None

    def test_no_boots_and_no_kernel_boot_id_returns_unknown(self, monkeypatch):
        monkeypatch.setattr(monitor_mod, "_kernel_boot_id", lambda: None)
        tel = self._tel(boots=[])
        current, previous, crash_time = _extract_boot_context(tel)
        assert current == "unknown"
        assert previous is None

    def test_falls_back_to_last_two_boots_when_ids_missing(self):
        # No current_boot_id/previous_boot_id keys at all - exercises the
        # boots[-1]/boots[-2] fallback directly, oldest-first like real
        # journalctl output.
        tel = {
            "journal": {
                "boots": [
                    {"boot_id": "oldest", "first_entry": "t0", "last_entry": "t1"},
                    {"boot_id": "prev", "first_entry": "t2", "last_entry": "t3"},
                    {"boot_id": "cur", "first_entry": "t4", "last_entry": "t5"},
                ],
                "shutdown_info": "",
            }
        }
        current, previous, crash_time = _extract_boot_context(tel)
        assert current == "cur"
        assert previous == "prev"
        assert crash_time == "t3"



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
    assert report["analysis"]["forensic_snapshot"]["schema_version"] == 1
    assert report["analysis"]["forensic_snapshot"]["fingerprint"]


@pytest.mark.asyncio
async def test_unknown_boot_id_never_permanently_suppresses_analysis(monkeypatch, tmp_path):
    """Regression: a platform with no discoverable boots (e.g. WSL1, no
    journalctl) gets boot_id "unknown" from _extract_boot_context's
    fallback. "unknown" isn't a real per-boot identifier, so persisting and
    comparing against it as last_analyzed_boot must never cause a second,
    genuinely-new crash to be silently skipped just because it also
    produced "unknown"."""
    monkeypatch.setenv("CRASHPILOT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("CRASHPILOT_DB_PATH", str(tmp_path / "crashpilot.db"))
    monkeypatch.setenv("CRASHPILOT_ANTHROPIC_API_KEY", "")
    import crashpilot.config as cfg_mod
    cfg_mod._settings = None
    # Not even the kernel's boot ID is readable.
    monkeypatch.setattr(monitor_mod, "_kernel_boot_id", lambda: None)

    telemetry = {
        "journal": {
            "boots": [],
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

    first = await check_and_analyze(force=False)
    second = await check_and_analyze(force=False)

    assert first is not None
    assert second is not None, (
        "second call was skipped as \"already analyzed\" even though "
        "boot_id is the non-identifying \"unknown\" sentinel both times"
    )
    # Neither may take an ID another machine could also produce.
    assert first["id"] != second["id"]


def _bare_metal_platform() -> dict:
    return {
        "type": "bare_metal",
        "distro": "ubuntu",
        "distro_version": "24.04",
        "init": "systemd",
        "kernel": "test",
        "arch": "x86_64",
        "hostname": "test-host",
    }


@pytest.mark.asyncio
async def test_a_failed_journal_collector_still_reports_under_the_real_boot(monkeypatch, tmp_path):
    monkeypatch.setenv("CRASHPILOT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("CRASHPILOT_ANTHROPIC_API_KEY", "")
    import crashpilot.config as cfg_mod
    cfg_mod._settings = None
    monkeypatch.setattr(monitor_mod, "_kernel_boot_id", lambda: "c" * 32)

    async def _collect():
        return {
            "journal": {"error": "'list' object has no attribute 'get'", "collector": "journal"},
            "dmesg": {"full_tail": "", "critical_events": [], "mce_events": ""},
            "platform": _bare_metal_platform(),
        }

    monkeypatch.setattr("crashpilot.monitor.collect_telemetry", _collect)

    report = await check_and_analyze(force=False)

    assert report is not None
    assert report["boot_id"] == "c" * 32
    assert report["id"] == _make_report_id("c" * 32)
    # A real boot ID, so this boot is not analyzed twice.
    assert await check_and_analyze(force=False) is None


@pytest.mark.asyncio
async def test_hardware_signals_do_not_break_analysis(monkeypatch, tmp_path):
    # EXT4 errors plus a failing SMART disk: the detector attaches its signals
    # as a dict, which the forensic snapshot used to slice like a list.
    monkeypatch.setenv("CRASHPILOT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("CRASHPILOT_ANTHROPIC_API_KEY", "")
    import crashpilot.config as cfg_mod
    cfg_mod._settings = None

    async def _collect():
        return {
            "journal": {
                "boots": [
                    {"boot_id": "prev", "first_entry": "t0", "last_entry": "t1"},
                    {"boot_id": "cur", "first_entry": "t2", "last_entry": "t3"},
                ],
                "current_boot_id": "cur",
                "previous_boot_id": "prev",
                "shutdown_info": "",
                "previous_boot_errors": "kernel: EXT4-fs error (device sda1): ext4_find_entry:1455",
                "previous_boot_logs_tail": "",
                "oom_events": "",
            },
            "dmesg": {"full_tail": "", "critical_events": [], "mce_events": ""},
            "smart": {"critical_disks": [{"device": "/dev/sda", "health": "FAILED"}]},
            "thermal": {"thermal_warnings": ["CPU at 99C"]},
            "platform": _bare_metal_platform(),
        }

    monkeypatch.setattr("crashpilot.monitor.collect_telemetry", _collect)

    report = await check_and_analyze(force=True)

    assert report is not None
    assert report["crash_type"] == "disk_error"
    assert report["analysis"]["heuristic"]["signals"] == {"smart_critical_disks": 1}
    assert len(report["analysis"]["forensic_snapshot"]["fingerprint"]) == 16
