"""
Built-in remediation knowledge base — makes the no-API-key experience useful.

When no Anthropic key is configured, CrashPilot still detects the crash type
heuristically. This module turns that classification into a plain-English root
cause, concrete remediation steps, and monitoring suggestions — so the free /
keyless path gives real, actionable answers instead of just a label.

Keyed by CrashType.value (see crash_detector.CrashType).
"""

from __future__ import annotations

from typing import Any

_DEFAULT: dict[str, Any] = {
    "root_cause": (
        "An abnormal shutdown or reboot was detected, but the logs don't contain "
        "a clear signature. The most common cause is a hard power loss or an "
        "external reset (no clean shutdown record was written)."
    ),
    "remediation": [
        {
            "priority": 1,
            "action": "Review the end of the previous boot: journalctl -b -1 -p warning --no-pager | tail -50",
            "rationale": "The last messages before the cut-off often hint at what happened.",
        },
        {
            "priority": 2,
            "action": "Check hardware health: sudo smartctl -a /dev/sda and sensors",
            "rationale": "Failing disks and overheating are frequent silent-reboot causes.",
        },
        {
            "priority": 3,
            "action": "Set CRASHPILOT_ANTHROPIC_API_KEY for an AI deep-dive on ambiguous cases.",
            "rationale": "AI analysis correlates weak signals that heuristics can't classify alone.",
        },
    ],
    "monitoring_suggestions": [
        "Track uptime / unexpected reboots over time.",
        "Enable persistent journald storage (Storage=persistent) so logs survive reboots.",
    ],
    "contributing_factors": [],
}

_ADVICE: dict[str, dict[str, Any]] = {
    "oom_kill": {
        "root_cause": "The kernel's out-of-memory killer terminated a process because the system ran out of available memory.",
        "remediation": [
            {
                "priority": 1,
                "action": "Find the killed process and the memory hog: journalctl -k | grep -iE 'killed process|out of memory'",
                "rationale": "Identifies which process was sacrificed and which consumed the memory.",
            },
            {
                "priority": 2,
                "action": "Add swap (or zram) as a safety buffer, or increase RAM.",
                "rationale": "Swap gives the kernel room to avoid killing processes under transient spikes.",
            },
            {
                "priority": 3,
                "action": "Cap the offending service with systemd: MemoryMax= / MemoryHigh= in its unit.",
                "rationale": "Bounds a leaky service so it can't take down the whole machine.",
            },
        ],
        "monitoring_suggestions": [
            "Alert when MemAvailable drops below ~10%.",
            "Track per-process RSS for the top consumers.",
        ],
        "contributing_factors": [
            "A memory leak in a long-running service.",
            "No swap configured, leaving no buffer for spikes.",
        ],
    },
    "kernel_panic": {
        "root_cause": "The kernel hit an unrecoverable error (panic / oops) and halted the system.",
        "remediation": [
            {
                "priority": 1,
                "action": "Read the panic trace: journalctl -k -b -1 --no-pager | grep -A40 -iE 'panic|BUG:|oops'",
                "rationale": "The call trace points to the driver or subsystem that faulted.",
            },
            {
                "priority": 2,
                "action": "Update the kernel and reload/blacklist any recently-changed out-of-tree modules.",
                "rationale": "Most panics trace to a buggy driver or a known kernel regression.",
            },
            {
                "priority": 3,
                "action": "Run a memory test (memtest86+) if the trace looks like memory corruption.",
                "rationale": "Bad RAM produces random panics that look like software bugs.",
            },
        ],
        "monitoring_suggestions": [
            "Enable kdump to capture a crash dump for the next panic.",
            "Alert on any kernel-level (priority 0-2) journal entries.",
        ],
        "contributing_factors": [
            "A recently installed or updated kernel module.",
            "Faulty RAM or an overclock.",
        ],
    },
    "thermal_shutdown": {
        "root_cause": "The system shut down (or throttled then died) because a component exceeded its critical temperature.",
        "remediation": [
            {
                "priority": 1,
                "action": "Inspect temperatures and throttling: sensors and journalctl -k | grep -i thermal",
                "rationale": "Confirms which sensor tripped and how hot it got.",
            },
            {
                "priority": 2,
                "action": "Clean dust, reseat heatsinks/fans, and verify airflow.",
                "rationale": "Most thermal events are cooling failures, not silicon faults.",
            },
            {
                "priority": 3,
                "action": "Reduce sustained load or cap CPU frequency (cpupower frequency-set).",
                "rationale": "Lowers heat output while you address cooling.",
            },
        ],
        "monitoring_suggestions": [
            "Alert when any core exceeds ~85°C.",
            "Graph fan RPM alongside temperature to catch failing fans.",
        ],
        "contributing_factors": [
            "Dust buildup or a failed fan.",
            "Dried-out thermal paste; ambient temperature too high.",
        ],
    },
    "power_loss": {
        "root_cause": "The machine lost power or was hard-reset — no clean shutdown record was written for the previous boot.",
        "remediation": [
            {
                "priority": 1,
                "action": "Check for a UPS / power events and loose power connections.",
                "rationale": "Sudden power loss leaves no software trace; the absence of a shutdown log is the signal.",
            },
            {
                "priority": 2,
                "action": "On a VM/cloud instance, check the host for spot/preemption or host maintenance events.",
                "rationale": "Spot interruptions and live-migration stalls look like power loss in guest logs.",
            },
            {
                "priority": 3,
                "action": "Rule out PSU/thermal: review sensors history and PSU health.",
                "rationale": "A failing PSU or thermal cutoff can mimic a clean power loss.",
            },
        ],
        "monitoring_suggestions": [
            "Add a UPS with monitored shutdown (nut).",
            "Track unexpected-reboot counts to spot a flaky PSU early.",
        ],
        "contributing_factors": [
            "No UPS; unreliable mains power.",
            "Cloud spot/preemptible instance reclaim.",
        ],
    },
    "watchdog_reset": {
        "root_cause": "A hardware/software watchdog reset the machine after the kernel stopped responding (hard lockup).",
        "remediation": [
            {
                "priority": 1,
                "action": "Look for the lockup just before reset: journalctl -k -b -1 | grep -iE 'watchdog|NMI|hard LOCKUP'",
                "rationale": "Shows which CPU/task wedged before the watchdog fired.",
            },
            {
                "priority": 2,
                "action": "Update drivers (especially GPU/network) and the kernel.",
                "rationale": "Hard lockups usually come from a driver spinning with interrupts disabled.",
            },
        ],
        "monitoring_suggestions": [
            "Alert on soft-lockup / RCU-stall warnings (early signs before a hard lockup).",
        ],
        "contributing_factors": ["A buggy driver holding a spinlock.", "Faulty hardware."],
    },
    "soft_lockup": {
        "root_cause": "A CPU was stuck on a task for too long (soft lockup / RCU stall / hung task) — often a precursor to a hard hang.",
        "remediation": [
            {
                "priority": 1,
                "action": "Identify the blocked task: journalctl -k -b -1 | grep -A20 -iE 'soft lockup|RCU.*stall|blocked for more than'",
                "rationale": "Names the task and stack that stalled the CPU.",
            },
            {
                "priority": 2,
                "action": "Check for I/O waits on a failing disk or an overloaded system.",
                "rationale": "Tasks blocked on slow/failing storage are a common cause.",
            },
        ],
        "monitoring_suggestions": [
            "Alert on hung-task and RCU-stall kernel messages.",
            "Track load average and iowait.",
        ],
        "contributing_factors": ["Storage I/O stalls.", "CPU oversubscription."],
    },
    "gpu_fault": {
        "root_cause": "The GPU faulted (e.g. NVIDIA Xid error, 'fell off the bus', or a GPU hang), which can hang or crash the system.",
        "remediation": [
            {
                "priority": 1,
                "action": "Read the GPU error: journalctl -k | grep -iE 'NVRM: Xid|amdgpu|GPU' and look up the Xid code.",
                "rationale": "Xid codes map to specific faults (ECC, fall-off-bus, app error).",
            },
            {
                "priority": 2,
                "action": "Update/reinstall the GPU driver; verify power and PCIe seating.",
                "rationale": "Driver mismatches and power delivery issues cause most GPU faults.",
            },
            {
                "priority": 3,
                "action": "Check GPU temperature and ECC errors (nvidia-smi -q).",
                "rationale": "Overheating or failing VRAM produces recurring Xid errors.",
            },
        ],
        "monitoring_suggestions": [
            "Alert on any NVRM Xid messages.",
            "Track GPU temperature and ECC error counts.",
        ],
        "contributing_factors": ["Driver/runtime mismatch.", "Insufficient PSU headroom."],
    },
    "disk_error": {
        "root_cause": "A storage device reported I/O errors (ATA/SCSI/NVMe or filesystem errors), which can corrupt data and crash the system.",
        "remediation": [
            {
                "priority": 1,
                "action": "Check SMART health: sudo smartctl -a /dev/sdX (look at reallocated/pending sectors).",
                "rationale": "Rising reallocated/pending sectors mean the drive is failing.",
            },
            {
                "priority": 2,
                "action": "Back up critical data now, then plan a replacement.",
                "rationale": "Disk errors tend to accelerate — act before total failure.",
            },
            {
                "priority": 3,
                "action": "Run a filesystem check (fsck) on the affected volume from a live environment.",
                "rationale": "Repairs filesystem damage caused by the I/O errors.",
            },
        ],
        "monitoring_suggestions": [
            "Enable smartd and alert on SMART attribute changes.",
            "Alert on EXT4/Btrfs/XFS error messages in the kernel log.",
        ],
        "contributing_factors": ["An aging or failing drive.", "A bad cable or controller."],
    },
    "machine_check_exception": {
        "root_cause": "The CPU raised a Machine Check Exception — a hardware-level error (memory, CPU, or bus).",
        "remediation": [
            {
                "priority": 1,
                "action": "Decode the MCE: install rasdaemon and review ras-mc-ctl --errors.",
                "rationale": "Pinpoints which DIMM/CPU/bank reported the error.",
            },
            {
                "priority": 2,
                "action": "Run memtest86+ and reseat/replace the implicated RAM.",
                "rationale": "Uncorrectable memory errors are the most common MCE source.",
            },
            {
                "priority": 3,
                "action": "Update BIOS/microcode; check CPU temperature and power.",
                "rationale": "Microcode fixes and thermal/power issues account for many MCEs.",
            },
        ],
        "monitoring_suggestions": [
            "Run rasdaemon and alert on any corrected/uncorrected MCE.",
            "Track ECC error rates per DIMM.",
        ],
        "contributing_factors": ["Failing RAM.", "Overheating or an unstable overclock."],
    },
    "clean_shutdown": {
        "root_cause": "The previous boot ended with a normal, clean shutdown — no crash.",
        "remediation": [],
        "monitoring_suggestions": [],
        "contributing_factors": [],
    },
}


def advice_for(crash_type: str) -> dict[str, Any]:
    """Return the remediation knowledge-base entry for a crash type (or a sensible default)."""
    return _ADVICE.get(crash_type, _DEFAULT)
