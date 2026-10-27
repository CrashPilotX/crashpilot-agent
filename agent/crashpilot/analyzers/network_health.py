"""Is this machine's network connection degraded, and if so, why? A verdict from a week of readings.

"Slow" is not one fault. The same complaint comes from a cable that negotiated 100 Mbps, from a
link losing packets, from a link that is simply full, and from a provider having a bad hour. They
need different answers (replace the cable, fix the Wi-Fi, buy more bandwidth, call the provider),
and only one of them is fixed by a faster plan. So the verdict keeps them apart and says which:

    link_slow   the wired port runs at 100 Mbps or less, or at half duplex. Needs no load to see.
    lossy       packets are being dropped or resent: many TCP retransmits, errors on the port, or
                pings to the router lost.
    saturated   latency to the internet climbed for a sustained period, while traffic was at the
                link's capacity. The link is the limit, and a faster plan would help.
    congested   latency climbed for a sustained period but traffic was low, or the router itself
                was slow to answer. More bandwidth would not have fixed it.
    ok          none of that in the window.
    learning    not enough readings yet (about an hour) to say.

Capacity is the weak point, because a plan's advertised speed is an "up to" figure. It is taken, in
order, from the plan speed in settings, from a speed test, or from the speed the traffic keeps
hitting and never exceeds (a ceiling reached repeatedly is what a full link looks like). Without
any of those a slowdown is reported as congestion and says why it cannot say more.

Nothing here runs anything; it reads the rows collectors/net_health.py stored.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

# Readings further apart than this are a gap (machine off, agent stopped), not an interval.
MIN_INTERVAL_S = 10
MAX_INTERVAL_S = 360
LEARNING_MINUTES = 60
# A slowdown must last this long, and cover at least this many consecutive readings, to count.
EPISODE_MIN_MINUTES = 15
EPISODE_MIN_READINGS = 2
# A reading is slow when internet latency is this many times its normal value, and at least this much higher.
INTERNET_FACTOR, INTERNET_MIN_RISE_MS = 3.0, 40.0
ROUTER_FACTOR, ROUTER_MIN_RISE_MS = 3.0, 20.0
# Traffic at this share of capacity counts as the link being full.
NEAR_CAPACITY = 0.8
# TCP resends and port errors are judged over the most recent hours, so an old fault that was fixed ends.
RECENT_HOURS = 6
RETRANSMIT_PCT = 1.5
MIN_SEGMENTS = 50_000
PORT_ERRORS = 50
ROUTER_LOSS_PCT = 1.0
# A traffic ceiling is only believed when it is reached this often, within this much of itself.
PLATEAU_READINGS, PLATEAU_BAND = 10, 0.95
MIN_CEILING_MBPS = 5.0
VERDICTS = ("learning", "ok", "link_slow", "lossy", "congested", "saturated")


def _percentile(values: list[float], share: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, int(round(share * (len(ordered) - 1)))))]


def _fmt(value: float | None) -> str:
    if value is None:
        return "unknown"
    return str(int(round(value))) if abs(value - round(value)) < 0.05 or value >= 100 else f"{value:.1f}"


def _minutes(value: float) -> str:
    return str(int(round(value)))


def _span(hours: float) -> str:
    """The window in words: '45 minutes', '4.3 hours', '7 days'."""
    if hours < 1.5:
        return f"{int(round(hours * 60))} minutes"
    if hours < 48:
        return f"{_fmt(hours)} hours"
    return f"{int(round(hours / 24))} days"


def _intervals(samples: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rates and changes between each pair of consecutive readings, skipping gaps and counter resets."""
    out: list[dict[str, Any]] = []
    for a, b in zip(samples, samples[1:], strict=False):
        dt = b["ts"] - a["ts"]
        if not MIN_INTERVAL_S <= dt <= MAX_INTERVAL_S or a.get("iface") != b.get("iface"):
            continue
        d_rx, d_tx = b["rx_bytes"] - a["rx_bytes"], b["tx_bytes"] - a["tx_bytes"]
        if d_rx < 0 or d_tx < 0:
            continue  # the counters were reset by a reboot: nothing can be said about this interval
        row: dict[str, Any] = {
            "ts": b["ts"], "dt": dt, "down": d_rx * 8 / dt / 1e6, "up": d_tx * 8 / dt / 1e6,
            "inet": b.get("inet_rtt_ms"), "gw": b.get("gw_rtt_ms"), "gw_loss": b.get("gw_loss_pct"),
            "segments": 0, "resent": 0, "errors": 0,
        }
        if a.get("tcp_out") is not None and b.get("tcp_out") is not None:
            segments, resent = b["tcp_out"] - a["tcp_out"], b["tcp_retrans"] - a["tcp_retrans"]
            if segments >= 0 and resent >= 0:
                row["segments"], row["resent"] = segments, resent
        errors = 0
        for key in ("rx_errs", "rx_drops", "tx_errs", "tx_drops"):
            if a.get(key) is not None and b.get(key) is not None and b[key] >= a[key]:
                errors += b[key] - a[key]
        row["errors"] = errors
        out.append(row)
    return out


def _episodes(intervals: list[dict[str, Any]], key: str, factor: float, rise_ms: float) -> tuple[list[dict[str, Any]], float | None]:
    """Intervals that belong to a sustained slowdown of `key` latency, and that latency's normal value."""
    values = [row[key] for row in intervals if row[key] is not None]
    if len(values) < 30:
        return [], None
    baseline = _percentile(values, 0.2)
    if baseline is None:
        return [], None
    threshold = max(factor * baseline, baseline + rise_ms)
    inside: list[dict[str, Any]] = []
    run: list[dict[str, Any]] = []
    previous = 0.0
    for row in intervals:
        slow = row[key] is not None and row[key] >= threshold
        if slow and (not run or row["ts"] - previous <= MAX_INTERVAL_S):
            run.append(row)
        else:
            if len(run) >= EPISODE_MIN_READINGS:
                inside.extend(run)
            run = [row] if slow else []
        previous = row["ts"]
    if len(run) >= EPISODE_MIN_READINGS:
        inside.extend(run)
    return inside, baseline


def _capacity(
    intervals: list[dict[str, Any]], plan_down: float | None, plan_up: float | None, speedtest: dict[str, Any] | None
) -> tuple[float | None, float | None, str | None]:
    if plan_down or plan_up:
        return plan_down, plan_up, "plan"
    test = speedtest or {}
    if test.get("available") and (test.get("download_mbps") or test.get("upload_mbps")):
        return test.get("download_mbps"), test.get("upload_mbps"), "speedtest"
    found: list[float | None] = []
    for direction in ("down", "up"):
        rates = [row[direction] for row in intervals]
        ceiling = _percentile(rates, 0.99) if len(rates) >= 100 else None
        near = [rate for rate in rates if ceiling and rate >= PLATEAU_BAND * ceiling]
        found.append(ceiling if ceiling and ceiling >= MIN_CEILING_MBPS and len(near) >= PLATEAU_READINGS else None)
    return found[0], found[1], ("observed_ceiling" if any(found) else None)


def _source_text(source: str | None) -> str:
    return {"plan": "your plan speed", "speedtest": "a speed test", "observed_ceiling": "the highest speed it keeps reaching"}.get(source or "", "")


def assess(
    samples: list[dict[str, Any]],
    *,
    plan_down_mbps: float | None = None,
    plan_up_mbps: float | None = None,
    speedtest: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The verdict on this machine's connection from its stored readings, oldest first."""
    intervals = _intervals(samples)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    minutes = sum(row["dt"] for row in intervals) / 60
    span_h = (samples[-1]["ts"] - samples[0]["ts"]) / 3600 if len(samples) > 1 else 0.0

    slow_inet, inet_base = _episodes(intervals, "inet", INTERNET_FACTOR, INTERNET_MIN_RISE_MS)
    slow_gw, gw_base = _episodes(intervals, "gw", ROUTER_FACTOR, ROUTER_MIN_RISE_MS)
    congested_min = sum(row["dt"] for row in slow_inet) / 60
    worst = max((row["inet"] for row in slow_inet), default=None)

    down_rates, up_rates = [row["down"] for row in intervals], [row["up"] for row in intervals]
    peak_down = _percentile(down_rates, 0.99) or 0.0
    peak_up = _percentile(up_rates, 0.99) or 0.0
    cap_down, cap_up, source = _capacity(intervals, plan_down_mbps, plan_up_mbps, speedtest)
    capacity = cap_down or cap_up

    def full(row: dict[str, Any]) -> bool:
        return bool((cap_down and row["down"] >= NEAR_CAPACITY * cap_down) or (cap_up and row["up"] >= NEAR_CAPACITY * cap_up))

    full_min = sum(row["dt"] for row in slow_inet if full(row)) / 60
    router_slow_share = 0.0
    if slow_inet and slow_gw:
        slow_ts = {row["ts"] for row in slow_gw}
        router_slow_share = sum(1 for row in slow_inet if row["ts"] in slow_ts) / len(slow_inet)

    recent = [row for row in intervals if row["ts"] >= (samples[-1]["ts"] - RECENT_HOURS * 3600)] if samples else []
    segments, resent = sum(r["segments"] for r in recent), sum(r["resent"] for r in recent)
    resent_pct = 100 * resent / segments if segments >= MIN_SEGMENTS else None
    errors = sum(r["errors"] for r in recent)
    router_loss = [r["gw_loss"] for r in recent if r["gw_loss"] is not None]
    router_loss_pct = sum(router_loss) / len(router_loss) if len(router_loss) >= 20 else None

    last = samples[-1] if samples else {}
    speed, duplex, medium, iface = last.get("link_speed_mbps"), last.get("duplex"), last.get("medium"), last.get("iface")
    slow_port = medium == "ethernet" and ((speed is not None and speed <= 100) or duplex == "half")

    result: dict[str, Any] = {
        "verdict": "ok", "headline": "", "advice": None, "evidence": [],
        "window_hours": round(span_h, 1), "samples": len(intervals),
        "capacity_mbps": capacity, "capacity_source": source if capacity else None,
        "peak_mbps": {"down": round(peak_down, 1), "up": round(peak_up, 1)},
        "congested_minutes": int(round(congested_min)),
        "baseline_internet_ms": round(inet_base, 1) if inet_base is not None else None,
        "worst_internet_ms": round(worst, 1) if worst is not None else None,
        "computed_at": now,
    }
    evidence: list[str] = []
    window = f"the last {_span(span_h)}"

    if slow_port:
        result["verdict"] = "link_slow"
        what = "half duplex" if duplex == "half" else f"{speed} Mbps"
        result["headline"] = f"This machine's network port runs at {what}."
        result["advice"] = (
            "A wired port should negotiate 1000 Mbps. A worn or damaged cable is the usual cause: replace it, "
            "then check the switch or router port it is plugged into."
        )
        evidence.append(f"The wired port {iface} negotiated {what}.")
        if peak_down >= 0.8 * (speed or 0) > 0:
            evidence.append(f"Traffic reached {_fmt(peak_down)} Mbps down, which is the most that port can carry.")
    elif resent_pct is not None and resent_pct >= RETRANSMIT_PCT or errors >= PORT_ERRORS or (router_loss_pct or 0) >= ROUTER_LOSS_PCT:
        result["verdict"] = "lossy"
        if router_loss_pct is not None and router_loss_pct >= ROUTER_LOSS_PCT:
            result["headline"] = f"Pings to your router are being lost ({_fmt(router_loss_pct)}%) over {RECENT_HOURS} hours."
            result["advice"] = "The loss is inside your own network: check the cable and port to the router, or the Wi-Fi signal if this machine uses it."
            evidence.append(f"{_fmt(router_loss_pct)}% of pings to the router were lost.")
        elif errors >= PORT_ERRORS:
            result["headline"] = f"The network port is reporting errors ({errors} in the last {RECENT_HOURS} hours)."
            result["advice"] = "Port errors almost always mean a bad cable or connector. Replace the cable and check again."
            evidence.append(f"{errors} packets were errored or dropped on {iface}.")
        else:
            result["headline"] = f"{_fmt(resent_pct)}% of this machine's TCP packets had to be sent again over the last {RECENT_HOURS} hours."
            result["advice"] = (
                "Resent packets mean data is being lost between this machine and the internet: a bad cable, a weak "
                "Wi-Fi signal, or a congested link. Test the cable first; if pings to your router are clean, the loss is beyond it."
            )
            evidence.append(f"{resent} of {segments} TCP segments were resent.")
    elif congested_min >= EPISODE_MIN_MINUTES and capacity and full_min >= 0.5 * congested_min and router_slow_share < 0.5:
        result["verdict"] = "saturated"
        result["headline"] = (
            f"Your internet link was full for about {_minutes(full_min)} minutes in {window}, and latency rose from "
            f"{_fmt(inet_base)} ms to {_fmt(worst)} ms each time."
        )
        result["advice"] = (
            "The link is the limit, so a faster plan would help, as would moving large transfers to quiet hours. "
            f"Capacity is taken from {_source_text(source)}: {_fmt(capacity)} Mbps."
        )
        evidence.append(f"Traffic was at or near {_fmt(capacity)} Mbps for {_minutes(full_min)} of the {_minutes(congested_min)} slow minutes.")
    elif congested_min >= EPISODE_MIN_MINUTES:
        result["verdict"] = "congested"
        result["headline"] = (
            f"Internet latency rose from {_fmt(inet_base)} ms to {_fmt(worst)} ms for about {_minutes(congested_min)} minutes in {window}."
        )
        if router_slow_share >= 0.5:
            result["advice"] = "The router was slow to answer too, so the slowdown is inside your own network: Wi-Fi, a switch, or a very busy router."
        elif capacity:
            result["advice"] = (
                "Traffic was well below the link's capacity at those times, so more bandwidth would not have fixed it. "
                "The cause is most likely your provider or another device on your network."
            )
        else:
            result["advice"] = (
                "This machine's traffic did not reach a clear ceiling, so the link is probably shared with other devices. "
                "A faster plan helps only if these slowdowns line up with your busiest hours; running a speed test on one "
                "machine lets CrashPilot tell."
            )
        evidence.append(f"{len(slow_inet)} readings were slow, in runs of at least {EPISODE_MIN_READINGS}.")
    elif minutes < LEARNING_MINUTES:
        result["verdict"] = "learning"
        result["headline"] = "Still collecting readings: a verdict needs about an hour."
    else:
        result["headline"] = f"No network problem found in {window}."

    verdict = result["verdict"]
    if verdict in ("lossy", "saturated", "congested") and slow_inet:
        down_avg = sum(r["down"] for r in slow_inet) / len(slow_inet)
        up_avg = sum(r["up"] for r in slow_inet) / len(slow_inet)
        evidence.append(f"During the slow periods traffic averaged {_fmt(down_avg)} Mbps down and {_fmt(up_avg)} Mbps up.")
    gw_values = [r["gw"] for r in intervals if r["gw"] is not None]
    if gw_values and verdict != "learning":
        evidence.append(f"Typical latency: {_fmt(_percentile(gw_values, 0.5))} ms to your router" + (f", {_fmt(inet_base)} ms to the internet." if inet_base else "."))
    if peak_down or peak_up:
        evidence.append(f"Highest speeds seen: {_fmt(peak_down)} Mbps down, {_fmt(peak_up)} Mbps up.")
    if medium == "wifi" and verdict in ("lossy", "congested"):
        evidence.append("This machine is connected over Wi-Fi, which is a common source of both.")
    result["evidence"] = evidence[:5]
    return result
