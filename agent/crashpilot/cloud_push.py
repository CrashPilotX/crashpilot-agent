"""
Cloud push — sends heartbeats and crash reports directly to Supabase.

Used in push mode (no public URL required). The agent authenticates using
the system_id + supabase_token pair, validated server-side by SECURITY DEFINER
RPCs in Postgres (so the anon key is safe to use here).
"""

from __future__ import annotations

import importlib.metadata
import json
import logging
import os
import re
import shutil
import socket
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger(__name__)

_TIMEOUT = httpx.Timeout(10.0)
_LIVE_DMESG_TTL_SECONDS = 300
_LIVE_DMESG_TAIL_CHARS = 8000
_LIVE_DMESG_MAX_CRITICAL = 80
_LIVE_DMESG_PATTERN = re.compile(
    "|".join([
        r"Kernel panic",
        r"BUG:",
        r"OOPS:",
        r"general protection fault",
        r"Hardware Error",
        r"MCE:",
        r"NMI:",
        r"watchdog: BUG",
        r"hung_task",
        r"soft lockup",
        r"hard LOCKUP",
        r"RCU stall",
        r"unable to handle kernel",
        r"page fault",
        r"I/O error",
        r"EXT4-fs error",
        r"BTRFS.*error",
        r"xfs.*error",
        r"ata\d+.*error",
        r"SCSI.*error",
        r"nvme.*error",
        r"GPU fault",
        r"NVRM:",
        r"amdgpu.*ERROR",
        r"PCIe.*error",
        r"AER:",
        r"thermal throttling",
        r"temperature.*critical",
    ]),
    re.IGNORECASE,
)


def _headers(anon_key: str) -> dict[str, str]:
    return {
        "apikey": anon_key,
        "Authorization": f"Bearer {anon_key}",
        "Content-Type": "application/json",
    }


def _agent_version() -> str:
    try:
        return importlib.metadata.version("crashpilot")
    except Exception:
        return "0.2.0"


def _hostname() -> str | None:
    try:
        return socket.gethostname()
    except Exception:
        return None


def _read_meminfo() -> dict[str, int]:
    parsed: dict[str, int] = {}
    try:
        with open("/proc/meminfo", encoding="utf-8") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2:
                    parsed[parts[0].rstrip(":")] = int(parts[1])
    except OSError:
        return {}
    return parsed


def _filesystem_entry(
    filesystem: str,
    mountpoint: str,
    total_bytes: int,
    used_bytes: int,
    free_bytes: int,
) -> dict[str, Any]:
    total_gb = round(total_bytes / 1024 / 1024 / 1024, 2)
    used_gb = round(used_bytes / 1024 / 1024 / 1024, 2)
    free_gb = round(free_bytes / 1024 / 1024 / 1024, 2)
    used_pct = round((used_bytes / total_bytes) * 100, 1) if total_bytes else 0
    return {
        "filesystem": filesystem,
        "mountpoint": mountpoint,
        "total_gb": total_gb,
        "used_gb": used_gb,
        "free_gb": free_gb,
        "used_pct": used_pct,
    }


def _collect_disk_usage() -> dict[str, Any]:
    """Collect disk pressure for root and useful local filesystems."""
    try:
        root_usage = shutil.disk_usage("/")
    except OSError:
        return {}

    root = _filesystem_entry(
        "/",
        "/",
        root_usage.total,
        root_usage.used,
        root_usage.free,
    )
    filesystems: list[dict[str, Any]] = []

    try:
        result = subprocess.run(
            ["df", "-P", "-B1", "-x", "tmpfs", "-x", "devtmpfs", "-x", "squashfs"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        result = None

    if result and result.returncode == 0:
        for line in result.stdout.splitlines()[1:]:
            parts = line.split()
            if len(parts) < 6:
                continue
            filesystem, total, used, free = parts[0], parts[1], parts[2], parts[3]
            mountpoint = parts[-1]
            try:
                filesystems.append(
                    _filesystem_entry(
                        filesystem,
                        mountpoint,
                        int(total),
                        int(used),
                        int(free),
                    )
                )
            except ValueError:
                continue

    if not filesystems:
        filesystems = [root]

    most_used = max(filesystems, key=lambda item: item.get("used_pct", 0))
    lowest_free = min(filesystems, key=lambda item: item.get("free_gb", float("inf")))
    return {
        "primary": root,
        "root": root,
        "filesystems": filesystems[:12],
        "most_used": most_used,
        "lowest_free": lowest_free,
    }


_NETWORK_INTERFACE_PREFIXES_TO_SKIP = (
    "br-",
    "cni",
    "docker",
    "flannel",
    "kube-ipvs",
    "lo",
    "veth",
    "virbr",
)


def _speedtest_cache_path() -> Path | None:
    try:
        from .config import get_settings

        data_dir = get_settings().data_dir
        if data_dir is None:
            return None
        data_dir.mkdir(parents=True, exist_ok=True)
        return data_dir / "speedtest_result.json"
    except Exception:
        return None


def _load_cached_speedtest(path: Path | None, max_age_seconds: int) -> dict[str, Any] | None:
    if path is None:
        return None
    try:
        cache = json.loads(path.read_text(encoding="utf-8"))
        saved_at = float(cache.get("saved_at", 0))
        result = cache.get("result")
        if isinstance(result, dict) and time.time() - saved_at <= max_age_seconds:
            cached = dict(result)
            cached["cached"] = True
            cached["age_seconds"] = round(time.time() - saved_at, 1)
            return cached
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None
    return None


def _collect_speedtest_capacity() -> dict[str, Any] | None:
    """Optionally run speedtest-cli and return internet capacity in Mbps."""
    try:
        from .config import get_settings

        settings = get_settings()
    except Exception as exc:
        return {"enabled": True, "available": False, "error": str(exc)}

    if not getattr(settings, "bandwidth_speedtest_enabled", False):
        return None

    interval = max(int(getattr(settings, "bandwidth_speedtest_interval_seconds", 21600)), 300)
    timeout = max(int(getattr(settings, "bandwidth_speedtest_timeout_seconds", 90)), 10)
    cache_path = _speedtest_cache_path()
    cached = _load_cached_speedtest(cache_path, interval)
    if cached:
        return cached

    if not shutil.which("speedtest-cli"):
        return {
            "enabled": True,
            "available": False,
            "cached": False,
            "error": "speedtest-cli is not installed",
        }

    try:
        completed = subprocess.run(
            ["speedtest-cli", "--json", "--secure"],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"enabled": True, "available": False, "cached": False, "error": str(exc)}

    if completed.returncode != 0:
        error = (completed.stderr or completed.stdout or "speedtest-cli failed").strip()
        return {"enabled": True, "available": False, "cached": False, "error": error[:500]}

    try:
        payload = json.loads(completed.stdout)
        download_bps = float(payload.get("download", 0) or 0)
        upload_bps = float(payload.get("upload", 0) or 0)
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        return {"enabled": True, "available": False, "cached": False, "error": f"invalid speedtest-cli JSON: {exc}"}

    server = payload.get("server") if isinstance(payload.get("server"), dict) else {}
    result: dict[str, Any] = {
        "enabled": True,
        "available": True,
        "cached": False,
        "tested_at": datetime.now(timezone.utc).isoformat(),
        "download_mbps": round(download_bps / 1_000_000, 2),
        "upload_mbps": round(upload_bps / 1_000_000, 2),
    }
    ping = payload.get("ping")
    if isinstance(ping, (int, float)):
        result["ping_ms"] = round(float(ping), 1)
    if server:
        result["server"] = {
            key: server.get(key)
            for key in ("sponsor", "name", "country", "host")
            if server.get(key)
        }

    if cache_path is not None:
        try:
            cache_path.write_text(
                json.dumps({"saved_at": time.time(), "result": result}),
                encoding="utf-8",
            )
        except OSError:
            pass

    return result


def _network_counters_cache_path() -> Path | None:
    try:
        from .config import get_settings

        data_dir = get_settings().data_dir
        if data_dir is None:
            return None
        data_dir.mkdir(parents=True, exist_ok=True)
        return data_dir / "network_counters.json"
    except Exception:
        return None


def _read_network_counters(proc_net_dev: Path = Path("/proc/net/dev")) -> dict[str, Any]:
    """Read /proc/net/dev and aggregate physical/default network interfaces."""
    interfaces: list[dict[str, Any]] = []
    try:
        lines = proc_net_dev.read_text(encoding="utf-8").splitlines()[2:]
    except OSError:
        return {}

    for line in lines:
        if ":" not in line:
            continue
        raw_name, raw_values = line.split(":", 1)
        name = raw_name.strip()
        if not name or name.startswith(_NETWORK_INTERFACE_PREFIXES_TO_SKIP):
            continue
        parts = raw_values.split()
        if len(parts) < 16:
            continue
        try:
            rx_bytes = int(parts[0])
            rx_packets = int(parts[1])
            tx_bytes = int(parts[8])
            tx_packets = int(parts[9])
        except ValueError:
            continue
        if rx_bytes == 0 and tx_bytes == 0:
            continue
        interfaces.append({
            "name": name,
            "rx_bytes": rx_bytes,
            "tx_bytes": tx_bytes,
            "rx_packets": rx_packets,
            "tx_packets": tx_packets,
        })

    if not interfaces:
        return {}

    return {
        "interfaces": interfaces[:12],
        "rx_bytes": sum(item["rx_bytes"] for item in interfaces),
        "tx_bytes": sum(item["tx_bytes"] for item in interfaces),
    }


def _collect_network_usage() -> dict[str, Any]:
    """Collect current network byte counters and RX/TX throughput since last heartbeat."""
    counters = _read_network_counters()
    if not counters:
        return {}

    now = time.time()
    cache_path = _network_counters_cache_path()
    previous: dict[str, Any] | None = None
    if cache_path is not None:
        try:
            previous = json.loads(cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            previous = None

    counters["sampled_at"] = datetime.now(timezone.utc).isoformat()
    counters["rate_interval_seconds"] = None
    counters["rx_bytes_per_second"] = None
    counters["tx_bytes_per_second"] = None
    counters["rx_mbps"] = None
    counters["tx_mbps"] = None

    if previous:
        try:
            elapsed = now - float(previous.get("saved_at", 0) or 0)
            prev_rx = int(previous.get("rx_bytes", 0) or 0)
            prev_tx = int(previous.get("tx_bytes", 0) or 0)
        except (TypeError, ValueError):
            elapsed = 0
            prev_rx = counters["rx_bytes"]
            prev_tx = counters["tx_bytes"]
        rx_delta = counters["rx_bytes"] - prev_rx
        tx_delta = counters["tx_bytes"] - prev_tx
        # Interface counters can reset on reboot/link flap; only compute sane positive rates.
        if elapsed > 0 and rx_delta >= 0 and tx_delta >= 0:
            rx_bps = rx_delta / elapsed
            tx_bps = tx_delta / elapsed
            counters["rate_interval_seconds"] = round(elapsed, 1)
            counters["rx_bytes_per_second"] = round(rx_bps, 1)
            counters["tx_bytes_per_second"] = round(tx_bps, 1)
            counters["rx_mbps"] = round((rx_bps * 8) / 1_000_000, 2)
            counters["tx_mbps"] = round((tx_bps * 8) / 1_000_000, 2)

    if cache_path is not None:
        try:
            cache_path.write_text(
                json.dumps({
                    "saved_at": now,
                    "rx_bytes": counters["rx_bytes"],
                    "tx_bytes": counters["tx_bytes"],
                }),
                encoding="utf-8",
            )
        except OSError:
            pass

    speedtest = _collect_speedtest_capacity()
    if speedtest:
        counters["speedtest"] = speedtest

    return counters


def _live_dmesg_cache_path() -> Path | None:
    try:
        from .config import get_settings

        data_dir = get_settings().data_dir
        if data_dir is None:
            return None
        data_dir.mkdir(parents=True, exist_ok=True)
        return data_dir / "live_dmesg.json"
    except Exception:
        return None


def _load_live_dmesg_cache(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    try:
        cache = json.loads(path.read_text(encoding="utf-8"))
        saved_at = float(cache.get("saved_at", 0))
        if time.time() - saved_at <= _LIVE_DMESG_TTL_SECONDS:
            return cache.get("dmesg")
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None
    return None


def _save_live_dmesg_cache(path: Path | None, dmesg: dict[str, Any]) -> None:
    if path is None:
        return
    try:
        path.write_text(
            json.dumps({"saved_at": time.time(), "dmesg": dmesg}),
            encoding="utf-8",
        )
    except OSError:
        return


def _collect_live_dmesg() -> dict[str, Any]:
    """Collect a small, throttled dmesg snapshot for the dashboard."""
    cache_path = _live_dmesg_cache_path()
    cached = _load_live_dmesg_cache(cache_path)
    if cached is not None:
        return cached

    output = ""
    for args in (
        ["dmesg", "-T", "--level=emerg,alert,crit,err,warn"],
        ["dmesg", "--level=emerg,alert,crit,err,warn"],
    ):
        try:
            result = subprocess.run(
                args,
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if result.returncode == 0 and result.stdout.strip():
            output = result.stdout
            break

    lines = [line for line in output.splitlines() if line.strip()]
    critical = [line for line in lines if _LIVE_DMESG_PATTERN.search(line)]
    dmesg = {
        "collected_at": datetime.now(timezone.utc).isoformat(),
        "tail": "\n".join(lines)[-_LIVE_DMESG_TAIL_CHARS:],
        "critical_events": critical[:_LIVE_DMESG_MAX_CRITICAL],
        "critical_count": len(critical),
        "refresh_interval_seconds": _LIVE_DMESG_TTL_SECONDS,
    }
    _save_live_dmesg_cache(cache_path, dmesg)
    return dmesg


def _systemctl_state(unit: str, verb: str) -> str | None:
    if not shutil.which("systemctl"):
        return None
    try:
        result = subprocess.run(
            ["systemctl", verb, unit],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return (result.stdout or result.stderr).strip() or None




def _collect_cpu_hardware() -> dict[str, Any]:
    hardware: dict[str, Any] = {}
    try:
        cpuinfo = Path("/proc/cpuinfo").read_text(encoding="utf-8", errors="replace")
        first: dict[str, str] = {}
        for line in cpuinfo.splitlines():
            if not line.strip():
                if first:
                    break
                continue
            if ":" in line:
                key, value = line.split(":", 1)
                first[key.strip()] = value.strip()
        flags = first.get("flags", "").split()
        instruction_flags = [
            flag for flag in flags
            if flag in {
                "sse", "sse2", "sse3", "ssse3", "sse4_1", "sse4_2",
                "avx", "avx2", "avx512f", "avx512dq", "avx512cd", "avx512bw", "avx512vl",
                "aes", "fma", "f16c", "sha_ni", "amx_tile", "amx_int8", "amx_bf16",
            }
        ]
        hardware.update({
            "model_name": first.get("model name") or first.get("Hardware"),
            "vendor_id": first.get("vendor_id"),
            "cpu_family": first.get("cpu family"),
            "model": first.get("model"),
            "stepping": first.get("stepping"),
            "microcode": first.get("microcode"),
            "flags": flags,
            "instruction_flags": instruction_flags,
        })
    except OSError:
        pass

    if shutil.which("lscpu"):
        try:
            result = subprocess.run(["lscpu", "-J"], check=False, capture_output=True, text=True, timeout=5)
            if result.returncode == 0:
                payload = json.loads(result.stdout)
                fields = {
                    item.get("field", "").rstrip(":"): item.get("data")
                    for item in payload.get("lscpu", [])
                    if item.get("field")
                }
                hardware.update({
                    "architecture": fields.get("Architecture"),
                    "threads_per_core": _safe_int(fields.get("Thread(s) per core")),
                    "cores_per_socket": _safe_int(fields.get("Core(s) per socket")),
                    "sockets": _safe_int(fields.get("Socket(s)")),
                    "cpu_max_mhz": _safe_float(fields.get("CPU max MHz")),
                    "cpu_min_mhz": _safe_float(fields.get("CPU min MHz")),
                    "virtualization": fields.get("Virtualization"),
                    "cache_l1d": fields.get("L1d cache"),
                    "cache_l1i": fields.get("L1i cache"),
                    "cache_l2": fields.get("L2 cache"),
                    "cache_l3": fields.get("L3 cache"),
                })
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
            pass
    return {key: value for key, value in hardware.items() if value not in (None, "")}


def _safe_int(value: Any) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _safe_float(value: Any) -> float | None:
    try:
        return round(float(str(value).strip()), 1)
    except (TypeError, ValueError):
        return None


def _collect_memory_hardware() -> dict[str, Any]:
    if not shutil.which("dmidecode"):
        return {"available": False, "error": "dmidecode not installed"}
    try:
        result = subprocess.run(["dmidecode", "--type", "memory"], check=False, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as exc:
        return {"available": False, "error": str(exc)}
    if result.returncode != 0:
        return {"available": False, "error": (result.stderr or "dmidecode failed").strip()[:300]}

    modules: list[dict[str, Any]] = []
    current: dict[str, str] = {}
    in_device = False
    for raw in result.stdout.splitlines():
        line = raw.rstrip()
        if line.startswith("Memory Device"):
            if current:
                modules.append(current)
            current = {}
            in_device = True
            continue
        if not in_device or ":" not in line:
            continue
        key, value = line.strip().split(":", 1)
        current[key.strip()] = value.strip()
    if current:
        modules.append(current)

    parsed = []
    for module in modules:
        size = module.get("Size", "")
        if not size or size in {"No Module Installed", "Unknown"}:
            continue
        parsed.append({
            "locator": module.get("Locator"),
            "bank_locator": module.get("Bank Locator"),
            "size": size,
            "type": module.get("Type"),
            "speed": module.get("Speed"),
            "configured_speed": module.get("Configured Memory Speed"),
            "manufacturer": module.get("Manufacturer"),
            "part_number": module.get("Part Number"),
            "serial_number": module.get("Serial Number"),
            "form_factor": module.get("Form Factor"),
        })
    return {"available": True, "modules": parsed[:32], "module_count": len(parsed)}


def _collect_block_devices() -> list[dict[str, Any]]:
    if not shutil.which("lsblk"):
        return []
    try:
        result = subprocess.run(
            ["lsblk", "-J", "-b", "-o", "NAME,TYPE,SIZE,MODEL,SERIAL,TRAN,MOUNTPOINT,FSTYPE"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if result.returncode != 0:
        return []
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return []

    devices: list[dict[str, Any]] = []
    def walk(items: list[dict[str, Any]], parent: str | None = None) -> None:
        for item in items:
            entry = {
                "name": item.get("name"),
                "type": item.get("type"),
                "size_gb": round((float(item.get("size") or 0) / 1024 / 1024 / 1024), 2),
                "model": item.get("model"),
                "serial": item.get("serial"),
                "transport": item.get("tran"),
                "mountpoint": item.get("mountpoint"),
                "fstype": item.get("fstype"),
                "parent": parent,
            }
            devices.append({key: value for key, value in entry.items() if value not in (None, "")})
            children = item.get("children") or []
            if isinstance(children, list):
                walk(children, item.get("name"))
    walk(payload.get("blockdevices", []))
    return devices[:80]
def _build_agent_health(dmesg: dict[str, Any]) -> dict[str, Any]:
    """Small diagnostic payload shown in the dashboard health panel."""
    health: dict[str, Any] = {
        "collected_at": datetime.now(timezone.utc).isoformat(),
        "version": _agent_version(),
        "hostname": _hostname(),
        "timer": {
            "active": _systemctl_state("crashpilot-heartbeat.timer", "is-active"),
            "enabled": _systemctl_state("crashpilot-heartbeat.timer", "is-enabled"),
        },
        "update_timer": {
            "active": _systemctl_state("crashpilot-update.timer", "is-active"),
            "enabled": _systemctl_state("crashpilot-update.timer", "is-enabled"),
        },
        "snapshot_timer": {
            "active": _systemctl_state("crashpilot-snapshot.timer", "is-active"),
            "enabled": _systemctl_state("crashpilot-snapshot.timer", "is-enabled"),
        },
        "tools": {
            "systemctl": bool(shutil.which("systemctl")),
            "dmesg": bool(shutil.which("dmesg")),
            "nvidia_smi": bool(shutil.which("nvidia-smi")),
        },
        "dmesg": {
            "readable": bool(dmesg.get("tail") or dmesg.get("critical_events")),
            "critical_count": dmesg.get("critical_count", 0),
            "refresh_interval_seconds": dmesg.get("refresh_interval_seconds"),
        },
    }
    try:
        from .config import get_settings

        settings = get_settings()
        health["push_config"] = {
            "configured": bool(
                settings.supabase_url
                and settings.supabase_system_id
                and settings.supabase_token
            ),
            "system_id": settings.supabase_system_id,
        }
        health["data_dir"] = str(settings.data_dir) if settings.data_dir else None
        from .storage.store import get_meta, webhook_delivery_status

        health["maintenance_until"] = (
            get_meta("maintenance_until") or settings.maintenance_until or None
        )
        health["webhooks"] = webhook_delivery_status()
    except Exception as exc:
        health["config_error"] = str(exc)
    return health


def _build_live_metrics() -> dict[str, Any]:
    """Small heartbeat payload for near-live resource load in the dashboard."""
    metrics: dict[str, Any] = {}

    cpu_count = os.cpu_count() or 1
    try:
        load_1m, load_5m, load_15m = os.getloadavg()
        metrics["cpu"] = {
            "cpu_count": cpu_count,
            "load_1m": round(load_1m, 2),
            "load_5m": round(load_5m, 2),
            "load_15m": round(load_15m, 2),
            "load_pct": min(round((load_1m / cpu_count) * 100, 1), 999.0),
            "hardware": _collect_cpu_hardware(),
        }
    except OSError:
        metrics["cpu"] = {"cpu_count": cpu_count, "hardware": _collect_cpu_hardware()}

    mem = _read_meminfo()
    if mem:
        total_kb = mem.get("MemTotal", 0)
        available_kb = mem.get("MemAvailable", mem.get("MemFree", 0))
        used_kb = max(total_kb - available_kb, 0)
        swap_total_kb = mem.get("SwapTotal", 0)
        swap_free_kb = mem.get("SwapFree", 0)
        metrics["memory"] = {
            "total_gb": round(total_kb / 1024 / 1024, 2),
            "used_gb": round(used_kb / 1024 / 1024, 2),
            "available_gb": round(available_kb / 1024 / 1024, 2),
            "used_pct": round((used_kb / total_kb) * 100, 1) if total_kb else 0,
            "swap_used_gb": round((swap_total_kb - swap_free_kb) / 1024 / 1024, 2),
            "swap_total_gb": round(swap_total_kb / 1024 / 1024, 2),
            "hardware": _collect_memory_hardware(),
        }

    disk = _collect_disk_usage()
    if disk:
        disk["block_devices"] = _collect_block_devices()
        metrics["disk"] = disk

    network = _collect_network_usage()
    if network:
        metrics["network"] = network

    if shutil.which("nvidia-smi"):
        try:
            result = subprocess.run(
                [
                    "nvidia-smi",
                    "--query-gpu=index,name,utilization.gpu,utilization.memory,memory.total,memory.used,temperature.gpu,power.draw",
                    "--format=csv,noheader,nounits",
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=5,
            )
            gpus = []
            if result.returncode == 0:
                for line in result.stdout.splitlines():
                    parts = [p.strip() for p in line.split(",")]
                    if len(parts) < 8:
                        continue

                    def as_int(value: str) -> int | None:
                        try:
                            return int(float(value))
                        except ValueError:
                            return None

                    def as_float(value: str) -> float | None:
                        try:
                            return round(float(value), 1)
                        except ValueError:
                            return None

                    gpus.append({
                        "index": as_int(parts[0]),
                        "name": parts[1],
                        "utilization_gpu_pct": as_int(parts[2]),
                        "utilization_memory_pct": as_int(parts[3]),
                        "memory_total_mb": as_int(parts[4]),
                        "memory_used_mb": as_int(parts[5]),
                        "temperature_gpu": as_int(parts[6]),
                        "power_draw_w": as_float(parts[7]),
                    })
            metrics["gpu"] = {"nvidia": {"available": True, "gpus": gpus}}
        except (OSError, subprocess.SubprocessError):
            metrics["gpu"] = {"nvidia": {"available": True, "gpus": []}}
    else:
        metrics["gpu"] = {"nvidia": {"available": False, "gpus": []}}

    metrics["dmesg"] = _collect_live_dmesg()
    try:
        from .flight_recorder import record_snapshot, summarize_window

        record_snapshot()
        metrics["flight_recorder"] = summarize_window(hours=6)
    except Exception as exc:
        metrics["flight_recorder"] = {"error": str(exc)}
    metrics["agent_health"] = _build_agent_health(metrics["dmesg"])

    return metrics


def _explain_http_error(exc: httpx.HTTPStatusError) -> str:
    """Turn a Supabase REST error into an actionable message.

    Supabase returns useful JSON in the body (code/message/hint) that
    raise_for_status() drops — surface it so the user can self-diagnose.
    """
    resp = exc.response
    body = (resp.text or "").strip()
    status = resp.status_code

    # Missing RPC → the push-mode schema was never applied to this database.
    if status == 404 or "Could not find the function" in body or "PGRST202" in body:
        return (
            f"HTTP {status}: the agent_heartbeat function does not exist on your "
            f"Supabase project. Run supabase/schema.sql in the Supabase SQL editor "
            f"to create the push-mode tables and RPCs.\n  Server said: {body}"
        )
    if status in (401, 403):
        return (
            f"HTTP {status}: Supabase rejected the request — check that "
            f"CRASHPILOT_SUPABASE_ANON_KEY in /etc/crashpilot/.env matches your "
            f"project's anon key.\n  Server said: {body}"
        )
    if status == 400 and ("Invalid system_id or agent_token" in body or "P0001" in body):
        return (
            f"HTTP {status}: this system_id / agent_token pair is not in the "
            f"systems table. Re-run `sudo crashpilot configure cpilot_...` with a "
            f"fresh connection string from the dashboard.\n  Server said: {body}"
        )
    return f"HTTP {status} from Supabase.\n  Server said: {body}"


async def push_heartbeat(
    supabase_url: str,
    anon_key: str,
    system_id: str,
    agent_token: str,
    metrics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """UPSERT a heartbeat row via the agent_heartbeat RPC."""
    url = f"{supabase_url.rstrip('/')}/rest/v1/rpc/agent_heartbeat"
    payload = {
        "p_system_id": system_id,
        "p_agent_token": agent_token,
        "p_hostname": _hostname(),
        "p_version": _agent_version(),
        "p_metrics": metrics if metrics is not None else _build_live_metrics(),
    }
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(url, headers=_headers(anon_key), json=payload)
        if resp.status_code == 404 and "p_metrics" in resp.text:
            # Existing deployments may not have the metrics-enabled RPC yet.
            legacy_payload = {k: v for k, v in payload.items() if k != "p_metrics"}
            resp = await client.post(url, headers=_headers(anon_key), json=legacy_payload)
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise RuntimeError(_explain_http_error(exc)) from exc
        status_url = f"{supabase_url.rstrip('/')}/rest/v1/rpc/agent_system_status"
        status_resp = await client.post(
            status_url,
            headers=_headers(anon_key),
            json={"p_system_id": system_id, "p_agent_token": agent_token},
        )

    response_data: dict[str, Any] = {}
    if status_resp.status_code != 404:
        try:
            status_resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise RuntimeError(_explain_http_error(exc)) from exc
        try:
            decoded = status_resp.json()
            if isinstance(decoded, dict):
                response_data = decoded
        except (ValueError, json.JSONDecodeError):
            pass

        from .storage.store import set_meta

        set_meta("maintenance_until", response_data.get("maintenance_until") or "")
    log.debug("Heartbeat sent for system %s", system_id)
    return response_data


# Caps for what we send to the cloud — enough to be useful, small enough for JSONB.
_DMESG_TAIL_CHARS = 8000
_JOURNAL_SNIPPET_CHARS = 4000
_MAX_CRITICAL_EVENTS = 80


def _build_telemetry_summary(telemetry: dict[str, Any]) -> dict[str, Any]:
    """Trim the raw telemetry down to what the dashboard shows (kernel log etc.)."""
    dmesg = telemetry.get("dmesg") or {}
    journal = telemetry.get("journal") or {}
    system = telemetry.get("system") or {}
    flight_recorder = dict(telemetry.get("flight_recorder") or {})
    if flight_recorder.get("samples"):
        flight_recorder["samples"] = flight_recorder["samples"][-15:]
    return {
        "dmesg": {
            "critical_events": (dmesg.get("critical_events") or [])[:_MAX_CRITICAL_EVENTS],
            "critical_count": dmesg.get("critical_count", 0),
            # Keep the tail (most recent lines) — that's where the crash shows up.
            "tail": (dmesg.get("full_tail") or "")[-_DMESG_TAIL_CHARS:],
            "mce_events": (dmesg.get("mce_events") or "")[:_JOURNAL_SNIPPET_CHARS],
        },
        "journal": {
            "oom_events": (journal.get("oom_events") or "")[:_JOURNAL_SNIPPET_CHARS],
            "previous_boot_errors": (journal.get("previous_boot_errors") or "")[-_DMESG_TAIL_CHARS:],
        },
        "system": {
            "memory": system.get("memory"),
            "cpu": system.get("cpu"),
            "disk": system.get("disk"),
        },
        "flight_recorder": flight_recorder,
    }


async def push_report(
    supabase_url: str,
    anon_key: str,
    system_id: str,
    agent_token: str,
    report: dict[str, Any],
) -> str | None:
    """Push a crash report to Supabase via the agent_push_report RPC.

    Strips the raw telemetry blob (too large) before sending.
    Returns the report id on success, None on failure.
    """
    # The raw telemetry blob is too large for Supabase, but the dashboard still
    # needs the kernel log to show "why". Build a trimmed summary (dmesg + key
    # journal snippets) before dropping the full telemetry.
    telemetry_summary = _build_telemetry_summary(report.get("telemetry") or {})

    cloud_report = {k: v for k, v in report.items() if k != "telemetry"}
    cloud_report["telemetry_summary"] = telemetry_summary

    # The agent_push_report RPC reads these from the TOP level, but they live
    # inside `analysis`. Lift them so the cloud row (and the dashboard's "AI
    # analyzed" badge/stat + severity) reflects the analysis, not just heuristics.
    analysis = report.get("analysis") or {}
    cloud_report["ai_analyzed"] = bool(analysis.get("ai_analyzed", False))
    if analysis.get("severity"):
        cloud_report["severity"] = analysis["severity"]
    if analysis.get("crash_type"):
        cloud_report["crash_type"] = analysis["crash_type"]

    url = f"{supabase_url.rstrip('/')}/rest/v1/rpc/agent_push_report"
    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
        resp = await client.post(
            url,
            headers=_headers(anon_key),
            json={
                "p_system_id": system_id,
                "p_agent_token": agent_token,
                "p_report": cloud_report,
            },
        )
        resp.raise_for_status()

    report_id = report.get("id")
    result = resp.json()
    cloud_result = result if isinstance(result, str) else report_id
    if cloud_result and cloud_result.startswith("suppressed:"):
        log.info("Report %s suppressed by an active maintenance window", report_id)
    else:
        log.info("Report %s pushed to Supabase cloud", report_id)
    return cloud_result
