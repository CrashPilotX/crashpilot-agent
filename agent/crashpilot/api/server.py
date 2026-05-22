"""Local FastAPI server — serves crash reports to the web dashboard."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from ..config import get_settings
from ..storage.store import get_report, init_db, list_reports

log = logging.getLogger(__name__)

app = FastAPI(
    title="CrashPilot Agent API",
    description="Local crash forensics API",
    version="0.1.0",
    docs_url="/docs",
)

cfg = get_settings()

app.add_middleware(
    CORSMiddleware,
    allow_origins=cfg.api_cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def startup():
    init_db()
    log.info("CrashPilot API server started on %s:%d", cfg.api_host, cfg.api_port)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "version": "0.1.0"}


@app.get("/api/v1/reports")
async def list_crash_reports(limit: int = 20) -> dict[str, Any]:
    """List recent crash reports (summary only, no raw telemetry)."""
    reports = list_reports(limit=min(limit, 100))
    summaries = []
    for r in reports:
        analysis = r.get("analysis") or {}
        summaries.append({
            "id": r["id"],
            "boot_id": r["boot_id"],
            "detected_at": r["detected_at"],
            "crash_time": r.get("crash_time"),
            "crash_type": r["crash_type"],
            "severity": r["severity"],
            "summary": r.get("summary"),
            "ai_analyzed": analysis.get("ai_analyzed", False),
            "confidence": analysis.get("confidence") or analysis.get("heuristic", {}).get("confidence"),
            "root_cause": analysis.get("root_cause"),
        })
    return {"reports": summaries, "total": len(summaries)}


@app.get("/api/v1/reports/{report_id}")
async def get_crash_report(report_id: str) -> dict[str, Any]:
    """Get full crash report including telemetry and AI analysis."""
    report = get_report(report_id)
    if report is None:
        raise HTTPException(status_code=404, detail=f"Report {report_id!r} not found")

    # Sanitize telemetry — remove raw log blobs from the API response
    # (keep analysis, smart, gpu, thermal summaries)
    tel = report.get("telemetry", {})
    safe_telemetry = {
        "collected_at": tel.get("collected_at"),
        "system": {
            k: v for k, v in tel.get("system", {}).items()
            if k != "top_processes_at_crash"
        },
        "smart": {
            k: v for k, v in tel.get("smart", {}).items()
            if k != "disks"  # keep critical_disks summary
        },
        "smart_critical": tel.get("smart", {}).get("critical_disks", []),
        "gpu": tel.get("gpu", {}),
        "thermal": {
            "thermal_warnings": tel.get("thermal", {}).get("thermal_warnings", []),
            "sysfs_temperatures": tel.get("thermal", {}).get("sysfs_temperatures", [])[:20],
        },
        "docker": {
            k: v for k, v in tel.get("docker", {}).items()
            if k != "daemon_logs_tail"
        },
        "journal_summary": {
            "boots": tel.get("journal", {}).get("boots", []),
            "coredumps": tel.get("journal", {}).get("coredumps", []),
            "oom_events_snippet": tel.get("journal", {}).get("oom_events", "")[:2000],
        },
        "dmesg_summary": {
            "critical_events": tel.get("dmesg", {}).get("critical_events", [])[:50],
            "critical_count": tel.get("dmesg", {}).get("critical_count", 0),
        },
    }

    return {
        "id": report["id"],
        "boot_id": report["boot_id"],
        "detected_at": report["detected_at"],
        "crash_time": report.get("crash_time"),
        "crash_type": report["crash_type"],
        "severity": report["severity"],
        "analysis": report.get("analysis"),
        "telemetry_summary": safe_telemetry,
        "created_at": report.get("created_at"),
    }


@app.get("/api/v1/status")
async def get_status() -> dict[str, Any]:
    """Agent status and configuration summary."""
    from ..storage.store import get_meta
    reports = list_reports(limit=5)
    return {
        "agent_version": "0.1.0",
        "reports_count": len(list_reports(limit=1000)),
        "last_analyzed_boot": get_meta("last_analyzed_boot"),
        "recent_crashes": [
            {
                "id": r["id"],
                "crash_type": r["crash_type"],
                "detected_at": r["detected_at"],
                "severity": r["severity"],
            }
            for r in reports
        ],
        "api_key_configured": bool(cfg.anthropic_api_key),
        "claude_model": cfg.claude_model,
        "data_dir": str(cfg.data_dir),
    }
