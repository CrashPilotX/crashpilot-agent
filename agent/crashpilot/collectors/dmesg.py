"""dmesg and kernel ring buffer collector."""

from __future__ import annotations

import re
from typing import Any

from .base import BaseCollector, run_cmd

# Patterns that indicate hardware/kernel problems
CRITICAL_PATTERNS = [
    r"Kernel panic",
    r"BUG:",
    r"OOPS:",
    r"general protection fault",
    r"segfault at",
    r"stack overflow",
    r"RIP:.*\[",
    r"Call Trace:",
    r"Hardware Error",
    r"MCE:",                    # Machine Check Exception
    r"EDAC",                    # Error Detection And Correction
    r"NMI:",
    r"watchdog: BUG",
    r"hung_task",
    r"soft lockup",
    r"hard LOCKUP",
    r"RCU stall",
    r"unable to handle kernel",
    r"page fault",
    r"I/O error",
    r"EXT4-fs error",
    r"BTRFS.*error",
    r"xfs.*error",
    r"ata\d+.*error",
    r"SCSI.*error",
    r"nvme.*error",
    r"GPU fault",
    r"NVRM:",
    r"amdgpu.*ERROR",
    r"PCIe.*error",
    r"AER:",                    # PCIe Advanced Error Reporting
    r"Unrecovered Error",
    r"thermal throttling",
    r"temperature.*critical",
    r"overcurrent",
    r"power.*fault",
]

_PATTERN = re.compile("|".join(CRITICAL_PATTERNS), re.IGNORECASE)


class DmesgCollector(BaseCollector):
    name = "dmesg"

    def __init__(self, max_lines: int = 2000):
        self.max_lines = max_lines

    async def collect(self) -> dict[str, Any]:
        full_output = await self._get_dmesg()
        critical_lines = self._extract_critical(full_output)
        mce_info = await self._get_mce()
        kernel_info = await self._get_kernel_info()

        return {
            "full_tail": "\n".join(full_output[-self.max_lines:]),
            "critical_events": critical_lines,
            "mce_events": mce_info,
            "kernel_version": kernel_info,
            "critical_count": len(critical_lines),
        }

    async def _get_dmesg(self) -> list[str]:
        # Try human-readable timestamps first (requires CONFIG_PRINTK_TIME or dmesg -T)
        stdout, _, rc = await run_cmd("dmesg", "-T", "--level=emerg,alert,crit,err,warn", timeout=30)
        if rc == 0 and stdout.strip():
            return stdout.splitlines()
        # Fallback without -T
        stdout, _, _ = await run_cmd("dmesg", "--level=emerg,alert,crit,err,warn", timeout=30)
        return stdout.splitlines() if stdout.strip() else []

    def _extract_critical(self, lines: list[str]) -> list[str]:
        return [line for line in lines if _PATTERN.search(line)]

    async def _get_mce(self) -> str:
        stdout, _, rc = await run_cmd("mcelog", "--client", timeout=10)
        if rc == 0:
            return stdout
        # Try reading the mcelog file directly
        stdout, _, _ = await run_cmd("cat", "/var/log/mcelog", timeout=5)
        return stdout

    async def _get_kernel_info(self) -> dict:
        uname, _, _ = await run_cmd("uname", "-a")
        cmdline, _, _ = await run_cmd("cat", "/proc/cmdline")
        return {
            "uname": uname.strip(),
            "cmdline": cmdline.strip(),
        }
