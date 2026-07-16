"""Tests for outbound incident webhooks."""

from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

import httpx
import pytest
import respx

from crashpilot.notifications import build_webhook_payload, notify_webhook


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    db_path = tmp_path / "notifications.db"
    monkeypatch.setattr(
        "crashpilot.storage.store.get_settings",
        lambda: SimpleNamespace(db_path=db_path),
    )
    return db_path


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
        assert await notify_webhook(url, report, secret="signing-secret") is True
        payload = json.loads(route.calls[0].request.content)
        headers = route.calls[0].request.headers

    assert payload["event"] == "crashpilot.incident.created"
    assert payload["incident"]["fingerprint"] == "abc123"
    assert payload["incident"]["recommended_action"] == "Set MemoryMax"
    assert headers["x-crashpilot-signature"].startswith("sha256=")
    assert headers["x-crashpilot-delivery"]
    assert headers["x-crashpilot-timestamp"]


@pytest.mark.asyncio
async def test_webhook_failure_is_retained_for_retry(isolated_db):
    url = "https://hooks.example.com/crashpilot"
    with respx.mock:
        respx.post(url).mock(return_value=httpx.Response(503))
        assert await notify_webhook(url, {"id": "retry-me"}) is False

    with sqlite3.connect(isolated_db) as con:
        attempts, delivered_at, last_error = con.execute(
            "SELECT attempts, delivered_at, last_error FROM webhook_deliveries"
        ).fetchone()
    assert attempts == 1
    assert delivered_at is None
    assert "503" in last_error


@pytest.mark.asyncio
async def test_webhook_rejects_non_https_urls():
    with pytest.raises(ValueError, match="HTTPS"):
        await notify_webhook("http://hooks.example.com/crashpilot", {})


@pytest.mark.asyncio
async def test_empty_webhook_is_disabled():
    assert await notify_webhook("", {}) is False


def test_webhook_payload_tolerates_malformed_analysis_shapes():
    payload = build_webhook_payload({
        "id": "weird-analysis",
        "summary": "fallback summary",
        "analysis": {
            "heuristic": "not-a-dict",
            "forensic_snapshot": "not-a-dict",
            "remediation": ["not-a-dict"],
        },
    })

    assert payload["incident"]["summary"] == "fallback summary"
    assert payload["incident"]["confidence"] is None
    assert payload["incident"]["fingerprint"] is None
    assert payload["incident"]["recommended_action"] is None
