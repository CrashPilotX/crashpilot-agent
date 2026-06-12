"""Tests for heuristic crash detection logic."""

from __future__ import annotations

from crashpilot.analyzers.crash_detector import (
    CrashType,
    Severity,
    _build_corpus,
    _match_patterns,
    detect_crash_type,
)


def _tel(journal_errors: str = "", dmesg_tail: str = "", oom: str = "") -> dict:
    """Build a minimal telemetry dict for testing."""
    return {
        "journal": {
            "previous_boot_errors": journal_errors,
            "previous_boot_logs_tail": "",
            "oom_events": oom,
            "boots": [
                {"boot_id": "aaa", "first_entry": "", "last_entry": ""},
                {"boot_id": "bbb", "first_entry": "", "last_entry": ""},
            ],
            "shutdown_info": "",
        },
        "dmesg": {
            "full_tail": dmesg_tail,
            "critical_events": [],
            "mce_events": "",
        },
        "gpu": {"nvidia": {"xid_errors": ""}},
        "smart": {},
        "thermal": {},
    }


class TestKernelPanic:
    def test_kernel_panic_detected(self):
        tel = _tel(journal_errors="Jan 01 00:00:01 host kernel: Kernel panic - not syncing: VFS")
        result = detect_crash_type(tel)
        assert result.crash_type == CrashType.KERNEL_PANIC
        assert result.severity == Severity.CRITICAL
        assert result.confidence >= 0.5
        assert len(result.evidence) >= 1

    def test_general_protection_fault(self):
        tel = _tel(dmesg_tail="[12345.678] general protection fault: 0000")
        result = detect_crash_type(tel)
        assert result.crash_type == CrashType.KERNEL_PANIC

    def test_double_fault(self):
        tel = _tel(journal_errors="kernel: double fault: 0000")
        result = detect_crash_type(tel)
        assert result.crash_type == CrashType.KERNEL_PANIC


class TestOomKill:
    def test_modern_oom_pattern(self):
        """Kernel 5.x+ uses 'Killed process' (past tense)."""
        tel = _tel(oom="Out of memory: Killed process 1234 (python3) total-vm:4096000kB")
        result = detect_crash_type(tel)
        assert result.crash_type == CrashType.OOM_KILL
        assert result.severity == Severity.HIGH

    def test_legacy_oom_pattern(self):
        """Kernel < 5.x uses 'Kill process' (present tense)."""
        tel = _tel(oom="Out of memory: Kill process 5678 (java) score 800 or sacrifice child")
        result = detect_crash_type(tel)
        assert result.crash_type == CrashType.OOM_KILL

    def test_cgroup_oom(self):
        """Container / cgroup v2 OOM."""
        tel = _tel(journal_errors="Memory cgroup out of memory: Killed process 999 (nginx)")
        result = detect_crash_type(tel)
        assert result.crash_type == CrashType.OOM_KILL

    def test_oom_killer_invoked(self):
        tel = _tel(dmesg_tail="oom-killer invoked with order=0")
        result = detect_crash_type(tel)
        assert result.crash_type == CrashType.OOM_KILL


class TestThermalShutdown:
    def test_acpi_thermal_critical(self):
        tel = _tel(journal_errors="ACPI: Thermal Zone TZ00 reached critical temperature")
        result = detect_crash_type(tel)
        assert result.crash_type == CrashType.THERMAL_SHUTDOWN


class TestWatchdog:
    def test_hard_lockup(self):
        tel = _tel(dmesg_tail="[1000.0] NMI watchdog: hard LOCKUP on cpu 2")
        result = detect_crash_type(tel)
        assert result.crash_type == CrashType.WATCHDOG_RESET


class TestGpuFault:
    def test_nvidia_xid(self):
        tel = _tel(journal_errors="NVRM: Xid (PCI:0000:01:00): 79, pid='<unknown>', name=<unknown>")
        result = detect_crash_type(tel)
        assert result.crash_type == CrashType.GPU_FAULT

    def test_drm_gpu_hang(self):
        tel = _tel(dmesg_tail="drm/i915: GPU HANG: ecode 9:1:85dffffb, in chrome [1234]")
        result = detect_crash_type(tel)
        assert result.crash_type == CrashType.GPU_FAULT


class TestCleanShutdown:
    def test_clean_poweroff(self):
        tel = _tel(journal_errors="systemd-shutdown: Shutting down")
        result = detect_crash_type(tel)
        assert result.crash_type == CrashType.CLEAN_SHUTDOWN
        assert result.severity == Severity.INFO
        assert result.confidence >= 0.8

    def test_panic_overrides_clean_shutdown(self):
        """If both clean shutdown and panic signals appear, panic wins."""
        tel = _tel(
            journal_errors=(
                "systemd-shutdown: Shutting down\n"
                "kernel: Kernel panic - not syncing: fatal exception"
            )
        )
        result = detect_crash_type(tel)
        assert result.crash_type == CrashType.KERNEL_PANIC


class TestUnknownAndPowerLoss:
    def test_unknown_when_no_match(self):
        tel = _tel()
        tel["journal"]["shutdown_info"] = "some content"
        result = detect_crash_type(tel)
        assert result.crash_type == CrashType.UNKNOWN
        assert result.confidence == 0.30

    def test_power_loss_inferred(self):
        """No shutdown record + previous boot entry → power loss."""
        tel = _tel()
        # shutdown_info empty, boots has 2 entries
        result = detect_crash_type(tel)
        assert result.crash_type == CrashType.POWER_LOSS
        assert result.confidence == 0.55


class TestCorpusBuilding:
    def test_corpus_aggregates_sources(self):
        tel = _tel(
            journal_errors="error from journal",
            dmesg_tail="error from dmesg",
            oom="oom event",
        )
        corpus = _build_corpus(tel)
        assert "error from journal" in corpus
        assert "error from dmesg" in corpus
        assert "oom event" in corpus

    def test_empty_telemetry_safe(self):
        """Should not raise on completely empty telemetry."""
        corpus = _build_corpus({})
        assert corpus == ""


class TestMatchPatterns:
    def test_returns_lines_not_substrings(self):
        corpus = "2024-01-01 kernel: Kernel panic - not syncing: oops\nother line"
        hits = _match_patterns("test", corpus, [r"Kernel panic"])
        assert len(hits) == 1
        assert "Kernel panic" in hits[0]
        # Should return the full line, not just the matched word
        assert "not syncing" in hits[0]

    def test_no_duplicate_lines(self):
        corpus = "Kernel panic first\nKernel panic second"
        hits = _match_patterns("test", corpus, [r"Kernel panic"])
        # Both lines are unique so both returned
        assert len(hits) == 2

    def test_case_insensitive(self):
        hits = _match_patterns("test", "KERNEL PANIC occurred", [r"kernel panic"])
        assert len(hits) == 1

    def test_truncates_long_lines(self):
        long_line = "A" * 300
        hits = _match_patterns("test", long_line, [r"AAA"])
        assert all(len(h) <= 200 for h in hits)
