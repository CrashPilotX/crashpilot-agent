"""Verified agent bundle updates for installed CrashPilot agents."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path
from typing import Any

from .config import get_settings

BUNDLE_URL = "https://crashpilotx.com/crashpilot-agent.tar.gz"
CHECKSUM_URL = f"{BUNDLE_URL}.sha256"
_SHA256_RE = re.compile(r"^[a-fA-F0-9]{64}$")
SYSTEMD_UNIT_DIR = Path("/etc/systemd/system")

_TIMERS = [
    "crashpilot-heartbeat.timer",
    "crashpilot-update.timer",
    "crashpilot-snapshot.timer",
]
_SIGNOFF_UNIT = "crashpilot-signoff.service"
# Every unit an update keeps in step with the bundle, installing any that are
# missing. crashpilot.service and the API server template are left to
# install.sh.
REFRESHED_UNITS = [
    "crashpilot-heartbeat.service",
    "crashpilot-heartbeat.timer",
    "crashpilot-update.service",
    "crashpilot-update.timer",
    "crashpilot-snapshot.service",
    "crashpilot-snapshot.timer",
    _SIGNOFF_UNIT,
]
# The local API server is long-running, so it serves whatever code it started
# with until it restarts.
_API_UNITS = "crashpilot-api@*.service"


def _download(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "CrashPilotX updater"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read()


def _parse_checksum(payload: bytes) -> str:
    checksum = payload.decode("utf-8").strip().split()[0].lower()
    if not _SHA256_RE.fullmatch(checksum):
        raise ValueError("published bundle checksum is invalid")
    return checksum


def _safe_extract(bundle_path: Path, destination: Path) -> None:
    destination = destination.resolve()
    with tarfile.open(bundle_path, "r:gz") as archive:
        for member in archive.getmembers():
            target = (destination / member.name).resolve()
            if destination not in target.parents and target != destination:
                raise ValueError(f"unsafe bundle path: {member.name}")
            if member.issym() or member.islnk():
                raise ValueError(f"bundle links are not allowed: {member.name}")
        archive.extractall(destination)


def _crashpilot_bin_path() -> str:
    """Return the installed console-script path used by systemd units."""
    candidate = Path(sys.executable).with_name("crashpilot")
    if candidate.exists():
        return str(candidate)
    resolved = shutil.which("crashpilot")
    if resolved:
        return resolved
    return str(candidate)


def _install_systemd_unit_template(src: Path, dest: Path) -> None:
    rendered = src.read_text(encoding="utf-8").replace(
        "__CRASHPILOT_BIN__",
        _crashpilot_bin_path(),
    )
    dest.write_text(rendered, encoding="utf-8")


def _systemctl(*args: str) -> None:
    subprocess.run(["systemctl", *args], check=False, capture_output=True, text=True, timeout=30)


def _units_missing() -> bool:
    """Whether this systemd install lacks a unit an update should have added.

    The running updater refreshes units with its own list, so the update that
    installs a version with a longer list still refreshes with the old one.
    Checking on every run fills the gap within an hour instead of waiting for
    the next release. Hosts installed without systemd units are left alone.
    """
    unit_dir = SYSTEMD_UNIT_DIR
    if not unit_dir.is_dir() or not os.access(unit_dir, os.W_OK):
        return False
    if not (unit_dir / "crashpilot-heartbeat.service").is_file():
        return False
    return any(not (unit_dir / name).is_file() for name in REFRESHED_UNITS)


def _refresh_systemd_units(bundle_root: Path) -> dict[str, Any]:
    """Best-effort refresh of installed systemd units from the verified bundle.

    Also restarts a running API server so it picks up the installed code.
    """
    systemd_dir = bundle_root / "systemd"
    unit_dir = SYSTEMD_UNIT_DIR
    result: dict[str, Any] = {"refreshed": False, "units": [], "error": None}
    if not systemd_dir.is_dir() or not unit_dir.is_dir() or not os.access(unit_dir, os.W_OK):
        return result

    copied: list[str] = []
    added: list[str] = []
    try:
        for name in REFRESHED_UNITS:
            src = systemd_dir / name
            if src.is_file():
                dest = unit_dir / name
                if not dest.exists():
                    added.append(name)
                if src.suffix == ".service":
                    _install_systemd_unit_template(src, dest)
                else:
                    shutil.copy2(src, dest)
                copied.append(name)
        if not copied:
            return result
        _systemctl("daemon-reload")
        for timer in _TIMERS:
            if timer in copied:
                _systemctl("enable", "--now", timer)
        # Its ExecStop only runs at shutdown if it was started. Only a new
        # install is enabled, so one an operator disabled stays disabled.
        if _SIGNOFF_UNIT in added:
            _systemctl("enable", "--now", _SIGNOFF_UNIT)
        # try-restart leaves a stopped server stopped.
        _systemctl("try-restart", _API_UNITS)
        result.update({"refreshed": True, "units": copied})
    except Exception as exc:
        result["error"] = str(exc)
    return result


def _clear_egress_tracker(data_dir: Path) -> bool:
    """Remove stale daily egress state after an update.

    Bad quota defaults can leave an otherwise healthy agent stuck in slim or
    minimal mode until UTC midnight.  A verified update is an intentional
    operator-controlled recovery point, so start the new agent with a fresh
    daily counter.
    """
    try:
        (data_dir / "daily_egress.json").unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return False


def install_latest(
    *,
    force: bool = False,
    bundle_url: str = BUNDLE_URL,
    checksum_url: str = CHECKSUM_URL,
) -> dict[str, Any]:
    """Install the latest verified public bundle into the current virtualenv."""
    # The .deb ships a PyInstaller binary: there is no virtualenv, and
    # sys.executable is crashpilot itself, so `-m pip` can only fail, after
    # downloading the bundle, on every hourly run.
    if getattr(sys, "frozen", False):
        return {
            "updated": False,
            "packaged": True,
            "message": (
                "This is a packaged install: it updates through the package manager "
                "(apt), not `crashpilot update`."
            ),
        }
    settings = get_settings()
    data_dir = settings.data_dir
    if data_dir is None:
        raise RuntimeError("CrashPilot data directory is not configured")
    data_dir.mkdir(parents=True, exist_ok=True)
    state_path = data_dir / "agent-bundle.sha256"

    expected = _parse_checksum(_download(checksum_url))
    current = not force and state_path.exists() and state_path.read_text().strip() == expected
    if current and not _units_missing():
        return {"updated": False, "checksum": expected}

    bundle = _download(bundle_url)
    actual = hashlib.sha256(bundle).hexdigest()
    if actual != expected:
        raise RuntimeError("agent bundle checksum verification failed")

    systemd_result: dict[str, Any] = {"refreshed": False, "units": [], "error": None}
    with tempfile.TemporaryDirectory(prefix="crashpilot-update-") as temp:
        temp_dir = Path(temp)
        bundle_path = temp_dir / "crashpilot-agent.tar.gz"
        bundle_path.write_bytes(bundle)
        _safe_extract(bundle_path, temp_dir)

        bundle_root = temp_dir / "CrashPilot"
        agent_dir = bundle_root / "agent"
        if not (agent_dir / "pyproject.toml").is_file():
            raise RuntimeError("agent bundle is missing agent/pyproject.toml")

        if not current:
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "install",
                    "--quiet",
                    "--force-reinstall",
                    "--no-deps",
                    str(agent_dir),
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=180,
            )
            if result.returncode != 0:
                detail = (result.stderr or result.stdout).strip()
                raise RuntimeError(f"agent update install failed: {detail}")

        systemd_result = _refresh_systemd_units(bundle_root)

    if current:
        # The code was already current; only missing units were filled in.
        return {"updated": False, "checksum": expected, "systemd": systemd_result}

    egress_tracker_cleared = _clear_egress_tracker(data_dir)
    state_path.write_text(expected + "\n", encoding="utf-8")
    return {
        "updated": True,
        "checksum": expected,
        "systemd": systemd_result,
        "egress_tracker_cleared": egress_tracker_cleared,
    }
