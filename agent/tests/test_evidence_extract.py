from __future__ import annotations

from crashpilot.analyzers.evidence_extract import (
    extract_evidence_metadata,
    extract_pid,
    extract_process,
    extract_timestamp,
)


def test_extracts_pid_and_process_from_oom_kill_line():
    line = "Out of memory: Kill process 38291 (torch_train) score 923 or sacrifice child"
    assert extract_pid(line) == 38291
    assert extract_process(line) == "torch_train"


def test_extracts_iso_timestamp_from_journalctl_short_iso_line():
    line = "2026-06-15T03:12:44+0000 host kernel: Out of memory: Killed process 1234 (python3)"
    assert extract_timestamp(line) == "2026-06-15T03:12:44+0000"


def test_extracts_bracketed_timestamp_from_dmesg_dash_t_line():
    line = "[Sun Jun 15 03:12:44 2026] NVRM: Xid (PCI:0000:01:00): 79, pid=5678, name=Xorg"
    assert extract_timestamp(line) == "Sun Jun 15 03:12:44 2026"
    assert extract_pid(line) == 5678
    assert extract_process(line) == "Xorg"


def test_does_not_treat_dmesg_monotonic_time_as_a_wall_clock_timestamp():
    # [12345.678901] is seconds-since-boot, not a date — must not be
    # reported as a timestamp (that would be fabricating a date).
    line = "[12345.678901] kswapd0: page allocation failure order=3"
    meta = extract_evidence_metadata(line)
    assert meta == {"timestamp": None, "process": None, "pid": None}


def test_extracts_process_name_from_invoked_oom_killer_line():
    line = "torch_train invoked oom-killer: gfp_mask=0x, order=0"
    assert extract_process(line) == "torch_train"


def test_returns_none_for_all_fields_when_nothing_is_present():
    line = "Reached target Power-Off."
    meta = extract_evidence_metadata(line)
    assert meta == {"timestamp": None, "process": None, "pid": None}
