"""Build a compact, versioned forensic context for incident investigation."""

from __future__ import annotations

import hashlib
import json
from typing import Any

FORENSIC_SCHEMA_VERSION = 1
_COLLECTOR_NAMES = ("journal", "dmesg", "system", "smart", "gpu", "thermal", "wsl")


def _collector_status(value: Any) -> str:
    if value is None:
        return "not_collected"
    if isinstance(value, dict) and value.get("error"):
        return "error"
    if value in ({}, [], ""):
        return "empty"
    return "collected"


def _root_disk(system: dict[str, Any]) -> dict[str, Any] | None:
    disk = system.get("disk") or {}
    filesystems = disk.get("filesystems") or []
    return next((item for item in filesystems if item.get("mountpoint") == "/"), None)


def build_forensic_snapshot(
    telemetry: dict[str, Any],
    detection: dict[str, Any],
    timeline: list[dict[str, Any]],
) -> dict[str, Any]:
    coverage = {
        name: _collector_status(telemetry.get(name))
        for name in _COLLECTOR_NAMES
        if name in telemetry or name in ("journal", "dmesg", "system")
    }
    gaps = [
        f"{name} evidence {_collector_status(telemetry.get(name)).replace('_', ' ')}"
        for name in ("journal", "dmesg", "system")
        if _collector_status(telemetry.get(name)) != "collected"
    ]

    journal = telemetry.get("journal") or {}
    if not journal.get("previous_boot_errors") and not journal.get("oom_events"):
        gaps.append("previous-boot journal did not contain retained error evidence")

    evidence = detection.get("evidence") or []
    signals = detection.get("signals") or []
    sources = sorted({
        str(event.get("source"))
        for event in timeline
        if event.get("source")
    })
    fingerprint_payload = {
        "crash_type": detection.get("crash_type"),
        "evidence": evidence[:8],
        "signals": signals[:8],
    }
    fingerprint = hashlib.sha256(
        json.dumps(fingerprint_payload, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:16]

    platform = telemetry.get("platform") or {}
    system = telemetry.get("system") or {}
    return {
        "schema_version": FORENSIC_SCHEMA_VERSION,
        "fingerprint": fingerprint,
        "collected_at": telemetry.get("collected_at"),
        "coverage": coverage,
        "evidence_gaps": gaps,
        "signal_summary": {
            "evidence_count": len(evidence),
            "timeline_event_count": len(timeline),
            "sources": sources,
            "strongest_evidence": evidence[:3],
        },
        "system_context": {
            "hostname": platform.get("hostname"),
            "platform": platform.get("type"),
            "kernel": platform.get("kernel"),
            "memory": system.get("memory"),
            "root_disk": _root_disk(system),
        },
    }
