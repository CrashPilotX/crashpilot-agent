"""GPU telemetry collector: NVIDIA and AMD."""

from __future__ import annotations

import json
from typing import Any

from .base import BaseCollector, cmd_available, run_cmd


class GpuCollector(BaseCollector):
    name = "gpu"

    async def collect(self) -> dict[str, Any]:
        nvidia = await self._collect_nvidia()
        amd = await self._collect_amd()
        return {
            "nvidia": nvidia,
            "amd": amd,
            "any_gpu_found": bool(nvidia.get("gpus") or amd.get("gpus")),
        }

    # ── NVIDIA ────────────────────────────────────────────────────────────────

    async def _collect_nvidia(self) -> dict:
        if not cmd_available("nvidia-smi"):
            return {"available": False}

        # Query key metrics as CSV
        query_fields = ",".join([
            "index", "name", "uuid", "driver_version",
            "temperature.gpu", "temperature.memory",
            "utilization.gpu", "utilization.memory",
            "memory.total", "memory.used", "memory.free",
            "power.draw", "power.limit",
            "clocks.current.graphics", "clocks.current.memory",
            "pcie.link.gen.current", "pcie.link.width.current",
            "ecc.errors.corrected.aggregate.total",
            "ecc.errors.uncorrected.aggregate.total",
            "retired_pages.single_bit_ecc.count",
            "retired_pages.double_bit.count",
        ])
        stdout, _, rc = await run_cmd(
            "nvidia-smi",
            f"--query-gpu={query_fields}",
            "--format=csv,noheader,nounits",
            timeout=30,
        )

        gpus = []
        if rc == 0 and stdout.strip():
            for line in stdout.strip().splitlines():
                parts = [p.strip() for p in line.split(",")]
                if len(parts) < 20:
                    continue

                def safe_int(v: str) -> int | None:
                    try:
                        return int(v)
                    except (ValueError, TypeError):
                        return None

                def safe_float(v: str) -> float | None:
                    try:
                        return float(v)
                    except (ValueError, TypeError):
                        return None

                gpus.append({
                    "index": safe_int(parts[0]),
                    "name": parts[1],
                    "uuid": parts[2],
                    "driver_version": parts[3],
                    "temperature_gpu": safe_int(parts[4]),
                    "temperature_memory": safe_int(parts[5]),
                    "utilization_gpu_pct": safe_int(parts[6]),
                    "utilization_memory_pct": safe_int(parts[7]),
                    "memory_total_mb": safe_int(parts[8]),
                    "memory_used_mb": safe_int(parts[9]),
                    "memory_free_mb": safe_int(parts[10]),
                    "power_draw_w": safe_float(parts[11]),
                    "power_limit_w": safe_float(parts[12]),
                    "clock_graphics_mhz": safe_int(parts[13]),
                    "clock_memory_mhz": safe_int(parts[14]),
                    "pcie_gen": safe_int(parts[15]),
                    "pcie_width": safe_int(parts[16]),
                    "ecc_corrected": safe_int(parts[17]),
                    "ecc_uncorrected": safe_int(parts[18]),
                    "retired_pages_single": safe_int(parts[19]),
                    "retired_pages_double": safe_int(parts[20]) if len(parts) > 20 else None,
                })

        # Also grab recent GPU error log
        error_log, _, _ = await run_cmd(
            "nvidia-smi", "--query-gpu=timestamp,name,pstate", "--format=csv", timeout=10
        )

        # XID errors from dmesg (NVIDIA-specific fault codes)
        xid_stdout, _, _ = await run_cmd(
            "journalctl", "--no-pager", "--lines=500", "--grep=NVRM.*Xid"
        )

        return {
            "available": True,
            "gpus": gpus,
            "xid_errors": xid_stdout.strip(),
        }

    # ── AMD ───────────────────────────────────────────────────────────────────

    async def _collect_amd(self) -> dict:
        if not cmd_available("rocm-smi"):
            # Try reading sysfs directly
            return await self._amd_sysfs()

        stdout, _, rc = await run_cmd("rocm-smi", "--json", timeout=30)
        if rc != 0:
            return {"available": False}

        try:
            data = json.loads(stdout)
            return {"available": True, "gpus": data}
        except json.JSONDecodeError:
            return {"available": True, "raw": stdout[:2000]}

    async def _amd_sysfs(self) -> dict:
        """Read basic AMD GPU info from sysfs when rocm-smi is absent."""
        from pathlib import Path

        gpus = []
        base = Path("/sys/class/drm")
        if not base.exists():
            return {"available": False}

        for card in sorted(base.glob("card[0-9]")):
            vendor_path = card / "device" / "vendor"
            try:
                vendor = vendor_path.read_text().strip()
            except OSError:
                continue
            if vendor != "0x1002":  # AMD PCI vendor ID
                continue
            temp = None
            for tp in card.glob("device/hwmon/*/temp1_input"):
                try:
                    temp = int(tp.read_text().strip()) // 1000
                except (OSError, ValueError):
                    pass
            gpus.append({"sysfs_path": str(card), "temperature_c": temp})

        return {"available": bool(gpus), "gpus": gpus}
