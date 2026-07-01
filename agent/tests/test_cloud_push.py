"""Tests for cloud_push.py — push heartbeats and reports to Supabase.

Uses respx to mock httpx calls without requiring a real Supabase instance.
asyncio_mode = "auto" is set in pyproject.toml so no @pytest.mark.asyncio needed.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

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
STATUS_URL = f"{SUPABASE_URL}/rest/v1/rpc/agent_system_status"
RPT_URL = f"{SUPABASE_URL}/rest/v1/rpc/agent_push_report"


def mock_legacy_status():
    return respx.post(STATUS_URL).mock(return_value=httpx.Response(404))

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
            mock_legacy_status()
            await push_heartbeat(SUPABASE_URL, ANON_KEY, SYSTEM_ID, AGENT_TOKEN)
            assert route.called

    async def test_correct_payload(self):
        """Heartbeat includes system_id, agent_token, hostname, version, and metrics."""
        with respx.mock:
            route = respx.post(HB_URL).mock(return_value=httpx.Response(200))
            mock_legacy_status()
            await push_heartbeat(SUPABASE_URL, ANON_KEY, SYSTEM_ID, AGENT_TOKEN)
            payload = json.loads(route.calls[0].request.content)
            assert payload["p_system_id"] == SYSTEM_ID
            assert payload["p_agent_token"] == AGENT_TOKEN
            assert "p_hostname" in payload
            assert "p_version" in payload
            assert "p_metrics" in payload
            assert "cpu" in payload["p_metrics"]
            assert "memory" in payload["p_metrics"]
            assert "disk" in payload["p_metrics"]

    async def test_accepts_explicit_metrics(self):
        """Heartbeat can send caller-supplied live metrics."""
        metrics = {"cpu": {"load_pct": 12.5}, "memory": {"used_pct": 42.0}}
        with respx.mock:
            route = respx.post(HB_URL).mock(return_value=httpx.Response(200))
            mock_legacy_status()
            await push_heartbeat(SUPABASE_URL, ANON_KEY, SYSTEM_ID, AGENT_TOKEN, metrics=metrics)
            payload = json.loads(route.calls[0].request.content)
            assert payload["p_metrics"] == metrics

    async def test_syncs_maintenance_status(self, monkeypatch):
        saved: list[tuple[str, str]] = []
        monkeypatch.setattr(
            "crashpilot.storage.store.set_meta",
            lambda key, value: saved.append((key, value)),
        )
        with respx.mock:
            respx.post(HB_URL).mock(return_value=httpx.Response(200))
            status = respx.post(STATUS_URL).mock(return_value=httpx.Response(200, json={
                "maintenance_until": "2026-06-18T20:00:00Z",
                "in_maintenance": True,
            }))
            result = await push_heartbeat(
                SUPABASE_URL, ANON_KEY, SYSTEM_ID, AGENT_TOKEN, metrics={}
            )
        assert status.called
        assert result["in_maintenance"] is True
        assert saved == [("maintenance_until", "2026-06-18T20:00:00Z")]

    async def test_metrics_rpc_falls_back_to_legacy_heartbeat(self):
        """Older Supabase schemas without p_metrics still receive a legacy heartbeat."""
        with respx.mock:
            route = respx.post(HB_URL).mock(
                side_effect=[
                    httpx.Response(404, json={"message": "Could not find function with p_metrics"}),
                    httpx.Response(200),
                ]
            )
            mock_legacy_status()
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
            mock_legacy_status()
            await push_heartbeat(SUPABASE_URL, ANON_KEY, SYSTEM_ID, AGENT_TOKEN)
            headers = route.calls[0].request.headers
            assert headers["apikey"] == ANON_KEY
            assert headers["authorization"] == f"Bearer {ANON_KEY}"

    async def test_trailing_slash_stripped_from_url(self):
        """Trailing slash in supabase_url must not create a double-slash in the path."""
        with respx.mock:
            route = respx.post(HB_URL).mock(return_value=httpx.Response(200))
            mock_legacy_status()
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

    def test_agent_health_payload_reports_timer_tools_and_config(self, monkeypatch):
        """Heartbeat metrics include agent health diagnostics for the dashboard."""
        from crashpilot import config

        monkeypatch.setattr(cloud_push, "_agent_version", lambda: "0.1.0-test")
        monkeypatch.setattr(cloud_push, "_hostname", lambda: "ci-host")
        monkeypatch.setattr(
            cloud_push,
            "_systemctl_state",
            lambda unit, verb: "active" if verb == "is-active" else "enabled",
        )
        monkeypatch.setattr(
            cloud_push.shutil,
            "which",
            lambda name: f"/usr/bin/{name}" if name in {"systemctl", "dmesg"} else None,
        )
        monkeypatch.setattr(
            config,
            "get_settings",
            lambda: SimpleNamespace(
                supabase_url="https://example.supabase.co",
                supabase_system_id="system-id",
                supabase_token="token",
                data_dir="/opt/crashpilot",
            ),
        )

        health = cloud_push._build_agent_health({
            "tail": "kernel warning",
            "critical_count": 2,
            "refresh_interval_seconds": 300,
        })

        assert health["version"] == "0.1.0-test"
        assert health["hostname"] == "ci-host"
        assert health["timer"] == {"active": "active", "enabled": "enabled"}
        assert health["tools"]["systemctl"] is True
        assert health["tools"]["dmesg"] is True
        assert health["tools"]["nvidia_smi"] is False
        assert health["push_config"]["configured"] is True
        assert health["dmesg"]["critical_count"] == 2

    def test_collect_disk_usage_reports_root_and_filesystems(self, monkeypatch):
        """Heartbeat metrics include root disk pressure for low-space alerts."""

        class Usage:
            total = 100 * 1024 * 1024 * 1024
            used = 92 * 1024 * 1024 * 1024
            free = 8 * 1024 * 1024 * 1024

        class Result:
            returncode = 0
            stdout = (
                "Filesystem 1B-blocks Used Available Use% Mounted on\n"
                "/dev/sda1 100000000000 92000000000 8000000000 92% /\n"
                "/dev/sdb1 200000000000 150000000000 50000000000 75% /data\n"
            )

        monkeypatch.setattr(cloud_push.shutil, "disk_usage", lambda path: Usage())
        monkeypatch.setattr(cloud_push.subprocess, "run", lambda *args, **kwargs: Result())

        disk = cloud_push._collect_disk_usage()

        assert disk["primary"]["mountpoint"] == "/"
        assert disk["primary"]["used_pct"] == 92.0
        assert disk["root"]["mountpoint"] == "/"
        assert disk["root"]["used_pct"] == 92.0
        assert disk["lowest_free"]["mountpoint"] == "/"
        assert disk["most_used"]["used_pct"] == 92.0
        assert len(disk["filesystems"]) == 2

    def test_collect_disk_usage_keeps_root_primary_when_boot_has_less_free_space(self, monkeypatch):
        """Primary disk should be /, not a small /boot partition with less free GB."""

        class Usage:
            total = 106 * 1000 * 1000 * 1000
            used = 94 * 1000 * 1000 * 1000
            free = 6 * 1000 * 1000 * 1000

        class Result:
            returncode = 0
            stdout = (
                "Filesystem 1B-blocks Used Available Use% Mounted on\n"
                "/dev/mapper/ubuntu--vg-ubuntu--lv 106000000000 94000000000 6000000000 89% /\n"
                "/dev/nvme0n1p2 2100000000 210000000 1800000000 10% /boot\n"
            )

        monkeypatch.setattr(cloud_push.shutil, "disk_usage", lambda path: Usage())
        monkeypatch.setattr(cloud_push.subprocess, "run", lambda *args, **kwargs: Result())

        disk = cloud_push._collect_disk_usage()

        assert disk["primary"]["mountpoint"] == "/"
        assert disk["lowest_free"]["mountpoint"] == "/boot"
        assert disk["most_used"]["mountpoint"] == "/"


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



class TestNetworkMetrics:
    def test_read_network_counters_skips_loopback_and_virtual_links(self, tmp_path):
        proc = tmp_path / "net_dev"
        proc.write_text(
            "Inter-|   Receive                                                |  Transmit\n"
            " face |bytes    packets errs drop fifo frame compressed multicast|bytes    packets errs drop fifo colls carrier compressed\n"
            "    lo: 1000 10 0 0 0 0 0 0 1000 10 0 0 0 0 0 0\n"
            "  eth0: 125000000 100 0 0 0 0 0 0 25000000 50 0 0 0 0 0 0\n"
            "docker0: 999 9 0 0 0 0 0 0 999 9 0 0 0 0 0 0\n",
            encoding="utf-8",
        )

        counters = cloud_push._read_network_counters(proc)

        assert counters["rx_bytes"] == 125000000
        assert counters["tx_bytes"] == 25000000
        assert counters["interfaces"] == [{
            "name": "eth0",
            "rx_bytes": 125000000,
            "tx_bytes": 25000000,
            "rx_packets": 100,
            "tx_packets": 50,
        }]

    def test_collect_network_usage_ignores_malformed_cache(self, monkeypatch, tmp_path):
        cache = tmp_path / "network_counters.json"
        cache.write_text('{"saved_at":"not-a-time","rx_bytes":"bad","tx_bytes":0}', encoding="utf-8")
        monkeypatch.setattr(
            cloud_push,
            "_read_network_counters",
            lambda: {"interfaces": [{"name": "eth0"}], "rx_bytes": 11_000_000, "tx_bytes": 7_000_000},
        )
        monkeypatch.setattr(cloud_push, "_network_counters_cache_path", lambda: cache)
        monkeypatch.setattr(cloud_push.time, "time", lambda: 1010.0)

        current = cloud_push._collect_network_usage()

        assert current["rx_mbps"] is None
        assert current["tx_mbps"] is None
        assert current["interfaces"] == [{"name": "eth0"}]

    def test_collect_network_usage_reports_rates_from_previous_sample(self, monkeypatch, tmp_path):
        samples = iter([
            {"interfaces": [{"name": "eth0"}], "rx_bytes": 1_000_000, "tx_bytes": 2_000_000},
            {"interfaces": [{"name": "eth0"}], "rx_bytes": 11_000_000, "tx_bytes": 7_000_000},
        ])
        times = iter([1000.0, 1010.0])
        cache = tmp_path / "network_counters.json"
        monkeypatch.setattr(cloud_push, "_read_network_counters", lambda: next(samples))
        monkeypatch.setattr(cloud_push, "_network_counters_cache_path", lambda: cache)
        monkeypatch.setattr(cloud_push.time, "time", lambda: next(times))

        baseline = cloud_push._collect_network_usage()
        current = cloud_push._collect_network_usage()

        assert baseline["rx_mbps"] is None
        assert current["rate_interval_seconds"] == 10.0
        assert current["rx_bytes_per_second"] == 1_000_000.0
        assert current["tx_bytes_per_second"] == 500_000.0
        assert current["rx_mbps"] == 8.0
        assert current["tx_mbps"] == 4.0


    def test_speedtest_capacity_parses_speedtest_cli_json(self, monkeypatch, tmp_path):
        class Result:
            returncode = 0
            stdout = json.dumps({
                "download": 942_500_000.0,
                "upload": 118_250_000.0,
                "ping": 8.3,
                "server": {"sponsor": "Example ISP", "name": "Chicago", "country": "United States"},
            })
            stderr = ""

        settings = SimpleNamespace(
            data_dir=tmp_path,
            bandwidth_speedtest_enabled=True,
            bandwidth_speedtest_interval_seconds=21600,
            bandwidth_speedtest_timeout_seconds=90,
        )
        monkeypatch.setattr("crashpilot.config.get_settings", lambda: settings)
        monkeypatch.setattr(cloud_push.shutil, "which", lambda name: "/usr/bin/speedtest-cli")
        monkeypatch.setattr(cloud_push.subprocess, "run", lambda *args, **kwargs: Result())

        result = cloud_push._collect_speedtest_capacity()

        assert result["available"] is True
        assert result["download_mbps"] == 942.5
        assert result["upload_mbps"] == 118.25
        assert result["ping_ms"] == 8.3
        assert result["server"]["sponsor"] == "Example ISP"

    def test_speedtest_capacity_uses_cache_without_running_cli(self, monkeypatch, tmp_path):
        cached = {
            "enabled": True,
            "available": True,
            "download_mbps": 100.0,
            "upload_mbps": 20.0,
            "tested_at": "2026-07-01T00:00:00+00:00",
        }
        (tmp_path / "speedtest_result.json").write_text(
            json.dumps({"saved_at": 1000.0, "result": cached}),
            encoding="utf-8",
        )
        settings = SimpleNamespace(
            data_dir=tmp_path,
            bandwidth_speedtest_enabled=True,
            bandwidth_speedtest_interval_seconds=21600,
            bandwidth_speedtest_timeout_seconds=90,
        )
        monkeypatch.setattr("crashpilot.config.get_settings", lambda: settings)
        monkeypatch.setattr(cloud_push.time, "time", lambda: 1010.0)
        monkeypatch.setattr(cloud_push.subprocess, "run", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should not run")))

        result = cloud_push._collect_speedtest_capacity()

        assert result["download_mbps"] == 100.0
        assert result["cached"] is True
        assert result["age_seconds"] == 10.0

    def test_speedtest_capacity_reports_missing_cli_when_enabled(self, monkeypatch, tmp_path):
        settings = SimpleNamespace(
            data_dir=tmp_path,
            bandwidth_speedtest_enabled=True,
            bandwidth_speedtest_interval_seconds=21600,
            bandwidth_speedtest_timeout_seconds=90,
        )
        monkeypatch.setattr("crashpilot.config.get_settings", lambda: settings)
        monkeypatch.setattr(cloud_push.shutil, "which", lambda name: None)

        result = cloud_push._collect_speedtest_capacity()

        assert result["available"] is False
        assert "speedtest-cli" in result["error"]
