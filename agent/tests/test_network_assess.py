"""The network verdict: which kind of slow it is, and whether a faster plan would help."""

from __future__ import annotations

import json
import math
import re

import pytest

from crashpilot.analyzers.network_health import VERDICTS, _fmt, _percentile, assess

START = 1_700_000_000.0


def series(
    n: int, *, step: int = 70, down=lambda i: 3.0, up=lambda i: 1.0, inet=lambda i: 14.0, gw=lambda i: 0.5,
    gw_loss=lambda i: 0.0, resend_pct=lambda i: 0.02, segs=lambda i: 400, errs=lambda i: 0, iface="eth0",
    medium="ethernet", speed=1000, duplex="full", start=START,
) -> list[dict]:
    """Readings as the collector stores them: cumulative counters, one row per heartbeat tick."""
    rx = tx = out = resent = errors = 0
    rows = []
    for i in range(n):
        rows.append({
            "ts": start + i * step, "iface": iface, "medium": medium, "link_speed_mbps": speed, "duplex": duplex,
            "rx_bytes": rx, "tx_bytes": tx, "rx_errs": errors, "rx_drops": 0, "tx_errs": 0, "tx_drops": 0,
            "tcp_out": out, "tcp_retrans": resent, "gw_rtt_ms": gw(i), "gw_loss_pct": gw_loss(i), "inet_rtt_ms": inet(i),
        })
        rx += int(down(i) * 1e6 / 8 * step)
        tx += int(up(i) * 1e6 / 8 * step)
        out += segs(i)
        resent += round(segs(i) * resend_pct(i) / 100)
        errors += errs(i)
    return rows


def slow_from(first: int, last: int, slow: float = 130.0, normal: float = 14.0):
    return lambda i: slow if first <= i < last else normal


class TestNotEnoughData:
    def test_the_first_half_hour_says_it_is_still_learning(self):
        result = assess(series(30))
        assert result["verdict"] == "learning" and "about an hour" in result["headline"]

    def test_nothing_at_all_does_not_crash(self):
        for samples in ([], series(1)):
            result = assess(samples)
            assert result["verdict"] == "learning" and result["samples"] == 0

    def test_a_calm_connection_is_ok(self):
        result = assess(series(120))
        assert result["verdict"] == "ok" and result["advice"] is None
        assert re.fullmatch(r"No network problem found in the last \d+(\.\d)? hours\.", result["headline"])
        assert assess(series(7200))["headline"] == "No network problem found in the last 6 days."  # 140 hours
        assert assess(series(40))["headline"].startswith("Still collecting")  # under an hour: no window to name
        assert result["congested_minutes"] == 0 and result["samples"] == 119


class TestSlowPort:
    @pytest.mark.parametrize(("speed", "duplex", "said"), [(100, "full", "100 Mbps"), (10, "full", "10 Mbps"), (1000, "half", "half duplex")])
    def test_a_wired_port_that_negotiated_down_is_reported_at_once(self, speed, duplex, said):
        result = assess(series(30, speed=speed, duplex=duplex))  # no waiting for an hour of readings
        assert result["verdict"] == "link_slow" and said in result["headline"]
        assert "cable" in result["advice"]

    def test_gigabit_is_fine(self):
        assert assess(series(120, speed=1000))["verdict"] == "ok"

    @pytest.mark.parametrize("medium", ["wifi", "other"])
    def test_wifi_and_virtual_ports_are_not_judged_by_a_wired_speed(self, medium):
        assert assess(series(120, medium=medium, speed=None, duplex=None))["verdict"] == "ok"
        assert assess(series(120, medium=medium, speed=100))["verdict"] == "ok"

    def test_it_notes_when_traffic_has_hit_what_the_port_can_carry(self):
        result = assess(series(120, speed=100, down=lambda i: 94.0))
        assert any("94 Mbps down" in line and "most that port can carry" in line for line in result["evidence"])


class TestCongestion:
    def test_a_slowdown_with_light_traffic_and_no_known_capacity_does_not_claim_bandwidth(self):
        result = assess(series(220, inet=slow_from(100, 126)))
        assert result["verdict"] == "congested"
        assert result["congested_minutes"] == 30 and result["baseline_internet_ms"] == 14.0 and result["worst_internet_ms"] == 130.0
        assert "14 ms to 130 ms" in result["headline"] and "speed test" in result["advice"]

    def test_with_a_known_plan_light_traffic_means_more_bandwidth_would_not_help(self):
        result = assess(series(220, inet=slow_from(100, 126)), plan_down_mbps=100)
        assert result["verdict"] == "congested" and "well below" in result["advice"] and "more bandwidth would not" in result["advice"]

    def test_a_slow_router_too_means_the_problem_is_inside_the_network(self):
        rows = series(220, inet=slow_from(100, 126), gw=slow_from(100, 126, slow=90.0, normal=0.5), down=lambda i: 95.0 if 98 <= i < 128 else 3.0)
        result = assess(rows, plan_down_mbps=100)
        assert result["verdict"] == "congested" and "inside your own network" in result["advice"]

    def test_wifi_is_named_as_a_suspect(self):
        result = assess(series(220, inet=slow_from(100, 126), medium="wifi", speed=None, duplex=None))
        assert any("Wi-Fi" in line for line in result["evidence"])

    def test_a_few_slow_readings_are_noise_not_an_episode(self):
        every_twentieth = assess(series(220, inet=lambda i: 500.0 if i % 20 == 0 else 14.0))
        two_in_a_row = assess(series(220, inet=slow_from(100, 102)))  # 2 readings is minutes, not a slowdown
        assert every_twentieth["verdict"] == "ok" and two_in_a_row["verdict"] == "ok"
        assert every_twentieth["congested_minutes"] == 0

    def test_a_gap_in_the_readings_splits_a_slowdown(self):
        rows = series(220, inet=slow_from(100, 126))
        for row in rows[113:]:
            row["ts"] += 3600  # the machine was off for an hour in the middle of it
        assert assess(rows)["congested_minutes"] < 30

    def test_a_modest_rise_is_not_a_slowdown(self):
        assert assess(series(220, inet=slow_from(100, 126, slow=40.0)))["verdict"] == "ok"  # 14 -> 40 ms is under 3x and +40


class TestSaturation:
    def test_traffic_at_the_plan_speed_while_latency_climbs_says_buy_bandwidth(self):
        rows = series(220, inet=slow_from(100, 126), down=lambda i: 95.0 if 98 <= i < 128 else 3.0)
        result = assess(rows, plan_down_mbps=100)
        assert result["verdict"] == "saturated" and result["capacity_mbps"] == 100 and result["capacity_source"] == "plan"
        assert "full for about 30 minutes" in result["headline"] and "14 ms to 130 ms" in result["headline"]
        assert "faster plan would help" in result["advice"] and "your plan speed" in result["advice"]

    def test_upload_can_be_the_side_that_fills(self):
        rows = series(220, inet=slow_from(100, 126), up=lambda i: 19.0 if 98 <= i < 128 else 1.0)
        assert assess(rows, plan_down_mbps=500, plan_up_mbps=20)["verdict"] == "saturated"

    def test_a_speed_test_supplies_the_capacity(self):
        rows = series(220, inet=slow_from(100, 126), down=lambda i: 95.0 if 98 <= i < 128 else 3.0)
        result = assess(rows, speedtest={"available": True, "download_mbps": 100.0, "upload_mbps": 20.0})
        assert result["verdict"] == "saturated" and result["capacity_source"] == "speedtest"
        assert "a speed test" in result["advice"]

    def test_a_speed_test_that_failed_is_not_capacity(self):
        rows = series(220, inet=slow_from(100, 126), down=lambda i: 95.0 if 98 <= i < 128 else 3.0)
        assert assess(rows, speedtest={"available": False, "download_mbps": 100.0})["capacity_source"] == "observed_ceiling"

    def test_with_no_plan_or_test_a_ceiling_the_traffic_keeps_hitting_is_the_capacity(self):
        rows = series(220, inet=slow_from(100, 126), down=lambda i: 95.0 if 98 <= i < 128 else 3.0)
        result = assess(rows)
        assert result["verdict"] == "saturated" and result["capacity_source"] == "observed_ceiling" and result["capacity_mbps"] == 95
        assert "the highest speed it keeps reaching" in result["advice"]

    def test_a_single_burst_is_not_a_ceiling(self):
        rows = series(220, inet=slow_from(100, 126), down=lambda i: 95.0 if i == 110 else 3.0)
        result = assess(rows)
        assert result["verdict"] == "congested" and result["capacity_mbps"] is None and result["capacity_source"] is None

    def test_slow_periods_that_are_mostly_not_at_capacity_are_not_saturation(self):
        rows = series(220, inet=slow_from(100, 126), down=lambda i: 95.0 if 98 <= i < 106 else 3.0)
        assert assess(rows, plan_down_mbps=100)["verdict"] == "congested"


class TestLoss:
    def test_many_resent_packets_over_the_last_hours(self):
        result = assess(series(200, resend_pct=lambda i: 3.0, segs=lambda i: 600))
        assert result["verdict"] == "lossy" and "3% of this machine's TCP packets" in result["headline"]

    @pytest.mark.parametrize(("pct", "segs"), [(1.0, 600), (3.0, 100)])
    def test_a_little_loss_or_too_little_traffic_to_judge_is_not_a_verdict(self, pct, segs):
        assert assess(series(200, resend_pct=lambda i: pct, segs=lambda i: segs))["verdict"] == "ok"

    def test_a_fault_that_ended_long_ago_is_not_reported(self):
        # 10% resent for the first 3 hours of nearly 12: over the whole window that is 2.5%, enough to
        # be a verdict, but the last 6 hours are clean, and only those are judged.
        rows = series(600, resend_pct=lambda i: 10.0 if i < 150 else 0.02, segs=lambda i: 600)
        assert assess(rows)["verdict"] == "ok"
        assert assess(series(600, resend_pct=lambda i: 10.0, segs=lambda i: 600))["verdict"] == "lossy"

    def test_port_errors_point_at_the_cable(self):
        result = assess(series(200, errs=lambda i: 1))
        assert result["verdict"] == "lossy" and "errors" in result["headline"] and "cable" in result["advice"]

    def test_lost_pings_to_the_router_are_loss_inside_the_network(self):
        result = assess(series(200, gw_loss=lambda i: 5.0))
        assert result["verdict"] == "lossy" and "router" in result["headline"] and "inside your own network" in result["advice"]

    def test_one_lost_burst_is_not_but_several_are(self):
        assert assess(series(200, gw_loss=lambda i: 100.0 if i == 77 else 0.0))["verdict"] == "ok"  # 0.5% of pings
        assert assess(series(200, gw_loss=lambda i: 100.0 if i % 50 == 0 else 0.0))["verdict"] == "lossy"  # 2%


class TestWhichProblemWins:
    def test_a_slow_port_beats_loss(self):
        assert assess(series(200, speed=100, resend_pct=lambda i: 3.0, segs=lambda i: 600))["verdict"] == "link_slow"

    def test_loss_beats_saturation(self):
        rows = series(220, inet=slow_from(100, 126), down=lambda i: 95.0 if 98 <= i < 128 else 3.0, resend_pct=lambda i: 3.0, segs=lambda i: 600)
        assert assess(rows, plan_down_mbps=100)["verdict"] == "lossy"


class TestBadReadings:
    def test_counters_reset_by_a_reboot_are_not_a_huge_rate(self):
        rows = series(220, down=lambda i: 3.0)
        base = rows[100]["rx_bytes"]
        for row in rows[100:]:
            row["rx_bytes"] -= base
        result = assess(rows)
        assert result["peak_mbps"]["down"] < 10 and result["verdict"] == "ok"

    def test_a_gap_is_not_an_interval_and_a_new_interface_is_not_a_continuation(self):
        rows = series(220)
        del rows[50:70]
        for row in rows[100:]:
            row["iface"] = "eth1"
        # 220 readings make 219 intervals; deleting 20 leaves 199, and two of those are not intervals:
        # the one that spans the gap, and the one where the interface changes.
        assert assess(rows)["samples"] == 197

    def test_missing_latency_and_counters_never_break_the_verdict(self):
        rows = series(120)
        for row in rows:
            row.update(inet_rtt_ms=None, gw_rtt_ms=None, gw_loss_pct=None, tcp_out=None, tcp_retrans=None, rx_errs=None)
        assert assess(rows)["verdict"] == "ok"


class TestTheResult:
    def results(self):
        slow = series(220, inet=slow_from(100, 126), down=lambda i: 95.0 if 98 <= i < 128 else 3.0)
        return [
            assess([]), assess(series(120)), assess(series(30, speed=100)), assess(series(200, errs=lambda i: 1)),
            assess(slow, plan_down_mbps=100), assess(slow), assess(series(220, inet=slow_from(100, 126))),
        ]

    def test_the_shape_the_dashboard_reads(self):
        for result in self.results():
            assert result["verdict"] in VERDICTS
            assert isinstance(result["headline"], str) and result["headline"]
            assert result["advice"] is None or isinstance(result["advice"], str)
            assert isinstance(result["evidence"], list) and len(result["evidence"]) <= 5
            assert set(result["peak_mbps"]) == {"down", "up"}
            assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", result["computed_at"])

    def test_it_is_plain_json_with_no_nan(self):
        for result in self.results():
            text = json.dumps(result, allow_nan=False)  # raises on NaN or Infinity
            assert "None" not in result["headline"] and "nan" not in text.lower()

    def test_the_words_are_plain_and_free_of_dashes_and_curly_quotes(self):
        for result in self.results():
            words = " ".join([result["headline"], result["advice"] or "", *result["evidence"]])
            assert not re.search("[\u2013\u2014\u2018\u2019\u201c\u201d]", words)
            assert "bufferbloat" not in words.lower()


class TestHelpers:
    def test_percentile(self):
        assert _percentile([], 0.5) is None
        assert _percentile([5], 0.99) == 5
        assert _percentile(list(range(1, 101)), 0.2) == 21
        assert _percentile([3, 1, 2], 0.5) == 2

    def test_numbers_read_naturally(self):
        assert [_fmt(v) for v in (None, 14.0, 12.9, 0.49, 130.2, 3.04)] == ["unknown", "14", "12.9", "0.5", "130", "3"]
        assert not math.isnan(float(_fmt(7.5)))
