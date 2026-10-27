"""The readings behind the network verdict: link, loss and latency, from fake /proc and /sys trees."""

from __future__ import annotations

import math
import socket
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from crashpilot.collectors import net_health
from crashpilot.collectors.net_health import (
    connect_time_ms,
    dashboard_host,
    default_route,
    link_info,
    ping_gateway,
    read_counters,
    take_sample,
)

ROUTE = (
    "Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT\n"
    "eth0\t00000000\tFE01A8C0\t0003\t0\t0\t100\t00000000\t0\t0\t0\n"
    "eth0\t0001A8C0\t00000000\t0001\t0\t0\t100\t00FFFFFF\t0\t0\t0\n"
)
NET_DEV = (
    "Inter-|   Receive                                                |  Transmit\n"
    " face |bytes    packets errs drop fifo frame compressed multicast|bytes    packets errs drop fifo colls carrier compressed\n"
    "    lo: 1000 10 0 0 0 0 0 0 1000 10 0 0 0 0 0 0\n"
    "  eth0: 5000000 4000 3 121 0 0 0 0 900000 3000 1 8 0 0 0 0\n"
)
SNMP = (
    "Tcp: RtoAlgorithm RtoMin RtoMax MaxConn ActiveOpens PassiveOpens AttemptFails EstabResets CurrEstab InSegs OutSegs RetransSegs InErrs\n"
    "Tcp: 1 200 120000 -1 100 50 2 3 7 900000 1200000 24000 0\n"
)


def tree(tmp_path: Path, *, route=ROUTE, speed="1000", duplex="full", wireless=False, device=True) -> tuple[Path, Path]:
    proc, sys_root = tmp_path / "proc", tmp_path / "sys"
    (proc / "net").mkdir(parents=True)
    (proc / "net" / "route").write_text(route)
    (proc / "net" / "dev").write_text(NET_DEV)
    (proc / "net" / "snmp").write_text(SNMP)
    nic = sys_root / "class" / "net" / "eth0"
    nic.mkdir(parents=True)
    if device:
        (nic / "device").mkdir()
    if wireless:
        (nic / "wireless").mkdir()
    if speed is not None:
        (nic / "speed").write_text(speed + "\n")
    if duplex is not None:
        (nic / "duplex").write_text(duplex + "\n")
    return proc, sys_root


class TestDefaultRoute:
    def test_the_gateway_is_decoded_from_the_kernels_little_endian_hex(self, tmp_path):
        proc, _ = tree(tmp_path)
        assert default_route(proc) == ("eth0", "192.168.1.254")

    def test_the_lowest_metric_wins(self, tmp_path):
        proc, _ = tree(tmp_path, route=ROUTE + "wlan0\t00000000\t0101A8C0\t0003\t0\t0\t600\t00000000\t0\t0\t0\n")
        assert default_route(proc) == ("eth0", "192.168.1.254")
        proc, _ = tree(tmp_path / "b", route=ROUTE.replace("\t100\t00000000\t0\t0\t0\neth0\t0001", "\t900\t00000000\t0\t0\t0\neth0\t0001")
                       + "wlan0\t00000000\t0101A8C0\t0003\t0\t0\t600\t00000000\t0\t0\t0\n")
        assert default_route(proc) == ("wlan0", "192.168.1.1")

    def test_a_route_that_is_not_a_gateway_is_not_a_default_route(self, tmp_path):
        proc, _ = tree(tmp_path, route=ROUTE.replace("0003", "0001"))
        assert default_route(proc) is None

    @pytest.mark.parametrize("route", ["", "Iface\tDestination\n", "garbage\nmore garbage\n", "eth0 00000000 ZZZZ 0003 0 0 1 0 0 0 0\n"])
    def test_no_usable_route_means_no_network(self, tmp_path, route):
        proc, _ = tree(tmp_path, route="header\n" + route if route else "")
        assert default_route(proc) is None

    def test_a_missing_proc_is_no_network(self, tmp_path):
        assert default_route(tmp_path / "nothing") is None


class TestLinkInfo:
    def test_a_gigabit_wired_port(self, tmp_path):
        _, sys_root = tree(tmp_path)
        assert link_info("eth0", sys_root) == {"medium": "ethernet", "link_speed_mbps": 1000, "duplex": "full"}

    def test_a_port_that_negotiated_100_half(self, tmp_path):
        _, sys_root = tree(tmp_path, speed="100", duplex="half")
        assert link_info("eth0", sys_root) == {"medium": "ethernet", "link_speed_mbps": 100, "duplex": "half"}

    @pytest.mark.parametrize("speed", ["-1", "0", "abc", None])
    def test_a_speed_the_kernel_cannot_give_is_unknown(self, tmp_path, speed):
        _, sys_root = tree(tmp_path, speed=speed)
        assert link_info("eth0", sys_root)["link_speed_mbps"] is None

    def test_wifi_reports_no_wired_speed_even_if_the_file_exists(self, tmp_path):
        _, sys_root = tree(tmp_path, wireless=True)
        assert link_info("eth0", sys_root) == {"medium": "wifi", "link_speed_mbps": None, "duplex": None}

    def test_a_virtual_port_is_not_judged_by_its_speed(self, tmp_path):
        _, sys_root = tree(tmp_path, device=False, speed="10000")
        assert link_info("eth0", sys_root) == {"medium": "other", "link_speed_mbps": None, "duplex": None}

    def test_an_unknown_interface_is_other(self, tmp_path):
        assert link_info("nope0", tmp_path / "sys")["medium"] == "other"


class TestCounters:
    def test_port_counters_and_the_tcp_resend_counters(self, tmp_path):
        proc, _ = tree(tmp_path)
        assert read_counters("eth0", proc) == {
            "rx_bytes": 5000000, "rx_errs": 3, "rx_drops": 121, "tx_bytes": 900000, "tx_errs": 1, "tx_drops": 8,
            "tcp_out": 1200000, "tcp_retrans": 24000,
        }

    def test_an_interface_that_is_not_there_has_no_counters(self, tmp_path):
        proc, _ = tree(tmp_path)
        assert read_counters("wlan9", proc) is None

    def test_tcp_counters_missing_leave_the_port_counters(self, tmp_path):
        proc, _ = tree(tmp_path)
        (proc / "net" / "snmp").write_text("")
        counters = read_counters("eth0", proc)
        assert counters is not None and "tcp_out" not in counters and counters["rx_bytes"] == 5000000

    def test_a_short_or_garbled_line_is_not_trusted(self, tmp_path):
        proc, _ = tree(tmp_path)
        (proc / "net" / "dev").write_text(NET_DEV.replace("5000000 4000 3 121 0 0 0 0 900000 3000 1 8 0 0 0 0", "5000000 4000"))
        assert read_counters("eth0", proc) is None
        (proc / "net" / "dev").write_text(NET_DEV.replace("5000000", "lots"))
        assert read_counters("eth0", proc) is None


class TestPing:
    OK = "PING 192.168.1.254\n--- 192.168.1.254 ping statistics ---\n3 packets transmitted, 3 received, 0% packet loss, time 405ms\nrtt min/avg/max/mdev = 0.244/0.492/0.807/0.230 ms\n"

    def run(self, stdout="", exc=None):
        def fake(*args, **kwargs):
            if exc:
                raise exc
            return SimpleNamespace(stdout=stdout)
        return fake

    def test_the_average_and_the_loss_are_read(self):
        assert ping_gateway("192.168.1.254", self.run(self.OK)) == {"rtt_ms": 0.49, "loss_pct": 0.0}

    def test_partial_loss(self):
        out = self.OK.replace("3 received, 0%", "2 received, 33.3%")
        assert ping_gateway("x", self.run(out))["loss_pct"] == 33.3

    def test_every_ping_lost_is_loss_not_silence(self):
        out = "3 packets transmitted, 0 received, 100% packet loss, time 405ms\n"
        reply = ping_gateway("x", self.run(out))
        assert reply["loss_pct"] == 100.0 and math.isnan(reply["rtt_ms"])

    @pytest.mark.parametrize("stdout", ["", "ping: unknown host\n", "no output we know"])
    def test_output_it_cannot_read_gives_no_reading(self, stdout):
        assert ping_gateway("x", self.run(stdout)) is None

    @pytest.mark.parametrize("exc", [OSError("no ping"), subprocess.TimeoutExpired("ping", 6)])
    def test_a_ping_that_cannot_run_gives_no_reading(self, exc):
        assert ping_gateway("x", self.run(exc=exc)) is None

    def test_no_ping_binary_means_skip(self, monkeypatch):
        monkeypatch.setattr(net_health.shutil, "which", lambda name: None)
        assert ping_gateway("192.168.1.254") is None


class TestConnectTime:
    def patch_dns(self, monkeypatch, result=None, exc=None):
        def fake(host, port, type=0):
            if exc:
                raise exc
            return result if result is not None else [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("203.0.113.7", port))]
        monkeypatch.setattr(socket, "getaddrinfo", fake)

    def test_the_time_to_open_a_connection_is_measured_after_the_name_is_resolved(self, monkeypatch):
        self.patch_dns(monkeypatch)
        ticks = iter([10.0, 10.0129])
        closed = []
        sock = SimpleNamespace(close=lambda: closed.append(True))
        assert connect_time_ms("db.example.com", connect=lambda addr, timeout: sock, clock=lambda: next(ticks)) == 12.9
        assert closed == [True]

    def test_an_unreachable_host_is_no_reading(self, monkeypatch):
        self.patch_dns(monkeypatch)

        def refuse(addr, timeout):
            raise OSError("timed out")

        assert connect_time_ms("db.example.com", connect=refuse) is None

    def test_a_name_that_does_not_resolve_is_no_reading(self, monkeypatch):
        self.patch_dns(monkeypatch, exc=socket.gaierror("no such host"))
        assert connect_time_ms("nope.invalid") is None
        self.patch_dns(monkeypatch, result=[])
        assert connect_time_ms("nope.invalid") is None

    @pytest.mark.parametrize(("url", "host"), [
        ("https://abc.supabase.co", "abc.supabase.co"),
        ("https://abc.supabase.co/rest/v1", "abc.supabase.co"),
        ("", None),
        ("not a url", None),
    ])
    def test_the_host_is_the_one_the_agent_already_talks_to(self, url, host):
        assert dashboard_host(url) == host


class TestTakeSample:
    def sample(self, tmp_path, **kwargs):
        proc, sys_root = tree(tmp_path)
        defaults = dict(
            dashboard_url="https://abc.supabase.co", proc_root=proc, sys_root=sys_root,
            ping=lambda gateway: {"rtt_ms": 0.49, "loss_pct": 0.0}, connect=lambda host: 12.9, now=lambda: 1_700_000_000.0,
        )
        return take_sample(**{**defaults, **kwargs})

    def test_a_full_reading(self, tmp_path):
        assert self.sample(tmp_path) == {
            "ts": 1_700_000_000.0, "iface": "eth0", "medium": "ethernet", "link_speed_mbps": 1000, "duplex": "full",
            "rx_bytes": 5000000, "rx_errs": 3, "rx_drops": 121, "tx_bytes": 900000, "tx_errs": 1, "tx_drops": 8,
            "tcp_out": 1200000, "tcp_retrans": 24000, "gw_rtt_ms": 0.49, "gw_loss_pct": 0.0, "inet_rtt_ms": 12.9,
        }

    def test_the_probes_can_be_switched_off_and_then_nothing_is_contacted(self, tmp_path):
        def boom(*_):
            raise AssertionError("must not be contacted")

        sample = self.sample(tmp_path, probe_gateway=False, probe_internet=False, ping=boom, connect=boom)
        assert (sample["gw_rtt_ms"], sample["gw_loss_pct"], sample["inet_rtt_ms"]) == (None, None, None)
        assert sample["rx_bytes"] == 5000000  # the free counters are still read

    def test_without_a_dashboard_address_there_is_no_internet_reading(self, tmp_path):
        assert self.sample(tmp_path, dashboard_url="")["inet_rtt_ms"] is None

    def test_a_router_that_never_answered_is_loss_with_no_latency(self, tmp_path):
        sample = self.sample(tmp_path, ping=lambda gw: {"rtt_ms": float("nan"), "loss_pct": 100.0})
        assert sample["gw_rtt_ms"] is None and sample["gw_loss_pct"] == 100.0

    def test_no_ping_leaves_the_router_reading_empty(self, tmp_path):
        sample = self.sample(tmp_path, ping=lambda gw: None)
        assert sample["gw_rtt_ms"] is None and sample["gw_loss_pct"] is None

    def test_no_default_route_is_no_sample(self, tmp_path):
        proc, sys_root = tree(tmp_path, route="Iface\tDestination\n")
        assert take_sample(proc_root=proc, sys_root=sys_root) is None

    def test_a_virtual_interface_carrying_the_default_route_is_not_sampled(self, tmp_path):
        proc, sys_root = tree(tmp_path, route=ROUTE.replace("eth0", "docker0"))
        assert take_sample(proc_root=proc, sys_root=sys_root) is None

    def test_a_default_route_whose_interface_has_no_counters_is_no_sample(self, tmp_path):
        proc, sys_root = tree(tmp_path, route=ROUTE.replace("eth0", "wlan9"))
        assert take_sample(proc_root=proc, sys_root=sys_root) is None
