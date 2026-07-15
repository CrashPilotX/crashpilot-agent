"""Tests for verified CrashPilot agent updates."""

from __future__ import annotations

import hashlib
import io
import tarfile
from types import SimpleNamespace

import pytest

from crashpilot import updater


def _bundle_bytes(member_name: str = "CrashPilot/agent/pyproject.toml") -> bytes:
    payload = b"[project]\nname='crashpilot'\nversion='0.1.0'\n"
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        info = tarfile.TarInfo(member_name)
        info.size = len(payload)
        archive.addfile(info, io.BytesIO(payload))
    return output.getvalue()


def _bundle_with_systemd() -> bytes:
    output = io.BytesIO()
    members = {
        "CrashPilot/agent/pyproject.toml": b"[project]\nname='crashpilot'\nversion='0.1.0'\n",
        "CrashPilot/systemd/crashpilot-update.timer": b"[Timer]\nOnCalendar=hourly\n",
        "CrashPilot/systemd/crashpilot-update.service": b"[Service]\nExecStart=/opt/crashpilot/venv/bin/crashpilot update --quiet\nExecStartPost=/opt/crashpilot/venv/bin/crashpilot heartbeat --quiet\n",
        "CrashPilot/systemd/crashpilot-heartbeat.timer": b"[Timer]\nOnUnitActiveSec=60s\n",
    }
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        for name, payload in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
    return output.getvalue()


def test_unchanged_bundle_skips_reinstall(tmp_path, monkeypatch):
    bundle = _bundle_bytes()
    checksum = hashlib.sha256(bundle).hexdigest()
    (tmp_path / "agent-bundle.sha256").write_text(checksum + "\n")

    monkeypatch.setattr(updater, "get_settings", lambda: SimpleNamespace(data_dir=tmp_path))
    monkeypatch.setattr(updater, "_download", lambda url: checksum.encode())

    result = updater.install_latest()

    assert result == {"updated": False, "checksum": checksum}


def test_checksum_mismatch_stops_update(tmp_path, monkeypatch):
    bundle = _bundle_bytes()
    monkeypatch.setattr(updater, "get_settings", lambda: SimpleNamespace(data_dir=tmp_path))
    monkeypatch.setattr(
        updater,
        "_download",
        lambda url: b"0" * 64 if url.endswith(".sha256") else bundle,
    )

    with pytest.raises(RuntimeError, match="checksum verification failed"):
        updater.install_latest()


def test_update_refreshes_systemd_units(tmp_path, monkeypatch):
    bundle = _bundle_with_systemd()
    checksum = hashlib.sha256(bundle).hexdigest()
    unit_dir = tmp_path / "systemd"
    unit_dir.mkdir()
    calls: list[list[str]] = []

    class Result:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(updater, "get_settings", lambda: SimpleNamespace(data_dir=tmp_path))
    monkeypatch.setattr(updater, "SYSTEMD_UNIT_DIR", unit_dir)
    monkeypatch.setattr(
        updater,
        "_download",
        lambda url: checksum.encode() if url.endswith(".sha256") else bundle,
    )
    monkeypatch.setattr(updater.subprocess, "run", lambda args, **kwargs: calls.append(args) or Result())

    result = updater.install_latest()

    assert result["updated"] is True
    assert result["egress_tracker_cleared"] is False
    assert result["systemd"]["refreshed"] is True
    assert (unit_dir / "crashpilot-update.timer").read_text() == "[Timer]\nOnCalendar=hourly\n"
    assert "heartbeat --quiet" in (unit_dir / "crashpilot-update.service").read_text()
    assert ["systemctl", "daemon-reload"] in calls
    assert ["systemctl", "enable", "--now", "crashpilot-update.timer"] in calls
    assert ["systemctl", "enable", "--now", "crashpilot-heartbeat.timer"] in calls


def test_update_clears_stale_egress_tracker(tmp_path, monkeypatch):
    bundle = _bundle_bytes()
    checksum = hashlib.sha256(bundle).hexdigest()
    tracker = tmp_path / "daily_egress.json"
    tracker.write_text('{"bytes_sent":999999999}', encoding="utf-8")

    class Result:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(updater, "get_settings", lambda: SimpleNamespace(data_dir=tmp_path))
    monkeypatch.setattr(
        updater,
        "_download",
        lambda url: checksum.encode() if url.endswith(".sha256") else bundle,
    )
    monkeypatch.setattr(updater.subprocess, "run", lambda *args, **kwargs: Result())

    result = updater.install_latest()

    assert result["updated"] is True
    assert result["egress_tracker_cleared"] is True
    assert not tracker.exists()


def test_safe_extract_rejects_path_traversal(tmp_path):
    bundle_path = tmp_path / "bad.tar.gz"
    bundle_path.write_bytes(_bundle_bytes("../../outside"))

    with pytest.raises(ValueError, match="unsafe bundle path"):
        updater._safe_extract(bundle_path, tmp_path / "extract")
