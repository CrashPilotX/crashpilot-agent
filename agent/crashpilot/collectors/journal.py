"""journalctl telemetry collector - previous and current boot logs."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

from .base import BaseCollector, run_cmd

# systemd before 254 (Ubuntu 22.04 has 249) ignores --output=json for
# --list-boots and prints a table: "-1 <boot id> Mon 2026-08-17 22:05:00 UTC—...".
_TEXT_BOOT_ROW = re.compile(r"^\s*(-?\d+)\s+([0-9a-f]{32})\s+(.*)$")
_TEXT_UTC_TIME = re.compile(r"(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2}) UTC")


def _text_boot_entry(row: re.Match[str]) -> dict[str, Any]:
    # Only UTC times are trusted (--utc asks for them); anything else is left
    # blank rather than guessed from a local zone abbreviation.
    times = [f"{day}T{clock}Z" for day, clock in _TEXT_UTC_TIME.findall(row.group(3))]
    return {
        "index": int(row.group(1)),
        "boot_id": row.group(2),
        "first_entry": times[0] if len(times) == 2 else "",
        "last_entry": times[1] if len(times) == 2 else "",
    }


def _boot_timestamp_to_iso(value: Any) -> str:
    """journalctl --output=json reports first_entry/last_entry as microseconds
    since epoch, not a formatted date. Convert so downstream consumers (crash
    report retention, ordering, display) get a real ISO-8601 string instead of
    a raw integer that happens to sort/parse wrong."""
    if isinstance(value, int) or (isinstance(value, str) and value.isdigit()):
        try:
            return datetime.fromtimestamp(int(value) / 1_000_000, tz=timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
        except (OverflowError, OSError, ValueError):
            return str(value)
    return str(value)


class JournalCollector(BaseCollector):
    name = "journal"

    def __init__(self, max_lines: int = 5000):
        self.max_lines = max_lines

    async def collect(self) -> dict[str, Any]:
        boots = await self._list_boots()
        # journalctl --list-boots returns boots oldest-first (index 0, the
        # current boot, is always the LAST entry) - the current/previous boot
        # are the last two entries, not the first two.
        current_boot_id = boots[-1]["boot_id"] if boots else None
        prev_boot_id = boots[-2]["boot_id"] if len(boots) > 1 else None

        prev_logs = ""
        prev_priority_logs = ""
        if prev_boot_id:
            prev_logs = await self._get_boot_logs(prev_boot_id, self.max_lines)
            prev_priority_logs = await self._get_boot_logs(
                prev_boot_id, 500, priority="3"  # err and above
            )

        # Last shutdown / poweroff record
        shutdown_info = await self._get_shutdown_info()
        # systemd-coredump entries
        coredumps = await self._get_coredumps()
        # OOM kills
        oom_events = await self._get_oom_events(prev_boot_id)

        return {
            "boots": boots[-5:],
            "current_boot_id": current_boot_id,
            "previous_boot_id": prev_boot_id,
            "previous_boot_logs_tail": prev_logs,
            "previous_boot_errors": prev_priority_logs,
            "shutdown_info": shutdown_info,
            "coredumps": coredumps,
            "oom_events": oom_events,
        }

    async def _list_boots(self) -> list[dict]:
        stdout, _, rc = await run_cmd(
            "journalctl", "--list-boots", "--output=json", "--utc", "--no-pager"
        )
        if rc != 0 or not stdout.strip():
            return []
        # systemd 254+ prints the whole list as one JSON array on a single
        # line; accept that, one object per line, and the older text table,
        # and skip anything else rather than losing the whole collector to
        # one unexpected value.
        entries: list[Any] = []
        for line in stdout.strip().splitlines():
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                row = _TEXT_BOOT_ROW.match(line)
                if row:
                    entries.append(_text_boot_entry(row))
                continue
            entries.extend(parsed if isinstance(parsed, list) else [parsed])
        return [
            {
                "index": entry.get("index", 0),
                "boot_id": entry.get("boot_id", ""),
                "first_entry": _boot_timestamp_to_iso(entry.get("first_entry", "")),
                "last_entry": _boot_timestamp_to_iso(entry.get("last_entry", "")),
            }
            for entry in entries
            if isinstance(entry, dict)
        ]

    async def _get_boot_logs(
        self, boot_id: str, lines: int, priority: str | None = None
    ) -> str:
        args = [
            "journalctl",
            f"--boot={boot_id}",
            "--no-pager",
            f"--lines={lines}",
            "--output=short-iso",
        ]
        if priority:
            args += [f"--priority={priority}"]
        stdout, _, _ = await run_cmd(*args, timeout=60)
        return stdout

    async def _get_shutdown_info(self) -> str:
        stdout, _, _ = await run_cmd(
            "journalctl",
            "--no-pager",
            "--output=short-iso",
            "--lines=100",
            "-b", "-1",
            "-u", "systemd-shutdown",
        )
        return stdout

    async def _get_coredumps(self) -> list[dict]:
        stdout, _, rc = await run_cmd(
            "coredumpctl", "list", "--output=json", "--no-pager"
        )
        if rc != 0:
            return []
        try:
            return json.loads(stdout) if stdout.strip() else []
        except json.JSONDecodeError:
            return []

    async def _get_oom_events(self, boot_id: str | None) -> str:
        args = ["journalctl", "--no-pager", "--output=short-iso", "--lines=200"]
        if boot_id:
            args += [f"--boot={boot_id}"]
        args += ["--grep=Out of memory|oom_kill|Killed process"]
        stdout, _, _ = await run_cmd(*args)
        return stdout
