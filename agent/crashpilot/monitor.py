"""Crash monitor — runs on boot, detects and analyzes abnormal shutdowns."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Any

from .analyzers.ai_analyzer import analyze_crash
from .analyzers.crash_detector import CrashType, detect_crash_type
from .analyzers.timeline import build_timeline
from .collectors.dmesg import DmesgCollector
from .collectors.docker import DockerCollector
from .collectors.gpu import GpuCollector
from .collectors.journal import JournalCollector
from .collectors.smart import SmartCollector
from .collectors.system import SystemCollector
from .collectors.thermal import ThermalCollector
from .config import get_settings
from .storage.store import get_meta, init_db, save_report, set_meta, update_analysis

log = logging.getLogger(__name__)


async def collect_telemetry() -> dict[str, Any]:
    """Run all collectors in parallel and return combined telemetry."""
    collectors = [
        JournalCollector(),
        DmesgCollector(),
        SmartCollector(),
        GpuCollector(),
        ThermalCollector(),
        DockerCollector(),
        SystemCollector(),
    ]

    results = await asyncio.gather(
        *[c.safe_collect() for c in collectors], return_exceptions=True
    )

    telemetry: dict[str, Any] = {}
    for collector, result in zip(collectors, results):
        if isinstance(result, Exception):
            telemetry[collector.name] = {"error": str(result)}
        else:
            telemetry[collector.name] = result

    telemetry["collected_at"] = datetime.now(timezone.utc).isoformat()
    return telemetry


def _make_report_id(boot_id: str) -> str:
    h = hashlib.sha256(boot_id.encode()).hexdigest()[:12]
    return f"crash_{h}"


async def check_and_analyze(force: bool = False) -> dict | None:
    """
    Check if the previous boot ended abnormally. If so, collect telemetry,
    run heuristic detection, call AI, and store the report.

    Returns the crash report dict if a crash was detected, None otherwise.
    """
    cfg = get_settings()
    init_db()

    log.info("Collecting telemetry...")
    telemetry = await collect_telemetry()

    journal = telemetry.get("journal", {})
    boots = journal.get("boots", [])

    if len(boots) < 2 and not force:
        log.info("Only one boot in history — nothing to analyze.")
        return None

    current_boot_id = journal.get("current_boot_id", "unknown")
    prev_boot_id = journal.get("previous_boot_id", "unknown")

    # Avoid re-analyzing the same boot
    last_analyzed = get_meta("last_analyzed_boot")
    if last_analyzed == prev_boot_id and not force:
        log.info("Previous boot %s already analyzed — skipping.", prev_boot_id)
        return None

    log.info("Running crash detection on boot %s...", prev_boot_id)
    detection = detect_crash_type(telemetry)

    # Don't store clean shutdowns as crash reports (unless forced)
    if detection.crash_type == CrashType.CLEAN_SHUTDOWN and not force:
        log.info("Previous shutdown was clean. No crash report needed.")
        set_meta("last_analyzed_boot", prev_boot_id)
        return None

    log.info(
        "Detected: %s (confidence %.0f%%)", detection.crash_type.value, detection.confidence * 100
    )

    # Determine crash time (last entry of previous boot)
    crash_time = None
    if len(boots) >= 2:
        crash_time = boots[1].get("last_entry")

    # Build timeline
    timeline = build_timeline(telemetry)

    report_id = _make_report_id(prev_boot_id)
    report = {
        "id": report_id,
        "boot_id": prev_boot_id,
        "detected_at": datetime.now(timezone.utc).isoformat(),
        "crash_time": crash_time,
        "crash_type": detection.crash_type.value,
        "severity": detection.severity.value,
        "summary": f"Detected {detection.crash_type.value} with {detection.confidence:.0%} confidence",
        "telemetry": telemetry,
        "analysis": {
            "heuristic": {
                "crash_type": detection.crash_type.value,
                "severity": detection.severity.value,
                "confidence": detection.confidence,
                "evidence": detection.evidence,
                "signals": detection.signals,
            },
            "timeline": timeline,
            "ai_analyzed": False,
        },
    }

    # Save heuristic result immediately
    save_report(report)
    log.info("Heuristic report saved: %s", report_id)

    # AI analysis
    if cfg.anthropic_api_key:
        log.info("Running AI analysis (model: %s)...", cfg.claude_model)
        try:
            ai_result = await asyncio.wait_for(
                analyze_crash(
                    telemetry=telemetry,
                    detection_result={
                        "crash_type": detection.crash_type.value,
                        "severity": detection.severity.value,
                        "confidence": detection.confidence,
                        "evidence": detection.evidence,
                    },
                    timeline=timeline,
                ),
                timeout=cfg.analysis_timeout,
            )
            # Merge AI results into analysis
            report["analysis"].update(ai_result)
            report["analysis"]["ai_analyzed"] = ai_result.get("ai_analyzed", True)
            # Update stored report with AI analysis
            update_analysis(report_id, report["analysis"])
            log.info("AI analysis complete. Root cause: %s", ai_result.get("root_cause", "N/A"))
        except asyncio.TimeoutError:
            log.warning("AI analysis timed out after %ds", cfg.analysis_timeout)
    else:
        log.warning("No Anthropic API key — skipping AI analysis. Set CRASHPILOT_ANTHROPIC_API_KEY.")

    set_meta("last_analyzed_boot", prev_boot_id)
    return report
