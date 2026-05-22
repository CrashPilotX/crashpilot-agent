"""Heuristic crash type detection — runs before AI analysis."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class CrashType(str, Enum):
    KERNEL_PANIC = "kernel_panic"
    OOM_KILL = "oom_kill"
    THERMAL_SHUTDOWN = "thermal_shutdown"
    POWER_LOSS = "power_loss"
    WATCHDOG_RESET = "watchdog_reset"
    GPU_FAULT = "gpu_fault"
    DISK_ERROR = "disk_error"
    MCE = "machine_check_exception"
    SOFT_LOCKUP = "soft_lockup"
    CLEAN_SHUTDOWN = "clean_shutdown"
    UNKNOWN = "unknown"


class Severity(str, Enum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"


@dataclass
class DetectionResult:
    crash_type: CrashType
    severity: Severity
    confidence: float  # 0-1
    evidence: list[str] = field(default_factory=list)
    signals: dict[str, Any] = field(default_factory=dict)


# Heuristic rules — ordered by priority
_RULES: list[tuple[CrashType, Severity, list[str]]] = [
    (CrashType.KERNEL_PANIC, Severity.CRITICAL, [
        r"Kernel panic",
        r"BUG: unable to handle",
        r"general protection fault",
        r"double fault",
        r"triple fault",
    ]),
    (CrashType.MCE, Severity.CRITICAL, [
        r"Machine check exception",
        r"MCE:.*MEMORY_ERROR",
        r"EDAC.*correctable",
        r"EDAC.*uncorrectable",
        r"mcelog",
    ]),
    (CrashType.OOM_KILL, Severity.HIGH, [
        r"Out of memory: Kill process",
        r"oom_kill_process",
        r"Killed process \d+.*oom_kill",
        r"oom-killer invoked",
    ]),
    (CrashType.THERMAL_SHUTDOWN, Severity.HIGH, [
        r"ACPI: Thermal Zone.*critical",
        r"thermal: critical",
        r"Critical temperature reached",
        r"CPU.*thermal throttle",
        r"PM: suspend.*thermal",
    ]),
    (CrashType.WATCHDOG_RESET, Severity.HIGH, [
        r"watchdog: BUG",
        r"hard LOCKUP",
        r"NMI watchdog",
        r"watchdog reset",
    ]),
    (CrashType.SOFT_LOCKUP, Severity.HIGH, [
        r"soft lockup",
        r"RCU.*stall",
        r"hung_task.*blocked",
        r"INFO: task.*blocked for more than",
    ]),
    (CrashType.GPU_FAULT, Severity.HIGH, [
        r"NVRM: Xid",
        r"amdgpu.*GPU fault",
        r"GPU fell off the bus",
        r"NVRM.*RmInitAdapter.*failed",
        r"drm.*GPU HANG",
    ]),
    (CrashType.DISK_ERROR, Severity.HIGH, [
        r"ata\d+.*error",
        r"SCSI.*I/O error",
        r"nvme.*error",
        r"EXT4-fs error",
        r"BTRFS.*error",
        r"end_request: I/O error",
    ]),
    (CrashType.CLEAN_SHUTDOWN, Severity.INFO, [
        r"System is powering down",
        r"Reached target.*Power-Off",
        r"reboot: Power down",
        r"Stopped target.*Shutdown",
        r"systemd-shutdown.*Shutting down",
    ]),
]


def detect_crash_type(telemetry: dict[str, Any]) -> DetectionResult:
    """Run heuristic rules against collected telemetry to classify the crash."""

    # Aggregate all text evidence
    text_corpus = _build_corpus(telemetry)
    lower_corpus = text_corpus.lower()

    # Check clean shutdown first
    shutdown_hits = _match_patterns(CrashType.CLEAN_SHUTDOWN.value, text_corpus, _RULES[-1][2])
    if shutdown_hits and not _has_panic_patterns(lower_corpus):
        return DetectionResult(
            crash_type=CrashType.CLEAN_SHUTDOWN,
            severity=Severity.INFO,
            confidence=0.85,
            evidence=shutdown_hits,
        )

    # Run through prioritized rules
    best: DetectionResult | None = None
    all_matches: list[DetectionResult] = []

    for crash_type, severity, patterns in _RULES[:-1]:  # skip CLEAN_SHUTDOWN
        hits = _match_patterns(crash_type.value, text_corpus, patterns)
        if not hits:
            continue
        confidence = min(0.95, 0.5 + len(hits) * 0.1)
        result = DetectionResult(
            crash_type=crash_type,
            severity=severity,
            confidence=confidence,
            evidence=hits[:5],
        )
        all_matches.append(result)

    # Enrich with hardware signals
    _enrich_with_hardware(all_matches, telemetry)

    if all_matches:
        # Return highest-confidence match (ties broken by severity)
        severity_rank = {
            Severity.CRITICAL: 0, Severity.HIGH: 1,
            Severity.MEDIUM: 2, Severity.LOW: 3, Severity.INFO: 4,
        }
        all_matches.sort(key=lambda r: (-r.confidence, severity_rank[r.severity]))
        best = all_matches[0]
        # Collect signals from other matches
        for other in all_matches[1:]:
            best.evidence.extend([f"[secondary] {e}" for e in other.evidence[:2]])
        return best

    # No pattern matched — power loss or unknown
    return _infer_power_loss_or_unknown(telemetry)


def _build_corpus(telemetry: dict) -> str:
    """Extract all loggable text from telemetry into one string."""
    parts = []
    journal = telemetry.get("journal", {})
    parts.append(journal.get("previous_boot_errors", ""))
    parts.append(journal.get("previous_boot_logs_tail", "")[-10000:])
    parts.append(journal.get("oom_events", ""))

    dmesg = telemetry.get("dmesg", {})
    parts.append(dmesg.get("full_tail", "")[-10000:])
    parts.append("\n".join(dmesg.get("critical_events", [])))
    parts.append(dmesg.get("mce_events", ""))

    gpu = telemetry.get("gpu", {})
    parts.append(gpu.get("nvidia", {}).get("xid_errors", ""))

    return "\n".join(filter(None, parts))


def _match_patterns(label: str, corpus: str, patterns: list[str]) -> list[str]:
    hits = []
    for pattern in patterns:
        for match in re.finditer(pattern, corpus, re.IGNORECASE | re.MULTILINE):
            # Return the line containing the match
            start = corpus.rfind("\n", 0, match.start()) + 1
            end = corpus.find("\n", match.end())
            if end == -1:
                end = len(corpus)
            line = corpus[start:end].strip()
            if line and line not in hits:
                hits.append(line[:200])
    return hits


def _has_panic_patterns(lower_corpus: str) -> bool:
    return any(p in lower_corpus for p in ["kernel panic", "bug:", "general protection fault"])


def _enrich_with_hardware(results: list[DetectionResult], telemetry: dict) -> None:
    """Add hardware evidence signals to detection results."""
    smart = telemetry.get("smart", {})
    if smart.get("critical_disks"):
        for r in results:
            if r.crash_type == CrashType.DISK_ERROR:
                r.confidence = min(0.95, r.confidence + 0.2)
                r.signals["smart_critical_disks"] = len(smart["critical_disks"])

    thermal = telemetry.get("thermal", {})
    if thermal.get("thermal_warnings"):
        for r in results:
            if r.crash_type == CrashType.THERMAL_SHUTDOWN:
                r.confidence = min(0.95, r.confidence + 0.2)
                r.signals["thermal_warnings"] = thermal["thermal_warnings"]


def _infer_power_loss_or_unknown(telemetry: dict) -> DetectionResult:
    """When no patterns match, try to distinguish power loss from unknown."""
    journal = telemetry.get("journal", {})
    boots = journal.get("boots", [])
    shutdown = journal.get("shutdown_info", "")

    # If shutdown_info is empty, it's likely a power loss (no clean shutdown marker)
    if not shutdown.strip() and len(boots) > 1:
        return DetectionResult(
            crash_type=CrashType.POWER_LOSS,
            severity=Severity.HIGH,
            confidence=0.55,
            evidence=["No systemd shutdown record found for previous boot"],
        )

    return DetectionResult(
        crash_type=CrashType.UNKNOWN,
        severity=Severity.MEDIUM,
        confidence=0.30,
        evidence=["No clear crash signature detected; requires AI analysis"],
    )
