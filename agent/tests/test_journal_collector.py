"""Tests for the journal collector's boot selection and timestamp handling.

journalctl --list-boots --output=json returns boots oldest-first (the
current boot, index 0, is always the LAST entry) and represents
first_entry/last_entry as microseconds-since-epoch integers, not ISO
strings. Both of those real-world formats are easy to get backwards
against synthetic test fixtures, so these tests use realistic values.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

import crashpilot.collectors.journal as journal_module
from crashpilot.collectors.journal import JournalCollector, _boot_timestamp_to_iso

_BOOTS = [
    {"index": -2, "boot_id": "aaaa", "first_entry": 1787216153115928, "last_entry": 1787372927498060},
    {"index": -1, "boot_id": "bbbb", "first_entry": 1787372937558594, "last_entry": 1787372970458461},
    {"index": 0, "boot_id": "cccc", "first_entry": 1787617204966268, "last_entry": 1787619833519387},
]

# One JSON object per line: oldest boot first, current boot (index 0) last.
_REAL_LIST_BOOTS_OUTPUT = "\n".join(json.dumps(entry) for entry in _BOOTS)

# What systemd 255 (Ubuntu 24.04) actually prints: one JSON array on a
# single line, compact, with a trailing newline.
_SYSTEMD_255_LIST_BOOTS_OUTPUT = json.dumps(_BOOTS, separators=(",", ":")) + "\n"

# What systemd 249 (Ubuntu 22.04) prints for the same command: it ignores
# --output=json for --list-boots, and --utc keeps the times in UTC.
_SYSTEMD_249_LIST_BOOTS_OUTPUT = (
    "-2 1111111111111111111111111111111a Mon 2026-08-17 21:10:00 UTC—Mon 2026-08-17 22:00:00 UTC\n"
    "-1 2222222222222222222222222222222b Mon 2026-08-17 22:05:00 UTC—Tue 2026-08-18 01:03:53 UTC\n"
    " 0 3333333333333333333333333333333c Tue 2026-08-18 09:00:00 UTC—Tue 2026-08-18 10:00:00 UTC\n"
)


class TestBootTimestampToIso:
    def test_converts_microsecond_epoch_int(self):
        assert _boot_timestamp_to_iso(1787619833519387) == "2026-08-25T01:03:53Z"

    def test_converts_microsecond_epoch_string(self):
        assert _boot_timestamp_to_iso("1787619833519387") == "2026-08-25T01:03:53Z"

    def test_passes_through_non_numeric_string(self):
        assert _boot_timestamp_to_iso("") == ""


@pytest.mark.asyncio
class TestJournalCollectorBootSelection:
    async def test_current_and_previous_are_the_last_two_entries(self, monkeypatch):
        monkeypatch.setattr(
            journal_module,
            "run_cmd",
            AsyncMock(return_value=(_REAL_LIST_BOOTS_OUTPUT, "", 0)),
        )
        collector = JournalCollector()
        collector._get_boot_logs = AsyncMock(return_value="")
        collector._get_shutdown_info = AsyncMock(return_value="")
        collector._get_coredumps = AsyncMock(return_value=[])
        collector._get_oom_events = AsyncMock(return_value="")
        collector._get_boot_endings = AsyncMock(return_value=[])

        result = await collector.collect()

        # index 0 ("cccc") is the current boot even though it's last in
        # journalctl's output; "bbbb" (index -1) is the previous boot.
        assert result["current_boot_id"] == "cccc"
        assert result["previous_boot_id"] == "bbbb"

    async def test_boots_list_timestamps_are_iso_not_raw_epoch(self, monkeypatch):
        monkeypatch.setattr(
            journal_module,
            "run_cmd",
            AsyncMock(return_value=(_REAL_LIST_BOOTS_OUTPUT, "", 0)),
        )
        collector = JournalCollector()
        collector._get_boot_logs = AsyncMock(return_value="")
        collector._get_shutdown_info = AsyncMock(return_value="")
        collector._get_coredumps = AsyncMock(return_value=[])
        collector._get_oom_events = AsyncMock(return_value="")
        collector._get_boot_endings = AsyncMock(return_value=[])

        result = await collector.collect()

        current_boot = next(b for b in result["boots"] if b["boot_id"] == "cccc")
        assert current_boot["last_entry"] == "2026-08-25T01:03:53Z"

    async def test_systemd_255_single_json_array(self, monkeypatch):
        # Found on Ubuntu 24.04: the whole list is one array, and reading it as
        # one object per line raised AttributeError, which took the collector
        # down and left every report with boot_id "unknown".
        monkeypatch.setattr(
            journal_module,
            "run_cmd",
            AsyncMock(return_value=(_SYSTEMD_255_LIST_BOOTS_OUTPUT, "", 0)),
        )
        collector = JournalCollector()
        collector._get_boot_logs = AsyncMock(return_value="")
        collector._get_shutdown_info = AsyncMock(return_value="")
        collector._get_coredumps = AsyncMock(return_value=[])
        collector._get_oom_events = AsyncMock(return_value="")
        collector._get_boot_endings = AsyncMock(return_value=[])

        result = await collector.safe_collect()

        assert "error" not in result
        assert result["current_boot_id"] == "cccc"
        assert result["previous_boot_id"] == "bbbb"
        assert [b["boot_id"] for b in result["boots"]] == ["aaaa", "bbbb", "cccc"]
        assert result["boots"][-1]["last_entry"] == "2026-08-25T01:03:53Z"

    async def test_systemd_249_text_table(self, monkeypatch):
        run = AsyncMock(return_value=(_SYSTEMD_249_LIST_BOOTS_OUTPUT, "", 0))
        monkeypatch.setattr(journal_module, "run_cmd", run)
        collector = JournalCollector()

        boots = await collector._list_boots()

        assert "--utc" in run.call_args.args
        assert [b["boot_id"] for b in boots] == [
            "1111111111111111111111111111111a",
            "2222222222222222222222222222222b",
            "3333333333333333333333333333333c",
        ]
        assert boots[1] == {
            "index": -1,
            "boot_id": "2222222222222222222222222222222b",
            "first_entry": "2026-08-17T22:05:00Z",
            "last_entry": "2026-08-18T01:03:53Z",
        }

    async def test_unexpected_json_is_skipped_not_fatal(self, monkeypatch):
        monkeypatch.setattr(
            journal_module,
            "run_cmd",
            AsyncMock(return_value=('"not a boot"\n[1, 2]\nnot json\n', "", 0)),
        )
        collector = JournalCollector()

        assert await collector._list_boots() == []


_CLEAN_END = "\n".join([
    "2026-10-20T12:30:18+00:00 host systemd[1]: Shutting down.",
    "2026-10-20T12:30:18+00:00 host systemd-shutdown[1]: Syncing filesystems and block devices.",
    "2026-10-20T12:30:18+00:00 host systemd-journald[585]: Journal stopped",
])
_CUT_OFF_END = "\n".join([
    "2026-10-22T14:12:07+00:00 host runsvc.sh[500]: Running job: gpu-burn on rtx",
    "2026-10-22T14:12:39+00:00 host cloudflared[40]: INF Tunnel connection",
])


@pytest.mark.asyncio
class TestShutdownRecordAndBootEndings:
    async def test_the_shutdown_record_is_asked_for_by_identifier_not_unit(self, monkeypatch):
        # `-u systemd-shutdown` finds nothing even after a clean shutdown: its lines are not written
        # under a unit. The identifier is what finds them.
        calls = []

        async def fake(*args, **kwargs):
            calls.append(args)
            return "-- No entries --\n", "", 0

        monkeypatch.setattr(journal_module, "run_cmd", fake)
        assert await JournalCollector()._get_shutdown_info() == ""  # the placeholder is not a record
        assert "-t" in calls[0] and "systemd-shutdown" in calls[0] and "-u" not in calls[0]

    async def test_a_real_shutdown_record_is_returned(self, monkeypatch):
        record = "2026-10-20T12:30:18+00:00 host systemd-shutdown[1]: Syncing filesystems and block devices."
        monkeypatch.setattr(journal_module, "run_cmd", AsyncMock(return_value=(record + "\n", "", 0)))
        assert await JournalCollector()._get_shutdown_info() == record

    async def test_each_earlier_boot_is_marked_clean_cut_off_or_unreadable(self, monkeypatch):
        outputs = {
            "clean": (_CLEAN_END, "", 0),
            "cutoff": (_CUT_OFF_END, "", 0),
            "failed": ("", "boot not found", 1),
            "empty": ("", "", 0),
        }

        async def fake(*args, **kwargs):
            boot = next(a.split("=", 1)[1] for a in args if a.startswith("--boot="))
            return outputs[boot]

        monkeypatch.setattr(journal_module, "run_cmd", fake)
        boots = [{"boot_id": name, "first_entry": "f", "last_entry": "l"} for name in ("clean", "cutoff", "failed", "empty", "current")]
        endings = await JournalCollector()._get_boot_endings(boots)
        assert [(e["boot_id"], e["ended_cleanly"]) for e in endings] == [
            ("clean", True), ("cutoff", False), ("failed", None), ("empty", None),
        ]  # the current boot has not ended, so it is not listed
        assert endings[0]["first_entry"] == "f" and endings[0]["last_entry"] == "l"

    async def test_only_the_most_recent_boots_are_checked(self, monkeypatch):
        asked = []

        async def fake(*args, **kwargs):
            asked.append(next(a for a in args if a.startswith("--boot=")))
            return _CLEAN_END, "", 0

        monkeypatch.setattr(journal_module, "run_cmd", fake)
        boots = [{"boot_id": f"b{i:02d}"} for i in range(15)]
        endings = await JournalCollector()._get_boot_endings(boots)
        assert [e["boot_id"] for e in endings] == [f"b{i:02d}" for i in range(6, 14)]
        assert len(asked) == 8

    async def test_collect_reports_the_endings(self, monkeypatch):
        monkeypatch.setattr(journal_module, "run_cmd", AsyncMock(return_value=(_REAL_LIST_BOOTS_OUTPUT, "", 0)))
        collector = JournalCollector()
        collector._get_boot_logs = AsyncMock(return_value="")
        collector._get_shutdown_info = AsyncMock(return_value="")
        collector._get_coredumps = AsyncMock(return_value=[])
        collector._get_oom_events = AsyncMock(return_value="")
        collector._get_boot_endings = AsyncMock(return_value=[{"boot_id": "bbbb", "ended_cleanly": False}])
        result = await collector.collect()
        assert result["boot_endings"] == [{"boot_id": "bbbb", "ended_cleanly": False}]
