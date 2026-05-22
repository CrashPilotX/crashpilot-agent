"""
Crash monitor — platform-aware orchestrator.

On boot it:
  1. Detects the platform (bare metal / WSL / Docker / k8s / VM / cloud)
  2. Selects the appropriate collectors for that platform
  3. Runs them in parallel
  4. Runs heuristic crash detection
  5. Calls Claude AI for root-cause analysis
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
from .analyzers.timeline import build_timeline
from .collectors.platform import PlatformType, detect_platform
from .collectors.dmesg import DmesgCollector
from .collectors.docker import DockerCollector
from .collectors.gpu import GpuCollector
from .collectors.journal import JournalCollector
from .collectors.kubernetes import KubernetesCollector
from .collectors.smart import SmartCollector
from .collectors.system import SystemCollector
from .collectors.thermal import ThermalCollector
from .collectors.vm import VmCollector
from .collectors.wsl import WslCollector
from .config import get_settings
from .storage.store import cleanup_old_reports, get_meta, init_db, save_report, set_meta, update_analysis

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

    # Build collector list based on platform capabilities
    collectors = []

    # System-level collectors (available almost everywhere)
    collectors.append(SystemCollector())

    # WSL
    if ptype in (PlatformType.WSL1, PlatformType.WSL2):
        collectors.append(WslCollector())
        if ptype == PlatformType.WSL2:
            # WSL2 has a real (Microsoft) kernel — dmesg works
            collectors.append(DmesgCollector())
        # Journal available if systemd enabled in wsl.conf
        if platform_info.init.value == "systemd":
            collectors.append(JournalCollector())

    # Kubernetes pod (DaemonSet on node)
    elif ptype == PlatformType.KUBERNETES:
        collectors.append(KubernetesCollector())
        # Also collect host-level telemetry if we have hostPID + privileged
        if _has_host_access():
            collectors.extend([
                JournalCollector(),
                DmesgCollector(),
            ])

    # Docker container
    elif ptype == PlatformType.DOCKER:
        # Limited host access from inside a container — try anyway
        if _has_host_access():
            collectors.extend([
                JournalCollector(),
                DmesgCollector(),
                DockerCollector(),
            ])
        else:
            log.warning(
                "Running in Docker without host access (--privileged not set). "
                "Mount /var/log and /run/log/journal for full telemetry."
            )

    # Bare metal or VMs/Cloud — full telemetry
    else:
        collectors.extend([
            JournalCollector(),
            DmesgCollector(),
            SmartCollector(),
            GpuCollector(),
            ThermalCollector(),
            DockerCollector(),
        ])
        # VM-specific collector
        if platform_info.is_virtual or platform_info.is_cloud:
            collectors.append(VmCollector(
                hypervisor=platform_info.hypervisor or "",
                cloud=platform_info.cloud_provider or "",
            ))

    # Run all collectors in parallel
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

    # Always include platform info
    telemetry["platform"] = {
        "type": ptype.value,
        "distro": platform_info.distro.value,
        "distro_version": platform_info.distro_version,
        "init": platform_info.init.value,
        "kernel": platform_info.kernel,
        "arch": platform_info.arch,
        "hostname": platform_info.hostname,
        "is_container": platform_info.is_container,
        "is_virtual": platform_info.is_virtual,
        "is_cloud": platform_info.is_cloud,
        "hypervisor": platform_info.hypervisor,
        "cloud_provider": platform_info.cloud_provider,
        "wsl_version": platform_info.wsl_version,
        "k8s_node": platform_info.k8s_node,
    }
    telemetry["collected_at"] = datetime.now(timezone.utc).isoformat()

    return telemetry


def _has_host_access() -> bool:
    """Detect if we have privileged host access from inside a container."""
    import os
    # Running as root with access to /proc/1/exe (host init)
    try:
        os.readlink("/proc/1/exe")
        return True
    except (OSError, PermissionError):
        return False


async def check_and_analyze(force: bool = False) -> dict | None:
    """
    Main entry point: detect crash, collect telemetry, run AI analysis.
    Returns the crash report dict or None if no crash detected.
    """
    cfg = get_settings()
    init_db()

    log.info("Collecting platform telemetry...")
    telemetry = await collect_telemetry()

    platform = telemetry.get("platform", {})
    ptype = platform.get("type", "unknown")

    # Determine boot context — differs by platform
    boot_id, prev_boot_id, crash_time = _extract_boot_context(telemetry)

    # Skip re-analysis unless forced
    last_analyzed = get_meta("last_analyzed_boot")
    if last_analyzed == boot_id and not force:
        log.info("Boot %s already analyzed — skipping.", boot_id)
        return None

    log.info("Running crash detection...")
    detection = detect_crash_type(telemetry)

    # Skip clean shutdowns
    if detection.crash_type == CrashType.CLEAN_SHUTDOWN and not force:
        log.info("Previous shutdown was clean — no report needed.")
        set_meta("last_analyzed_boot", boot_id)
        return None

    log.info(
        "Detected: %s | severity: %s | confidence: %.0f%%",
        detection.crash_type.value,
        detection.severity.value,
        detection.confidence * 100,
    )

    # Build timeline
    timeline = build_timeline(telemetry)

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
            "ai_analyzed": False,
        },
    }

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
                        "platform": ptype,
                    },
                    timeline=timeline,
                ),
                timeout=cfg.analysis_timeout,
            )
            report["analysis"].update(ai_result)
            report["analysis"]["ai_analyzed"] = ai_result.get("ai_analyzed", True)
            update_analysis(report_id, report["analysis"])
            log.info("AI analysis complete: %s", ai_result.get("root_cause", "N/A"))
        except asyncio.TimeoutError:
            log.warning("AI analysis timed out after %ds", cfg.analysis_timeout)
    else:
        log.warning("No API key — set CRASHPILOT_ANTHROPIC_API_KEY for AI analysis.")

    set_meta("last_analyzed_boot", boot_id)

    # Purge reports older than max_report_age_days
    if cfg.max_report_age_days > 0:
        removed = cleanup_old_reports(cfg.max_report_age_days)
        if removed:
            log.info("Pruned %d report(s) older than %d days", removed, cfg.max_report_age_days)

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

    # Kubernetes: use pod UID as boot_id proxy
    k8s = telemetry.get("kubernetes", {})
    if k8s.get("available") and not boots:
        pod_id = telemetry.get("platform", {}).get("container_id") or "k8s-pod"
        return pod_id, None, None

    return current, previous, crash_time
