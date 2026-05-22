"""journalctl telemetry collector — previous and current boot logs."""

from __future__ import annotations

import json
from typing import Any

from .base import BaseCollector, run_cmd


class JournalCollector(BaseCollector):
    name = "journal"

    def __init__(self, max_lines: int = 5000):
        self.max_lines = max_lines

    async def collect(self) -> dict[str, Any]:
        boots = await self._list_boots()
        prev_boot_id = boots[1]["boot_id"] if len(boots) > 1 else None
        current_boot_id = boots[0]["boot_id"] if boots else None

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
            "boots": boots[:5],
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
            "journalctl", "--list-boots", "--output=json", "--no-pager"
        )
        if rc != 0 or not stdout.strip():
            return []
        boots = []
        for line in stdout.strip().splitlines():
            try:
                entry = json.loads(line)
                boots.append({
                    "index": entry.get("index", 0),
                    "boot_id": entry.get("boot_id", ""),
                    "first_entry": entry.get("first_entry", ""),
                    "last_entry": entry.get("last_entry", ""),
                })
            except json.JSONDecodeError:
                continue
        return boots

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
