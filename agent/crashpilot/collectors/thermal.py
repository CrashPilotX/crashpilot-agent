"""Thermal and power sensor collector."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .base import BaseCollector, cmd_available, run_cmd


class ThermalCollector(BaseCollector):
    name = "thermal"

    async def collect(self) -> dict[str, Any]:
        lm_sensors = await self._collect_lm_sensors()
        sysfs_temps = await self._collect_sysfs_temps()
        cpu_freq = await self._collect_cpu_freq()
        rapl = await self._collect_rapl()
        thermal_zones = await self._collect_thermal_zones()

        # Find any over-threshold readings
        warnings = self._find_warnings(lm_sensors, sysfs_temps, thermal_zones)

        return {
            "lm_sensors": lm_sensors,
            "sysfs_temperatures": sysfs_temps,
            "cpu_frequencies_mhz": cpu_freq,
            "rapl_power": rapl,
            "thermal_zones": thermal_zones,
            "thermal_warnings": warnings,
        }

    async def _collect_lm_sensors(self) -> dict:
        if not cmd_available("sensors"):
            return {"available": False}
        stdout, _, rc = await run_cmd("sensors", "-j", timeout=15)
        if rc != 0:
            return {"available": False, "error": "sensors command failed"}
        try:
            return {"available": True, "data": json.loads(stdout)}
        except json.JSONDecodeError:
            return {"available": True, "raw": stdout[:3000]}

    async def _collect_sysfs_temps(self) -> list[dict]:
        """Read hwmon temperature inputs from sysfs."""
        temps = []
        for hwmon in Path("/sys/class/hwmon").glob("hwmon*"):
            try:
                name_file = hwmon / "name"
                chip_name = name_file.read_text().strip() if name_file.exists() else hwmon.name
                for temp_file in sorted(hwmon.glob("temp*_input")):
                    label_file = temp_file.parent / (temp_file.name.replace("_input", "_label"))
                    label = label_file.read_text().strip() if label_file.exists() else temp_file.name
                    crit_file = temp_file.parent / (temp_file.name.replace("_input", "_crit"))
                    try:
                        temp_c = int(temp_file.read_text().strip()) / 1000
                        crit_c = int(crit_file.read_text().strip()) / 1000 if crit_file.exists() else None
                        temps.append({
                            "chip": chip_name,
                            "label": label,
                            "temp_c": temp_c,
                            "crit_c": crit_c,
                        })
                    except (OSError, ValueError):
                        continue
            except OSError:
                continue
        return temps

    async def _collect_cpu_freq(self) -> list[dict]:
        freqs = []
        base = Path("/sys/devices/system/cpu")
        for cpu in sorted(base.glob("cpu[0-9]*"))[:8]:  # first 8 cores
            freq_file = cpu / "cpufreq" / "scaling_cur_freq"
            max_file = cpu / "cpufreq" / "scaling_max_freq"
            try:
                cur_mhz = int(freq_file.read_text().strip()) // 1000
                max_mhz = int(max_file.read_text().strip()) // 1000 if max_file.exists() else None
                freqs.append({"cpu": cpu.name, "current_mhz": cur_mhz, "max_mhz": max_mhz})
            except (OSError, ValueError):
                continue
        return freqs

    async def _collect_rapl(self) -> list[dict]:
        """Intel RAPL power readings."""
        entries = []
        for zone in Path("/sys/class/powercap").glob("intel-rapl:*"):
            try:
                name = (zone / "name").read_text().strip()
                energy_uj = int((zone / "energy_uj").read_text().strip())
                entries.append({"zone": zone.name, "name": name, "energy_uj": energy_uj})
            except (OSError, ValueError):
                continue
        return entries

    async def _collect_thermal_zones(self) -> list[dict]:
        zones = []
        for zone in sorted(Path("/sys/class/thermal").glob("thermal_zone*")):
            try:
                zone_type = (zone / "type").read_text().strip()
                temp_c = int((zone / "temp").read_text().strip()) / 1000
                trip_points = []
                for trip in sorted(zone.glob("trip_point_*_temp")):
                    try:
                        tp_type = (zone / trip.name.replace("_temp", "_type")).read_text().strip()
                        tp_temp = int(trip.read_text().strip()) / 1000
                        trip_points.append({"type": tp_type, "temp_c": tp_temp})
                    except (OSError, ValueError):
                        continue
                zones.append({
                    "zone": zone.name,
                    "type": zone_type,
                    "temp_c": temp_c,
                    "trip_points": trip_points,
                })
            except (OSError, ValueError):
                continue
        return zones

    def _find_warnings(self, lm_sensors: dict, sysfs: list, zones: list) -> list[str]:
        warnings = []
        # Check sysfs temps against critical thresholds
        for t in sysfs:
            if t.get("crit_c") and t["temp_c"] >= t["crit_c"] * 0.9:
                warnings.append(
                    f"{t['chip']}/{t['label']} at {t['temp_c']}°C (critical: {t['crit_c']}°C)"
                )
        # Generic high-temp check (>95°C for CPU/GPU)
        for t in sysfs:
            if t["temp_c"] >= 95:
                warnings.append(f"CRITICAL temp: {t['chip']}/{t['label']} = {t['temp_c']}°C")
        return warnings
