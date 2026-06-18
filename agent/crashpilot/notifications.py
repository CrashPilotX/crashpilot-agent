"""Durable, signed outbound incident notifications."""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse

import httpx

from .storage.store import (
    enqueue_webhook_delivery,
    init_db,
    list_due_webhook_deliveries,
    mark_webhook_delivered,
    mark_webhook_failed,
)

MAX_WEBHOOK_ATTEMPTS = 8


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _validate_url(webhook_url: str) -> None:
    parsed = urlparse(webhook_url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError("CRASHPILOT_WEBHOOK_URL must be an HTTPS URL")


def build_webhook_payload(report: dict[str, Any]) -> dict[str, Any]:
    analysis = report.get("analysis") or {}
    return {
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


def queue_incident_webhook(webhook_url: str, report: dict[str, Any]) -> str | None:
    if not webhook_url:
        return None
    _validate_url(webhook_url)
    init_db()
    now = _now().isoformat()
    return enqueue_webhook_delivery({
        "id": str(uuid.uuid4()),
        "event_type": "crashpilot.incident.created",
        "webhook_url": webhook_url,
        "payload": build_webhook_payload(report),
        "next_attempt_at": now,
        "created_at": now,
    })


def _signature(secret: str, timestamp: str, body: bytes) -> str:
    digest = hmac.new(
        secret.encode("utf-8"),
        timestamp.encode("utf-8") + b"." + body,
        hashlib.sha256,
    ).hexdigest()
    return f"sha256={digest}"


async def flush_webhook_deliveries(secret: str = "", limit: int = 20) -> dict[str, int]:
    delivered = 0
    failed = 0
    async with httpx.AsyncClient(timeout=10) as client:
        for delivery in list_due_webhook_deliveries(limit=limit):
            timestamp = str(int(_now().timestamp()))
            body = json.dumps(
                delivery["payload"],
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
            headers = {
                "Content-Type": "application/json",
                "X-CrashPilot-Event": delivery["event_type"],
                "X-CrashPilot-Delivery": delivery["id"],
                "X-CrashPilot-Timestamp": timestamp,
            }
            if secret:
                headers["X-CrashPilot-Signature"] = _signature(secret, timestamp, body)
            try:
                response = await client.post(
                    delivery["webhook_url"],
                    content=body,
                    headers=headers,
                )
                response.raise_for_status()
                mark_webhook_delivered(delivery["id"], _now().isoformat())
                delivered += 1
            except Exception as exc:
                attempts = int(delivery["attempts"]) + 1
                delay_minutes = min(2 ** max(attempts - 1, 0), 12 * 60)
                if attempts >= MAX_WEBHOOK_ATTEMPTS:
                    delay_minutes = 24 * 60
                mark_webhook_failed(
                    delivery["id"],
                    attempts=attempts,
                    next_attempt_at=(_now() + timedelta(minutes=delay_minutes)).isoformat(),
                    error=str(exc),
                )
                failed += 1
    return {"delivered": delivered, "failed": failed}


async def notify_webhook(
    webhook_url: str,
    report: dict[str, Any],
    *,
    secret: str = "",
) -> bool:
    """Compatibility helper: queue and immediately attempt delivery."""
    delivery_id = queue_incident_webhook(webhook_url, report)
    if not delivery_id:
        return False
    result = await flush_webhook_deliveries(secret=secret)
    return result["delivered"] > 0
