"""
Crash monitor - Ubuntu Linux / Ubuntu WSL orchestrator.

On boot it:
  1. Detects the platform (Ubuntu Linux or Ubuntu on WSL)
  2. Selects the appropriate collectors for that platform
  3. Runs them in parallel
  4. Runs heuristic crash detection
  5. Calls Claude AI or built-in heuristic advice for root-cause analysis
  6. Stores the report in SQLite
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import datetime, timezone
from typing import Any

from .analyzers.ai_analyzer import analyze_crash
from .analyzers.crash_detector import CrashType, detect_crash_type
from .analyzers.forensic_snapshot import build_forensic_snapshot
from .analyzers.timeline import build_timeline
from .collectors.dmesg import DmesgCollector
from .collectors.gpu import GpuCollector
from .collectors.journal import JournalCollector
from .collectors.platform import PlatformType, detect_platform
from .collectors.smart import SmartCollector
from .collectors.system import SystemCollector
from .collectors.thermal import ThermalCollector
from .collectors.wsl import WslCollector
from .config import get_settings
from .storage.store import (
    cleanup_old_reports,
    get_meta,
    init_db,
    mark_pushed,
    save_report,
    set_meta,
    update_analysis,
)

log = logging.getLogger(__name__)


def _make_report_id(boot_id: str) -> str:
    h = hashlib.sha256(boot_id.encode()).hexdigest()[:12]
    return f"crash_{h}"


async def collect_telemetry() -> dict[str, Any]:
    """
    Detect the platform first, then select and run appropriate collectors.
    Returns the combined telemetry dict.
    """
    log.info("Detecting platform...")
    platform_info = await detect_platform()
    ptype = platform_info.platform

    log.info(
        "Platform: %s | Distro: %s %s | Init: %s",
        ptype.value,
        platform_info.distro.value,
        platform_info.distro_version,
        platform_info.init.value,
    )

    collectors = [SystemCollector()]

    if ptype in (PlatformType.WSL1, PlatformType.WSL2):
        collectors.append(WslCollector())
        if ptype == PlatformType.WSL2:
            collectors.append(DmesgCollector())
        if platform_info.init.value == "systemd":
            collectors.append(JournalCollector())
    else:
        collectors.extend([
            JournalCollector(),
            DmesgCollector(),
            SmartCollector(),
            GpuCollector(),
            ThermalCollector(),
        ])

    log.info("Running %d collectors...", len(collectors))
    results = await asyncio.gather(
        *[c.safe_collect() for c in collectors], return_exceptions=True
    )

    telemetry: dict[str, Any] = {}
    for collector, result in zip(collectors, results):
        if isinstance(result, Exception):
            telemetry[collector.name] = {"error": str(result), "collector": collector.name}
        else:
            telemetry[collector.name] = result

    telemetry["platform"] = {
        "type": ptype.value,
        "distro": platform_info.distro.value,
        "distro_version": platform_info.distro_version,
        "init": platform_info.init.value,
        "kernel": platform_info.kernel,
        "arch": platform_info.arch,
        "hostname": platform_info.hostname,
        "is_virtual": platform_info.is_virtual,
        "hypervisor": platform_info.hypervisor,
        "wsl_version": platform_info.wsl_version,
        "supported": platform_info.supported,
        "support_note": platform_info.support_note,
    }
    telemetry["collected_at"] = datetime.now(timezone.utc).isoformat()

    return telemetry


async def check_and_analyze(force: bool = False) -> dict | None:
    """
    Main entry point: detect crash, collect telemetry, run analysis.
    Returns the crash report dict or None if no crash detected.
    """
    cfg = get_settings()
    init_db()

    log.info("Collecting platform telemetry...")
    telemetry = await collect_telemetry()

    platform = telemetry.get("platform", {})
    ptype = platform.get("type", "unknown")

    boot_id, prev_boot_id, crash_time = _extract_boot_context(telemetry)

    last_analyzed = get_meta("last_analyzed_boot")
    if last_analyzed == boot_id and not force:
        log.info("Boot %s already analyzed - skipping.", boot_id)
        return None

    log.info("Running crash detection...")
    detection = detect_crash_type(telemetry)

    if detection.crash_type == CrashType.CLEAN_SHUTDOWN and not force:
        log.info("Previous shutdown was clean - no report needed.")
        set_meta("last_analyzed_boot", boot_id)
        return None

    log.info(
        "Detected: %s | severity: %s | confidence: %.0f%%",
        detection.crash_type.value,
        detection.severity.value,
        detection.confidence * 100,
    )

    try:
        from .flight_recorder import summarize_window

        telemetry["flight_recorder"] = summarize_window(hours=1)
    except Exception as exc:
        telemetry["flight_recorder"] = {"error": str(exc)}

    timeline = build_timeline(telemetry)
    detection_payload = {
        "crash_type": detection.crash_type.value,
        "severity": detection.severity.value,
        "confidence": detection.confidence,
        "evidence": detection.evidence,
        "signals": detection.signals,
        "platform": ptype,
    }
    forensic_snapshot = build_forensic_snapshot(telemetry, detection_payload, timeline)
    report_id = _make_report_id(boot_id)
    report = {
        "id": report_id,
        "boot_id": boot_id,
        "detected_at": datetime.now(timezone.utc).isoformat(),
        "crash_time": crash_time,
        "crash_type": detection.crash_type.value,
        "severity": detection.severity.value,
        "platform": ptype,
        "summary": (
            f"[{ptype}] Detected {detection.crash_type.value} "
            f"with {detection.confidence:.0%} confidence"
        ),
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
            "forensic_snapshot": forensic_snapshot,
            "ai_analyzed": False,
        },
    }

    save_report(report)
    log.info("Heuristic report saved: %s", report_id)

    if cfg.anthropic_api_key:
        log.info("Running AI analysis (model: %s)...", cfg.claude_model)
    else:
        log.warning(
            "No API key - using built-in heuristic advice. "
            "Set CRASHPILOT_ANTHROPIC_API_KEY for AI analysis."
        )

    try:
        analysis_result = await asyncio.wait_for(
            analyze_crash(
                telemetry=telemetry,
                detection_result=detection_payload,
                timeline=timeline,
            ),
            timeout=cfg.analysis_timeout,
        )
        report["analysis"].update(analysis_result)
        report["analysis"]["ai_analyzed"] = analysis_result.get("ai_analyzed", False)
        update_analysis(report_id, report["analysis"])
        log.info("Analysis complete: %s", analysis_result.get("root_cause", "N/A"))
    except asyncio.TimeoutError:
        log.warning("Analysis timed out after %ds", cfg.analysis_timeout)

    set_meta("last_analyzed_boot", boot_id)

    if cfg.max_report_age_days > 0:
        removed = cleanup_old_reports(cfg.max_report_age_days)
        if removed:
            log.info("Pruned %d report(s) older than %d days", removed, cfg.max_report_age_days)

    cloud_suppressed = False
    if cfg.supabase_url and cfg.supabase_system_id and cfg.supabase_token:
        try:
            from .cloud_push import push_report as cloud_push_report

            cloud_result = await cloud_push_report(
                supabase_url=cfg.supabase_url,
                anon_key=cfg.supabase_anon_key,
                system_id=cfg.supabase_system_id,
                agent_token=cfg.supabase_token,
                report=report,
            )
            mark_pushed(report_id)
            cloud_suppressed = bool(
                cloud_result and cloud_result.startswith("suppressed:")
            )
        except Exception as exc:
            log.warning(
                "Could not push report to Supabase (will retry on next heartbeat): %s",
                exc,
            )

    if cfg.webhook_url and not cloud_suppressed:
        try:
            from .notifications import flush_webhook_deliveries, queue_incident_webhook

            maintenance_until = get_meta("maintenance_until") or cfg.maintenance_until
            in_maintenance = False
            if maintenance_until:
                try:
                    until = datetime.fromisoformat(
                        maintenance_until.replace("Z", "+00:00")
                    )
                    in_maintenance = until > datetime.now(timezone.utc)
                except ValueError:
                    log.warning("Invalid maintenance timestamp: %s", maintenance_until)
            if not in_maintenance:
                queue_incident_webhook(cfg.webhook_url, report)
                await flush_webhook_deliveries(secret=cfg.webhook_secret)
            else:
                log.info("Webhook suppressed by maintenance window until %s", maintenance_until)
        except Exception as exc:
            log.warning("Could not queue/deliver incident webhook: %s", exc)

    return report


def _extract_boot_context(
    telemetry: dict,
) -> tuple[str, str | None, str | None]:
    """Extract (current_boot_id, previous_boot_id, crash_time) from telemetry."""
    journal = telemetry.get("journal", {})
    boots = journal.get("boots", [])

    current = journal.get("current_boot_id") or (boots[0]["boot_id"] if boots else "unknown")
    previous = journal.get("previous_boot_id") or (boots[1]["boot_id"] if len(boots) > 1 else None)
    crash_time = boots[1].get("last_entry") if len(boots) > 1 else None

    return current, previous, crash_time
