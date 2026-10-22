"""A boot whose log just stops: what the report says about it, and what it must not mistake for one.

The fixtures are synthetic. They have the shape of the logs of a real machine that kept dying with
nothing in its logs (a GPU stress job starting as the last line, an idle stop, a clean reboot on
Ubuntu 24.04) with the hostnames, users and commands made generic.
"""

from __future__ import annotations

import re

import pytest

from crashpilot.analyzers.abrupt_end import (
    RIGHT_BEFORE_S,
    _heavy_kind,
    analyze_abrupt_end,
    confidence,
    describe,
    human_duration,
)
from crashpilot.analyzers.ai_analyzer import _no_api_key_result
from crashpilot.analyzers.crash_detector import CrashType, Severity, detect_crash_type
from crashpilot.analyzers.heuristic_advice import advice_for, advice_for_detection
from crashpilot.shutdown_markers import ended_with_shutdown, journal_output

# What a stop in the middle of a GPU stress run looks like: ordinary activity, the agent's own
# timers, CI jobs finishing and starting, and the heavy job's start as the very last line.
GPU_BURN_TAIL = "\n".join([
    "2026-10-22T14:09:56+00:00 host systemd[1]: Removed slice user-1001.slice - User Slice of UID 1001.",
    "2026-10-22T14:10:00+00:00 host systemd[1]: Starting sysstat-collect.service - system activity accounting tool...",
    "2026-10-22T14:10:01+00:00 host CRON[11]: (alice) CMD (/usr/local/bin/refresh-report)",
    "2026-10-22T14:10:20+00:00 host systemd[1]: Starting crashpilot-heartbeat.service - CrashPilot Cloud Heartbeat...",
    "2026-10-22T14:10:20+00:00 host systemd[1]: Started crashpilot-heartbeat.service - CrashPilot Cloud Heartbeat.",
    "2026-10-22T14:12:05+00:00 host runsvc.sh[500]: 2026-10-22 14:12:05Z: Job polybench on nvidia/rtx completed with result: Succeeded",
    "2026-10-22T14:12:07+00:00 host runsvc.sh[500]: 2026-10-22 14:12:07Z: Running job: cuda-memtest on nvidia/rtx",
    "2026-10-22T14:12:35+00:00 host systemd[1]: Starting crashpilot-heartbeat.service - CrashPilot Cloud Heartbeat...",
    "2026-10-22T14:12:37+00:00 host runsvc.sh[500]: 2026-10-22 14:12:37Z: Job cuda-memtest on nvidia/rtx completed with result: Succeeded",
    "2026-10-22T14:12:39+00:00 host runsvc.sh[500]: 2026-10-22 14:12:39Z: Running job: gpu-burn on nvidia/rtx",
])

# An idle machine: the log ends in the middle of a tunnel reconnecting, nothing started for minutes.
IDLE_TAIL = "\n".join([
    "2026-10-14T19:27:14+00:00 host runsvc.sh[500]: 2026-10-14 19:27:14Z: Job publish completed with result: Succeeded",
    "2026-10-14T19:55:52+00:00 host cloudflared[40]: INF Tunnel connection curve preferences connIndex=3",
    "2026-10-14T19:55:52+00:00 host cloudflared[40]: INF Tunnel connection curve preferences connIndex=2",
])

# How Ubuntu 24.04 ends a plain `reboot`. None of these lines match the patterns written for older systemd.
CLEAN_REBOOT_TAIL = "\n".join([
    "2026-10-20T12:30:17+00:00 host systemd[1]: Stopped target multi-user.target - Multi-User System.",
    "2026-10-20T12:30:18+00:00 host systemd[1]: Reached target shutdown.target - System Shutdown.",
    "2026-10-20T12:30:18+00:00 host systemd[1]: Reached target final.target - Late Shutdown Services.",
    "2026-10-20T12:30:18+00:00 host systemd[1]: Finished systemd-reboot.service - System Reboot.",
    "2026-10-20T12:30:18+00:00 host systemd[1]: Reached target reboot.target - System Reboot.",
    "2026-10-20T12:30:18+00:00 host systemd[1]: Shutting down.",
    "2026-10-20T12:30:18+00:00 host systemd-shutdown[1]: Syncing filesystems and block devices.",
    "2026-10-20T12:30:18+00:00 host systemd-shutdown[1]: Sending SIGTERM to remaining processes...",
    "2026-10-20T12:30:18+00:00 host systemd-journald[585]: Journal stopped",
])

BOOTS = [
    {"boot_id": "aaa", "first_entry": "2026-10-20T12:30:40Z", "last_entry": "2026-10-22T14:12:39Z"},
    {"boot_id": "bbb", "first_entry": "2026-10-22T15:45:58Z", "last_entry": "2026-10-22T19:50:00Z"},
]


def telemetry(tail: str, *, boots=BOOTS, endings=None, gpu=None, shutdown_info="-- No entries --") -> dict:
    return {
        "journal": {
            "previous_boot_errors": "",
            "previous_boot_logs_tail": tail,
            "oom_events": "",
            "boots": boots,
            "shutdown_info": shutdown_info,
            "boot_endings": endings if endings is not None else [],
        },
        "dmesg": {"full_tail": "", "critical_events": [], "mce_events": ""},
        "gpu": gpu or {"nvidia": {"available": False, "xid_errors": ""}},
        "smart": {},
        "thermal": {},
    }


def endings(*cleanly) -> list[dict]:
    return [{"boot_id": f"b{i}", "first_entry": "", "last_entry": "", "ended_cleanly": c} for i, c in enumerate(cleanly)]


class TestWhatCountsAsAShutdown:
    def test_a_plain_reboot_on_ubuntu_24_04_is_a_shutdown(self):
        assert ended_with_shutdown(CLEAN_REBOOT_TAIL.splitlines())

    def test_a_clean_reboot_is_not_reported_as_a_crash(self):
        # It used to be: none of its lines matched the clean-shutdown patterns, so every normal
        # reboot became an "unknown" incident.
        result = detect_crash_type(telemetry(CLEAN_REBOOT_TAIL))
        assert result.crash_type == CrashType.CLEAN_SHUTDOWN

    def test_a_login_sessions_own_shutdown_target_is_not_the_machines(self):
        # Every login session runs its own systemd, which reaches its own shutdown.target when the
        # session ends. That line in the middle of a log says nothing about the machine stopping.
        tail = "\n".join([
            "2026-10-22T14:09:56+00:00 host systemd[1170287]: Reached target shutdown.target - Shutdown.",
            "2026-10-22T14:10:00+00:00 host systemd[1]: Starting sysstat-collect.service - system activity...",
            "2026-10-22T14:12:39+00:00 host runsvc.sh[500]: 2026-10-22 14:12:39Z: Running job: gpu-burn on rtx",
        ])
        assert not ended_with_shutdown(tail.splitlines())
        assert detect_crash_type(telemetry(tail)).crash_type == CrashType.POWER_LOSS

    def test_journal_stopped_counts_only_at_the_end_of_the_log(self):
        # journald prints it when it is merely restarted (a package upgrade does that).
        middle = ["2026-10-22T10:00:00+00:00 host systemd-journald[585]: Journal stopped"]
        busy = [f"2026-10-22T10:{m:02d}:00+00:00 host app[1]: working" for m in range(1, 51)]
        assert not ended_with_shutdown(middle + busy)
        assert ended_with_shutdown(busy + middle)

    def test_journalctl_placeholder_is_not_a_record(self):
        assert journal_output("-- No entries --\n") == ""
        assert journal_output(None) == ""
        assert journal_output("2026-10-20T12:30:18+00:00 host systemd-shutdown[1]: Syncing") != ""

    def test_the_power_loss_branch_works_when_the_only_shutdown_info_is_the_placeholder(self):
        # No log of the previous boot to read, so all there is is the shutdown record: absent.
        result = detect_crash_type(telemetry("", shutdown_info="-- No entries --"))
        assert result.crash_type == CrashType.POWER_LOSS
        assert result.confidence == 0.55

    def test_a_real_shutdown_record_with_no_other_signal_is_still_unknown(self):
        result = detect_crash_type(telemetry("", shutdown_info="2026-10-20T12:30:18+00:00 host systemd-shutdown[1]: x"))
        assert result.crash_type == CrashType.UNKNOWN


class TestAnalyzeAbruptEnd:
    def test_the_gpu_stress_job_is_found_and_the_agents_own_lines_are_not(self):
        analysis = analyze_abrupt_end(telemetry(GPU_BURN_TAIL))
        assert analysis["last_log_at"] == "2026-10-22T14:12:39Z"
        heavy = analysis["heavy_start"]
        assert (heavy["heavy"], heavy["what"], heavy["seconds_before_end"]) == ("gpu_stress", "gpu-burn", 0)
        assert heavy["heavy_label"] == "a GPU stress test"
        assert not any("crashpilot" in line for line in analysis["last_lines"])
        assert not any("crashpilot" in start["what"] for start in analysis["recent_starts"])
        assert [s["what"] for s in analysis["recent_starts"]] == ["/usr/local/bin/refresh-report", "cuda-memtest", "gpu-burn"]

    def test_how_long_the_machine_stayed_down(self):
        assert analyze_abrupt_end(telemetry(GPU_BURN_TAIL))["down_for_s"] == 5599  # 1 h 33 min

    def test_an_idle_stop_has_no_start_just_before_it(self):
        analysis = analyze_abrupt_end(telemetry(IDLE_TAIL))
        assert analysis["recent_starts"] == [] and analysis["heavy_start"] is None
        assert "Nothing was started in the 5 min before the log stops" in " ".join(describe(analysis))

    def test_a_start_older_than_the_window_does_not_count(self):
        tail = "\n".join([
            "2026-10-22T14:00:00+00:00 host runsvc.sh[500]: 2026-10-22 14:00:00Z: Running job: gpu-burn on rtx",
            "2026-10-22T14:05:01+00:00 host app[1]: still working",
        ])
        assert analyze_abrupt_end(telemetry(tail))["recent_starts"] == []  # 301 s before the end

    def test_a_refused_sudo_started_nothing(self):
        tail = "\n".join([
            "2026-10-22T15:52:42+00:00 host sudo[26307]:    alice : a password is required ; PWD=/x ; USER=root ; COMMAND=/usr/bin/systemctl restart ollama",
            "2026-10-22T15:52:47+00:00 host systemd-logind[1120]: Removed session 9.",
        ])
        assert analyze_abrupt_end(telemetry(tail))["recent_starts"] == []

    def test_login_sessions_are_not_starts_or_last_lines(self):
        tail = "\n".join([
            "2026-10-22T15:52:46+00:00 host app[1]: job tick",
            "2026-10-22T15:52:47+00:00 host systemd[1]: Started session-9.scope - Session 9 of User alice.",
            "2026-10-22T15:52:47+00:00 host systemd-logind[1120]: Removed session 9.",
        ])
        analysis = analyze_abrupt_end(telemetry(tail))
        assert analysis["recent_starts"] == []
        assert analysis["last_lines"] == ["2026-10-22T15:52:46+00:00 host app[1]: job tick"]

    @pytest.mark.parametrize("tail", [
        "",
        "no timestamps on any of these lines\nnor on this one",
        CLEAN_REBOOT_TAIL,
    ])
    def test_nothing_to_say_without_a_log_to_read_or_after_a_normal_shutdown(self, tail):
        assert analyze_abrupt_end(telemetry(tail)) is None

    def test_older_systemd_prints_the_offset_without_a_colon(self):
        tail = "2026-10-22T14:12:39+0000 host runsvc.sh[1]: Running job: gpu-burn on rtx"
        assert analyze_abrupt_end(telemetry(tail))["last_log_at"] == "2026-10-22T14:12:39Z"

    def test_a_local_offset_is_converted_to_utc(self):
        tail = "2026-10-22T10:12:39-04:00 host app[1]: working"
        assert analyze_abrupt_end(telemetry(tail))["last_log_at"] == "2026-10-22T14:12:39Z"

    def test_down_time_is_unknown_when_the_boot_times_are_missing_or_backwards(self):
        blank = [{"boot_id": "a", "first_entry": "", "last_entry": ""}, {"boot_id": "b", "first_entry": "", "last_entry": ""}]
        backwards = [BOOTS[1], BOOTS[0]]
        assert analyze_abrupt_end(telemetry(IDLE_TAIL, boots=blank))["down_for_s"] is None
        assert analyze_abrupt_end(telemetry(IDLE_TAIL, boots=backwards))["down_for_s"] is None
        assert analyze_abrupt_end(telemetry(IDLE_TAIL, boots=BOOTS[:1]))["down_for_s"] is None

    def test_boots_that_could_not_be_read_are_not_counted_as_cut_off(self):
        analysis = analyze_abrupt_end(telemetry(IDLE_TAIL, endings=endings(False, None, True, False, None)))
        assert (analysis["boots_checked"], analysis["abrupt_boots"]) == (3, 2)

    @pytest.mark.parametrize(("gpu", "vendor"), [
        ({"nvidia": {"available": True}, "amd": {"available": False}}, "nvidia"),
        ({"nvidia": {"available": False}, "amd": {"available": True}}, "amd"),
        ({"nvidia": {"available": False}, "amd": {"available": False}}, None),
    ])
    def test_the_gpu_vendor(self, gpu, vendor):
        assert analyze_abrupt_end(telemetry(IDLE_TAIL, gpu=gpu))["gpu_vendor"] == vendor


class TestHeavyWorkloads:
    @pytest.mark.parametrize(("text", "kind"), [
        ("Running job: gpu-burn on nvidia/rtx", "gpu_stress"),
        ("Running job: cuda-memtest", "gpu_stress"),
        ("CMD (/opt/gpu_burn 600)", "gpu_stress"),
        ("COMMAND=/usr/bin/stress-ng --cpu 16", "cpu_stress"),
        ("Started stress.service", "cpu_stress"),
        ("CMD (mprime -t)", "cpu_stress"),
        ("COMMAND=/usr/bin/memtester 8G", "memory_test"),
        ("Running job: polybench on nvidia", "benchmark"),
        ("Running job: benchmark-suite", "benchmark"),
        ("Started ollama.service - Ollama Service.", "gpu_compute"),
    ])
    def test_heavy_work_is_recognised(self, text, kind):
        assert _heavy_kind(text)[0] == kind

    @pytest.mark.parametrize("text", [
        "Running job: build-and-deploy",
        "Started apt-daily.service - Daily apt download activities",
        "CMD (/usr/local/bin/backup-job)",
        "Running job: distress-signal-handler",
        "Running job: benchling-export",
    ])
    def test_ordinary_work_is_not(self, text):
        assert _heavy_kind(text) is None


class TestHowSureTheDetectorIs:
    def test_a_bare_stop_keeps_the_old_starting_confidence(self):
        assert confidence(analyze_abrupt_end(telemetry(IDLE_TAIL))) == 0.55

    def test_a_heavy_job_right_before_it_adds_a_little(self):
        assert confidence(analyze_abrupt_end(telemetry(GPU_BURN_TAIL))) == 0.65

    def test_a_heavy_job_long_before_it_adds_nothing(self):
        tail = (
            f"2026-10-22T14:00:00+00:00 host runsvc.sh[1]: Running job: gpu-burn on rtx\n"
            f"2026-10-22T14:{RIGHT_BEFORE_S // 60 + 1:02d}:30+00:00 host app[1]: working"
        )
        analysis = analyze_abrupt_end(telemetry(tail))
        assert analysis["heavy_start"] is not None and analysis["heavy_start"]["seconds_before_end"] > RIGHT_BEFORE_S
        assert confidence(analysis) == 0.55

    def test_a_machine_that_keeps_stopping_adds_more_but_never_certainty(self):
        assert confidence(analyze_abrupt_end(telemetry(IDLE_TAIL, endings=endings(False, False, True)))) == 0.70
        assert confidence(analyze_abrupt_end(telemetry(GPU_BURN_TAIL, endings=endings(False, False, False)))) == 0.80

    def test_one_stop_in_the_history_is_not_a_pattern(self):
        # The boot being analysed is itself one of the boots checked, so one is just this one.
        assert confidence(analyze_abrupt_end(telemetry(IDLE_TAIL, endings=endings(True, True, False)))) == 0.55


class TestTheDetection:
    def test_a_sudden_stop_is_reported_with_what_is_known_about_it(self):
        result = detect_crash_type(telemetry(GPU_BURN_TAIL, endings=endings(True, False, False, True, False)))
        assert (result.crash_type, result.severity) == (CrashType.POWER_LOSS, Severity.HIGH)
        assert result.confidence == 0.80
        assert result.evidence_sources == ["journal"] * len(result.evidence)
        text = "\n".join(result.evidence)
        assert "stops at 2026-10-22 14:12:39 UTC with no shutdown sequence" in text
        assert "A GPU stress test (gpu-burn) started in the same second the log stops." in text
        assert "1 h 33 min later" in text and "restarted by hand" in text
        assert "3 of the last 5 boots ended without a shutdown sequence" in text
        assert result.signals["abrupt_end"]["heavy_start"]["what"] == "gpu-burn"

    def test_a_short_gap_reads_as_an_automatic_reset(self):
        boots = [BOOTS[0], {**BOOTS[1], "first_entry": "2026-10-22T14:13:24Z"}]
        text = "\n".join(detect_crash_type(telemetry(GPU_BURN_TAIL, boots=boots)).evidence)
        assert "45 s later, which fits an automatic reset or a power cycle" in text

    def test_a_real_crash_message_still_wins(self):
        tail = GPU_BURN_TAIL + "\n2026-10-22T14:12:40+00:00 host kernel: Kernel panic - not syncing: Fatal exception"
        assert detect_crash_type(telemetry(tail)).crash_type == CrashType.KERNEL_PANIC

    def test_evidence_never_claims_a_cause(self):
        text = " ".join(detect_crash_type(telemetry(GPU_BURN_TAIL)).evidence).lower()
        assert "caused" not in text and "because" not in text


class TestTheAdvice:
    def advice(self, tail=GPU_BURN_TAIL, **kw):
        result = detect_crash_type(telemetry(tail, **kw))
        payload = {"crash_type": result.crash_type.value, "signals": result.signals}
        return advice_for_detection(payload)

    def steps(self, advice):
        return [step["action"] for step in advice["remediation"]]

    def test_the_old_generic_advice_pointed_at_the_wrong_things(self):
        # What every sudden stop used to be told, whatever its evidence.
        assert "smartctl" in " ".join(s["action"] for s in advice_for("unknown")["remediation"])

    def test_a_gpu_load_on_nvidia_leads_with_capping_the_card(self):
        advice = self.advice(gpu={"nvidia": {"available": True}})
        assert "nvidia-smi -pl" in self.steps(advice)[0]
        assert "started in the same second the log stops" in advice["root_cause"]
        assert "smartctl" not in " ".join(self.steps(advice))

    def test_a_gpu_load_on_amd_names_its_power_cap(self):
        advice = self.advice(gpu={"nvidia": {"available": False}, "amd": {"available": True}})
        assert "power1_cap" in self.steps(advice)[0]

    def test_a_gpu_load_with_no_known_vendor_still_says_to_cap_it(self):
        assert "power limit" in self.steps(self.advice())[0]

    def test_an_idle_stop_is_not_blamed_on_a_workload(self):
        advice = self.advice(IDLE_TAIL)
        steps = self.steps(advice)
        assert "power supply" in steps[0]
        assert not any("nvidia-smi" in s or "Repeat the load" in s for s in steps)
        assert "started" not in advice["root_cause"]

    def test_a_machine_that_keeps_stopping_is_pointed_at_hardware_and_told_to_compare_its_endings(self):
        advice = self.advice(IDLE_TAIL, endings=endings(False, False, False, True))
        assert "3 of the last 4 boots" in advice["root_cause"] and "hardware or power" in advice["root_cause"]
        assert any("journalctl -b -1 -n 30" in s for s in self.steps(advice))
        assert "The same ending on several boots of the same machine." in advice["contributing_factors"]

    def test_the_first_stop_does_not_claim_a_pattern(self):
        advice = self.advice(IDLE_TAIL, endings=endings(True, True, False))
        assert "hardware or power" not in advice["root_cause"]
        assert not any("journalctl -b -1 -n 30" in s for s in self.steps(advice))

    def test_the_logs_never_claim_to_know_which_it_was(self):
        assert "cannot tell a failing power supply from a hardware lock-up" in self.advice()["root_cause"]

    def test_steps_are_numbered_in_order(self):
        assert [s["priority"] for s in self.advice()["remediation"]] == list(range(1, len(self.advice()["remediation"]) + 1))

    def test_other_crash_types_keep_their_fixed_advice(self):
        assert advice_for_detection({"crash_type": "oom_kill"}) == advice_for("oom_kill")
        assert advice_for_detection({"crash_type": "power_loss"}) == advice_for("power_loss")  # no abrupt-end detail
        assert advice_for_detection({}) == advice_for("unknown")

    def test_the_keyless_report_carries_it_with_readable_evidence(self):
        result = detect_crash_type(telemetry(GPU_BURN_TAIL, gpu={"nvidia": {"available": True}}))
        report = _no_api_key_result({
            "crash_type": result.crash_type.value, "severity": result.severity.value, "confidence": result.confidence,
            "evidence": result.evidence, "evidence_sources": result.evidence_sources,
            "signals": result.signals, "alternatives": [],
        })
        assert report["remediation"][0]["action"].startswith("Cap the GPU's power")
        assert all(item["source"] == "journal" and item["excerpt"] for item in report["evidence"])
        assert report["root_cause"] in report["summary"]


class TestWording:
    def test_durations(self):
        assert [human_duration(s) for s in (0, 4, 45, 89, 100, 300, 5599, 7200)] == [
            "0 s", "4 s", "45 s", "89 s", "2 min", "5 min", "1 h 33 min", "2 h",
        ]

    def test_nothing_in_the_report_uses_dashes_or_curly_quotes(self):
        result = detect_crash_type(telemetry(GPU_BURN_TAIL, endings=endings(False, False, False)))
        texts = [*result.evidence, *(s["action"] + s["rationale"] for s in advice_for_detection(
            {"crash_type": "power_loss", "signals": result.signals})["remediation"])]
        assert not re.search("[–—‘’“”]", "\n".join(texts))
