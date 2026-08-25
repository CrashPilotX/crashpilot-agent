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

# Real journalctl --list-boots --output=json sample: oldest boot first,
# current boot (index 0) last.
_REAL_LIST_BOOTS_OUTPUT = "\n".join(
    json.dumps(entry)
    for entry in [
        {"index": -2, "boot_id": "aaaa", "first_entry": 1787216153115928, "last_entry": 1787372927498060},
        {"index": -1, "boot_id": "bbbb", "first_entry": 1787372937558594, "last_entry": 1787372970458461},
        {"index": 0, "boot_id": "cccc", "first_entry": 1787617204966268, "last_entry": 1787619833519387},
    ]
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

        result = await collector.collect()

        current_boot = next(b for b in result["boots"] if b["boot_id"] == "cccc")
        assert current_boot["last_entry"] == "2026-08-25T01:03:53Z"
