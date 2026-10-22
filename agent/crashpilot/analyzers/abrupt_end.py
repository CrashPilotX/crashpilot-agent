"""What the previous boot was doing when its log stopped, for a stop that left no error behind.

A machine that loses power, or locks up hard, cannot write down why. The detector used to
say "unknown, 30% confidence" and tell the owner to go and read the journal. But the log
does say something even then, in what is around the cut-off:

  - how it ended: with no shutdown sequence at all, in the middle of ordinary activity;
  - what had just started: a stress test, a benchmark, a CI job, a scheduled command;
  - how long the machine then stayed down, which separates an automatic reset (a minute or
    two) from a machine that stayed dead until someone restarted it;
  - and above all whether this is the first time. A machine whose last several boots all
    ended the same way, with nothing in the logs, is not having a software problem.

This reads only what the collectors already gathered; it runs nothing. It returns None
when there is no previous-boot log to read, and when the log ended with a shutdown
sequence (that is a normal shutdown, not a stop).
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from ..shutdown_markers import ended_with_shutdown

# journalctl --output=short-iso: "2026-10-22T14:12:39+00:00 host unit[pid]: message". Older
# systemd prints the offset as +0000, and a line may have none, which is read as UTC.
_STAMP = re.compile(r"^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2}:\d{2})(Z|[+-]\d{2}:?\d{2})?\s")

# Lines that say nothing about what the machine was doing: the agent's own timers, sysstat,
# and login-session bookkeeping that happens whenever anyone connects.
_ROUTINE = re.compile(
    r"crashpilot|sysstat-(?:collect|summary)|pam_unix\(|session-\d+\.scope|user-\d+\.slice|user@\d+"
    r"|user-runtime-dir@|systemd-logind\[\d+\]: (?:New session|Session \d+ logged out|Removed session"
    r"|Watching system buttons)",
    re.IGNORECASE,
)

# Something starting, as the journal records it.
_STARTS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("CI job", re.compile(r"Running job: (?P<what>.+?)(?: on \S+)?\s*$")),
    ("service", re.compile(r"systemd\[\d+\]: Started (?P<what>\S+?)(?:\.service|\.scope|\.timer)?(?: - .*)?\s*$")),
    ("scheduled command", re.compile(r"CRON\[\d+\]: \([^)]*\) CMD \((?P<what>.+)\)\s*$")),
    ("command run with sudo", re.compile(r"sudo\[\d+\]: .*COMMAND=(?P<what>.+?)\s*$")),
)
# A sudo line that was refused started nothing.
_REFUSED = re.compile(r"password is required|incorrect password|not allowed|NOT in sudoers", re.IGNORECASE)

# Workloads that put a machine under heavy, sudden load. The word chosen is the one a person
# would use, because it ends up in a sentence ("a GPU stress test started 4 s before...").
_HEAVY: tuple[tuple[str, str, re.Pattern[str]], ...] = (
    ("gpu_stress", "a GPU stress test",
     re.compile(r"gpu[-_ ]?burn|cuda[-_ ]?memtest|memtest[gc]l?\b|furmark|gpu[-_ ]?stress|unigine", re.IGNORECASE)),
    ("cpu_stress", "a CPU stress test",
     re.compile(r"stress-ng|(?<![\w-])stress(?![\w-])|mprime|prime95|linpack|y-cruncher|cpuburn|stressapptest",
                re.IGNORECASE)),
    ("memory_test", "a memory test", re.compile(r"memtester|memtest86", re.IGNORECASE)),
    ("benchmark", "a benchmark", re.compile(r"bench(?:mark)?(?![a-z])|geekbench|phoronix", re.IGNORECASE)),
    ("gpu_compute", "a GPU compute workload",
     re.compile(r"\bollama\b|llama[-_.]?cpp|\bvllm\b|comfyui|stable[-_ ]?diffusion", re.IGNORECASE)),
)

# How far back from the last line a start still counts as "just before the stop".
START_WINDOW_S = 300
# A start this close to the cut-off is reported as having come right before it.
RIGHT_BEFORE_S = 120
_MAX_STARTS = 5
_LAST_LINES = 5


def _stamp(line: str) -> datetime | None:
    match = _STAMP.match(line)
    if not match:
        return None
    zone = match.group(3) or "Z"
    zone = "+00:00" if zone == "Z" else (zone if ":" in zone else f"{zone[:3]}:{zone[3:]}")
    try:
        return datetime.fromisoformat(f"{match.group(1)}T{match.group(2)}{zone}").astimezone(timezone.utc)
    except ValueError:
        return None


def _iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def human_duration(seconds: float) -> str:
    seconds = int(round(seconds))
    if seconds < 90:
        return f"{seconds} s"
    minutes = round(seconds / 60)
    if minutes < 90:
        return f"{minutes} min"
    hours, rest = divmod(minutes, 60)
    return f"{hours} h {rest} min" if rest else f"{hours} h"


def when_text(seconds: int) -> str:
    """"in the same second the log stops" or "12 s before the log stops"."""
    return "in the same second the log stops" if seconds < 1 else f"{human_duration(seconds)} before the log stops"


def _heavy_kind(text: str) -> tuple[str, str] | None:
    for kind, label, pattern in _HEAVY:
        if pattern.search(text):
            return kind, label
    return None


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def _down_for(journal: dict[str, Any]) -> int | None:
    """Seconds between the previous boot's last log line and this boot's first."""
    boots = journal.get("boots") or []
    if len(boots) < 2:
        return None
    last = _parse_iso(boots[-2].get("last_entry"))
    first = _parse_iso(boots[-1].get("first_entry"))
    if last is None or first is None or first < last:
        return None
    return int((first - last).total_seconds())


def _gpu_vendor(telemetry: dict[str, Any]) -> str | None:
    gpu = telemetry.get("gpu") or {}
    if (gpu.get("nvidia") or {}).get("available"):
        return "nvidia"
    if (gpu.get("amd") or {}).get("available"):
        return "amd"
    return None


def _starts(lines: list[tuple[datetime, str]], end: datetime) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for moment, line in lines:
        age = (end - moment).total_seconds()
        if age > START_WINDOW_S or _ROUTINE.search(line) or _REFUSED.search(line):
            continue
        for via, pattern in _STARTS:
            match = pattern.search(line)
            if not match:
                continue
            what = match.group("what").strip()[:80]
            start: dict[str, Any] = {"seconds_before_end": int(age), "via": via, "what": what, "heavy": None}
            heavy = _heavy_kind(line)
            if heavy:
                start["heavy"], start["heavy_label"] = heavy
            found.append(start)
            break
    return found[-_MAX_STARTS:]


def analyze_abrupt_end(telemetry: dict[str, Any]) -> dict[str, Any] | None:
    """How the previous boot's log ended, or None if it ended with a shutdown or cannot be read."""
    journal = telemetry.get("journal") or {}
    raw = [line for line in (journal.get("previous_boot_logs_tail") or "").splitlines() if line.strip()]
    stamped = [(moment, line) for line in raw if (moment := _stamp(line)) is not None]
    if not stamped or ended_with_shutdown(raw):
        return None

    end = stamped[-1][0]
    meaningful = [line for _, line in stamped if not _ROUTINE.search(line)]
    starts = _starts(stamped, end)
    heavy = next((start for start in reversed(starts) if start["heavy"]), None)

    endings = [e for e in (journal.get("boot_endings") or []) if isinstance(e, dict) and e.get("ended_cleanly") is not None]
    analysis: dict[str, Any] = {
        "last_log_at": _iso(end),
        "last_lines": [line[:160] for line in meaningful[-_LAST_LINES:]],
        "lines_checked": len(raw),
        "down_for_s": _down_for(journal),
        "recent_starts": starts,
        "heavy_start": heavy,
        "boots_checked": len(endings),
        # The boot being analysed is one of the boots checked, and is abrupt by definition.
        "abrupt_boots": sum(1 for e in endings if e["ended_cleanly"] is False),
        "gpu_vendor": _gpu_vendor(telemetry),
    }
    return analysis


def describe(analysis: dict[str, Any]) -> list[str]:
    """The findings as plain sentences, in the order of how much they say."""
    lines = [
        f"The previous boot's log stops at {analysis['last_log_at'].replace('T', ' ').rstrip('Z')} UTC with no "
        "shutdown sequence: nothing was written to say the system was stopping."
    ]
    heavy = analysis.get("heavy_start")
    starts = analysis.get("recent_starts") or []
    if heavy and heavy["seconds_before_end"] <= RIGHT_BEFORE_S:
        lines.append(
            f"{heavy['heavy_label'][0].upper()}{heavy['heavy_label'][1:]} ({heavy['what']}) started "
            f"{when_text(heavy['seconds_before_end'])}."
        )
    elif starts:
        last = starts[-1]
        lines.append(f"The last thing started was {last['via']} \"{last['what']}\", {when_text(last['seconds_before_end'])}.")
    else:
        lines.append(
            f"Nothing was started in the {human_duration(START_WINDOW_S)} before the log stops: "
            "the machine was doing ordinary work."
        )
    lines.append(
        "None of the known crash messages (kernel panic, GPU fault, overheating, out of memory, disk error) "
        "appear in the end of that boot's log or in its logged errors."
    )
    down = analysis.get("down_for_s")
    if down is not None:
        if down >= 300:
            lines.append(
                f"The next boot started {human_duration(down)} later. A watchdog or panic reset takes a minute or "
                "two, so this points at a machine that stayed dead until it was restarted by hand."
            )
        else:
            lines.append(f"The next boot started {human_duration(down)} later, which fits an automatic reset or a power cycle.")
    if analysis.get("abrupt_boots", 0) >= 2:
        lines.append(
            f"{analysis['abrupt_boots']} of the last {analysis['boots_checked']} boots ended without a shutdown "
            "sequence."
        )
    return lines


def confidence(analysis: dict[str, Any]) -> float:
    """How sure the heuristic can be that this was a sudden stop, from what surrounds it.

    A log with no shutdown sequence and no error is the same evidence whatever started
    before it, so the starting point is the same as it always was. It rises when something
    corroborates a sudden stop rather than a crash that could not be logged: a heavy job
    that started right before it, and a machine that keeps stopping this way. It stays well
    under certainty, because the logs cannot tell a failing power supply from a lock-up.
    """
    value = 0.55
    heavy = analysis.get("heavy_start")
    if heavy and heavy["seconds_before_end"] <= RIGHT_BEFORE_S:
        value += 0.10
    if analysis.get("abrupt_boots", 0) >= 2:
        value += 0.15
    return round(min(value, 0.80), 2)
