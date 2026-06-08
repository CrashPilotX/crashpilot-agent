"""
Platform detection for the current CrashPilot scope.

Supported for now:
  - Ubuntu Linux
  - Ubuntu on WSL 1 / WSL 2
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from .base import BaseCollector, run_cmd


class PlatformType(str, Enum):
    BARE_METAL = "bare_metal"
    WSL1 = "wsl1"
    WSL2 = "wsl2"
    UNKNOWN = "unknown"


class Distro(str, Enum):
    UBUNTU = "ubuntu"
    UNKNOWN = "unknown"


class InitSystem(str, Enum):
    SYSTEMD = "systemd"
    SYSV = "sysv"
    UPSTART = "upstart"
    NONE = "none"


@dataclass
class PlatformInfo:
    platform: PlatformType
    distro: Distro
    distro_version: str
    init: InitSystem
    kernel: str
    arch: str
    hostname: str
    is_virtual: bool
    hypervisor: str | None
    wsl_version: int | None
    supported: bool
    support_note: str
    extra: dict[str, Any] = field(default_factory=dict)


async def detect_platform() -> PlatformInfo:
    """Probe the environment and return a PlatformInfo."""
    kernel = await _kernel_version()
    arch_out, _, _ = await run_cmd("uname", "-m")
    hostname, _, _ = await run_cmd("hostname", "-f")
    hostname = hostname.strip() or "unknown"
    arch = arch_out.strip()

    distro, distro_ver = await _detect_distro()
    init = await _detect_init()
    wsl = _detect_wsl(kernel)

    if wsl:
        platform = PlatformType.WSL1 if wsl == 1 else PlatformType.WSL2
        supported = distro == Distro.UBUNTU
        return PlatformInfo(
            platform=platform,
            distro=distro,
            distro_version=distro_ver,
            init=init,
            kernel=kernel,
            arch=arch,
            hostname=hostname,
            is_virtual=True,
            hypervisor="WSL",
            wsl_version=wsl,
            supported=supported,
            support_note=(
                "Supported Ubuntu WSL environment"
                if supported
                else "Unsupported WSL distro; CrashPilot currently supports Ubuntu on WSL only"
            ),
        )

    supported = distro == Distro.UBUNTU
    return PlatformInfo(
        platform=PlatformType.BARE_METAL if supported else PlatformType.UNKNOWN,
        distro=distro,
        distro_version=distro_ver,
        init=init,
        kernel=kernel,
        arch=arch,
        hostname=hostname,
        is_virtual=False,
        hypervisor=None,
        wsl_version=None,
        supported=supported,
        support_note=(
            "Supported Ubuntu Linux environment"
            if supported
            else "Unsupported Linux distro; CrashPilot currently supports Ubuntu only"
        ),
    )


async def _kernel_version() -> str:
    out, _, _ = await run_cmd("uname", "-r")
    return out.strip()


async def _detect_distro() -> tuple[Distro, str]:
    try:
        text = Path("/etc/os-release").read_text()
        fields = dict(
            line.split("=", 1)
            for line in text.splitlines()
            if "=" in line
        )
        name_raw = fields.get("ID", "").strip('"').lower()
        version = fields.get("VERSION_ID", "").strip('"')
        return (Distro.UBUNTU if name_raw == "ubuntu" else Distro.UNKNOWN), version
    except OSError:
        return Distro.UNKNOWN, ""


async def _detect_init() -> InitSystem:
    try:
        comm = Path("/proc/1/comm").read_text().strip()
        if comm == "systemd":
            return InitSystem.SYSTEMD
        if comm == "init":
            out, _, _ = await run_cmd("/sbin/init", "--version")
            if "upstart" in out.lower():
                return InitSystem.UPSTART
            return InitSystem.SYSV
    except OSError:
        pass
    return InitSystem.NONE


def _detect_wsl(kernel: str) -> int | None:
    """Return 1 or 2 if running under WSL, else None."""
    try:
        osrelease = Path("/proc/sys/kernel/osrelease").read_text().lower()
        if "wsl2" in osrelease:
            return 2
        if "microsoft" in osrelease or "wsl" in osrelease:
            return 1
    except OSError:
        pass
    lower_kernel = kernel.lower()
    if "microsoft" in lower_kernel or "wsl" in lower_kernel:
        return 2 if "wsl2" in lower_kernel else 1
    return None


class PlatformCollector(BaseCollector):
    name = "platform"

    async def collect(self) -> dict[str, Any]:
        info = await detect_platform()
        return {
            "platform": info.platform.value,
            "distro": info.distro.value,
            "distro_version": info.distro_version,
            "init_system": info.init.value,
            "kernel": info.kernel,
            "arch": info.arch,
            "hostname": info.hostname,
            "is_virtual": info.is_virtual,
            "hypervisor": info.hypervisor,
            "wsl_version": info.wsl_version,
            "supported": info.supported,
            "support_note": info.support_note,
        }
