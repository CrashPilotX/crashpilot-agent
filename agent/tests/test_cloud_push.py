"""Tests for cloud_push.py — push heartbeats and reports to Supabase.

Uses respx to mock httpx calls without requiring a real Supabase instance.
asyncio_mode = "auto" is set in pyproject.toml so no @pytest.mark.asyncio needed.
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from crashpilot import cloud_push
from crashpilot.cloud_push import push_heartbeat, push_report

SUPABASE_URL = "https://test.supabase.co"
ANON_KEY = "test-anon-key"
SYSTEM_ID = "11111111-1111-1111-1111-111111111111"
AGENT_TOKEN = "test-agent-token-abc"

HB_URL = f"{SUPABASE_URL}/rest/v1/rpc/agent_heartbeat"
RPT_URL = f"{SUPABASE_URL}/rest/v1/rpc/agent_push_report"

SAMPLE_REPORT: dict = {
    "id": "crash_abc123def456",
    "boot_id": "boot-uuid-here",
    "detected_at": "2026-05-26T10:00:00+00:00",
    "crash_time": "2026-05-26T09:58:00+00:00",
    "crash_type": "oom_kill",
    "severity": "high",
    "summary": "OOM killed process python3",
    "telemetry": {"raw": "this should be stripped"},
    "analysis": {
        "ai_analyzed": True,
        "root_cause": "System ran out of memory",
        "confidence": 0.92,
    },
}


# ── push_heartbeat ────────────────────────────────────────────────────────────

class TestPushHeartbeat:
    async def test_success(self):
        """Heartbeat posts to the correct RPC endpoint."""
        with respx.mock:
            route = respx.post(HB_URL).mock(return_value=httpx.Response(200))
            await push_heartbeat(SUPABASE_URL, ANON_KEY, SYSTEM_ID, AGENT_TOKEN)
            assert route.called

    async def test_correct_payload(self):
        """Heartbeat includes system_id, agent_token, hostname, version, and metrics."""
        with respx.mock:
            route = respx.post(HB_URL).mock(return_value=httpx.Response(200))
            await push_heartbeat(SUPABASE_URL, ANON_KEY, SYSTEM_ID, AGENT_TOKEN)
            payload = json.loads(route.calls[0].request.content)
            assert payload["p_system_id"] == SYSTEM_ID
            assert payload["p_agent_token"] == AGENT_TOKEN
            assert "p_hostname" in payload
            assert "p_version" in payload
            assert "p_metrics" in payload
            assert "cpu" in payload["p_metrics"]
            assert "memory" in payload["p_metrics"]

    async def test_accepts_explicit_metrics(self):
        """Heartbeat can send caller-supplied live metrics."""
        metrics = {"cpu": {"load_pct": 12.5}, "memory": {"used_pct": 42.0}}
        with respx.mock:
            route = respx.post(HB_URL).mock(return_value=httpx.Response(200))
            await push_heartbeat(SUPABASE_URL, ANON_KEY, SYSTEM_ID, AGENT_TOKEN, metrics=metrics)
            payload = json.loads(route.calls[0].request.content)
            assert payload["p_metrics"] == metrics

    async def test_metrics_rpc_falls_back_to_legacy_heartbeat(self):
        """Older Supabase schemas without p_metrics still receive a legacy heartbeat."""
        with respx.mock:
            route = respx.post(HB_URL).mock(
                side_effect=[
                    httpx.Response(404, json={"message": "Could not find function with p_metrics"}),
                    httpx.Response(200),
                ]
            )
            await push_heartbeat(
                SUPABASE_URL,
                ANON_KEY,
                SYSTEM_ID,
                AGENT_TOKEN,
                metrics={"cpu": {"load_pct": 1}},
            )
            assert route.call_count == 2
            first = json.loads(route.calls[0].request.content)
            second = json.loads(route.calls[1].request.content)
            assert "p_metrics" in first
            assert "p_metrics" not in second

    async def test_sends_auth_headers(self):
        """Request must include apikey and Authorization headers."""
        with respx.mock:
            route = respx.post(HB_URL).mock(return_value=httpx.Response(200))
            await push_heartbeat(SUPABASE_URL, ANON_KEY, SYSTEM_ID, AGENT_TOKEN)
            headers = route.calls[0].request.headers
            assert headers["apikey"] == ANON_KEY
            assert headers["authorization"] == f"Bearer {ANON_KEY}"

    async def test_trailing_slash_stripped_from_url(self):
        """Trailing slash in supabase_url must not create a double-slash in the path."""
        with respx.mock:
            route = respx.post(HB_URL).mock(return_value=httpx.Response(200))
            await push_heartbeat(SUPABASE_URL + "/", ANON_KEY, SYSTEM_ID, AGENT_TOKEN)
            assert route.called

    async def test_http_error_raises_with_explanation(self):
        """Non-2xx response should raise a RuntimeError carrying the server body."""
        with respx.mock:
            respx.post(HB_URL).mock(
                return_value=httpx.Response(401, json={"message": "Invalid token"})
            )
            with pytest.raises(RuntimeError) as excinfo:
                await push_heartbeat(SUPABASE_URL, ANON_KEY, SYSTEM_ID, AGENT_TOKEN)
            msg = str(excinfo.value)
            assert "401" in msg
            assert "Invalid token" in msg  # server body is surfaced

    async def test_missing_rpc_error_mentions_schema(self):
        """A 404 / missing-function error should tell the user to run schema.sql."""
        with respx.mock:
            respx.post(HB_URL).mock(
                return_value=httpx.Response(
                    404, json={"message": "Could not find the function public.agent_heartbeat"}
                )
            )
            with pytest.raises(RuntimeError) as excinfo:
                await push_heartbeat(SUPABASE_URL, ANON_KEY, SYSTEM_ID, AGENT_TOKEN)
            assert "schema.sql" in str(excinfo.value)

    async def test_network_error_propagates(self):
        """Network failure should raise a ConnectError (not wrapped)."""
        with respx.mock:
            respx.post(HB_URL).mock(side_effect=httpx.ConnectError("connection refused"))
            with pytest.raises(httpx.ConnectError):
                await push_heartbeat(SUPABASE_URL, ANON_KEY, SYSTEM_ID, AGENT_TOKEN)

    def test_live_dmesg_cache_reused_for_five_minutes(self, tmp_path, monkeypatch):
        """Live dmesg snapshots are cached so heartbeats do not collect every minute."""
        cache_path = tmp_path / "live_dmesg.json"
        calls = {"count": 0}

        class Result:
            returncode = 0
            stdout = "Kernel panic: test\nordinary warning\n"

        def fake_subprocess_run(*args, **kwargs):
            calls["count"] += 1
            return Result()

        monkeypatch.setattr(cloud_push, "_live_dmesg_cache_path", lambda: cache_path)
        monkeypatch.setattr(cloud_push.subprocess, "run", fake_subprocess_run)

        first = cloud_push._collect_live_dmesg()
        second = cloud_push._collect_live_dmesg()

        assert calls["count"] == 1
        assert first == second
        assert first["refresh_interval_seconds"] == 300
        assert first["critical_count"] == 1


# ── push_report ───────────────────────────────────────────────────────────────

class TestPushReport:
    async def test_success_returns_report_id(self):
        """Successful push returns the report id."""
        with respx.mock:
            respx.post(RPT_URL).mock(
                return_value=httpx.Response(200, json="crash_abc123def456")
            )
            result = await push_report(SUPABASE_URL, ANON_KEY, SYSTEM_ID, AGENT_TOKEN, SAMPLE_REPORT)
            assert result == "crash_abc123def456"

    async def test_telemetry_stripped(self):
        """Raw telemetry must NOT be included in the cloud payload (too large)."""
        with respx.mock:
            route = respx.post(RPT_URL).mock(
                return_value=httpx.Response(200, json="crash_abc123def456")
            )
            await push_report(SUPABASE_URL, ANON_KEY, SYSTEM_ID, AGENT_TOKEN, SAMPLE_REPORT)
            payload = json.loads(route.calls[0].request.content)
            assert "telemetry" not in payload["p_report"]

    async def test_structured_fields_preserved(self):
        """Fields other than telemetry must reach the cloud."""
        with respx.mock:
            route = respx.post(RPT_URL).mock(
                return_value=httpx.Response(200, json="crash_abc123def456")
            )
            await push_report(SUPABASE_URL, ANON_KEY, SYSTEM_ID, AGENT_TOKEN, SAMPLE_REPORT)
            report_body = json.loads(route.calls[0].request.content)["p_report"]
            assert report_body["id"] == "crash_abc123def456"
            assert report_body["crash_type"] == "oom_kill"
            assert report_body["severity"] == "high"
            assert "analysis" in report_body

    async def test_ai_analyzed_lifted_to_top_level(self):
        """analysis.ai_analyzed must be lifted to the top level for the RPC,
        otherwise the dashboard's 'AI analyzed' badge/stat is always false."""
        with respx.mock:
            route = respx.post(RPT_URL).mock(
                return_value=httpx.Response(200, json="crash_abc123def456")
            )
            await push_report(SUPABASE_URL, ANON_KEY, SYSTEM_ID, AGENT_TOKEN, SAMPLE_REPORT)
            report_body = json.loads(route.calls[0].request.content)["p_report"]
            assert report_body["ai_analyzed"] is True

    async def test_ai_analyzed_false_when_heuristic_only(self):
        """A heuristic-only report (analysis.ai_analyzed false) stays false."""
        heuristic = {**SAMPLE_REPORT, "analysis": {"ai_analyzed": False, "confidence": 0.5}}
        with respx.mock:
            route = respx.post(RPT_URL).mock(
                return_value=httpx.Response(200, json="crash_abc123def456")
            )
            await push_report(SUPABASE_URL, ANON_KEY, SYSTEM_ID, AGENT_TOKEN, heuristic)
            report_body = json.loads(route.calls[0].request.content)["p_report"]
            assert report_body["ai_analyzed"] is False

    async def test_dmesg_summary_included(self):
        """A trimmed dmesg/kernel log must reach the cloud for the dashboard to show."""
        report = {
            **SAMPLE_REPORT,
            "telemetry": {
                "dmesg": {
                    "critical_events": ["[12.34] Out of memory: Killed process 999"],
                    "critical_count": 1,
                    "full_tail": "line1\nline2\nkernel panic here\n",
                },
                "journal": {"oom_events": "oom killer invoked"},
            },
        }
        with respx.mock:
            route = respx.post(RPT_URL).mock(
                return_value=httpx.Response(200, json="crash_abc123def456")
            )
            await push_report(SUPABASE_URL, ANON_KEY, SYSTEM_ID, AGENT_TOKEN, report)
            body = json.loads(route.calls[0].request.content)["p_report"]
            assert "telemetry" not in body                       # raw blob still stripped
            ts = body["telemetry_summary"]
            assert ts["dmesg"]["critical_count"] == 1
            assert "kernel panic" in ts["dmesg"]["tail"]
            assert "oom killer" in ts["journal"]["oom_events"]

    async def test_telemetry_summary_present_when_no_telemetry(self):
        """Reports with no telemetry still get an (empty) summary, not a crash."""
        report = {k: v for k, v in SAMPLE_REPORT.items() if k != "telemetry"}
        with respx.mock:
            route = respx.post(RPT_URL).mock(
                return_value=httpx.Response(200, json="crash_abc123def456")
            )
            await push_report(SUPABASE_URL, ANON_KEY, SYSTEM_ID, AGENT_TOKEN, report)
            body = json.loads(route.calls[0].request.content)["p_report"]
            assert "telemetry_summary" in body
            assert body["telemetry_summary"]["dmesg"]["tail"] == ""

    async def test_correct_rpc_params(self):
        """Payload must include p_system_id, p_agent_token, p_report."""
        with respx.mock:
            route = respx.post(RPT_URL).mock(
                return_value=httpx.Response(200, json="crash_abc123def456")
            )
            await push_report(SUPABASE_URL, ANON_KEY, SYSTEM_ID, AGENT_TOKEN, SAMPLE_REPORT)
            payload = json.loads(route.calls[0].request.content)
            assert payload["p_system_id"] == SYSTEM_ID
            assert payload["p_agent_token"] == AGENT_TOKEN
            assert isinstance(payload["p_report"], dict)

    async def test_http_error_propagates(self):
        """Non-2xx response should raise."""
        with respx.mock:
            respx.post(RPT_URL).mock(
                return_value=httpx.Response(403, json={"message": "Forbidden"})
            )
            with pytest.raises(httpx.HTTPStatusError):
                await push_report(SUPABASE_URL, ANON_KEY, SYSTEM_ID, AGENT_TOKEN, SAMPLE_REPORT)

    async def test_report_without_telemetry_key(self):
        """Reports that have no telemetry key at all should still be pushed."""
        report_no_tel = {k: v for k, v in SAMPLE_REPORT.items() if k != "telemetry"}
        with respx.mock:
            respx.post(RPT_URL).mock(
                return_value=httpx.Response(200, json=report_no_tel["id"])
            )
            result = await push_report(SUPABASE_URL, ANON_KEY, SYSTEM_ID, AGENT_TOKEN, report_no_tel)
            assert result == report_no_tel["id"]

    async def test_content_type_header(self):
        """Request must declare Content-Type: application/json."""
        with respx.mock:
            route = respx.post(RPT_URL).mock(
                return_value=httpx.Response(200, json="crash_abc123def456")
            )
            await push_report(SUPABASE_URL, ANON_KEY, SYSTEM_ID, AGENT_TOKEN, SAMPLE_REPORT)
            assert "application/json" in route.calls[0].request.headers["content-type"]
