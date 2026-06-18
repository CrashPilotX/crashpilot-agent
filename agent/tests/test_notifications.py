"""Tests for outbound incident webhooks."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from crashpilot.notifications import notify_webhook


@pytest.mark.asyncio
async def test_webhook_posts_compact_incident_payload():
    url = "https://hooks.example.com/crashpilot"
    report = {
        "id": "crash_123",
        "crash_type": "oom_kill",
        "severity": "high",
        "detected_at": "2026-06-18T00:00:00Z",
        "analysis": {
            "root_cause": "worker exhausted memory",
            "confidence": 0.91,
            "forensic_snapshot": {"fingerprint": "abc123"},
            "remediation": [{"action": "Set MemoryMax"}],
        },
    }
    with respx.mock:
        route = respx.post(url).mock(return_value=httpx.Response(204))
        assert await notify_webhook(url, report) is True
        payload = json.loads(route.calls[0].request.content)

    assert payload["event"] == "crashpilot.incident.created"
    assert payload["incident"]["fingerprint"] == "abc123"
    assert payload["incident"]["recommended_action"] == "Set MemoryMax"


@pytest.mark.asyncio
async def test_webhook_rejects_non_https_urls():
    with pytest.raises(ValueError, match="HTTPS"):
        await notify_webhook("http://hooks.example.com/crashpilot", {})


@pytest.mark.asyncio
async def test_empty_webhook_is_disabled():
    assert await notify_webhook("", {}) is False
