"""
WSL-specific telemetry collector.

WSL1: No real kernel, no dmesg, no systemd. Reads Windows Event Log via
      PowerShell interop and WSL-specific /proc files.
WSL2: Real Linux kernel (custom Microsoft build). Has dmesg but no ACPI,
      limited hardware sensors. Reads Windows host info via /mnt/c.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .base import BaseCollector, run_cmd


class WslCollector(BaseCollector):
    name = "wsl"

    async def collect(self) -> dict[str, Any]:
        version = self._detect_wsl_version()
        if version is None:
            return {"available": False}

        wsl_conf = self._read_wsl_conf()
        interop = self._read_interop_info()
        win_host = await self._get_windows_host_info()
        memory_info = await self._get_wsl_memory()
        crash_hint = await self._get_wsl_crash_hints(version)

        return {
            "available": True,
            "version": version,
            "wsl_conf": wsl_conf,
            "interop": interop,
            "windows_host": win_host,
            "memory": memory_info,
            "crash_hints": crash_hint,
            "limitations": self._limitations(version),
        }

    def _detect_wsl_version(self) -> int | None:
        try:
            osrelease = Path("/proc/sys/kernel/osrelease").read_text().lower()
            if "microsoft" not in osrelease and "wsl" not in osrelease:
                return None
            return 2 if "wsl2" in osrelease else 1
        except OSError:
            return None

    def _read_wsl_conf(self) -> dict:
        try:
            text = Path("/etc/wsl.conf").read_text()
            conf: dict[str, dict] = {}
            section = "default"
            for line in text.splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("[") and line.endswith("]"):
                    section = line[1:-1]
                    conf[section] = {}
                elif "=" in line and section in conf:
                    k, _, v = line.partition("=")
                    conf[section][k.strip()] = v.strip()
            return conf
        except OSError:
            return {}

    def _read_interop_info(self) -> dict:
        """Read WSL/Windows interop state."""
        info: dict[str, Any] = {}
        try:
            info["wsl_env"] = os.environ.get("WSL_DISTRO_NAME", "")
            info["windows_username"] = os.environ.get("LOGNAME", "")
            # Check if Windows filesystem is mounted
            info["windows_mounted"] = Path("/mnt/c/Windows").exists()
            # WSL2 VM memory
            info["wsl_interop_pid"] = Path("/var/run/WSL").exists()
        except Exception:
            pass
        return info

    async def _get_windows_host_info(self) -> dict:
        """Gather Windows host information via PowerShell.exe (if available)."""
        info: dict[str, Any] = {}

        # Try running PowerShell.exe from Windows (available in WSL interop)
        stdout, _, rc = await run_cmd(
            "powershell.exe", "-NoProfile", "-Command",
            "[PSCustomObject]@{"
            "OS=(Get-ComputerInfo).OsName;"
            "Build=(Get-ComputerInfo).OsBuildNumber;"
            "RAM_GB=[math]::Round((Get-ComputerInfo).CsTotalPhysicalMemory/1GB,1)"
            "} | ConvertTo-Json",
            timeout=15,
        )
        if rc == 0 and stdout.strip():
            import json
            try:
                info["windows"] = json.loads(stdout)
            except Exception:
                info["windows_raw"] = stdout[:500]

        # Windows event log — last 20 critical/error system events
        evt_out, _, rc2 = await run_cmd(
            "powershell.exe", "-NoProfile", "-Command",
            "Get-EventLog -LogName System -EntryType Error,Warning -Newest 20 "
            "| Select-Object TimeGenerated,Source,EventID,Message "
            "| ConvertTo-Json -Compress",
            timeout=20,
        )
        if rc2 == 0 and evt_out.strip():
            import json
            try:
                info["windows_event_log"] = json.loads(evt_out)
            except Exception:
                info["windows_event_log_raw"] = evt_out[:2000]

        return info

    async def _get_wsl_memory(self) -> dict:
        """WSL memory info — useful because WSL2 has a memory limit."""
        try:
            meminfo = Path("/proc/meminfo").read_text()
            parsed = {}
            for line in meminfo.splitlines():
                parts = line.split()
                if len(parts) >= 2:
                    parsed[parts[0].rstrip(":")] = int(parts[1])
            return {
                "total_gb": round(parsed.get("MemTotal", 0) / 1024 / 1024, 2),
                "available_gb": round(parsed.get("MemAvailable", 0) / 1024 / 1024, 2),
                "note": "WSL2 memory is limited by .wslconfig MemoryMB setting",
            }
        except OSError:
            return {}

    async def _get_wsl_crash_hints(self, version: int) -> list[str]:
        """WSL-specific crash causes to check."""
        hints = []

        if version == 1:
            hints.append("WSL1: No true kernel — crashes are almost always Windows host issues or application bugs")
            hints.append("WSL1: Check Windows Event Viewer for System/Application errors around crash time")

        if version == 2:
            # Try to read .wslconfig via /mnt path to check memory limits
            for candidate in [
                "/mnt/c/Users/" + (os.environ.get("LOGNAME", "") or "default") + "/.wslconfig",
            ]:
                try:
                    text = Path(candidate).read_text()
                    if "memory" in text.lower():
                        hints.append(f".wslconfig found at {candidate} — check MemoryMB limit")
                    break
                except OSError:
                    continue

            # Check WSL2 kernel panic log
            for log_path in ["/var/log/dmesg", "/var/log/kern.log"]:
                try:
                    text = Path(log_path).read_text()
                    if "kernel panic" in text.lower():
                        hints.append(f"Kernel panic found in {log_path}")
                except OSError:
                    continue

        return hints

    def _limitations(self, version: int) -> list[str]:
        lims = []
        if version == 1:
            lims += [
                "No kernel ring buffer (dmesg unavailable)",
                "No systemd — cannot use journalctl boot analysis",
                "No SMART access (no direct disk hardware)",
                "No GPU telemetry (paravirtualized)",
                "No thermal sensors",
                "Crash analysis relies on Windows Event Log",
            ]
        else:
            lims += [
                "No ACPI thermal sensors (virtualized)",
                "No SMART disk access (virtual disk)",
                "GPU pass-through available but not always enabled",
                "systemd available if enabled in /etc/wsl.conf",
                "Memory limited by .wslconfig — OOM more likely than on bare metal",
            ]
        return lims
