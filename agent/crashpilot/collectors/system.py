"""System-level telemetry: memory, CPU, PCIe, network, uptime."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .base import BaseCollector, run_cmd


class SystemCollector(BaseCollector):
    name = "system"

    async def collect(self) -> dict[str, Any]:
        mem = await self._collect_memory()
        cpu = await self._collect_cpu()
        disk = await self._collect_disk()
        pcie = await self._collect_pcie()
        last_reboot = await self._collect_last_reboot()
        boot_params = await self._collect_boot_info()
        ps_info = await self._collect_process_info()

        return {
            "memory": mem,
            "cpu": cpu,
            "disk": disk,
            "pcie_errors": pcie,
            "last_reboot_info": last_reboot,
            "boot_params": boot_params,
            "top_processes_at_crash": ps_info,
        }

    async def _collect_memory(self) -> dict:
        try:
            meminfo = Path("/proc/meminfo").read_text()
            parsed = {}
            for line in meminfo.splitlines():
                parts = line.split()
                if len(parts) >= 2:
                    key = parts[0].rstrip(":")
                    val = int(parts[1])  # kB
                    parsed[key] = val
            total_gb = round(parsed.get("MemTotal", 0) / 1024 / 1024, 2)
            free_gb = round(parsed.get("MemFree", 0) / 1024 / 1024, 2)
            avail_gb = round(parsed.get("MemAvailable", 0) / 1024 / 1024, 2)
            swap_total = round(parsed.get("SwapTotal", 0) / 1024 / 1024, 2)
            swap_free = round(parsed.get("SwapFree", 0) / 1024 / 1024, 2)
            return {
                "total_gb": total_gb,
                "free_gb": free_gb,
                "available_gb": avail_gb,
                "swap_total_gb": swap_total,
                "swap_free_gb": swap_free,
                "huge_pages_total": parsed.get("HugePages_Total", 0),
                "huge_pages_free": parsed.get("HugePages_Free", 0),
            }
        except (OSError, ValueError, IndexError) as e:
            # ValueError/IndexError guard against a malformed /proc file
            # (unexpected int()/float() parse or missing loadavg field) -
            # without this, that single bad line would raise past this
            # method into safe_collect() and lose every other system field
            # (memory, cpu, disk, etc. are collected together) instead of
            # just degrading the one field that couldn't be parsed.
            return {"error": str(e)}

    async def _collect_disk(self) -> dict:
        stdout, stderr, rc = await run_cmd(
            "df", "-h", "-P", "-x", "tmpfs", "-x", "devtmpfs", "-x", "squashfs", timeout=10
        )
        if rc != 0:
            return {"error": stderr.strip() or "df failed"}

        filesystems = []
        for line in stdout.splitlines()[1:]:
            parts = line.split()
            if len(parts) < 6:
                continue
            filesystems.append({
                "filesystem": parts[0],
                "size": parts[1],
                "used": parts[2],
                "available": parts[3],
                "use_pct": parts[4],
                "mountpoint": parts[-1],
            })
        return {"filesystems": filesystems[:20]}

    async def _collect_cpu(self) -> dict:
        try:
            cpuinfo = Path("/proc/cpuinfo").read_text()
            processors = [b for b in cpuinfo.split("\n\n") if b.strip()]
            cpu_count = len(processors)
            model = ""
            for line in cpuinfo.splitlines():
                if "model name" in line:
                    model = line.split(":", 1)[-1].strip()
                    break
            # Load average
            loadavg = Path("/proc/loadavg").read_text().split()
            return {
                "model": model,
                "cpu_count": cpu_count,
                "load_1m": float(loadavg[0]),
                "load_5m": float(loadavg[1]),
                "load_15m": float(loadavg[2]),
            }
        except (OSError, ValueError, IndexError) as e:
            # ValueError/IndexError guard against a malformed /proc file
            # (unexpected int()/float() parse or missing loadavg field) -
            # without this, that single bad line would raise past this
            # method into safe_collect() and lose every other system field
            # (memory, cpu, disk, etc. are collected together) instead of
            # just degrading the one field that couldn't be parsed.
            return {"error": str(e)}

    async def _collect_pcie(self) -> list[str]:
        """Extract PCIe AER (Advanced Error Reporting) events from dmesg."""
        stdout, _, _ = await run_cmd(
            "journalctl", "-k", "--no-pager", "--lines=1000",
            "--grep=AER|PCIe.*error|Corrected error|Uncorrected.*error",
            timeout=20,
        )
        lines = [line for line in stdout.splitlines() if line.strip()]
        return lines[:100]

    async def _collect_last_reboot(self) -> dict:
        # last -x shows shutdown/reboot entries
        stdout, _, _ = await run_cmd("last", "-x", "-n", "10", timeout=10)
        # also who -b for last boot time
        who_boot, _, _ = await run_cmd("who", "-b", timeout=5)
        # uptime
        uptime_out, _, _ = await run_cmd("uptime", timeout=5)
        return {
            "last_output": stdout,
            "last_boot_who": who_boot.strip(),
            "current_uptime": uptime_out.strip(),
        }

    async def _collect_boot_info(self) -> dict:
        """Systemd boot time breakdown."""
        stdout, _, rc = await run_cmd(
            "systemd-analyze", "blame", "--no-pager", timeout=15
        )
        critical, _, _ = await run_cmd(
            "systemd-analyze", "critical-chain", "--no-pager", timeout=15
        )
        time_out, _, _ = await run_cmd("systemd-analyze", "time", timeout=10)
        return {
            "boot_time_breakdown": time_out.strip(),
            "slow_units": stdout[:2000] if rc == 0 else "",
            "critical_chain": critical[:2000],
        }

    async def _collect_process_info(self) -> str:
        """Snapshot of processes - useful for context around a crash."""
        stdout, _, _ = await run_cmd(
            "ps", "aux", "--sort=-%mem", timeout=10
        )
        lines = stdout.splitlines()
        return "\n".join(lines[:30])  # top 30 by memory
