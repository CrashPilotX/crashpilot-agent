"""SMART disk health collector via smartctl."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .base import BaseCollector, cmd_available, run_cmd

# SMART attributes that are strong predictors of failure
CRITICAL_ATTRS = {
    1: "Raw_Read_Error_Rate",
    5: "Reallocated_Sectors_Count",
    10: "Spin_Retry_Count",
    184: "End-to-End_Error",
    187: "Reported_Uncorrectable_Errors",
    188: "Command_Timeout",
    196: "Reallocated_Event_Count",
    197: "Current_Pending_Sector",
    198: "Offline_Uncorrectable",
    199: "UDMA_CRC_Error_Count",
}


class SmartCollector(BaseCollector):
    name = "smart"

    async def collect(self) -> dict[str, Any]:
        if not cmd_available("smartctl"):
            return {"available": False, "reason": "smartctl not found"}

        disks = await self._discover_disks()
        results = []
        for disk in disks:
            info = await self._get_disk_smart(disk)
            if info:
                results.append(info)

        return {
            "available": True,
            "disks": results,
            "critical_disks": [d for d in results if d.get("health") != "PASSED"],
        }

    async def _discover_disks(self) -> list[str]:
        """Find physical block devices."""
        disks = []
        # Use lsblk to find real disks (not partitions)
        stdout, _, rc = await run_cmd(
            "lsblk", "-d", "-n", "-o", "NAME,TYPE", "--json"
        )
        if rc == 0 and stdout.strip():
            try:
                data = json.loads(stdout)
                for dev in data.get("blockdevices", []):
                    if dev.get("type") in ("disk", "nvme"):
                        disks.append(f"/dev/{dev['name']}")
            except json.JSONDecodeError:
                pass

        if not disks:
            # Fallback: scan /dev/sd* and /dev/nvme*
            for path in Path("/dev").glob("sd[a-z]"):
                disks.append(str(path))
            for path in Path("/dev").glob("nvme[0-9]"):
                disks.append(str(path))

        return disks[:8]  # cap at 8 drives

    async def _get_disk_smart(self, device: str) -> dict | None:
        stdout, _, rc = await run_cmd(
            "smartctl", "-a", "--json=c", device, timeout=30
        )
        if not stdout.strip():
            return None
        try:
            data = json.loads(stdout)
        except json.JSONDecodeError:
            return {"device": device, "error": "failed to parse smartctl output"}

        # Extract health
        health = data.get("smart_status", {})
        passed = health.get("passed")

        # Find critical attribute values
        critical_attrs = {}
        for attr in data.get("ata_smart_attributes", {}).get("table", []):
            attr_id = attr.get("id")
            if attr_id in CRITICAL_ATTRS:
                raw_val = attr.get("raw", {}).get("value", 0)
                if raw_val and raw_val > 0:
                    critical_attrs[CRITICAL_ATTRS[attr_id]] = raw_val

        # NVMe errors
        nvme_log = data.get("nvme_smart_health_information_log", {})
        nvme_errors = {
            "media_errors": nvme_log.get("media_errors", 0),
            "num_err_log_entries": nvme_log.get("num_err_log_entries", 0),
            "critical_warning": nvme_log.get("critical_warning", 0),
        } if nvme_log else {}

        # Self-test history
        tests = []
        for entry in data.get("ata_smart_self_test_log", {}).get("table", [])[:5]:
            tests.append({
                "type": entry.get("type", {}).get("string", ""),
                "status": entry.get("status", {}).get("string", ""),
                "lifetime_hours": entry.get("lifetime_hours", 0),
            })

        return {
            "device": device,
            "model": data.get("model_name", "Unknown"),
            "serial": data.get("serial_number", ""),
            "firmware": data.get("firmware_version", ""),
            "capacity_gb": round(
                data.get("user_capacity", {}).get("bytes", 0) / 1e9, 1
            ),
            "power_on_hours": data.get("power_on_time", {}).get("hours", 0),
            "health": "PASSED" if passed else ("FAILED" if passed is False else "UNKNOWN"),
            "critical_attributes": critical_attrs,
            "nvme_errors": nvme_errors,
            "recent_tests": tests,
            "temperature": data.get("temperature", {}).get("current", None),
        }
