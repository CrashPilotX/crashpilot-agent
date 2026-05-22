"""
VM and cloud platform telemetry collector.

Gathers hypervisor-specific and cloud-metadata information useful for
diagnosing VM-related crashes:
  - Balloon driver memory pressure
  - Paravirtual disk errors (virtio, vmxnet3)
  - Live migration / vMotion events
  - Cloud instance metadata (type, region, spot interruptions)
  - Hyper-V heartbeat, VMware tools status
  - SCSI/NVMe controller errors in virtual disks
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .base import BaseCollector, cmd_available, run_cmd


class VmCollector(BaseCollector):
    name = "vm"

    def __init__(self, hypervisor: str | None = None, cloud: str | None = None):
        self.hypervisor = hypervisor or ""
        self.cloud = cloud or ""

    async def collect(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "hypervisor": self.hypervisor,
            "cloud": self.cloud,
        }

        if self.cloud == "aws":
            result["aws"] = await self._collect_aws()
        elif self.cloud == "gcp":
            result["gcp"] = await self._collect_gcp()
        elif self.cloud == "azure":
            result["azure"] = await self._collect_azure()
        elif self.cloud == "digitalocean":
            result["digitalocean"] = await self._collect_do()

        if "vmware" in self.hypervisor.lower():
            result["vmware"] = await self._collect_vmware()
        elif "hyperv" in self.hypervisor.lower() or "microsoft" in self.hypervisor.lower():
            result["hyperv"] = await self._collect_hyperv()
        elif "kvm" in self.hypervisor.lower() or "qemu" in self.hypervisor.lower():
            result["kvm"] = await self._collect_kvm()

        result["balloon_driver"] = await self._collect_balloon_driver()
        result["virtio_errors"] = await self._collect_virtio_errors()
        result["vm_crash_hints"] = self._vm_crash_hints()

        return result

    # ── Cloud metadata ────────────────────────────────────────────────────────

    async def _collect_aws(self) -> dict:
        """AWS EC2 instance metadata via IMDSv2."""
        # Get token
        token, _, rc = await run_cmd(
            "curl", "-sf", "--max-time", "2",
            "-X", "PUT",
            "http://169.254.169.254/latest/api/token",
            "-H", "X-aws-ec2-metadata-token-ttl-seconds: 30",
            timeout=5,
        )
        if rc != 0:
            return {"available": False}

        headers = ["-H", f"X-aws-ec2-metadata-token: {token.strip()}"]

        async def imds(path: str) -> str:
            out, _, _ = await run_cmd(
                "curl", "-sf", "--max-time", "2",
                *headers,
                f"http://169.254.169.254/latest/meta-data/{path}",
                timeout=5,
            )
            return out.strip()

        instance_id    = await imds("instance-id")
        instance_type  = await imds("instance-type")
        region         = await imds("placement/region")
        az             = await imds("placement/availability-zone")
        lifecycle      = await imds("instance-life-cycle")  # spot vs on-demand

        # Spot interruption notice (2-minute warning)
        spot_notice, _, _ = await run_cmd(
            "curl", "-sf", "--max-time", "2",
            *headers,
            "http://169.254.169.254/latest/meta-data/spot/instance-action",
            timeout=5,
        )

        return {
            "available": True,
            "instance_id": instance_id,
            "instance_type": instance_type,
            "region": region,
            "availability_zone": az,
            "lifecycle": lifecycle,
            "spot_interruption_notice": spot_notice.strip() if spot_notice.strip() and "404" not in spot_notice else None,
        }

    async def _collect_gcp(self) -> dict:
        headers = ["-H", "Metadata-Flavor: Google"]
        base = "http://metadata.google.internal/computeMetadata/v1/instance"

        async def meta(path: str) -> str:
            out, _, _ = await run_cmd(
                "curl", "-sf", "--max-time", "2",
                *headers,
                f"{base}/{path}",
                timeout=5,
            )
            return out.strip()

        return {
            "available": True,
            "instance_id": await meta("id"),
            "machine_type": (await meta("machine-type")).split("/")[-1],
            "zone": (await meta("zone")).split("/")[-1],
            "preemptible": await meta("scheduling/preemptible"),
        }

    async def _collect_azure(self) -> dict:
        out, _, rc = await run_cmd(
            "curl", "-sf", "--max-time", "3",
            "-H", "Metadata: true",
            "http://169.254.169.254/metadata/instance?api-version=2021-02-01",
            timeout=5,
        )
        if rc != 0:
            return {"available": False}
        try:
            data = json.loads(out)
            compute = data.get("compute", {})
            return {
                "available": True,
                "vm_id": compute.get("vmId"),
                "vm_size": compute.get("vmSize"),
                "location": compute.get("location"),
                "priority": compute.get("priority"),  # Spot vs Regular
                "subscription_id": compute.get("subscriptionId"),
            }
        except json.JSONDecodeError:
            return {"available": True, "raw": out[:500]}

    async def _collect_do(self) -> dict:
        out, _, rc = await run_cmd(
            "curl", "-sf", "--max-time", "2",
            "http://169.254.169.254/metadata/v1.json",
            timeout=5,
        )
        if rc != 0:
            return {"available": False}
        try:
            data = json.loads(out)
            return {
                "available": True,
                "droplet_id": data.get("droplet_id"),
                "hostname": data.get("hostname"),
                "region": data.get("region"),
                "size_slug": data.get("size", {}).get("slug"),
            }
        except json.JSONDecodeError:
            return {"available": False}

    # ── Hypervisor-specific ───────────────────────────────────────────────────

    async def _collect_vmware(self) -> dict:
        info: dict[str, Any] = {}
        if cmd_available("vmware-toolbox-cmd"):
            stat, _, _ = await run_cmd("vmware-toolbox-cmd", "stat", "hosttime", timeout=10)
            info["host_time"] = stat.strip()
            ver, _, _ = await run_cmd("vmware-toolbox-cmd", "version", timeout=5)
            info["tools_version"] = ver.strip()

        # VMware PVSCSI/vmxnet3 errors from dmesg
        dmesg_out, _, _ = await run_cmd(
            "dmesg", "--level=err,warn", timeout=15
        )
        vmware_errs = [line for line in dmesg_out.splitlines()
                       if any(k in line.lower() for k in ["vmxnet", "pvscsi", "vmw_", "vmware"])]
        info["vmware_driver_errors"] = vmware_errs[:20]
        return info

    async def _collect_hyperv(self) -> dict:
        info: dict[str, Any] = {}
        # Hyper-V modules
        mods, _, _ = await run_cmd("lsmod", timeout=5)
        hv_mods = [line.split()[0] for line in mods.splitlines() if line.startswith("hv_")]
        info["loaded_hv_modules"] = hv_mods

        # Hyper-V heartbeat
        heartbeat = Path("/sys/bus/vmbus/devices")
        if heartbeat.exists():
            info["vmbus_devices"] = len(list(heartbeat.iterdir()))

        # KVP (Key-Value Pair) daemon
        kvp_out, _, _ = await run_cmd("systemctl", "is-active", "hv-kvp-daemon", timeout=5)
        info["kvp_daemon_active"] = kvp_out.strip() == "active"

        # dmesg Hyper-V errors
        dmesg_out, _, _ = await run_cmd("dmesg", "--level=err,warn", timeout=15)
        hv_errs = [line for line in dmesg_out.splitlines()
                   if any(k in line.lower() for k in ["hv_", "hyperv", "vmbus", "storvsc", "netvsc"])]
        info["hyperv_driver_errors"] = hv_errs[:20]
        return info

    async def _collect_kvm(self) -> dict:
        info: dict[str, Any] = {}
        # virtio module list
        mods, _, _ = await run_cmd("lsmod", timeout=5)
        virtio_mods = [line.split()[0] for line in mods.splitlines() if line.startswith("virtio")]
        info["virtio_modules"] = virtio_mods

        # QEMU guest agent
        if cmd_available("qemu-ga"):
            ga_out, _, _ = await run_cmd("qemu-ga", "--version", timeout=5)
            info["qemu_ga_version"] = ga_out.strip()

        # KVM-specific dmesg
        dmesg_out, _, _ = await run_cmd("dmesg", "--level=err,warn", timeout=15)
        kvm_errs = [line for line in dmesg_out.splitlines()
                    if any(k in line.lower() for k in ["kvm", "virtio", "qemu"])]
        info["kvm_errors"] = kvm_errs[:20]
        return info

    # ── Generic VM diagnostics ────────────────────────────────────────────────

    async def _collect_balloon_driver(self) -> dict:
        """Memory balloon driver can cause OOM when hypervisor reclaims guest RAM."""
        mods, _, _ = await run_cmd("lsmod", timeout=5)
        balloon_mods = [
            line.split()[0] for line in mods.splitlines()
            if any(k in line for k in ["balloon", "vmmemctl"])
        ]
        # Check if balloon is actively reducing available memory
        meminfo = {}
        try:
            for line in Path("/proc/meminfo").read_text().splitlines():
                k, _, v = line.partition(":")
                if k.strip() in ("MemTotal", "MemAvailable", "MemFree"):
                    meminfo[k.strip()] = int(v.strip().split()[0])
        except OSError:
            pass

        return {
            "balloon_modules": balloon_mods,
            "balloon_active": bool(balloon_mods),
            "mem_total_kb": meminfo.get("MemTotal", 0),
            "mem_available_kb": meminfo.get("MemAvailable", 0),
        }

    async def _collect_virtio_errors(self) -> list[str]:
        dmesg_out, _, _ = await run_cmd(
            "journalctl", "-k", "--no-pager", "--lines=500",
            "--grep=virtio|virt_blk|virt_net|virtio_scsi",
            timeout=20,
        )
        return [line for line in dmesg_out.splitlines()
                if any(k in line.lower() for k in ["error", "timeout", "reset", "failed"])][:30]

    def _vm_crash_hints(self) -> list[str]:
        hints = []
        if self.cloud in ("aws",):
            hints.append("Check for EC2 Spot interruption (2-minute notice in instance metadata)")
            hints.append("Check CloudWatch logs for host health events affecting this instance")
        if self.cloud in ("gcp",):
            hints.append("Check for preemptible VM termination in GCP console")
        if self.cloud in ("azure",):
            hints.append("Check Azure Service Health for maintenance events on this region")
        if "vmware" in self.hypervisor.lower():
            hints.append("Check ESXi host logs for vmkernel errors — vMotion or storage vMotion can cause guest pauses")
            hints.append("VMware balloon driver (vmmemctl) can cause OOM if host is overcommitted")
        if "hyperv" in self.hypervisor.lower() or "microsoft" in self.hypervisor.lower():
            hints.append("Hyper-V Dynamic Memory can reduce guest RAM — check balloon driver events")
            hints.append("Check Hyper-V host event log for Integration Services errors")
        if "kvm" in self.hypervisor.lower():
            hints.append("KVM virtio-balloon can cause OOM under host memory pressure")
            hints.append("Check qemu-kvm logs on host for disk/network timeouts affecting guest")
        return hints
