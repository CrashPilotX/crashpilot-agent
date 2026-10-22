"""CPU, memory and disk temperatures for the heartbeat, read from the kernel's hwmon files.

Why sysfs and not `sensors` or smartctl: the files under /sys/class/hwmon are readable by
any user, need no extra software and cost a few microseconds, so they can be read on every
heartbeat. Nothing here runs a subprocess or opens a device node.

Each hwmon chip is matched by the name the driver gives it:

    cpu     k10temp (AMD), coretemp (Intel), zenpower, via_cputemp, cpu_thermal (ARM)
    memory  spd5118, jc42 (the temperature sensor on a DIMM's SPD chip)
    disk    nvme (every NVMe drive), drivetemp (SATA and SAS drives, once that module is loaded)

Everything else is ignored: GPUs are reported by their own collectors, and chips such as
network cards, the motherboard (acpitz, the board's own sensors) and batteries are not a
component the dashboard shows. A reading is only reported if the kernel gave a plausible
one. A sensor that refuses to be read, or answers with nonsense (-273 C, 255 C: the
values a sensor with nothing attached tends to return), is left out rather than reported
as a made-up number. A component with no usable sensor is left out entirely, and the
dashboard says so; many machines have no DIMM sensor, for example.

The result is keyed by component and shaped so each part can sit beside that component's
other readings in the heartbeat:

    {"cpu":    {"package_c": 93.0, "max_c": 93.0, "sensors": [...]},
     "memory": {"max_c": 35.0, "sensors": [...]},
     "disk":   {"max_c": 32.0, "sensors": [...]}}

A sensor is {"chip", "label", "temp_c"} plus "high_c" and "crit_c" when the driver states
those limits, and "device" and "model" for a disk.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

CPU_CHIPS = frozenset({"k10temp", "coretemp", "zenpower", "via_cputemp", "cpu_thermal"})
MEMORY_CHIPS = frozenset({"spd5118", "jc42"})
DISK_CHIPS = frozenset({"nvme", "drivetemp"})

# Plausible range for a temperature that is real. Outside it the sensor is broken or has
# nothing attached.
_MIN_C = -40.0
_MAX_C = 150.0
# How many sensors of one component are sent. A 64-core CPU has 64 core sensors; the
# package and chiplet readings that matter come first, so the cap drops the rest. The
# component's max_c is taken over everything read, not over what is sent.
_SENSOR_CAP = {"cpu": 16, "memory": 8, "disk": 8}

_TEMP_INPUT = re.compile(r"^temp(\d+)_input$")
_NUMBER = re.compile(r"(\d+)")
_PACKAGE_LABELS = ("package id", "tdie", "tctl")


def _read(path: Path) -> str | None:
    """A sysfs value, or None: absent, or refused (EBUSY, EIO, ENODATA)."""
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None


def _celsius(path: Path) -> float | None:
    """A hwmon temperature file (millidegrees) as degrees C, or None if unreadable or implausible."""
    raw = _read(path)
    if not raw:
        return None
    try:
        value = int(raw) / 1000
    except ValueError:
        return None
    return round(value, 1) if _MIN_C < value < _MAX_C else None


def _natural(text: str) -> list[Any]:
    """Sort key that puts 'Core 2' before 'Core 10'."""
    return [int(part) if part.isdigit() else part.lower() for part in _NUMBER.split(text)]


def _hwmon_dirs(root: Path) -> list[Path]:
    try:
        return sorted(root.glob("hwmon*"), key=lambda p: _natural(p.name))
    except OSError:
        return []


def _clean(text: str | None, limit: int = 40) -> str | None:
    if not text:
        return None
    cleaned = "".join(ch for ch in text if ch.isprintable()).strip()
    return cleaned[:limit] or None


def _sensors(hwmon: Path, chip: str) -> list[dict[str, Any]]:
    """Every readable temperature on one hwmon chip, with the limits the driver states."""
    found: list[dict[str, Any]] = []
    inputs = []
    try:
        for entry in hwmon.iterdir():
            match = _TEMP_INPUT.match(entry.name)
            if match:
                inputs.append((int(match.group(1)), entry))
    except OSError:
        return found
    for index, entry in sorted(inputs):
        temp = _celsius(entry)
        if temp is None:
            continue
        sensor: dict[str, Any] = {
            "chip": chip,
            "label": _clean(_read(hwmon / f"temp{index}_label"), 24) or f"temp{index}",
            "temp_c": temp,
        }
        for key, suffix in (("high_c", "max"), ("crit_c", "crit")):
            limit = _celsius(hwmon / f"temp{index}_{suffix}")
            if limit is not None and limit > 0:
                sensor[key] = limit
        found.append(sensor)
    return found


def _disk_identity(hwmon: Path, sys_root: Path, chip: str) -> dict[str, str]:
    """Which drive a disk sensor belongs to: nvme0 or sda, and its model when the kernel says."""
    identity: dict[str, str] = {}
    try:
        device = (hwmon / "device").resolve()
    except OSError:
        return identity
    if chip == "nvme":
        name = device.name
        identity["device"] = name
        model = _clean(_read(sys_root / "class" / "nvme" / name / "model"))
    else:
        block = device / "block"
        try:
            names = sorted(child.name for child in block.iterdir())
        except OSError:
            names = []
        identity["device"] = names[0] if names else device.name
        model = _clean(_read(device / "model"))
    if model:
        identity["model"] = model
    return identity


def _cpu_order(sensor: dict[str, Any]) -> tuple[int, list[Any]]:
    label = sensor["label"].lower()
    if label.startswith(_PACKAGE_LABELS):
        rank = 0
    elif label.startswith("tccd"):
        rank = 1
    else:
        rank = 2
    return rank, _natural(sensor["label"])


def _package_temperature(sensors: list[dict[str, Any]]) -> float | None:
    """The CPU's headline temperature: Intel's package, else AMD's Tdie, else Tctl, else the hottest."""
    for prefix in _PACKAGE_LABELS:
        matches = [s["temp_c"] for s in sensors if s["label"].lower().startswith(prefix)]
        if matches:
            return max(matches)
    return max((s["temp_c"] for s in sensors), default=None)


def collect_temperatures(sys_root: Path = Path("/sys")) -> dict[str, dict[str, Any]]:
    """CPU, memory and disk temperatures. Components with no usable sensor are absent."""
    hwmons = _hwmon_dirs(sys_root / "class" / "hwmon")
    by_component: dict[str, list[dict[str, Any]]] = {"cpu": [], "memory": [], "disk": []}
    dimm = 0

    for hwmon in hwmons:
        chip = _read(hwmon / "name")
        if chip is None:
            continue
        if chip in CPU_CHIPS:
            by_component["cpu"].extend(_sensors(hwmon, chip))
        elif chip in MEMORY_CHIPS:
            for sensor in _sensors(hwmon, chip):
                dimm += 1
                sensor["label"] = f"DIMM {dimm}"
                by_component["memory"].append(sensor)
        elif chip in DISK_CHIPS:
            identity = _disk_identity(hwmon, sys_root, chip)
            for sensor in _sensors(hwmon, chip):
                if chip == "drivetemp":
                    sensor["label"] = "Drive"
                sensor.update(identity)
                by_component["disk"].append(sensor)

    result: dict[str, dict[str, Any]] = {}
    for component, sensors in by_component.items():
        if not sensors:
            continue
        if component == "cpu":
            sensors.sort(key=_cpu_order)
        readings: dict[str, Any] = {}
        if component == "cpu":
            readings["package_c"] = _package_temperature(sensors)
        readings["max_c"] = max(s["temp_c"] for s in sensors)
        readings["sensors"] = sensors[: _SENSOR_CAP[component]]
        result[component] = readings
    return result
