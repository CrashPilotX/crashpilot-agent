"""
Cloud push — sends heartbeats and crash reports directly to Supabase.

Used in push mode (no public URL required). The agent authenticates using
the system_id + supabase_token pair, validated server-side by SECURITY DEFINER
RPCs in Postgres (so the anon key is safe to use here).
"""

from __future__ import annotations

import importlib.metadata
import logging
import socket
from typing import Any

import httpx

log = logging.getLogger(__name__)

_TIMEOUT = httpx.Timeout(10.0)


def _headers(anon_key: str) -> dict[str, str]:
    return {
        "apikey": anon_key,
        "Authorization": f"Bearer {anon_key}",
        "Content-Type": "application/json",
    }


def _agent_version() -> str:
    try:
        return importlib.metadata.version("crashpilot")
    except Exception:
        return "0.1.0"


def _hostname() -> str | None:
    try:
        return socket.gethostname()
    except Exception:
        return None


def _explain_http_error(exc: httpx.HTTPStatusError) -> str:
    """Turn a Supabase REST error into an actionable message.

    Supabase returns useful JSON in the body (code/message/hint) that
    raise_for_status() drops — surface it so the user can self-diagnose.
    """
    resp = exc.response
    body = (resp.text or "").strip()
    status = resp.status_code

    # Missing RPC → the push-mode schema was never applied to this database.
    if status == 404 or "Could not find the function" in body or "PGRST202" in body:
        return (
            f"HTTP {status}: the agent_heartbeat function does not exist on your "
            f"Supabase project. Run supabase/schema.sql in the Supabase SQL editor "
            f"to create the push-mode tables and RPCs.\n  Server said: {body}"
        )
    if status in (401, 403):
        return (
            f"HTTP {status}: Supabase rejected the request — check that "
            f"CRASHPILOT_SUPABASE_ANON_KEY in /etc/crashpilot/.env matches your "
            f"project's anon key.\n  Server said: {body}"
        )
    if status == 400 and ("Invalid system_id or agent_token" in body or "P0001" in body):
        return (
            f"HTTP {status}: this system_id / agent_token pair is not in the "
            f"systems table. Re-run `sudo crashpilot configure cpilot_...` with a "
            f"fresh connection string from the dashboard.\n  Server said: {body}"
        )
    return f"HTTP {status} from Supabase.\n  Server said: {body}"


async def push_heartbeat(
    supabase_url: str,
    anon_key: str,
    system_id: str,
    agent_token: str,
) -> None:
    """UPSERT a heartbeat row via the agent_heartbeat RPC."""
    url = f"{supabase_url.rstrip('/')}/rest/v1/rpc/agent_heartbeat"
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(
            url,
            headers=_headers(anon_key),
            json={
                "p_system_id": system_id,
                "p_agent_token": agent_token,
                "p_hostname": _hostname(),
                "p_version": _agent_version(),
            },
        )
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise RuntimeError(_explain_http_error(exc)) from exc
    log.debug("Heartbeat sent for system %s", system_id)


# Caps for what we send to the cloud — enough to be useful, small enough for JSONB.
_DMESG_TAIL_CHARS = 8000
_JOURNAL_SNIPPET_CHARS = 4000
_MAX_CRITICAL_EVENTS = 80


def _build_telemetry_summary(telemetry: dict[str, Any]) -> dict[str, Any]:
    """Trim the raw telemetry down to what the dashboard shows (kernel log etc.)."""
    dmesg = telemetry.get("dmesg") or {}
    journal = telemetry.get("journal") or {}
    return {
        "dmesg": {
            "critical_events": (dmesg.get("critical_events") or [])[:_MAX_CRITICAL_EVENTS],
            "critical_count": dmesg.get("critical_count", 0),
            # Keep the tail (most recent lines) — that's where the crash shows up.
            "tail": (dmesg.get("full_tail") or "")[-_DMESG_TAIL_CHARS:],
            "mce_events": (dmesg.get("mce_events") or "")[:_JOURNAL_SNIPPET_CHARS],
        },
        "journal": {
            "oom_events": (journal.get("oom_events") or "")[:_JOURNAL_SNIPPET_CHARS],
            "previous_boot_errors": (journal.get("previous_boot_errors") or "")[-_DMESG_TAIL_CHARS:],
        },
    }


async def push_report(
    supabase_url: str,
    anon_key: str,
    system_id: str,
    agent_token: str,
    report: dict[str, Any],
) -> str | None:
    """Push a crash report to Supabase via the agent_push_report RPC.

    Strips the raw telemetry blob (too large) before sending.
    Returns the report id on success, None on failure.
    """
    # The raw telemetry blob is too large for Supabase, but the dashboard still
    # needs the kernel log to show "why". Build a trimmed summary (dmesg + key
    # journal snippets) before dropping the full telemetry.
    telemetry_summary = _build_telemetry_summary(report.get("telemetry") or {})

    cloud_report = {k: v for k, v in report.items() if k != "telemetry"}
    cloud_report["telemetry_summary"] = telemetry_summary

    # The agent_push_report RPC reads these from the TOP level, but they live
    # inside `analysis`. Lift them so the cloud row (and the dashboard's "AI
    # analyzed" badge/stat + severity) reflects the analysis, not just heuristics.
    analysis = report.get("analysis") or {}
    cloud_report["ai_analyzed"] = bool(analysis.get("ai_analyzed", False))
    if analysis.get("severity"):
        cloud_report["severity"] = analysis["severity"]
    if analysis.get("crash_type"):
        cloud_report["crash_type"] = analysis["crash_type"]

    url = f"{supabase_url.rstrip('/')}/rest/v1/rpc/agent_push_report"
    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
        resp = await client.post(
            url,
            headers=_headers(anon_key),
            json={
                "p_system_id": system_id,
                "p_agent_token": agent_token,
                "p_report": cloud_report,
            },
        )
        resp.raise_for_status()

    report_id = report.get("id")
    log.info("Report %s pushed to Supabase cloud", report_id)
    return report_id
