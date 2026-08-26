"""Low-overhead rolling system snapshots and capacity forecasts."""

from __future__ import annotations

import math
import os
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psutil

from .storage.store import init_db, list_flight_snapshots, save_flight_snapshot

_SERVICE_PATTERN = re.compile(r"(?:^|/)([^/]+\.service)(?:$|/)")


def _process_service(pid: int | None) -> str | None:
    if not pid:
        return None
    try:
        text = Path(f"/proc/{pid}/cgroup").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    match = _SERVICE_PATTERN.search(text)
    return match.group(1) if match else None


def _top_processes(limit: int = 8) -> dict[str, list[dict[str, Any]]]:
    rows: list[dict[str, Any]] = []
    for process in psutil.process_iter(
        [
            "pid", "name", "username", "cmdline", "exe", "create_time",
            "memory_info", "cpu_percent",
        ]
    ):
        try:
            info = process.info
            memory = info.get("memory_info")
            pid = info.get("pid")
            create_time = info.get("create_time")
            command = " ".join(info.get("cmdline") or [])[:300]
            identity = "|".join([
                str(pid or ""),
                str(round(float(create_time), 3)) if create_time else "",
                str(info.get("exe") or ""),
            ])
            rows.append({
                "pid": pid,
                "name": info.get("name") or "unknown",
                "user": info.get("username"),
                "command": command,
                "executable": info.get("exe"),
                "started_at": (
                    datetime.fromtimestamp(float(create_time), timezone.utc).isoformat()
                    if create_time else None
                ),
                "identity": identity,
                "rss_mb": round((memory.rss if memory else 0) / 1024 / 1024, 1),
                "cpu_pct": round(float(info.get("cpu_percent") or 0), 1),
            })
        except (psutil.AccessDenied, psutil.NoSuchProcess, OSError):
            continue
    by_memory = sorted(rows, key=lambda row: row["rss_mb"], reverse=True)[:limit]
    by_cpu = sorted(rows, key=lambda row: row["cpu_pct"], reverse=True)[:limit]
    # Resolve the cgroup/service name (a /proc/<pid>/cgroup read per process)
    # only for the small set of processes we're actually reporting, not for
    # every process on the host — on a busy or shared box this can be the
    # difference between ~16 file reads and several thousand every tick.
    for row in {row["pid"]: row for row in (*by_memory, *by_cpu)}.values():
        row["service"] = _process_service(row["pid"])
    return {"memory": by_memory, "cpu": by_cpu}


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


_PACKAGE_HISTORY_TAIL_BYTES = 65_536


def _recent_package_changes() -> list[str]:
    history = Path("/var/log/apt/history.log")
    try:
        # apt's history.log grows unboundedly over a host's lifetime (can
        # reach multiple MB); the entries we care about are always near the
        # end, so read only a bounded tail instead of the whole file on
        # every heartbeat tick.
        size = history.stat().st_size
        with history.open("rb") as f:
            read_size = min(size, _PACKAGE_HISTORY_TAIL_BYTES)
            f.seek(size - read_size)
            raw = f.read()
        lines = raw.decode("utf-8", errors="replace").splitlines()
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
    getloadavg = getattr(os, "getloadavg", None)
    load_1m, load_5m, load_15m = getloadavg() if getloadavg else (0.0, 0.0, 0.0)
    snapshot = {
        "schema_version": 1,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "cpu": {
            "count": psutil.cpu_count() or 1,
            "load_1m": round(load_1m, 2),
            "load_5m": round(load_5m, 2),
            "load_15m": round(load_15m, 2),
            # interval=None reports usage since the last call (or module
            # import) instead of blocking for interval=0.1 seconds — this
            # runs on every heartbeat tick, so that sleep was a guaranteed
            # 100ms stall each time for no accuracy benefit here.
            "used_pct": round(psutil.cpu_percent(interval=None), 1),
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
    if len(points) < 6:
        return None
    duration_hours = (points[-1][0] - points[0][0]) / 3600
    if duration_hours < 0.5:
        return None
    start = points[0][0]
    xs = [(x - start) / 3600 for x, _ in points]
    ys = [y for _, y in points]
    x_mean = sum(xs) / len(xs)
    y_mean = sum(ys) / len(ys)
    denominator = sum((x - x_mean) ** 2 for x in xs)
    if denominator == 0:
        return None
    slope = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys, strict=True)) / denominator
    if slope <= 0.05:
        return None
    fitted = [y_mean + slope * (x - x_mean) for x in xs]
    residual = sum((actual - predicted) ** 2 for actual, predicted in zip(ys, fitted, strict=True))
    total = sum((actual - y_mean) ** 2 for actual in ys)
    r_squared = 1 - (residual / total) if total else 0
    if r_squared < 0.55:
        return None
    hours = (threshold - ys[-1]) / slope
    if not math.isfinite(hours) or hours <= 0 or hours > 24 * 90:
        return None
    return {
        "current_pct": round(ys[-1], 1),
        "growth_pct_per_hour": round(slope, 2),
        "hours_to_threshold": round(hours, 1),
        "confidence": (
            "high" if r_squared >= 0.85 and duration_hours >= 2
            else "medium"
        ),
        "r_squared": round(r_squared, 3),
        "observation_hours": round(duration_hours, 1),
        "sample_count": len(points),
    }


def summarize_window(hours: int = 1) -> dict[str, Any]:
    samples = list_flight_snapshots(hours=hours)
    latest = samples[-1] if samples else None
    first = samples[0] if samples else None
    memory_growth: list[dict[str, Any]] = []
    if samples and latest:
        # Each snapshot's "memory" list is only the top-8 processes by RSS
        # at that moment - a process doesn't appear in it at all until it's
        # large enough to rank there. Comparing only against the very FIRST
        # snapshot missed every process that started small and grew into
        # the top 8 partway through the window: exactly the "quiet leak
        # that eventually OOMs the box" case this feature exists to catch.
        # Instead, compare against the EARLIEST snapshot in the window
        # where each process identity appears anywhere in a top-8 list -
        # the earliest point we have any recorded rss for it at all.
        earliest_by_identity: dict[str, dict[str, Any]] = {}
        for sample in samples:
            for row in (sample.get("processes") or {}).get("memory", []):
                identity = row.get("identity") or f"{row.get('pid')}|{row.get('name')}"
                earliest_by_identity.setdefault(identity, row)
        for row in (latest.get("processes") or {}).get("memory", []):
            identity = row.get("identity") or f"{row.get('pid')}|{row.get('name')}"
            previous = earliest_by_identity.get(identity)
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
