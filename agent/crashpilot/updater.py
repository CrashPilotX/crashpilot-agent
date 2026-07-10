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


def _refresh_systemd_units(bundle_root: Path) -> dict[str, Any]:
    """Best-effort refresh of installed systemd units from the verified bundle."""
    systemd_dir = bundle_root / "systemd"
    unit_dir = SYSTEMD_UNIT_DIR
    unit_names = [
        "crashpilot-heartbeat.service",
        "crashpilot-heartbeat.timer",
        "crashpilot-update.service",
        "crashpilot-update.timer",
        "crashpilot-snapshot.service",
        "crashpilot-snapshot.timer",
    ]
    result: dict[str, Any] = {"refreshed": False, "units": [], "error": None}
    if not systemd_dir.is_dir() or not unit_dir.is_dir() or not os.access(unit_dir, os.W_OK):
        return result

    copied: list[str] = []
    try:
        for name in unit_names:
            src = systemd_dir / name
            if src.is_file():
                shutil.copy2(src, unit_dir / name)
                copied.append(name)
        if not copied:
            return result
        subprocess.run(["systemctl", "daemon-reload"], check=False, capture_output=True, text=True, timeout=30)
        for timer in [
            "crashpilot-heartbeat.timer",
            "crashpilot-update.timer",
            "crashpilot-snapshot.timer",
        ]:
            if timer in copied:
                subprocess.run(
                    ["systemctl", "enable", "--now", timer],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
        result.update({"refreshed": True, "units": copied})
    except Exception as exc:
        result["error"] = str(exc)
    return result


def install_latest(
    *,
    force: bool = False,
    bundle_url: str = BUNDLE_URL,
    checksum_url: str = CHECKSUM_URL,
) -> dict[str, Any]:
    """Install the latest verified public bundle into the current virtualenv."""
    settings = get_settings()
    data_dir = settings.data_dir
    if data_dir is None:
        raise RuntimeError("CrashPilot data directory is not configured")
    data_dir.mkdir(parents=True, exist_ok=True)
    state_path = data_dir / "agent-bundle.sha256"

    expected = _parse_checksum(_download(checksum_url))
    if not force and state_path.exists() and state_path.read_text().strip() == expected:
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

    state_path.write_text(expected + "\n", encoding="utf-8")
    return {"updated": True, "checksum": expected, "systemd": systemd_result}
