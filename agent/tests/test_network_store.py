"""The agent's local record of network readings and of the bytes moved each month."""

from __future__ import annotations

import time
from datetime import datetime, timezone

import pytest

from crashpilot.storage.store import (
    get_network_usage,
    init_db,
    list_network_samples,
    save_network_sample,
)

OCT_15 = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc).timestamp()
GB = 10**9


def reading(ts: float, rx: int = 0, tx: int = 0, iface: str = "eth0", **extra) -> dict:
    return {
        "ts": ts, "iface": iface, "medium": "ethernet", "link_speed_mbps": 1000, "duplex": "full",
        "rx_bytes": rx, "tx_bytes": tx, "rx_errs": 0, "rx_drops": 0, "tx_errs": 0, "tx_drops": 0,
        "tcp_out": 100, "tcp_retrans": 1, "gw_rtt_ms": 0.5, "gw_loss_pct": 0.0, "inet_rtt_ms": 14.0, **extra,
    }


@pytest.fixture(autouse=True)
def _db():
    init_db()


class TestSamples:
    def test_what_is_saved_comes_back_oldest_first(self):
        now = time.time()
        for age in (300, 200, 100):
            save_network_sample(reading(now - age, rx=age))
        rows = list_network_samples(1)
        assert [r["rx_bytes"] for r in rows] == [300, 200, 100]
        assert rows[0]["iface"] == "eth0" and rows[0]["inet_rtt_ms"] == 14.0 and rows[0]["link_speed_mbps"] == 1000

    def test_the_same_moment_is_replaced_not_duplicated(self):
        now = time.time() - 10
        save_network_sample(reading(now, rx=1))
        save_network_sample(reading(now, rx=2))
        assert [r["rx_bytes"] for r in list_network_samples(1)] == [2]

    def test_readings_older_than_the_retention_are_dropped_as_new_ones_arrive(self):
        now = time.time()
        save_network_sample(reading(now - 9 * 86400, rx=1))
        save_network_sample(reading(now - 7 * 86400, rx=2))
        save_network_sample(reading(now - 60, rx=3))  # this save prunes anything older than 8 days
        assert [r["rx_bytes"] for r in list_network_samples(24 * 30)] == [2, 3]

    def test_the_window_and_the_future_are_respected(self):
        now = time.time()
        save_network_sample(reading(now - 7200, rx=1))
        save_network_sample(reading(now - 60, rx=2))
        save_network_sample(reading(now + 86400, rx=3))  # the clock ran ahead
        assert [r["rx_bytes"] for r in list_network_samples(1)] == [2]
        assert [r["rx_bytes"] for r in list_network_samples(3)] == [1, 2]

    def test_the_limit_keeps_the_newest(self):
        now = time.time()
        for age in (500, 400, 300, 200):
            save_network_sample(reading(now - age, rx=age))
        assert [r["rx_bytes"] for r in list_network_samples(1, limit=2)] == [300, 200]

    @pytest.mark.parametrize("missing", ["ts", "iface", "rx_bytes", "tx_bytes"])
    def test_a_reading_without_its_essentials_is_refused(self, missing):
        bad = reading(time.time())
        del bad[missing]
        with pytest.raises(ValueError):
            save_network_sample(bad)

    def test_optional_values_may_be_absent(self):
        save_network_sample({"ts": time.time() - 5, "iface": "eth0", "rx_bytes": 1, "tx_bytes": 2})
        (row,) = list_network_samples(1)
        assert row["gw_rtt_ms"] is None and row["tcp_out"] is None and row["medium"] is None


class TestMonthlyUsage:
    def test_nothing_recorded_is_none(self):
        assert get_network_usage("2026-10") is None

    def test_the_first_reading_counts_nothing_and_later_ones_add_their_growth(self):
        save_network_sample(reading(OCT_15, rx=100 * GB, tx=10 * GB))
        assert get_network_usage("2026-10")["rx_bytes"] == 0  # no earlier reading to difference against
        save_network_sample(reading(OCT_15 + 70, rx=100 * GB + 5_000_000, tx=10 * GB + 1_000_000))
        save_network_sample(reading(OCT_15 + 140, rx=100 * GB + 9_000_000, tx=10 * GB + 3_000_000))
        usage = get_network_usage("2026-10")
        assert (usage["rx_bytes"], usage["tx_bytes"]) == (9_000_000, 3_000_000)
        assert usage["since"] == "2026-10-15T12:00:00Z"

    def test_a_reboot_does_not_lose_what_came_before_it(self):
        save_network_sample(reading(OCT_15, rx=50 * GB, tx=GB))
        save_network_sample(reading(OCT_15 + 70, rx=50 * GB + 1000, tx=GB + 500))
        save_network_sample(reading(OCT_15 + 140, rx=700, tx=300))  # counters back to near zero
        save_network_sample(reading(OCT_15 + 210, rx=1700, tx=800))
        usage = get_network_usage("2026-10")
        assert (usage["rx_bytes"], usage["tx_bytes"]) == (1000 + 700 + 1000, 500 + 300 + 500)

    def test_time_the_machine_was_off_is_not_counted_as_traffic(self):
        save_network_sample(reading(OCT_15, rx=1000, tx=1000))
        save_network_sample(reading(OCT_15 + 7 * 3600, rx=900 * GB, tx=900 * GB))  # 7 hours later: no baseline to trust
        assert get_network_usage("2026-10")["rx_bytes"] == 0
        save_network_sample(reading(OCT_15 + 7 * 3600 + 70, rx=900 * GB + 4000, tx=900 * GB + 2000))
        assert get_network_usage("2026-10")["rx_bytes"] == 4000

    def test_a_different_interface_is_not_a_continuation(self):
        save_network_sample(reading(OCT_15, rx=1000, tx=1000, iface="eth0"))
        save_network_sample(reading(OCT_15 + 70, rx=5 * GB, tx=5 * GB, iface="wlan0"))
        assert get_network_usage("2026-10")["rx_bytes"] == 0
        save_network_sample(reading(OCT_15 + 140, rx=5 * GB + 100, tx=5 * GB + 50, iface="wlan0"))
        assert get_network_usage("2026-10")["rx_bytes"] == 100

    def test_each_month_has_its_own_row(self):
        end_of_oct = datetime(2026, 10, 31, 23, 59, 0, tzinfo=timezone.utc).timestamp()
        save_network_sample(reading(end_of_oct, rx=1000, tx=1000))
        save_network_sample(reading(end_of_oct + 30, rx=1600, tx=1200))  # 23:59:30, still October
        save_network_sample(reading(end_of_oct + 90, rx=2000, tx=1500))  # 00:00:30 on 1 November
        assert get_network_usage("2026-10")["rx_bytes"] == 600
        november = get_network_usage("2026-11")
        assert november["rx_bytes"] == 400 and november["tx_bytes"] == 300
        assert november["since"] == "2026-11-01T00:00:30Z"

    def test_the_current_month_is_the_default(self):
        now = time.time()
        save_network_sample(reading(now - 140, rx=0, tx=0))
        save_network_sample(reading(now - 70, rx=800, tx=100))
        month = datetime.now(timezone.utc).strftime("%Y-%m")
        assert get_network_usage()["month"] == month
