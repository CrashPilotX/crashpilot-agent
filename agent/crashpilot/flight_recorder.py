"""Low-overhead rolling system snapshots and capacity forecasts."""

from __future__ import annotations

import math
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psutil

from .storage.store import init_db, list_flight_snapshots, save_flight_snapshot


def _top_processes(limit: int = 8) -> dict[str, list[dict[str, Any]]]:
    rows: list[dict[str, Any]] = []
    for process in psutil.process_iter(
        ["pid", "name", "username", "cmdline", "memory_info", "cpu_percent"]
    ):
        try:
            info = process.info
            memory = info.get("memory_info")
            rows.append({
                "pid": info.get("pid"),
                "name": info.get("name") or "unknown",
                "user": info.get("username"),
                "command": " ".join(info.get("cmdline") or [])[:300],
                "rss_mb": round((memory.rss if memory else 0) / 1024 / 1024, 1),
                "cpu_pct": round(float(info.get("cpu_percent") or 0), 1),
            })
        except (psutil.AccessDenied, psutil.NoSuchProcess, OSError):
            continue
    return {
        "memory": sorted(rows, key=lambda row: row["rss_mb"], reverse=True)[:limit],
        "cpu": sorted(rows, key=lambda row: row["cpu_pct"], reverse=True)[:limit],
    }


def _failed_services() -> list[dict[str, str]]:
    if not shutil.which("systemctl"):
        return []
    try:
        result = subprocess.run(
            [
                "systemctl", "--failed", "--no-legend", "--plain",
                "--no-pager", "--type=service",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=8,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    services = []
    for line in result.stdout.splitlines()[:20]:
        parts = line.split(None, 4)
        if not parts:
            continue
        services.append({
            "unit": parts[0],
            "state": parts[2] if len(parts) > 2 else "failed",
            "description": parts[4] if len(parts) > 4 else "",
        })
    return services


def _recent_package_changes() -> list[str]:
    history = Path("/var/log/apt/history.log")
    try:
        lines = history.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    relevant = [
        line.strip()
        for line in lines[-120:]
        if line.startswith(("Start-Date:", "Commandline:", "Install:", "Upgrade:", "Remove:"))
    ]
    return relevant[-20:]


def _directory_usage() -> list[dict[str, Any]]:
    roots = [path for path in ("/var", "/home", "/opt", "/tmp") if Path(path).exists()]
    if not roots or not shutil.which("du"):
        return []
    try:
        result = subprocess.run(
            ["du", "-x", "-B1", "-s", *roots],
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    usage = []
    for line in result.stdout.splitlines():
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        try:
            usage.append({
                "path": parts[1],
                "used_gb": round(int(parts[0]) / 1024 / 1024 / 1024, 2),
            })
        except ValueError:
            continue
    return sorted(usage, key=lambda item: item["used_gb"], reverse=True)


def capture_snapshot(*, deep: bool = False) -> dict[str, Any]:
    memory = psutil.virtual_memory()
    swap = psutil.swap_memory()
    root = psutil.disk_usage("/")
    disk_io = psutil.disk_io_counters()
    net_io = psutil.net_io_counters()
    load_1m, load_5m, load_15m = os.getloadavg()
    snapshot = {
        "schema_version": 1,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "cpu": {
            "count": psutil.cpu_count() or 1,
            "load_1m": round(load_1m, 2),
            "load_5m": round(load_5m, 2),
            "load_15m": round(load_15m, 2),
            "used_pct": round(psutil.cpu_percent(interval=0.1), 1),
        },
        "memory": {
            "used_pct": round(memory.percent, 1),
            "available_gb": round(memory.available / 1024**3, 2),
            "swap_used_pct": round(swap.percent, 1),
        },
        "disk": {
            "mountpoint": "/",
            "used_pct": round(root.percent, 1),
            "free_gb": round(root.free / 1024**3, 2),
            "read_bytes": disk_io.read_bytes if disk_io else None,
            "write_bytes": disk_io.write_bytes if disk_io else None,
        },
        "network": {
            "sent_bytes": net_io.bytes_sent if net_io else None,
            "received_bytes": net_io.bytes_recv if net_io else None,
        },
        "processes": _top_processes(),
        "failed_services": _failed_services(),
        "package_changes": _recent_package_changes(),
    }
    if deep:
        snapshot["directory_usage"] = _directory_usage()
    return snapshot


def record_snapshot(*, deep: bool = False) -> dict[str, Any]:
    init_db()
    snapshot = capture_snapshot(deep=deep)
    save_flight_snapshot(snapshot)
    return snapshot


def _forecast(samples: list[dict], path: tuple[str, str], threshold: float) -> dict[str, Any] | None:
    points = []
    for sample in samples:
        value = (sample.get(path[0]) or {}).get(path[1])
        timestamp = sample.get("captured_at")
        if isinstance(value, (int, float)) and timestamp:
            try:
                seconds = datetime.fromisoformat(timestamp.replace("Z", "+00:00")).timestamp()
                points.append((seconds, float(value)))
            except ValueError:
                continue
    if len(points) < 3:
        return None
    start = points[0][0]
    xs = [(x - start) / 3600 for x, _ in points]
    ys = [y for _, y in points]
    x_mean = sum(xs) / len(xs)
    y_mean = sum(ys) / len(ys)
    denominator = sum((x - x_mean) ** 2 for x in xs)
    if denominator == 0:
        return None
    slope = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys)) / denominator
    if slope <= 0.05:
        return None
    hours = (threshold - ys[-1]) / slope
    if not math.isfinite(hours) or hours <= 0 or hours > 24 * 90:
        return None
    return {
        "current_pct": round(ys[-1], 1),
        "growth_pct_per_hour": round(slope, 2),
        "hours_to_threshold": round(hours, 1),
    }


def summarize_window(hours: int = 1) -> dict[str, Any]:
    samples = list_flight_snapshots(hours=hours)
    latest = samples[-1] if samples else None
    first = samples[0] if samples else None
    memory_growth: list[dict[str, Any]] = []
    if first and latest:
        first_by_name = {
            row["name"]: row
            for row in (first.get("processes") or {}).get("memory", [])
        }
        for row in (latest.get("processes") or {}).get("memory", []):
            previous = first_by_name.get(row["name"])
            if previous:
                growth = round(row["rss_mb"] - previous["rss_mb"], 1)
                if growth > 10:
                    memory_growth.append({**row, "growth_mb": growth})
    return {
        "schema_version": 1,
        "window_hours": hours,
        "sample_count": len(samples),
        "first_at": first.get("captured_at") if first else None,
        "last_at": latest.get("captured_at") if latest else None,
        "latest": latest,
        "forecasts": {
            "disk": _forecast(samples, ("disk", "used_pct"), 95),
            "memory": _forecast(samples, ("memory", "used_pct"), 95),
        },
        "process_memory_growth": sorted(
            memory_growth, key=lambda row: row["growth_mb"], reverse=True
        )[:8],
        "recent_package_changes": (latest or {}).get("package_changes", []),
        "failed_services": (latest or {}).get("failed_services", []),
        "directory_usage": (latest or {}).get("directory_usage", []),
        "samples": samples[-60:],
    }
