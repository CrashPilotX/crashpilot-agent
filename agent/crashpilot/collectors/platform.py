"""
Platform detection — identifies the environment CrashPilot is running in.

Supports:
  - Bare-metal Linux (all distros)
  - WSL 1 / WSL 2
  - Docker containers
  - Kubernetes pods / DaemonSet nodes
  - VMs: KVM, VMware, VirtualBox, Hyper-V, Xen, QEMU
  - Cloud VMs: AWS EC2, GCP, Azure, DigitalOcean, Hetzner
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from .base import BaseCollector, run_cmd


class PlatformType(str, Enum):
    BARE_METAL = "bare_metal"
    WSL1       = "wsl1"
    WSL2       = "wsl2"
    DOCKER     = "docker"
    KUBERNETES = "kubernetes"
    VM_KVM     = "vm_kvm"
    VM_VMWARE  = "vm_vmware"
    VM_VBOX    = "vm_virtualbox"
    VM_HYPERV  = "vm_hyperv"
    VM_XEN     = "vm_xen"
    VM_QEMU    = "vm_qemu"
    CLOUD_AWS  = "cloud_aws"
    CLOUD_GCP  = "cloud_gcp"
    CLOUD_AZURE= "cloud_azure"
    CLOUD_DO   = "cloud_digitalocean"
    UNKNOWN    = "unknown"


class Distro(str, Enum):
    UBUNTU   = "ubuntu"
    DEBIAN   = "debian"
    RHEL     = "rhel"
    CENTOS   = "centos"
    FEDORA   = "fedora"
    ROCKY    = "rocky"
    ALMA     = "almalinux"
    ARCH     = "arch"
    OPENSUSE = "opensuse"
    ALPINE   = "alpine"
    VOID     = "void"
    GENTOO   = "gentoo"
    NIXOS    = "nixos"
    UNKNOWN  = "unknown"


class InitSystem(str, Enum):
    SYSTEMD = "systemd"
    OPENRC  = "openrc"
    RUNIT   = "runit"
    SYSV    = "sysv"
    UPSTART = "upstart"
    NONE    = "none"


@dataclass
class PlatformInfo:
    platform:    PlatformType
    distro:      Distro
    distro_version: str
    init:        InitSystem
    kernel:      str
    arch:        str
    hostname:    str
    is_container: bool
    is_virtual:  bool
    is_cloud:    bool
    hypervisor:  str | None
    cloud_provider: str | None
    wsl_version: int | None          # 1 or 2
    k8s_node:    str | None
    k8s_namespace: str | None
    container_id: str | None
    extra: dict[str, Any] = field(default_factory=dict)


async def detect_platform() -> PlatformInfo:
    """Probe the environment and return a PlatformInfo."""
    kernel   = await _kernel_version()
    arch_out, _, _ = await run_cmd("uname", "-m")
    hostname, _, _ = await run_cmd("hostname", "-f")
    hostname = hostname.strip() or "unknown"
    arch     = arch_out.strip()

    distro, distro_ver = await _detect_distro()
    init = await _detect_init()

    # Layer checks — ordered from most-specific to least
    wsl = _detect_wsl(kernel)
    if wsl:
        platform = PlatformType.WSL1 if wsl == 1 else PlatformType.WSL2
        return PlatformInfo(
            platform=platform, distro=distro, distro_version=distro_ver,
            init=init, kernel=kernel, arch=arch, hostname=hostname,
            is_container=False, is_virtual=True, is_cloud=False,
            hypervisor="WSL", cloud_provider=None,
            wsl_version=wsl, k8s_node=None, k8s_namespace=None,
            container_id=None,
        )

    k8s_node, k8s_ns = _detect_kubernetes()
    if k8s_node or k8s_ns:
        cid = _detect_container_id()
        return PlatformInfo(
            platform=PlatformType.KUBERNETES, distro=distro, distro_version=distro_ver,
            init=InitSystem.NONE, kernel=kernel, arch=arch, hostname=hostname,
            is_container=True, is_virtual=True, is_cloud=False,
            hypervisor=None, cloud_provider=None,
            wsl_version=None, k8s_node=k8s_node, k8s_namespace=k8s_ns,
            container_id=cid,
        )

    if _detect_docker():
        cid = _detect_container_id()
        return PlatformInfo(
            platform=PlatformType.DOCKER, distro=distro, distro_version=distro_ver,
            init=InitSystem.NONE, kernel=kernel, arch=arch, hostname=hostname,
            is_container=True, is_virtual=True, is_cloud=False,
            hypervisor="docker", cloud_provider=None,
            wsl_version=None, k8s_node=None, k8s_namespace=None,
            container_id=cid,
        )

    hypervisor = await _detect_hypervisor()
    cloud = await _detect_cloud()

    platform = _map_hypervisor_to_platform(hypervisor, cloud)
    return PlatformInfo(
        platform=platform, distro=distro, distro_version=distro_ver,
        init=init, kernel=kernel, arch=arch, hostname=hostname,
        is_container=False,
        is_virtual=(hypervisor is not None),
        is_cloud=(cloud is not None),
        hypervisor=hypervisor, cloud_provider=cloud,
        wsl_version=None, k8s_node=None, k8s_namespace=None,
        container_id=None,
    )


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _kernel_version() -> str:
    out, _, _ = await run_cmd("uname", "-r")
    return out.strip()


async def _detect_distro() -> tuple[Distro, str]:
    # Prefer /etc/os-release
    try:
        text = Path("/etc/os-release").read_text()
        fields = dict(
            line.split("=", 1)
            for line in text.splitlines()
            if "=" in line
        )
        name_raw = fields.get("ID", "").strip('"').lower()
        version = fields.get("VERSION_ID", "").strip('"')

        mapping = {
            "ubuntu": Distro.UBUNTU,
            "debian": Distro.DEBIAN,
            "rhel":   Distro.RHEL,
            "centos": Distro.CENTOS,
            "fedora": Distro.FEDORA,
            "rocky":  Distro.ROCKY,
            "almalinux": Distro.ALMA,
            "arch":   Distro.ARCH,
            "opensuse": Distro.OPENSUSE,
            "opensuse-leap": Distro.OPENSUSE,
            "opensuse-tumbleweed": Distro.OPENSUSE,
            "alpine": Distro.ALPINE,
            "void":   Distro.VOID,
            "gentoo": Distro.GENTOO,
            "nixos":  Distro.NIXOS,
        }
        return mapping.get(name_raw, Distro.UNKNOWN), version
    except OSError:
        return Distro.UNKNOWN, ""


async def _detect_init() -> InitSystem:
    # PID 1 name
    try:
        comm = Path("/proc/1/comm").read_text().strip()
        if comm == "systemd":
            return InitSystem.SYSTEMD
        if comm in ("runit", "runsvdir"):
            return InitSystem.RUNIT
        if comm in ("openrc", "openrc-init"):
            return InitSystem.OPENRC
        if comm == "init":
            # Could be SysV or upstart
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
        if "wsl2" in osrelease or ("microsoft" in osrelease and "wsl2" in osrelease):
            return 2
        if "microsoft" in osrelease or "wsl" in osrelease:
            return 1
    except OSError:
        pass
    if "microsoft" in kernel.lower() or "wsl" in kernel.lower():
        return 2 if "wsl2" in kernel.lower() else 1
    return None


def _detect_kubernetes() -> tuple[str | None, str | None]:
    """Detect if running inside a Kubernetes pod."""
    svc_host = os.environ.get("KUBERNETES_SERVICE_HOST")
    if svc_host:
        ns_path = Path("/var/run/secrets/kubernetes.io/serviceaccount/namespace")
        node_path = Path("/etc/hostname")
        ns = ns_path.read_text().strip() if ns_path.exists() else None
        node = os.environ.get("NODE_NAME") or (node_path.read_text().strip() if node_path.exists() else None)
        return node, ns
    # Check for service account token even without env var
    if Path("/var/run/secrets/kubernetes.io/serviceaccount").exists():
        ns_path = Path("/var/run/secrets/kubernetes.io/serviceaccount/namespace")
        ns = ns_path.read_text().strip() if ns_path.exists() else None
        return os.environ.get("NODE_NAME"), ns
    return None, None


def _detect_docker() -> bool:
    """Return True if running inside a Docker container."""
    if Path("/.dockerenv").exists():
        return True
    try:
        cgroup = Path("/proc/1/cgroup").read_text()
        return "docker" in cgroup or "containerd" in cgroup
    except OSError:
        return False


def _detect_container_id() -> str | None:
    try:
        for line in Path("/proc/1/cgroup").read_text().splitlines():
            m = re.search(r"[0-9a-f]{64}", line)
            if m:
                return m.group(0)[:12]
    except OSError:
        pass
    return None


async def _detect_hypervisor() -> str | None:
    # systemd-detect-virt is the most reliable
    virt, _, rc = await run_cmd("systemd-detect-virt", "--vm", timeout=5)
    if rc == 0 and virt.strip() not in ("none", ""):
        return virt.strip()

    # virt-what (fallback)
    vw, _, rc2 = await run_cmd("virt-what", timeout=10)
    if rc2 == 0 and vw.strip():
        return vw.strip().splitlines()[0]

    # DMI product name
    try:
        product = Path("/sys/class/dmi/id/product_name").read_text().strip().lower()
        if "vmware" in product:
            return "vmware"
        if "virtualbox" in product:
            return "virtualbox"
        if "kvm" in product or "rhev" in product:
            return "kvm"
    except OSError:
        pass

    # /proc/cpuinfo hypervisor flag
    try:
        cpuinfo = Path("/proc/cpuinfo").read_text()
        if "hypervisor" in cpuinfo:
            return "unknown_vm"
    except OSError:
        pass

    return None


async def _detect_cloud() -> str | None:
    """Try IMDS endpoints to identify the cloud provider."""
    # AWS — IMDSv2
    tok, _, rc = await run_cmd(
        "curl", "-sf", "--max-time", "1",
        "-X", "PUT",
        "http://169.254.169.254/latest/api/token",
        "-H", "X-aws-ec2-metadata-token-ttl-seconds: 10",
        timeout=3,
    )
    if rc == 0 and tok.strip():
        return "aws"

    # GCP
    gcp, _, rc = await run_cmd(
        "curl", "-sf", "--max-time", "1",
        "-H", "Metadata-Flavor: Google",
        "http://metadata.google.internal/computeMetadata/v1/instance/id",
        timeout=3,
    )
    if rc == 0 and gcp.strip():
        return "gcp"

    # Azure
    az, _, rc = await run_cmd(
        "curl", "-sf", "--max-time", "1",
        "-H", "Metadata: true",
        "http://169.254.169.254/metadata/instance?api-version=2021-02-01",
        timeout=3,
    )
    if rc == 0 and "compute" in az:
        return "azure"

    # DigitalOcean
    do, _, rc = await run_cmd(
        "curl", "-sf", "--max-time", "1",
        "http://169.254.169.254/metadata/v1/id",
        timeout=3,
    )
    if rc == 0 and do.strip().isdigit():
        return "digitalocean"

    return None


def _map_hypervisor_to_platform(hypervisor: str | None, cloud: str | None) -> PlatformType:
    if cloud == "aws":
        return PlatformType.CLOUD_AWS
    if cloud == "gcp":
        return PlatformType.CLOUD_GCP
    if cloud == "azure":
        return PlatformType.CLOUD_AZURE
    if cloud == "digitalocean":
        return PlatformType.CLOUD_DO
    if hypervisor is None:
        return PlatformType.BARE_METAL
    hv = hypervisor.lower()
    if "kvm" in hv or "qemu" in hv:
        return PlatformType.VM_KVM
    if "vmware" in hv:
        return PlatformType.VM_VMWARE
    if "virtualbox" in hv or "vbox" in hv:
        return PlatformType.VM_VBOX
    if "microsoft" in hv or "hyper-v" in hv or "hyperv" in hv:
        return PlatformType.VM_HYPERV
    if "xen" in hv:
        return PlatformType.VM_XEN
    return PlatformType.UNKNOWN


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
            "is_container": info.is_container,
            "is_virtual": info.is_virtual,
            "is_cloud": info.is_cloud,
            "hypervisor": info.hypervisor,
            "cloud_provider": info.cloud_provider,
            "wsl_version": info.wsl_version,
            "k8s_node": info.k8s_node,
            "k8s_namespace": info.k8s_namespace,
            "container_id": info.container_id,
        }
