"""
Built-in remediation knowledge base: makes the no-API-key experience useful.

When no Anthropic key is configured, CrashPilot still detects the crash type
heuristically. This module turns that classification into a plain-English root
cause, concrete remediation steps, and monitoring suggestions: so the free /
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
        "root_cause": "The machine lost power or was hard-reset: no clean shutdown record was written for the previous boot.",
        "remediation": [
            {
                "priority": 1,
                "action": "Check for a UPS / power events and loose power connections.",
                "rationale": "Sudden power loss leaves no software trace; the absence of a shutdown log is the signal.",
            },
            {
                "priority": 2,
                "action": "If this Ubuntu machine is virtualized, check the host for maintenance or power events.",
                "rationale": "Host-level interruptions can look like power loss in guest logs.",
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
        "root_cause": "A CPU was stuck on a task for too long (soft lockup / RCU stall / hung task): often a precursor to a hard hang.",
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
        "root_cause": "The GPU faulted (e.g. an NVIDIA Xid error, an AMD ring timeout or GPU reset, 'fell off the bus', or a GPU hang), which can hang or crash the system.",
        "remediation": [
            {
                "priority": 1,
                "action": "Read the GPU error: journalctl -k | grep -iE 'NVRM: Xid|amdgpu|GPU'. NVIDIA: look up the Xid code. AMD: look for 'ring ... timeout', 'GPU reset' and page faults.",
                "rationale": "Xid codes (NVIDIA) and ring timeouts or resets (AMD) tell a hardware fault, a bus problem and an application error apart.",
            },
            {
                "priority": 2,
                "action": "Update/reinstall the GPU driver; verify power and PCIe seating.",
                "rationale": "Driver mismatches and power delivery issues cause most GPU faults.",
            },
            {
                "priority": 3,
                "action": "Check GPU temperature and memory errors (NVIDIA: nvidia-smi -q; AMD: rocm-smi, or the temperatures under /sys/class/drm/card*/device/hwmon).",
                "rationale": "Overheating or failing VRAM produces recurring GPU faults.",
            },
        ],
        "monitoring_suggestions": [
            "Alert on NVIDIA Xid messages and on AMD ring timeouts and GPU resets.",
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
                "rationale": "Disk errors tend to accelerate: act before total failure.",
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
        "root_cause": "The CPU raised a Machine Check Exception: a hardware-level error (memory, CPU, or bus).",
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
        "root_cause": "The previous boot ended with a normal, clean shutdown: no crash.",
        "remediation": [],
        "monitoring_suggestions": [],
        "contributing_factors": [],
    },
}


def advice_for(crash_type: str) -> dict[str, Any]:
    """Return the remediation knowledge-base entry for a crash type (or a sensible default)."""
    return _ADVICE.get(crash_type, _DEFAULT)


def advice_for_detection(detection: dict[str, Any]) -> dict[str, Any]:
    """Advice for a detection, using what the detector learned about it.

    Most crash types have one fixed entry. A sudden stop is different: the same missing
    shutdown sequence can follow a heavy job starting, an idle machine, or the tenth time in a
    week, and the useful advice differs. This is where the details the detector found
    (see analyzers/abrupt_end.py) change what the owner is told to do first.
    """
    abrupt = (detection.get("signals") or {}).get("abrupt_end")
    if detection.get("crash_type") == "power_loss" and isinstance(abrupt, dict):
        return _sudden_stop_advice(abrupt)
    return advice_for(detection.get("crash_type", "unknown"))


# How a GPU workload is capped, by vendor. Written as the thing to do, not a recipe: the right
# number depends on the card, so the steps say where to find it.
_GPU_CAP = {
    "nvidia": (
        "Cap the GPU's power and repeat the same load: sudo nvidia-smi -pl <watts>, starting at about 75% "
        "of the card's limit (nvidia-smi -q -d POWER shows the limit).",
    ),
    "amd": (
        "Lower the GPU's power cap and repeat the same load: write a smaller value, in microwatts, to "
        "/sys/class/drm/card*/device/hwmon/hwmon*/power1_cap, starting at about 75% of power1_cap_max.",
    ),
}


def _sudden_stop_advice(abrupt: dict[str, Any]) -> dict[str, Any]:
    from .abrupt_end import RIGHT_BEFORE_S, when_text

    heavy = abrupt.get("heavy_start")
    # The heavy job that started right before the stop, or None: close enough in time to be worth saying.
    right: dict[str, Any] | None = heavy if heavy and heavy["seconds_before_end"] <= RIGHT_BEFORE_S else None
    stops, checked = abrupt.get("abrupt_boots", 0), abrupt.get("boots_checked", 0)
    recurring = stops >= 2

    root_cause = (
        "The system stopped suddenly: its log ends with no shutdown sequence and no error, so it was not shut "
        "down and did not fail in a way it could write down."
    )
    if right:
        root_cause += (
            f" {right['heavy_label'][0].upper()}{right['heavy_label'][1:]} ({right['what']}) started "
            f"{when_text(right['seconds_before_end'])}; a sudden full load can expose a power supply that is "
            "too small or failing."
        )
    if recurring:
        root_cause += (
            f" It has happened on {stops} of the last {checked} boots, which points at hardware or power "
            "rather than a one-off software fault."
        )
    root_cause += (
        " The logs cannot tell a failing power supply from a hardware lock-up of the GPU, memory or motherboard."
    )

    steps: list[tuple[str, str]] = []
    if right and right["heavy"] in ("gpu_stress", "gpu_compute"):
        cap = _GPU_CAP.get(abrupt.get("gpu_vendor") or "")
        steps.append((
            cap[0] if cap else "Lower the GPU's power limit and repeat the same load.",
            "If the machine survives the same load with the card capped, the power supply or its cables "
            "cannot deliver the card's peak draw.",
        ))
    elif right:
        steps.append((
            "Repeat the load with less of it at once (fewer threads or jobs) and see whether the machine survives.",
            "A stop that follows a heavy load but not a light one points at power delivery or cooling.",
        ))
    steps += [
        (
            "Compare the power supply's rated wattage with the machine's peak draw (GPU board power plus CPU plus "
            "drives, with 20 to 30 percent to spare), and check how old the supply is.",
            "A GPU can spike far above its average draw for a few milliseconds. A marginal supply trips its "
            "protection and the machine switches off with nothing logged.",
        ),
        (
            "Reseat the power connectors at the power supply, the motherboard and the GPU, avoid adapters and "
            "splitters, and try a different outlet or a UPS.",
            "A loose or overloaded connector causes the same sudden stop.",
        ),
        (
            "If the machine has a BMC (IPMI, iDRAC, iLO), read its event log: sudo ipmitool sel list",
            "A BMC can record a power-supply or thermal event that the operating system never saw.",
        ),
    ]
    if recurring:
        steps.append((
            "Compare how the earlier boots ended: journalctl -b -1 -n 30 --no-pager, then -b -2, -b -3.",
            "Look for what the endings share (a job, a time of day, a level of load). Endings with nothing in "
            "common point at hardware.",
        ))
    steps.append((
        "Test the memory with memtest86+ for at least one full pass, and update the BIOS and firmware.",
        "Failing memory and unstable firmware also stop a machine without a trace.",
    ))

    contributing = []
    if right:
        contributing.append("A heavy load starting suddenly.")
    if recurring:
        contributing.append("The same ending on several boots of the same machine.")
    return {
        "root_cause": root_cause,
        "remediation": [
            {"priority": number, "action": action, "rationale": rationale}
            for number, (action, rationale) in enumerate(steps, start=1)
        ],
        "monitoring_suggestions": [
            "Keep persistent journald storage (Storage=persistent) so the end of every boot's log survives.",
            "Watch GPU power draw and temperature in the dashboard while the load runs: a card close to its "
            "limit leaves the power supply no headroom.",
        ],
        "contributing_factors": contributing,
    }
