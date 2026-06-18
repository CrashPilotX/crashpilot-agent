"""Outbound incident notifications."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

import httpx


async def notify_webhook(webhook_url: str, report: dict[str, Any]) -> bool:
    if not webhook_url:
        return False
    parsed = urlparse(webhook_url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError("CRASHPILOT_WEBHOOK_URL must be an HTTPS URL")
    analysis = report.get("analysis") or {}
    payload = {
        "event": "crashpilot.incident.created",
        "incident": {
            "id": report.get("id"),
            "crash_type": report.get("crash_type"),
            "severity": report.get("severity"),
            "detected_at": report.get("detected_at"),
            "crash_time": report.get("crash_time"),
            "summary": analysis.get("root_cause") or report.get("summary"),
            "confidence": analysis.get("confidence")
            or (analysis.get("heuristic") or {}).get("confidence"),
            "fingerprint": (analysis.get("forensic_snapshot") or {}).get("fingerprint"),
            "recommended_action": (
                (analysis.get("remediation") or [{}])[0].get("action")
            ),
        },
    }
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.post(webhook_url, json=payload)
        response.raise_for_status()
    return True
