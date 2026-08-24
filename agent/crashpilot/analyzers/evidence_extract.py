"""Extract timestamp/process/PID from a raw log line — never invent one.

Used to enrich heuristic evidence lines (crash_detector.py) with structured
metadata the dashboard can display next to each piece of evidence. Every
function here returns None when the source line doesn't contain the field in
a recognizable form — callers must never substitute a guessed or computed
value, since the whole point is that evidence stays traceable to exactly what
was in the log.

Formats handled (matching what this agent's collectors actually produce):
  - journalctl --output=short-iso: "2026-06-15T03:12:44+0000 host proc[1234]: ..."
  - dmesg -T:                      "[Sun Jun 15 03:12:44 2026] ..."
  - dmesg (no -T, monotonic):      "[12345.678901] ..." — NOT a wall-clock
    timestamp, so intentionally not extracted as one.
"""

from __future__ import annotations

import re

_ISO_TIMESTAMP_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:[+-]\d{2}:?\d{2}|Z)?")
_DMESG_T_TIMESTAMP_RE = re.compile(
    r"\[(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun) "
    r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) "
    r"[ \d]\d \d{2}:\d{2}:\d{2} \d{4}\]"
)
_SYSLOG_TIMESTAMP_RE = re.compile(
    r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{1,2}\s+\d{2}:\d{2}:\d{2}"
)

# "process 1234 (name)" / "process 1234 (name)" — the kernel OOM-killer's own
# wording ("Killed process", "Kill process ... or sacrifice child").
_PROCESS_PID_PAREN_RE = re.compile(r"process\s+(\d+)\s*\(([^)]+)\)", re.IGNORECASE)
# "name invoked oom-killer" — process name appears before the phrase, no PID.
_INVOKED_OOM_RE = re.compile(r"^\s*(\S+)\s+invoked oom-killer", re.IGNORECASE)
# GPU/driver style: "pid=1234" / "pid: 1234", optionally with "name=foo".
_PID_KV_RE = re.compile(r"\bpid[=:]\s*(\d+)", re.IGNORECASE)
_NAME_KV_RE = re.compile(r"\b(?:name|comm)[=:]\s*([^\s,)]+)", re.IGNORECASE)


def extract_timestamp(line: str) -> str | None:
    """Return the timestamp exactly as it appears in the line, or None."""
    for pattern in (_ISO_TIMESTAMP_RE, _DMESG_T_TIMESTAMP_RE, _SYSLOG_TIMESTAMP_RE):
        match = pattern.search(line)
        if match:
            return match.group(0).strip("[]")
    return None


def extract_pid(line: str) -> int | None:
    match = _PROCESS_PID_PAREN_RE.search(line)
    if match:
        return int(match.group(1))
    match = _PID_KV_RE.search(line)
    if match:
        return int(match.group(1))
    return None


def extract_process(line: str) -> str | None:
    match = _PROCESS_PID_PAREN_RE.search(line)
    if match:
        return match.group(2)
    match = _NAME_KV_RE.search(line)
    if match:
        return match.group(1)
    match = _INVOKED_OOM_RE.search(line)
    if match:
        return match.group(1)
    return None


def extract_evidence_metadata(line: str) -> dict[str, str | int | None]:
    """Best-effort timestamp/process/PID for one evidence line — all three are
    None when not literally present in the text, never guessed."""
    return {
        "timestamp": extract_timestamp(line),
        "process": extract_process(line),
        "pid": extract_pid(line),
    }
