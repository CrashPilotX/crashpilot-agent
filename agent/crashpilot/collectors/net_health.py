"""The facts needed to judge a network connection, sampled on every heartbeat tick.

Traffic alone cannot say whether a connection is the reason things feel slow. A link that
carries 900 Mbps may be fine, and one that carries 20 Mbps may be crawling. What separates
the two is what the connection does under load, so each sample records, besides the byte
counters:

    link         the speed and duplex the port negotiated (a wired port stuck at 100 Mbps, or
                 at half duplex, is the commonest slow-network fault and needs no load to see);
    loss         dropped and errored packets on the port, and the share of TCP segments that
                 had to be sent again (read from the kernel's counters, free);
    latency      the round trip to the router, and to the internet. The internet figure is the
                 time to open a TCP connection to the dashboard host, the one address the
                 agent already talks to, so it adds no new destination. Latency climbing while
                 traffic is high is how a saturated link shows itself.

The samples go into a small local table (storage/store.py) and analyzers/network_health.py
turns a week of them into a verdict. Nothing here needs root. The router is pinged with the
system's own `ping` (3 packets, well under a second) and skipped if there is none.
"""

from __future__ import annotations

import re
import shutil
import socket
import struct
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

# The same virtual interfaces the heartbeat's traffic figures leave out.
_SKIP_PREFIXES = ("br-", "cni", "docker", "flannel", "kube-ipvs", "lo", "veth", "virbr", "tailscale", "wg", "tun", "tap")
_RTT = re.compile(r"= [\d.]+/([\d.]+)/")
_LOSS = re.compile(r"([\d.]+)% packet loss")
_RTF_GATEWAY = 0x2


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None


def default_route(proc_root: Path = Path("/proc")) -> tuple[str, str] | None:
    """(interface, gateway address) of the default route, or None when there is no network."""
    text = _read(proc_root / "net" / "route")
    if not text:
        return None
    best: tuple[int, str, str] | None = None
    for line in text.splitlines()[1:]:
        fields = line.split()
        if len(fields) < 8 or fields[1] != "00000000":
            continue
        try:
            flags, metric = int(fields[3], 16), int(fields[6])
            gateway = socket.inet_ntoa(struct.pack("<L", int(fields[2], 16)))
        except (ValueError, struct.error, OSError):
            continue
        if flags & _RTF_GATEWAY and (best is None or metric < best[0]):
            best = (metric, fields[0], gateway)
    return (best[1], best[2]) if best else None


def link_info(interface: str, sys_root: Path = Path("/sys")) -> dict[str, Any]:
    """Medium, negotiated speed and duplex of an interface. Unknown values are None."""
    base = sys_root / "class" / "net" / interface
    if (base / "wireless").exists() or (base / "phy80211").exists():
        medium = "wifi"
    elif (base / "device").exists():
        medium = "ethernet"
    else:
        medium = "other"  # a bridge, a bond or a virtual port: its speed says nothing
    speed: int | None = None
    raw = _read(base / "speed")
    if raw:
        try:
            value = int(raw)
            speed = value if value > 0 else None  # -1 is what an unplugged or virtual port reports
        except ValueError:
            speed = None
    duplex = _read(base / "duplex")
    return {
        "medium": medium,
        "link_speed_mbps": speed if medium == "ethernet" else None,
        "duplex": duplex if duplex in ("full", "half") and medium == "ethernet" else None,
    }


def read_counters(interface: str, proc_root: Path = Path("/proc")) -> dict[str, int] | None:
    """Byte, error and drop counters of one interface, and the system's TCP segment counters."""
    text = _read(proc_root / "net" / "dev")
    if not text:
        return None
    counters: dict[str, int] | None = None
    for line in text.splitlines()[2:]:
        name, _, rest = line.partition(":")
        if name.strip() != interface:
            continue
        fields = rest.split()
        if len(fields) < 16:
            return None
        try:
            counters = {
                "rx_bytes": int(fields[0]), "rx_errs": int(fields[2]), "rx_drops": int(fields[3]),
                "tx_bytes": int(fields[8]), "tx_errs": int(fields[10]), "tx_drops": int(fields[11]),
            }
        except ValueError:
            return None
    if counters is None:
        return None
    snmp = _read(proc_root / "net" / "snmp") or ""
    rows = [line.split() for line in snmp.splitlines() if line.startswith("Tcp:")]
    if len(rows) >= 2:
        try:
            tcp = dict(zip(rows[0][1:], map(int, rows[1][1:]), strict=False))
            counters["tcp_out"], counters["tcp_retrans"] = tcp["OutSegs"], tcp["RetransSegs"]
        except (ValueError, KeyError):
            pass
    return counters


def ping_gateway(address: str, run: Callable[..., Any] = subprocess.run) -> dict[str, float] | None:
    """Average round trip and loss of 3 pings to the router, or None if there is no ping or no answer."""
    if run is subprocess.run and not shutil.which("ping"):
        return None
    try:
        result = run(["ping", "-c", "3", "-i", "0.2", "-W", "1", address], capture_output=True, text=True, timeout=6)
    except (OSError, subprocess.SubprocessError):
        return None
    rtt, loss = _RTT.search(result.stdout or ""), _LOSS.search(result.stdout or "")
    if not rtt:
        return {"rtt_ms": float("nan"), "loss_pct": 100.0} if loss and float(loss.group(1)) >= 100 else None
    return {"rtt_ms": round(float(rtt.group(1)), 2), "loss_pct": float(loss.group(1)) if loss else 0.0}


def connect_time_ms(
    host: str,
    port: int = 443,
    timeout: float = 3.0,
    connect: Callable[..., Any] = socket.create_connection,
    clock: Callable[[], float] = time.perf_counter,
) -> float | None:
    """Milliseconds to open a TCP connection to host:port (the name is resolved before the clock starts)."""
    try:
        family, kind, proto, _, address = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)[0]
    except (OSError, IndexError):
        return None
    started = clock()
    try:
        sock = connect(address[:2], timeout=timeout)
    except OSError:
        return None
    elapsed = (clock() - started) * 1000
    try:
        sock.close()
    except OSError:
        pass
    return round(elapsed, 1)


def dashboard_host(supabase_url: str) -> str | None:
    host = urlparse(supabase_url).hostname if supabase_url else None
    return host or None


def take_sample(
    *,
    dashboard_url: str = "",
    probe_gateway: bool = True,
    probe_internet: bool = True,
    sys_root: Path = Path("/sys"),
    proc_root: Path = Path("/proc"),
    ping: Callable[[str], dict[str, float] | None] = ping_gateway,
    connect: Callable[[str], float | None] = connect_time_ms,
    now: Callable[[], float] = time.time,
) -> dict[str, Any] | None:
    """One reading of the default connection, or None when the machine has no default route."""
    route = default_route(proc_root)
    if route is None:
        return None
    interface, gateway = route
    if interface.startswith(_SKIP_PREFIXES):
        return None
    counters = read_counters(interface, proc_root)
    if counters is None:
        return None

    sample: dict[str, Any] = {"ts": now(), "iface": interface, **link_info(interface, sys_root), **counters}
    sample["gw_rtt_ms"] = sample["gw_loss_pct"] = sample["inet_rtt_ms"] = None
    if probe_gateway:
        reply = ping(gateway)
        if reply:
            rtt = reply["rtt_ms"]
            sample["gw_rtt_ms"] = None if rtt != rtt else rtt  # NaN: every ping was lost
            sample["gw_loss_pct"] = reply["loss_pct"]
    host = dashboard_host(dashboard_url)
    if probe_internet and host:
        sample["inet_rtt_ms"] = connect(host)
    return sample
