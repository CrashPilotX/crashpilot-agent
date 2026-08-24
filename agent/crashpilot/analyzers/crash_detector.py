"""Heuristic crash type detection - runs before AI analysis."""

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
    # Other crash-type patterns that also matched, but scored lower than the
    # chosen result - real runner-up detections, not invented possibilities.
    # Populated only when more than one rule matched (see detect_crash_type).
    alternatives: list[dict[str, Any]] = field(default_factory=list)
    # Which collector each evidence[i] line actually came from - aligned by
    # index. "system" means the line wasn't found verbatim in any collected
    # log chunk (e.g. a derived signal like "no shutdown record found"),
    # not a guess at a specific source.
    evidence_sources: list[str] = field(default_factory=list)


# Heuristic rules - ordered by priority
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
        r"Out of memory: Kill process",    # kernel < 5.x
        r"Out of memory: Killed process",  # kernel 5.x+
        r"oom_kill_process",
        r"Killed process \d+.*oom_kill",
        r"oom-killer invoked",
        r"Memory cgroup out of memory",
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
    line_sources = _line_source_map(telemetry)

    # Check clean shutdown first
    shutdown_hits = _match_patterns(CrashType.CLEAN_SHUTDOWN.value, text_corpus, _RULES[-1][2])
    if shutdown_hits and not _has_panic_patterns(lower_corpus):
        return DetectionResult(
            crash_type=CrashType.CLEAN_SHUTDOWN,
            severity=Severity.INFO,
            confidence=0.85,
            evidence=shutdown_hits,
            evidence_sources=[_line_source(line_sources, e) for e in shutdown_hits],
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
        best.evidence_sources = [_line_source(line_sources, e) for e in best.evidence]
        # Real runner-up detections - each one genuinely matched its own
        # pattern set with its own evidence and confidence, just lower than
        # `best`. Surfaced as structured alternative hypotheses (see
        # alternatives below) instead of being duplicated into best.evidence
        # as "[secondary] ..." text, which used to show the same line twice
        # under two different labels.
        best.alternatives = [
            {
                "crash_type": other.crash_type.value,
                "confidence": other.confidence,
                "evidence": other.evidence[:3],
            }
            for other in all_matches[1:]
        ]
        return best

    # No pattern matched - power loss or unknown
    result = _infer_power_loss_or_unknown(telemetry)
    result.evidence_sources = [_line_source(line_sources, e) for e in result.evidence]
    return result


def _line_source_map(telemetry: dict) -> dict[str, str]:
    """Map each distinct log line to the collector it came from, so a
    heuristic evidence line (just matched text - see _match_patterns) can be
    honestly attributed to a real source instead of left unlabeled."""
    sources: dict[str, str] = {}
    journal = telemetry.get("journal", {})
    dmesg = telemetry.get("dmesg", {})
    gpu = telemetry.get("gpu", {})
    # Ordered most- to least-specific collector: NVIDIA driver lines often
    # appear in dmesg's own tail too (the kernel ring buffer captures
    # everything), so a duplicate line should be attributed to the specific
    # gpu_nvidia collector rather than the generic dmesg catch-all. First
    # match wins below, so check gpu_nvidia before dmesg/journal.
    chunks = (
        ("gpu_nvidia", gpu.get("nvidia", {}).get("xid_errors", "")),
        ("dmesg", dmesg.get("full_tail", "")[-10000:]),
        ("dmesg", "\n".join(dmesg.get("critical_events", []))),
        ("dmesg", dmesg.get("mce_events", "")),
        ("journal", journal.get("previous_boot_errors", "")),
        ("journal", journal.get("previous_boot_logs_tail", "")[-10000:]),
        ("journal", journal.get("oom_events", "")),
    )
    for source, text in chunks:
        for line in text.splitlines():
            stripped = line.strip()[:200]
            if stripped and stripped not in sources:
                sources[stripped] = source
    return sources


def _line_source(line_sources: dict[str, str], evidence_line: str) -> str:
    """Look up a real source for an evidence line, or "system" - an honest
    "not a specific collector" label - if it's a derived signal rather than
    text found verbatim in a collected log."""
    line = evidence_line.removeprefix("[secondary] ").strip()
    return line_sources.get(line, "system")


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
            line = corpus[start:end].strip()[:200]
            if line and line not in hits:
                hits.append(line)
    return hits


def _has_panic_patterns(lower_corpus: str) -> bool:
    # Must cover every KERNEL_PANIC/MCE pattern from _RULES above - otherwise
    # a real panic whose corpus also happens to contain a shutdown-target
    # line (e.g. a watchdog-forced reboot right after a double fault) gets
    # silently reclassified as CLEAN_SHUTDOWN instead of the actual panic.
    return any(p in lower_corpus for p in [
        "kernel panic", "bug:", "general protection fault",
        "double fault", "triple fault",
        "machine check exception", "mce:", "mcelog",
    ])


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
