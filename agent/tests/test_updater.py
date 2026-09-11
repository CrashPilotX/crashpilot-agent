"""Tests for verified CrashPilot agent updates."""

from __future__ import annotations

import hashlib
import io
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from crashpilot import updater

REPO_SYSTEMD = Path(__file__).resolve().parents[2] / "systemd"


@pytest.fixture(autouse=True)
def _no_real_systemd(monkeypatch, tmp_path):
    # Tests run as root in some environments; never let one write to or
    # inspect the host's /etc/systemd/system.
    monkeypatch.setattr(updater, "SYSTEMD_UNIT_DIR", tmp_path / "no-systemd")


class _Ok:
    returncode = 0
    stdout = ""
    stderr = ""


def _full_bundle() -> bytes:
    """A bundle carrying every refreshed unit, like the published one."""
    output = io.BytesIO()
    members = {"CrashPilot/agent/pyproject.toml": b"[project]\nname='crashpilot'\nversion='0.1.0'\n"}
    for name in updater.REFRESHED_UNITS:
        body = "[Service]\nExecStop=__CRASHPILOT_BIN__ sign-off --quiet\n" if name.endswith(".service") else "[Timer]\n"
        members[f"CrashPilot/systemd/{name}"] = body.encode()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        for name, payload in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
    return output.getvalue()


@pytest.fixture()
def systemd_host(tmp_path, monkeypatch):
    """A root-run systemd install serving the full bundle; records every command."""
    bundle = _full_bundle()
    checksum = hashlib.sha256(bundle).hexdigest()
    unit_dir = tmp_path / "systemd"
    unit_dir.mkdir()
    calls: list[list[str]] = []
    downloads: list[str] = []

    def _download(url: str) -> bytes:
        downloads.append(url)
        return checksum.encode() if url.endswith(".sha256") else bundle

    monkeypatch.setattr(updater, "get_settings", lambda: SimpleNamespace(data_dir=tmp_path))
    monkeypatch.setattr(updater, "SYSTEMD_UNIT_DIR", unit_dir)
    monkeypatch.setattr(updater, "_crashpilot_bin_path", lambda: "/opt/crashpilot/venv/bin/crashpilot")
    monkeypatch.setattr(updater, "_download", _download)
    monkeypatch.setattr(updater.subprocess, "run", lambda args, **kwargs: calls.append(args) or _Ok())
    return SimpleNamespace(
        unit_dir=unit_dir, calls=calls, downloads=downloads, checksum=checksum,
        state=tmp_path / "agent-bundle.sha256",
    )


def _pip_ran(calls: list[list[str]]) -> bool:
    return any("pip" in args for args in calls)


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
    monkeypatch.setattr(updater, "_crashpilot_bin_path", lambda: "/opt/crashpilot/venv/bin/crashpilot")
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
    update_service = (unit_dir / "crashpilot-update.service").read_text()
    assert "__CRASHPILOT_BIN__" not in update_service
    assert "ExecStart=/opt/crashpilot/venv/bin/crashpilot update --quiet" in update_service
    assert "ExecStartPost=/opt/crashpilot/venv/bin/crashpilot heartbeat --quiet" in update_service
    assert ["systemctl", "daemon-reload"] in calls
    assert ["systemctl", "enable", "--now", "crashpilot-update.timer"] in calls
    assert ["systemctl", "enable", "--now", "crashpilot-heartbeat.timer"] in calls


def test_systemd_unit_template_replaces_crashpilot_placeholder(tmp_path, monkeypatch):
    src = tmp_path / "unit.service"
    dest = tmp_path / "installed.service"
    src.write_text("[Service]\nExecStart=__CRASHPILOT_BIN__ heartbeat --quiet\n", encoding="utf-8")
    monkeypatch.setattr(updater, "_crashpilot_bin_path", lambda: "/opt/crashpilot/venv/bin/crashpilot")

    updater._install_systemd_unit_template(src, dest)

    assert dest.read_text(encoding="utf-8") == (
        "[Service]\nExecStart=/opt/crashpilot/venv/bin/crashpilot heartbeat --quiet\n"
    )


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


def test_update_installs_and_starts_the_signoff_unit(systemd_host):
    result = updater.install_latest()

    assert result["updated"] is True
    signoff = (systemd_host.unit_dir / "crashpilot-signoff.service").read_text()
    assert "ExecStop=/opt/crashpilot/venv/bin/crashpilot sign-off --quiet" in signoff
    assert ["systemctl", "enable", "--now", "crashpilot-signoff.service"] in systemd_host.calls


def test_update_does_not_re_enable_a_signoff_unit_already_there(systemd_host):
    # An operator may have disabled it; an update refreshes the file but
    # leaves that choice alone.
    (systemd_host.unit_dir / "crashpilot-signoff.service").write_text("[Service]\n")

    updater.install_latest()

    assert "sign-off" in (systemd_host.unit_dir / "crashpilot-signoff.service").read_text()
    assert ["systemctl", "enable", "--now", "crashpilot-signoff.service"] not in systemd_host.calls


def test_update_restarts_a_running_api_server(systemd_host):
    updater.install_latest()

    assert ["systemctl", "try-restart", "crashpilot-api@*.service"] in systemd_host.calls
    # After the new code is in place, not before.
    pip = next(i for i, args in enumerate(systemd_host.calls) if "pip" in args)
    restart = systemd_host.calls.index(["systemctl", "try-restart", "crashpilot-api@*.service"])
    assert pip < restart


def test_current_bundle_fills_in_units_an_older_updater_skipped(systemd_host):
    # A machine updated by a version whose list lacked the sign-off unit: the
    # code is current, but the unit never arrived.
    systemd_host.state.write_text(systemd_host.checksum + "\n")
    for name in updater.REFRESHED_UNITS:
        if name != "crashpilot-signoff.service":
            (systemd_host.unit_dir / name).write_text("old\n")

    result = updater.install_latest()

    assert result["updated"] is False
    assert result["systemd"]["refreshed"] is True
    assert (systemd_host.unit_dir / "crashpilot-signoff.service").is_file()
    assert ["systemctl", "enable", "--now", "crashpilot-signoff.service"] in systemd_host.calls
    # The code is already current, so it is not reinstalled.
    assert not _pip_ran(systemd_host.calls)
    # The API server may still be on code an older updater installed without
    # restarting it.
    assert ["systemctl", "try-restart", "crashpilot-api@*.service"] in systemd_host.calls


def test_current_bundle_with_every_unit_in_place_downloads_nothing_more(systemd_host):
    systemd_host.state.write_text(systemd_host.checksum + "\n")
    for name in updater.REFRESHED_UNITS:
        (systemd_host.unit_dir / name).write_text("current\n")

    result = updater.install_latest()

    assert result == {"updated": False, "checksum": systemd_host.checksum}
    assert systemd_host.downloads == [updater.CHECKSUM_URL]
    assert systemd_host.calls == []


def test_a_host_installed_without_systemd_units_gets_none(systemd_host):
    # No heartbeat unit means install.sh was told not to install units.
    systemd_host.state.write_text(systemd_host.checksum + "\n")

    result = updater.install_latest()

    assert result == {"updated": False, "checksum": systemd_host.checksum}
    assert list(systemd_host.unit_dir.iterdir()) == []


@pytest.mark.skipif(not REPO_SYSTEMD.is_dir(), reason="needs the repository checkout")
def test_every_refreshed_unit_ships_in_the_bundle():
    # A listed unit the bundle lacks would be "missing" forever, and every
    # hourly check would download the bundle again.
    shipped = {path.name for path in REPO_SYSTEMD.iterdir()}
    assert set(updater.REFRESHED_UNITS) <= shipped


def test_a_packaged_binary_leaves_updates_to_the_package_manager(tmp_path, monkeypatch):
    # In the .deb's PyInstaller binary, sys.executable is crashpilot itself,
    # so `-m pip` failed after downloading the bundle, every hour.
    monkeypatch.setattr(updater.sys, "frozen", True, raising=False)
    monkeypatch.setattr(updater, "get_settings", lambda: SimpleNamespace(data_dir=tmp_path))
    monkeypatch.setattr(updater, "_download", lambda url: pytest.fail("nothing is downloaded"))
    monkeypatch.setattr(updater.subprocess, "run", lambda *a, **k: pytest.fail("nothing is run"))

    result = updater.install_latest(force=True)

    assert result["updated"] is False
    assert result["packaged"] is True
    assert "package manager" in result["message"]


def test_the_update_command_explains_a_packaged_install(monkeypatch):
    from typer.testing import CliRunner

    from crashpilot.main import app

    monkeypatch.setattr(updater.sys, "frozen", True, raising=False)
    monkeypatch.setattr(updater, "_download", lambda url: pytest.fail("nothing is downloaded"))

    result = CliRunner().invoke(app, ["update"])

    assert result.exit_code == 0, result.output
    assert "package manager" in result.output
    assert CliRunner().invoke(app, ["update", "--quiet"]).exit_code == 0


def test_safe_extract_rejects_path_traversal(tmp_path):
    bundle_path = tmp_path / "bad.tar.gz"
    bundle_path.write_bytes(_bundle_bytes("../../outside"))

    with pytest.raises(ValueError, match="unsafe bundle path"):
        updater._safe_extract(bundle_path, tmp_path / "extract")
