"""What an amdgpu failure looks like, and what a healthy amdgpu machine looks like.

The failure lines are written the way the driver prints them. The healthy lines are the
ones a working machine prints on every boot and every power transition; none may match.
"""

from __future__ import annotations

import pytest

from crashpilot import amd_faults, cloud_push
from crashpilot.analyzers.crash_detector import CrashType, detect_crash_type
from crashpilot.collectors import dmesg as dmesg_collector

from .test_crash_detector import _tel

HANG_AND_RESET = [
    "[ 5123.401] [drm:amdgpu_job_timedout [amdgpu]] *ERROR* ring gfx_0.0.0 timeout, signaled seq=48211, emitted seq=48213",
    "Oct 14 10:02:11 box kernel: amdgpu 0000:03:00.0: amdgpu: ring gfx_0.0.0 timeout, signaled seq=3305329, emitted seq=3305331",
    "[ 5123.402] amdgpu 0000:03:00.0: amdgpu: ring sdma0 timeout, but soft recovered",
    "[ 5123.402] amdgpu 0000:03:00.0: amdgpu: GPU reset begin!",
    "[ 5123.402] amdgpu 0000:03:00.0: amdgpu: GPU reset begin!. Source:  1",
    "[ 5128.911] amdgpu 0000:03:00.0: amdgpu: GPU reset(2) succeeded!",
    "[ 5128.911] amdgpu 0000:03:00.0: amdgpu: GPU reset succeeded, trying to resume",
    "[ 5130.002] amdgpu 0000:03:00.0: amdgpu: GPU reset(3) failed",
    "[ 5130.100] amdgpu 0000:03:00.0: amdgpu: VRAM is lost due to GPU reset!",
    "[ 5130.101] amdgpu 0000:03:00.0: amdgpu: Ring gfx_0.0.0 reset failed",
    "[ 5130.102] amdgpu 0000:03:00.0: [drm] device wedged, but recovered through reset",
]
SCHEDULER = [
    "[ 700.1] [drm:mes_v11_0_submit_pkt_and_poll_completion.constprop.0 [amdgpu]] *ERROR* MES failed to response msg=14",
    "[ 700.2] amdgpu 0000:03:00.0: amdgpu: MES failed to respond to msg=REMOVE_QUEUE",
    "[ 700.2] amdgpu 0000:03:00.0: amdgpu: MES(1) failed to respond to msg=RESET",
    "[ 700.3] amdgpu: MES might be in unrecoverable state, issue a GPU reset",
    "[ 700.4] [drm:amdgpu_mes_unmap_legacy_queue [amdgpu]] *ERROR* failed to unmap legacy queue",
]
INIT_FAILURE = [
    "[   12.003] amdgpu 0000:03:00.0: amdgpu: Fatal error during GPU init",
    "[   12.004] amdgpu 0000:03:00.0: amdgpu: amdgpu_device_ip_init failed",
    "Oct 14 09:00:01 box kernel: drmn0: amdgpu_device_ip_init failed",
    "[   12.005] [drm:amdgpu_device_init [amdgpu]] *ERROR* sw_init of IP block <gmc_v8_0> failed -12",
    "[   12.006] [drm:amdgpu_device_fw_loading [amdgpu]] *ERROR* hw_init of IP block <psp> failed -22",
    "[   12.007] [drm:amdgpu_discovery_set_ip_blocks [amdgpu]] *ERROR* amdgpu_discovery_init failed",
    "[   12.008] amdgpu: probe of 0000:03:00.0 failed with error -22",
    "[   12.009] amdgpu 0000:03:00.0: probe with driver amdgpu failed with error -12",
    "[   12.010] amdgpu: [powerplay] SMU Firmware start failed!",
    "[   12.011] amdgpu 0000:03:00.0: Failed to setup smc hw!",
]
VM_FAULT = [
    "[ 5120.100] amdgpu 0000:03:00.0: amdgpu: [gfxhub] page fault (src_id:0 ring:24 vmid:6 pasid:32770, for process python3 pid 4412 thread python3 pid 4412)",
    "[ 5120.101] amdgpu 0000:03:00.0: amdgpu: [mmhub] page fault (src_id:0 ring:0 vmid:2 pasid:0)",
    "[ 5120.102] amdgpu 0000:03:00.0: amdgpu: GCVM_L2_PROTECTION_FAULT_STATUS:0x00800932",
    "[ 5120.103] amdgpu 0000:03:00.0: amdgpu: VM_L2_PROTECTION_FAULT_STATUS:0x00441FF1",
]
HEALTHY = [
    "[    8.101] amdgpu 0000:03:00.0: amdgpu: ring gfx_0.0.0 uses VM inv eng 0 on hub 0",
    "[    8.102] amdgpu 0000:03:00.0: amdgpu: ring sdma0 uses VM inv eng 13 on hub 0",
    "[    8.103] amdgpu 0000:03:00.0: amdgpu: ring kiq_0.2.1.0 uses VM inv eng 12 on hub 0",
    "[    8.104] amdgpu 0000:07:00.0: amdgpu: SMU is initialized successfully!",
    "[159563.805] amdgpu 0000:07:00.0: SMU is resuming...",
    "[159563.814] amdgpu 0000:07:00.0: SMU is resumed successfully!",
    "[159563.814] amdgpu 0000:07:00.0: kiq ring mec 2 pipe 1 q 0",
    "[    8.105] amdgpu 0000:03:00.0: amdgpu: Using BACO for runtime pm",
    "[    8.106] amdgpu 0000:03:00.0: amdgpu: VRAM: 16368M 0x0000008000000000 - 0x00000083FEFFFFFF (16368M used)",
    "[    8.107] [drm] Initialized amdgpu 3.57.0 for 0000:03:00.0 on minor 0",
    "[    8.108] amdgpu 0000:03:00.0: amdgpu: Trusted Memory Zone (TMZ) feature disabled as experimental (default)",
    "[    8.109] amdgpu 0000:03:00.0: [drm] fb0: amdgpudrmfb frame buffer device",
    "[    8.110] amdgpu 0000:03:00.0: amdgpu: PCIE GART of 512M enabled (table at 0x0000008000300000).",
]

LIVE = HANG_AND_RESET + SCHEDULER + INIT_FAILURE


class TestTheLiveAlert:
    @pytest.mark.parametrize("line", LIVE)
    def test_a_hang_reset_scheduler_or_start_failure_raises_it(self, line):
        assert cloud_push._is_critical_line(line)

    @pytest.mark.parametrize("line", HEALTHY)
    def test_a_healthy_machines_amdgpu_lines_do_not(self, line):
        assert not cloud_push._is_critical_line(line)

    def test_a_reset_that_prints_no_error_is_now_seen(self):
        # The old catch-all wanted the word ERROR; a reset never prints it.
        assert cloud_push._is_critical_line("[ 1.0] amdgpu 0000:03:00.0: amdgpu: GPU reset(3) failed")


class TestTheDmesgCollector:
    @pytest.mark.parametrize("line", LIVE)
    def test_it_lists_the_same_events_as_critical(self, line):
        assert dmesg_collector._PATTERN.search(line)

    @pytest.mark.parametrize("line", HEALTHY)
    def test_and_not_the_healthy_ones(self, line):
        assert not dmesg_collector._PATTERN.search(line)


class TestCrashClassification:
    @pytest.mark.parametrize("line", HANG_AND_RESET + SCHEDULER + INIT_FAILURE + VM_FAULT)
    def test_each_failure_is_a_gpu_fault_not_a_power_loss(self, line):
        result = detect_crash_type(_tel(dmesg_tail=line))
        assert result.crash_type is CrashType.GPU_FAULT, line

    def test_a_whole_incident_is_a_gpu_fault_with_evidence(self):
        incident = "\n".join([
            HANG_AND_RESET[1], HANG_AND_RESET[3], VM_FAULT[0], HANG_AND_RESET[7], HANG_AND_RESET[8],
        ])
        result = detect_crash_type(_tel(dmesg_tail=incident))
        assert result.crash_type is CrashType.GPU_FAULT
        assert result.confidence >= 0.6

    def test_a_healthy_boot_log_is_not_a_gpu_fault(self):
        result = detect_crash_type(_tel(dmesg_tail="\n".join(HEALTHY)))
        assert result.crash_type is not CrashType.GPU_FAULT

    def test_the_old_gcn_message_is_still_recognised(self):
        result = detect_crash_type(_tel(dmesg_tail="[ 900.1] amdgpu 0000:0a:00.0: amdgpu: GPU fault detected: 146 0x00020802"))
        assert result.crash_type is CrashType.GPU_FAULT

    def test_nvidia_is_unchanged(self):
        result = detect_crash_type(_tel(dmesg_tail="[ 900.1] NVRM: Xid (PCI:0000:01:00): 79, pid=0, GPU has fallen off the bus"))
        assert result.crash_type is CrashType.GPU_FAULT

    def test_a_userspace_process_called_gpu_crashing_is_not_a_gpu_fault(self):
        # A browser's GPU process dying logs "traps: GPU[1234] general protection fault".
        line = "May 23 18:59:50 box kernel: traps: GPU[77271] general protection fault ip:7edf106e1ee8 sp:7edea0fb3f00 error:0 in libc.so.6"
        assert detect_crash_type(_tel(dmesg_tail=line)).crash_type is not CrashType.GPU_FAULT


class TestThePatterns:
    def test_every_family_is_in_one_of_the_two_lists(self):
        assert set(amd_faults.LIVE_ALERT_PATTERNS) == set(amd_faults.HANG_AND_RESET + amd_faults.SCHEDULER + amd_faults.INIT_FAILURE)
        assert set(amd_faults.CRASH_EVIDENCE_PATTERNS) == set(amd_faults.LIVE_ALERT_PATTERNS + amd_faults.VM_FAULT)

    def test_vm_faults_are_not_added_to_the_live_alert_a_second_time(self):
        assert not set(amd_faults.VM_FAULT) & set(amd_faults.LIVE_ALERT_PATTERNS)

    def test_the_gpu_advice_names_both_vendors(self):
        from crashpilot.analyzers.heuristic_advice import advice_for

        text = str(advice_for("gpu_fault"))
        assert "NVIDIA" in text and "AMD" in text
