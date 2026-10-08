"""Tests for `crashpilot classify`: offline classification of pasted logs."""

from __future__ import annotations

import json

from typer.testing import CliRunner

from crashpilot.classify import classify_text
from crashpilot.main import app

runner = CliRunner()

XID_79 = (
    "[ 8123.456789] pcieport 0000:00:01.0: AER: Corrected error received: 0000:01:00.0\n"
    "[ 8123.456800] NVRM: Xid (PCI:0000:01:00): 79, pid=0, GPU has fallen off the bus.\n"
    "[ 8123.456900] NVRM: GPU 0000:01:00.0: GPU has fallen off the bus.\n"
)

OOM = (
    "kernel: java invoked oom-killer: gfp_mask=0x100cca(GFP_HIGHUSER_MOVABLE), order=0\n"
    "kernel: Out of memory: Killed process 4242 (java) total-vm:9000000kB\n"
)


def test_xid_79_is_gpu_fault_high():
    result = classify_text(XID_79)
    assert result["category"] == "gpu_fault"
    assert result["severity"] == "high"
    assert result["confidence"] == 0.70
    assert any("Xid" in line for line in result["evidence"])


def test_oom_and_panic_and_mce():
    assert classify_text(OOM)["category"] == "oom_kill"
    panic = "[ 1.0] Kernel panic - not syncing: VFS: Unable to mount root fs\n"
    assert classify_text(panic)["category"] == "kernel_panic"
    assert classify_text(panic)["severity"] == "critical"
    mce = "mce: [Hardware Error]: Machine check events logged\nMachine check exception: CPU 3\n"
    assert classify_text(mce)["category"] == "machine_check_exception"


def test_no_signature_is_unknown():
    result = classify_text("Oct 08 10:00:01 host sshd[1]: Accepted publickey for bob\n")
    assert result["category"] == "unknown"
    assert result["evidence"]


def test_long_log_is_not_truncated():
    # The signature sits before more than 10,000 characters of noise.
    noise = "[ 9.0] usb 1-1: new device found\n" * 600
    assert classify_text(XID_79 + noise)["category"] == "gpu_fault"


def test_cli_stdin_text():
    out = runner.invoke(app, ["classify"], input=XID_79)
    assert out.exit_code == 0
    assert "Category:   gpu_fault" in out.output
    assert "Severity:   HIGH" in out.output
    assert "Confidence: 70%" in out.output


def test_cli_json_from_file(tmp_path):
    log = tmp_path / "dmesg.txt"
    log.write_text(XID_79)
    out = runner.invoke(app, ["classify", str(log), "--json"])
    assert out.exit_code == 0
    data = json.loads(out.output)
    assert data["category"] == "gpu_fault"
    assert data["severity"] == "high"
    assert data["confidence"] == 0.7
    assert data["evidence"]


def test_cli_does_not_touch_host(tmp_path, monkeypatch):
    # No database or data directory may be created by classifying text.
    data_dir = tmp_path / "data"
    monkeypatch.setenv("CRASHPILOT_DATA_DIR", str(data_dir))
    out = runner.invoke(app, ["classify", "--json"], input=OOM)
    assert out.exit_code == 0
    assert not data_dir.exists()


def test_cli_errors_exit_2(tmp_path):
    assert runner.invoke(app, ["classify", str(tmp_path / "missing.log")]).exit_code == 2
    assert runner.invoke(app, ["classify"], input="   \n").exit_code == 2
