"""What an amdgpu failure looks like in the kernel log.

One definition for the crash detector, the live alert and the dmesg collector, which used
to carry their own copies and drift apart. The only AMD rule they had, "amdgpu.*GPU fault",
matches the old GCN-era message and none of the failures current kernels print: checked
against several thousand real amdgpu log lines from public bug reports, it matched none.
A hang, a reset, even a reset that failed, was filed as "power loss".

The patterns are specific on purpose. They name a failure, not the driver, because amdgpu
logs a great deal on a healthy machine: every ring it brings up ("ring gfx_0.0.0 uses VM inv
eng 0"), power-management resumes ("SMU is resuming..."), "Using BACO for runtime pm". None
of that matches here, which was checked against a healthy machine's full boot log.

Families:

  HANG_AND_RESET  the GPU stopped answering and the driver reset it, or tried to
  SCHEDULER       the GPU's hardware scheduler (MES, on newer cards) stopped answering
  INIT_FAILURE    the GPU did not start, at boot or after a reset
  VM_FAULT        the GPU touched memory it may not. Usually an application bug and the
                  card carries on. The live alert already catches these through its generic
                  "page fault" entry, so they are not listed there again; they are added
                  only as evidence when working out why a boot ended badly, where they
                  used to count for nothing.
"""

from __future__ import annotations

HANG_AND_RESET: tuple[str, ...] = (
    r"amdgpu.*\bring \S+ timeout",
    r"amdgpu.*GPU reset(?:\(\d+\))? ?(?:begin|succeeded|failed)",
    r"amdgpu.*VRAM is lost due to GPU reset",
    r"amdgpu.*\bRing \S+ reset failed",
    r"amdgpu.*device wedged, but recovered through reset",
)

SCHEDULER: tuple[str, ...] = (
    r"amdgpu.*\bMES(?:\(\d+\))? failed to respon",
    r"amdgpu.*MES might be in unrecoverable state",
    r"amdgpu.*\*ERROR\* failed to unmap legacy queue",
)

INIT_FAILURE: tuple[str, ...] = (
    r"amdgpu.*Fatal error during GPU init",
    # Several of these name the failing function, which is where "amdgpu" comes from: the
    # line itself may carry no other mention of the driver, so no "amdgpu.*" in front.
    r"\bamdgpu_device_ip_init failed",
    r"\bamdgpu_discovery_init failed",
    r"amdgpu.*\b(?:sw|hw|early|late)_init of IP block .* failed",
    r"amdgpu.*probe of \S+ failed with error",
    r"probe with driver amdgpu failed with error",
    r"amdgpu.*\[powerplay\] .*(?:Failed|failed)",
    r"amdgpu.*Failed to setup smc hw",
)

VM_FAULT: tuple[str, ...] = (
    r"amdgpu.*\[(?:gfx|mm)hub\] (?:VMC )?page fault",
    r"amdgpu.*(?:GC|MM)?VM_L2_PROTECTION_FAULT_STATUS",
)

# What raises the "critical kernel events" alert while the machine is running.
LIVE_ALERT_PATTERNS: tuple[str, ...] = HANG_AND_RESET + SCHEDULER + INIT_FAILURE

# What counts as GPU evidence when working out why a boot ended badly.
CRASH_EVIDENCE_PATTERNS: tuple[str, ...] = LIVE_ALERT_PATTERNS + VM_FAULT
