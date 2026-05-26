"""
Cloud push — sends heartbeats and crash reports directly to Supabase.

Used in push mode (no public URL required). The agent authenticates using
the system_id + supabase_token pair, validated server-side by SECURITY DEFINER
RPCs in Postgres (so the anon key is safe to use here).
"""

from __future__ import annotations

import logging
import socket
import importlib.metadata
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
        resp.raise_for_status()
    log.debug("Heartbeat sent for system %s", system_id)


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
    # Exclude raw telemetry — too large for Supabase; keep structured fields only
    cloud_report = {k: v for k, v in report.items() if k != "telemetry"}

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
