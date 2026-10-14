"""AMD GPU load for the heartbeat, read straight from the kernel's amdgpu driver.

Why sysfs and not rocm-smi or amd-smi: it needs no extra software (a machine that only
has the in-kernel driver is covered), it is fast, and above all it can be done without
waking a card. Idle discrete AMD GPUs are powered down by the driver after a few seconds
(runtime power management). While a card sleeps its sensor files answer "device or
resource busy" and reading them does not wake it, but the vendor tools do wake it.
A monitoring agent that woke every idle GPU once a minute would change the machine's
power behaviour, so for a sleeping card it reports what is true without asking:

    power_state "suspended", utilization 0 (nothing can be running on a powered-down
    card), memory as the kernel reports it, and no temperature or power (they cannot be
    read without waking it, and a made-up number would be worse than none).

Everything else is reported the way the NVIDIA entries are, with the same field names, so
the dashboard can treat both vendors alike. A value that could not be read is None.
"""

from __future__ import annotations

import logging
import os
import re
import shlex
import subprocess
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)

AMD_VENDOR_ID = "0x1002"
_CARD = re.compile(r"^card(\d+)$")
_MIB = 1024 * 1024
# Runtime power states in which the kernel will not answer sensor reads and the card
# must be left alone.
_ASLEEP = {"suspended", "suspending"}


def _read(path: Path) -> str | None:
    """A sysfs value, or None: absent, or refused (EBUSY while the card sleeps, EIO)."""
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None


def _read_int(path: Path) -> int | None:
    raw = _read(path)
    try:
        return int(raw) if raw not in (None, "") else None
    except ValueError:
        return None


def _percent(path: Path) -> int | None:
    value = _read_int(path)
    return value if value is not None and 0 <= value <= 100 else None


def _mib(path: Path) -> int | None:
    value = _read_int(path)
    return round(value / _MIB) if value is not None and value >= 0 else None


def lspci_device_names(runner: Callable[..., Any] = subprocess.run) -> dict[str, str]:
    """PCI address -> the PCI database's device name, for every AMD device, in one call.

    Empty when lspci is missing or fails; callers fall back to the vendor and device ids.
    """
    try:
        done = runner(["lspci", "-mm", "-d", "1002:"], capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return {}
    names: dict[str, str] = {}
    for line in (done.stdout or "").splitlines():
        try:
            fields = shlex.split(line)
        except ValueError:
            continue
        # 03:00.0 "VGA compatible controller" "Advanced Micro Devices, Inc. [AMD/ATI]" "Navi 21 [...]" ...
        if len(fields) >= 4:
            address = fields[0] if fields[0].count(":") >= 2 else f"0000:{fields[0]}"
            names[address.lower()] = fields[3]
    return names


def _display_name(product_name: str | None, pci_name: str | None, device_id: str | None) -> str:
    if product_name:
        return product_name
    if pci_name and not pci_name.lower().startswith("device "):
        return f"AMD {pci_name}"
    return f"AMD GPU 1002:{(device_id or 'unknown').removeprefix('0x')}"


def _hwmon_dirs(device: Path) -> list[Path]:
    try:
        return sorted((device / "hwmon").glob("hwmon*"))
    except OSError:
        return []


def _temperatures(device: Path) -> tuple[int | None, int | None]:
    """(edge, junction) in whole degrees Celsius. The kernel reports millidegrees."""
    edge = junction = None
    for hwmon in _hwmon_dirs(device):
        for label_file in sorted(hwmon.glob("temp*_label")):
            index = label_file.name.removeprefix("temp").removesuffix("_label")
            raw = _read_int(hwmon / f"temp{index}_input")
            if raw is None:
                continue
            label = (_read(label_file) or "").lower()
            if label == "edge" and edge is None:
                edge = round(raw / 1000)
            elif label == "junction" and junction is None:
                junction = round(raw / 1000)
        if edge is None:  # a card that names no sensors: the first one is the edge temperature
            raw = _read_int(hwmon / "temp1_input")
            if raw is not None:
                edge = round(raw / 1000)
    return edge, junction


def _power_watts(device: Path) -> float | None:
    """Average (or instantaneous) package power. The kernel reports microwatts."""
    for hwmon in _hwmon_dirs(device):
        for name in ("power1_average", "power1_input"):
            raw = _read_int(hwmon / name)
            if raw is not None and raw >= 0:
                return round(raw / 1_000_000, 1)
    return None


def collect_amd_gpus(
    sys_root: Path = Path("/sys"),
    pci_names: Callable[[], dict[str, str]] = lspci_device_names,
) -> list[dict[str, Any]]:
    """One entry per AMD GPU driven by amdgpu, in card order. Never wakes a sleeping card."""
    drm = sys_root / "class" / "drm"
    try:
        entries = list(drm.iterdir())
    except OSError:
        return []
    cards = sorted((c for c in entries if _CARD.match(c.name)), key=lambda c: int(_CARD.match(c.name).group(1)))  # type: ignore[union-attr]

    found: list[tuple[Path, Path]] = []
    for card in cards:
        device = card / "device"
        if _read(device / "vendor") != AMD_VENDOR_ID:
            continue
        driver = os.path.basename(os.path.realpath(device / "driver"))
        if driver != "amdgpu":  # the legacy radeon driver exposes different files; not covered
            continue
        found.append((card, device))
    if not found:
        return []

    names = pci_names()
    gpus: list[dict[str, Any]] = []
    for position, (_card, device) in enumerate(found):
        address = os.path.basename(os.path.realpath(device))
        asleep = (_read(device / "power" / "runtime_status") or "").lower() in _ASLEEP
        gpu: dict[str, Any] = {
            "index": position,
            "name": _display_name(_read(device / "product_name"), names.get(address.lower()), _read(device / "device")),
            "pci_address": address,
            "power_state": "suspended" if asleep else "active",
            # Memory sizes are readable even while the card sleeps.
            "memory_total_mb": _mib(device / "mem_info_vram_total"),
            "memory_used_mb": _mib(device / "mem_info_vram_used"),
        }
        if asleep:
            gpu.update(utilization_gpu_pct=0, utilization_memory_pct=0, temperature_gpu=None,
                       temperature_junction=None, power_draw_w=None)
        else:
            edge, junction = _temperatures(device)
            gpu.update(
                utilization_gpu_pct=_percent(device / "gpu_busy_percent"),
                utilization_memory_pct=_percent(device / "mem_busy_percent"),
                temperature_gpu=edge,
                temperature_junction=junction,
                power_draw_w=_power_watts(device),
            )
        gpus.append(gpu)
    return gpus
