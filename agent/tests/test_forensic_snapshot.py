"""Tests for the compact forensic snapshot attached to incident reports."""

from crashpilot.analyzers.forensic_snapshot import build_forensic_snapshot


def test_snapshot_reports_coverage_gaps_and_root_context():
    telemetry = {
        "collected_at": "2026-06-18T00:00:00+00:00",
        "platform": {
            "hostname": "server1",
            "type": "linux",
            "kernel": "6.8.0",
        },
        "system": {
            "memory": {"total_gb": 16, "available_gb": 1},
            "disk": {
                "filesystems": [
                    {"mountpoint": "/", "use_pct": "95%", "available": "6G"},
                    {"mountpoint": "/boot", "use_pct": "11%", "available": "1.8G"},
                ]
            },
        },
        "journal": {"previous_boot_errors": "Out of memory"},
        "dmesg": {"critical_events": ["oom-kill"]},
    }
    detection = {
        "crash_type": "oom_kill",
        "evidence": ["Out of memory: Killed process 42"],
        "signals": ["oom"],
    }
    timeline = [{"source": "oom", "level": "error", "message": "Killed process"}]

    snapshot = build_forensic_snapshot(telemetry, detection, timeline)

    assert snapshot["schema_version"] == 1
    assert snapshot["system_context"]["root_disk"]["mountpoint"] == "/"
    assert snapshot["coverage"]["journal"] == "collected"
    assert snapshot["signal_summary"]["sources"] == ["oom"]
    assert len(snapshot["fingerprint"]) == 16


def test_snapshot_accepts_the_detectors_signal_dict():
    # detect_crash_type puts hardware signals in a dict; slicing it like a
    # list made every analysis with SMART or thermal signals fail.
    from crashpilot.analyzers.crash_detector import detect_crash_type

    telemetry = {
        "journal": {"previous_boot_errors": "kernel: EXT4-fs error (device sda1): bad entry"},
        "smart": {"critical_disks": [{"device": "/dev/sda", "health": "FAILED"}]},
    }
    result = detect_crash_type(telemetry)
    assert result.signals == {"smart_critical_disks": 1}
    detection = {
        "crash_type": result.crash_type.value,
        "evidence": result.evidence,
        "signals": result.signals,
    }

    first = build_forensic_snapshot(telemetry, detection, [])
    # Same signal, different reading: same failure.
    second = build_forensic_snapshot(telemetry, {**detection, "signals": {"smart_critical_disks": 2}}, [])

    assert first["fingerprint"] == second["fingerprint"]


def test_snapshot_marks_missing_previous_boot_evidence():
    snapshot = build_forensic_snapshot(
        {"system": {}, "platform": {}},
        {"crash_type": "unknown", "evidence": [], "signals": []},
        [],
    )

    assert "journal evidence not collected" in snapshot["evidence_gaps"]
    assert any("previous-boot journal" in gap for gap in snapshot["evidence_gaps"])
