"""Local FastAPI server: serves crash reports to the web dashboard."""

from __future__ import annotations

import logging
import secrets
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from ..config import get_settings
from ..storage.store import (
    cleanup_old_reports,
    count_reports,
    delete_report,
    get_report,
    init_db,
    list_reports,
)

log = logging.getLogger(__name__)
_bearer = HTTPBearer(auto_error=False)


# ---------------------------------------------------------------------------
# Token helpers
# ---------------------------------------------------------------------------

def _token_file():
    return get_settings().data_dir / "agent.token"  # type: ignore[operator]


def get_agent_token() -> str:
    """Return the persistent API token, creating it on first call."""
    tf = _token_file()
    if tf.exists():
        return tf.read_text().strip()
    token = secrets.token_urlsafe(32)
    tf.write_text(token)
    tf.chmod(0o600)
    log.info("Generated new API token: run `sudo crashpilot token` to view it")
    return token


async def _require_token(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> str:
    """FastAPI dependency: validates the Bearer token on every protected route."""
    expected = get_agent_token()
    if credentials is None or not secrets.compare_digest(credentials.credentials, expected):
        raise HTTPException(
            status_code=401,
            detail="Missing or invalid token. Run `sudo crashpilot token` on the server to get it.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return credentials.credentials


# ---------------------------------------------------------------------------
# App lifecycle
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg = get_settings()
    init_db()
    # Eagerly create the token so it's ready before first request
    get_agent_token()
    log.info("CrashPilot API server started on %s:%d", cfg.api_host, cfg.api_port)
    yield
    if cfg.max_report_age_days > 0:
        deleted = cleanup_old_reports(cfg.max_report_age_days)
        if deleted:
            log.info(
                "Cleaned up %d reports older than %d days", deleted, cfg.max_report_age_days
            )


app = FastAPI(
    title="CrashPilot Agent API",
    description="Local crash forensics API",
    version="0.2.0",
    docs_url="/docs",
    lifespan=lifespan,
)

cfg = get_settings()
allow_credentials = "*" not in cfg.api_cors_origins
app.add_middleware(
    CORSMiddleware,
    allow_origins=cfg.api_cors_origins,
    allow_credentials=allow_credentials,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Public endpoints (no auth)
# ---------------------------------------------------------------------------

@app.get("/health")
async def health() -> dict:
    """Unauthenticated: used by the dashboard to check reachability."""
    return {"status": "ok", "version": "0.2.0"}


# ---------------------------------------------------------------------------
# Protected endpoints (Bearer token required)
# ---------------------------------------------------------------------------

@app.get("/api/v1/reports")
def list_crash_reports(
    limit: int = Query(default=20, ge=1, le=100),
    _: str = Depends(_require_token),
) -> dict[str, Any]:
    """List recent crash reports (summary only, no raw telemetry)."""
    reports = list_reports(limit=limit)
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
            "confidence": (
                analysis.get("confidence")
                or analysis.get("heuristic", {}).get("confidence")
            ),
            "root_cause": analysis.get("root_cause"),
        })
    return {"reports": summaries, "total": len(summaries)}


@app.get("/api/v1/reports/{report_id}")
def get_crash_report(
    report_id: str,
    _: str = Depends(_require_token),
) -> dict[str, Any]:
    """Get full crash report including telemetry and AI analysis."""
    report = get_report(report_id)
    if report is None:
        raise HTTPException(status_code=404, detail=f"Report {report_id!r} not found")

    tel = report.get("telemetry", {})
    safe_telemetry = {
        "collected_at": tel.get("collected_at"),
        "system": {
            k: v for k, v in tel.get("system", {}).items()
            if k != "top_processes_at_crash"
        },
        "smart": {k: v for k, v in tel.get("smart", {}).items() if k != "disks"},
        "smart_critical": tel.get("smart", {}).get("critical_disks", []),
        "gpu": tel.get("gpu", {}),
        "thermal": {
            "thermal_warnings": tel.get("thermal", {}).get("thermal_warnings", []),
            "sysfs_temperatures": tel.get("thermal", {}).get("sysfs_temperatures", [])[:20],
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


@app.delete("/api/v1/reports/{report_id}")
def delete_crash_report(
    report_id: str,
    _: str = Depends(_require_token),
) -> Response:
    """Delete a crash report by ID. Returns 204 No Content on success."""
    if not delete_report(report_id):
        raise HTTPException(status_code=404, detail=f"Report {report_id!r} not found")
    return Response(status_code=204)


@app.get("/api/v1/status")
def get_status(_: str = Depends(_require_token)) -> dict[str, Any]:
    """Agent status and configuration summary."""
    from ..storage.store import get_meta
    cfg = get_settings()
    reports = list_reports(limit=5)
    return {
        "agent_version": "0.2.0",
        "reports_count": count_reports(),
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
