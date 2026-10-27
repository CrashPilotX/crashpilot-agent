"""The network readings in the heartbeat: sampled every tick, assessed from what was stored, sent with the metrics."""

from __future__ import annotations

import logging
import time

import pytest
from typer.testing import CliRunner

import crashpilot.config as cfg_mod
from crashpilot import cloud_push
from crashpilot.main import app
from crashpilot.storage.store import init_db, list_network_samples, save_network_sample

runner = CliRunner()


def reading(ts: float, rx: int, tx: int = 0, **extra) -> dict:
    return {
        "ts": ts, "iface": "eth0", "medium": "ethernet", "link_speed_mbps": 1000, "duplex": "full",
        "rx_bytes": rx, "tx_bytes": tx, "rx_errs": 0, "rx_drops": 0, "tx_errs": 0, "tx_drops": 0,
        "tcp_out": 1000, "tcp_retrans": 5, "gw_rtt_ms": 0.5, "gw_loss_pct": 0.0, "inet_rtt_ms": 14.0, **extra,
    }


@pytest.fixture
def enabled(monkeypatch):
    """The test environment switches network readings off; these tests want them."""
    monkeypatch.setenv("CRASHPILOT_NETWORK_HEALTH_ENABLED", "true")
    monkeypatch.setenv("CRASHPILOT_SUPABASE_URL", "https://abc.supabase.co")
    cfg_mod._settings = None
    yield
    cfg_mod._settings = None


class TestRecording:
    def test_a_reading_is_stored_with_the_settings_it_was_asked_to_use(self, enabled, monkeypatch):
        seen = {}

        def fake_take(**kwargs):
            seen.update(kwargs)
            return reading(time.time() - 5, 1000)

        monkeypatch.setattr(cloud_push, "take_network_sample", fake_take)
        monkeypatch.setenv("CRASHPILOT_NETWORK_PROBE_GATEWAY", "false")
        cfg_mod._settings = None
        cloud_push.record_network_sample()
        assert seen == {"dashboard_url": "https://abc.supabase.co", "probe_gateway": False, "probe_internet": True}
        assert [r["rx_bytes"] for r in list_network_samples(1)] == [1000]

    def test_it_does_nothing_when_switched_off(self, monkeypatch):
        # The test environment has it off, as an owner who wants no probing would.
        monkeypatch.setattr(cloud_push, "take_network_sample", lambda **_: pytest.fail("must not be sampled"))
        cloud_push.record_network_sample()

    def test_a_machine_with_no_network_stores_nothing_and_does_not_fail(self, enabled, monkeypatch):
        monkeypatch.setattr(cloud_push, "take_network_sample", lambda **_: None)
        cloud_push.record_network_sample()
        init_db()
        assert list_network_samples(1) == []

    def test_a_failure_is_logged_and_never_stops_the_heartbeat(self, enabled, monkeypatch, caplog):
        def boom(**_):
            raise RuntimeError("proc went away")

        monkeypatch.setattr(cloud_push, "take_network_sample", boom)
        with caplog.at_level(logging.WARNING):
            cloud_push.record_network_sample()
        assert "Network reading failed" in caplog.text


class TestSections:
    def store(self, now, **second):
        init_db()
        save_network_sample(reading(now - 70, rx=1_000_000_000, tx=0, tcp_out=1000, tcp_retrans=5))
        save_network_sample(reading(now - 5, rx=1_000_875_000, tx=87_500, tcp_out=1200, tcp_retrans=11, **second))

    def test_nothing_stored_means_no_sections(self, enabled):
        init_db()
        assert cloud_push._network_health_sections(None) == {}

    def test_health_assessment_and_usage(self, enabled):
        self.store(time.time())
        sections = cloud_push._network_health_sections(None)
        assert sections["health"] == {
            "interface": "eth0", "medium": "ethernet", "link_speed_mbps": 1000, "duplex": "full",
            "gateway_rtt_ms": 0.5, "internet_rtt_ms": 14.0, "retransmit_pct": 3.0,  # 6 of 200 segments
        }
        assert sections["assessment"]["verdict"] == "learning" and sections["assessment"]["samples"] == 1
        usage = sections["usage"]
        assert (usage["rx_gb"], usage["tx_gb"], usage["total_gb"]) == (0.0, 0.0, 0.0)  # 875 KB and 87 KB, rounded to GB
        assert usage["month"] and usage["since"].endswith("Z")

    def test_too_little_tcp_traffic_to_quote_a_resend_rate(self, enabled):
        init_db()
        now = time.time()
        save_network_sample(reading(now - 70, rx=1, tcp_out=1000, tcp_retrans=5))
        save_network_sample(reading(now - 5, rx=2, tcp_out=1010, tcp_retrans=6))
        assert cloud_push._network_health_sections(None)["health"]["retransmit_pct"] is None

    def test_a_reading_without_tcp_counters_has_no_resend_rate(self, enabled):
        init_db()
        now = time.time()
        save_network_sample(reading(now - 70, rx=1, tcp_out=None, tcp_retrans=None))
        save_network_sample(reading(now - 5, rx=2, tcp_out=None, tcp_retrans=None))
        assert cloud_push._network_health_sections(None)["health"]["retransmit_pct"] is None

    def test_the_plan_speed_in_settings_is_the_capacity(self, enabled, monkeypatch):
        monkeypatch.setenv("CRASHPILOT_NETWORK_PLAN_DOWN_MBPS", "300")
        cfg_mod._settings = None
        self.store(time.time())
        assessment = cloud_push._network_health_sections(None)["assessment"]
        assert assessment["capacity_mbps"] == 300 and assessment["capacity_source"] == "plan"

    def test_a_speed_test_result_is_passed_to_the_verdict(self, enabled):
        self.store(time.time())
        assessment = cloud_push._network_health_sections({"available": True, "download_mbps": 480.0, "upload_mbps": 20.0})["assessment"]
        assert assessment["capacity_mbps"] == 480.0 and assessment["capacity_source"] == "speedtest"


class TestLiveMetrics:
    @pytest.fixture(autouse=True)
    def _isolated(self, monkeypatch, tmp_path, enabled):
        monkeypatch.setattr(cloud_push, "_section_ttl_path", lambda: tmp_path / "section_ttl.json")
        monkeypatch.setattr(cloud_push.os, "getloadavg", lambda: (0.5, 0.4, 0.3), raising=False)
        monkeypatch.setattr(cloud_push, "_read_meminfo", lambda: {})
        monkeypatch.setattr(cloud_push, "_collect_disk_usage", lambda: {"primary": {"used_pct": 10}})
        monkeypatch.setattr(cloud_push, "collect_amd_gpus", lambda: [])
        monkeypatch.setattr(cloud_push, "collect_temperatures", lambda: {})
        monkeypatch.setattr(cloud_push.shutil, "which", lambda name: None)
        monkeypatch.setattr(
            cloud_push, "_collect_network_usage",
            lambda: {"rx_mbps": 4.2, "tx_mbps": 1.1, "speedtest": {"available": True, "download_mbps": 480.0, "upload_mbps": 20.0}},
        )
        TestSections().store(time.time())

    def test_the_network_section_carries_the_rates_and_the_new_parts_together(self):
        network = cloud_push._build_live_metrics(slim=False)["network"]
        assert network["rx_mbps"] == 4.2 and network["speedtest"]["download_mbps"] == 480.0
        assert set(network) >= {"health", "assessment", "usage"}
        assert network["assessment"]["capacity_source"] == "speedtest"

    def test_slim_mode_drops_the_speed_test_from_the_payload_but_the_verdict_still_used_it(self):
        network = cloud_push._build_live_metrics(slim=True)["network"]
        assert "speedtest" not in network and network["assessment"]["capacity_source"] == "speedtest"
        assert {"health", "assessment", "usage"} <= set(network)

    def test_a_failure_in_the_new_parts_costs_them_and_not_the_heartbeat(self, monkeypatch):
        def broken(speedtest):
            raise RuntimeError("database is locked")

        monkeypatch.setattr(cloud_push, "_network_health_sections", broken)
        metrics = cloud_push._build_live_metrics(slim=False)
        assert metrics["network"]["rx_mbps"] == 4.2 and "assessment" not in metrics["network"]
        assert "cpu" in metrics and "disk" in metrics

    def test_a_machine_whose_counters_cannot_be_read_sends_no_network_section(self, monkeypatch):
        monkeypatch.setattr(cloud_push, "_collect_network_usage", lambda: {})
        assert "network" not in cloud_push._build_live_metrics(slim=False)

    def test_the_payload_stays_small(self):
        import json

        sections = {k: v for k, v in cloud_push._build_live_metrics(slim=False)["network"].items() if k in ("health", "assessment", "usage")}
        assert len(json.dumps(sections)) < 1500


class TestHeartbeatCommand:
    def test_a_reading_is_taken_before_every_push(self, monkeypatch):
        order: list[str] = []
        for name, value in (
            ("URL", "https://abc.supabase.co"), ("ANON_KEY", "anon"), ("SYSTEM_ID", "sys-1"), ("TOKEN", "tok"),
        ):
            monkeypatch.setenv(f"CRASHPILOT_SUPABASE_{name}", value)
        cfg_mod._settings = None

        async def fake_push(**_kwargs):
            order.append("push")

        monkeypatch.setattr("crashpilot.cloud_push.push_heartbeat", fake_push)
        monkeypatch.setattr("crashpilot.cloud_push.record_network_sample", lambda: order.append("reading"))
        result = runner.invoke(app, ["heartbeat", "--quiet"])
        assert result.exit_code == 0, result.output
        assert order == ["reading", "push"]
