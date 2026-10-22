"""What a systemd journal looks like when a boot ended on purpose.

A boot that was shut down or rebooted normally ends with systemd writing a run of lines
about stopping: the shutdown target, systemd-shutdown syncing and unmounting, and last of
all journald saying it has stopped. A boot that was cut off (power lost, a hard lock-up, a
reset) has none of that: its log just ends in the middle of ordinary activity.

The patterns here were first written for older systemd, and two things were missed that
matter on current releases. A plain `reboot` on Ubuntu 24.04 ends with "Reached target
reboot.target - System Reboot" and "systemd[1]: Shutting down.", which the earlier patterns
("Reached target ... Power-Off", "systemd-shutdown ... Shutting down") do not match, so a
normal reboot was reported as a crash. And `journalctl -u systemd-shutdown` finds nothing
even after a clean shutdown, because those lines are written under a syslog identifier, not
a unit, so the "shutdown record" the detector asked for never existed.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

# Lines that only appear while the machine is going down on purpose. Safe to look for
# anywhere in a log: nothing else prints them.
SHUTDOWN_SEQUENCE_PATTERNS: list[str] = [
    r"System is powering down",
    r"Reached target.*Power-Off",
    r"reboot: Power down",
    r"Stopped target.*Shutdown",
    r"systemd-shutdown.*Shutting down",
    r"systemd-shutdown\[\d+\]: (?:Syncing filesystems|Sending SIG(?:TERM|KILL)|Unmounting file systems"
    r"|Detaching|Powering off|Rebooting|Halting)",
    # PID 1 only. Every login session has its own systemd, with its own shutdown.target, that
    # reaches it whenever the session ends, long before the machine does.
    r"systemd\[1\]: Reached target (?:reboot|poweroff|halt|kexec|final|shutdown)\.target",
    r"systemd\[1\]: Shutting down\.",
    r"systemd-logind\[\d+\]: (?:System is (?:rebooting|halting)|The system will (?:power off|reboot|halt))",
]

# journald prints this when it stops, which is the very last line of a normal shutdown. It
# also prints it when journald alone is restarted (a package upgrade does that), so it only
# counts at the END of a log, never in the middle of one.
_JOURNAL_STOPPED = re.compile(r"systemd-journald\[\d+\]: Journal stopped")
_SEQUENCE = re.compile("|".join(f"(?:{pattern})" for pattern in SHUTDOWN_SEQUENCE_PATTERNS), re.IGNORECASE)

# How many of the final lines of a log are looked at when asking "did this end on purpose".
END_WINDOW_LINES = 40

# What journalctl prints, on stdout, when a query matches nothing. It is text, so a check
# for "is the output empty" never sees an empty result without removing it first.
_NO_ENTRIES = "-- No entries --"


def journal_output(text: str | None) -> str:
    """journalctl's output with its "nothing found" placeholder removed."""
    stripped = (text or "").strip()
    return "" if stripped == _NO_ENTRIES else stripped


def ended_with_shutdown(lines: Iterable[str]) -> bool:
    """Whether the end of a boot's log shows the machine being shut down or rebooted on purpose."""
    tail = [line for line in lines if line.strip()][-END_WINDOW_LINES:]
    return any(_JOURNAL_STOPPED.search(line) or _SEQUENCE.search(line) for line in tail)
