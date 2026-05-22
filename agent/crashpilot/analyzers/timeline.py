"""Build a chronological event timeline from telemetry data."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


@dataclass
class TimelineEvent:
    timestamp: str       # ISO-8601
    source: str          # e.g. "journal", "dmesg", "smart"
    level: str           # "critical", "error", "warning", "info"
    message: str
    raw: str = ""


# Regex patterns for extracting timestamps from log lines
_ISO_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")
_SYSLOG_RE = re.compile(
    r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{1,2}\s+\d{2}:\d{2}:\d{2}"
)
_DMESG_TS_RE = re.compile(r"\[\s*(\d+\.\d+)\]")

# Severity keywords
_LEVEL_MAP = {
    "critical": ["kernel panic", "oops", "bug:", "mce:", "double fault", "triple fault"],
    "error": ["error", "failed", "failure", "fault", "killed", "oom", "xid", "i/o error"],
    "warning": ["warn", "throttl", "throttle", "degraded", "hung", "stall", "timeout"],
}


def build_timeline(telemetry: dict[str, Any]) -> list[dict]:
    """Parse telemetry sources into a unified, sorted timeline."""
    events: list[TimelineEvent] = []

    # Journal logs
    journal = telemetry.get("journal", {})
    prev_errors = journal.get("previous_boot_errors", "")
    events.extend(_parse_journal_lines(prev_errors, "journal"))

    # OOM events
    oom = journal.get("oom_events", "")
    events.extend(_parse_journal_lines(oom, "oom"))

    # dmesg critical events
    dmesg_criticals = telemetry.get("dmesg", {}).get("critical_events", [])
    for line in dmesg_criticals:
        events.append(TimelineEvent(
            timestamp="",
            source="dmesg",
            level=_infer_level(line),
            message=line[:200],
            raw=line,
        ))

    # GPU XID errors
    xid_raw = telemetry.get("gpu", {}).get("nvidia", {}).get("xid_errors", "")
    events.extend(_parse_journal_lines(xid_raw, "gpu_nvidia"))

    # Thermal warnings
    thermal_warns = telemetry.get("thermal", {}).get("thermal_warnings", [])
    for w in thermal_warns:
        events.append(TimelineEvent(
            timestamp="", source="thermal", level="warning", message=w
        ))

    # SMART critical disks
    for disk in telemetry.get("smart", {}).get("critical_disks", []):
        events.append(TimelineEvent(
            timestamp="",
            source="smart",
            level="error",
            message=f"Disk {disk.get('device')} health: {disk.get('health')} — "
                    f"critical attrs: {disk.get('critical_attributes', {})}",
        ))

    # Sort by timestamp (events without timestamps go to the end)
    events.sort(key=lambda e: (e.timestamp == "", e.timestamp))

    return [
        {
            "timestamp": e.timestamp or None,
            "source": e.source,
            "level": e.level,
            "message": e.message,
        }
        for e in events
    ]


def _parse_journal_lines(text: str, source: str) -> list[TimelineEvent]:
    events = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        ts = _extract_timestamp(line)
        level = _infer_level(line)
        if level in ("critical", "error", "warning") or source == "oom":
            events.append(TimelineEvent(
                timestamp=ts,
                source=source,
                level=level,
                message=_strip_timestamp_prefix(line)[:200],
                raw=line,
            ))
    return events


def _extract_timestamp(line: str) -> str:
    m = _ISO_RE.search(line)
    if m:
        return m.group(0)
    m = _SYSLOG_RE.search(line)
    if m:
        return m.group(0)
    return ""


def _strip_timestamp_prefix(line: str) -> str:
    # Remove leading ISO timestamp + hostname + process prefix
    cleaned = _ISO_RE.sub("", line)
    # Remove syslog prefix: "Nov 12 10:23:45 hostname process[pid]:"
    cleaned = re.sub(r"^\S+\s+\S+\s+\S+\s+", "", cleaned)
    return cleaned.strip() or line.strip()


def _infer_level(text: str) -> str:
    lower = text.lower()
    for level, keywords in _LEVEL_MAP.items():
        if any(kw in lower for kw in keywords):
            return level
    return "info"
