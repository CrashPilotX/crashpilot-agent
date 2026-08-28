#!/usr/bin/env python3
"""Installer smoke/contract tests for the Ubuntu-only install path."""

from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "agent" / "install.sh"
INSTALLER_FOR_BASH = INSTALLER.relative_to(ROOT).as_posix()


def require(text: str, needle: str, reason: str) -> None:
    if needle not in text:
        raise AssertionError(f"Missing `{needle}`: {reason}")


def main() -> None:
    script = INSTALLER.read_text(encoding="utf-8")

    help_result = subprocess.run(
        ["bash", INSTALLER_FOR_BASH, "--help"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    require(help_result.stdout, "--connect", "installer help should document dashboard connection")

    missing_connect_result = subprocess.run(
        ["bash", INSTALLER_FOR_BASH, "--connect"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if missing_connect_result.returncode == 0:
        raise AssertionError("--connect without a value should fail before installing")
    require(
        missing_connect_result.stderr,
        "--connect requires",
        "installer should explain missing connection-string values",
    )

    require(script, '[[ "$DISTRO" != "ubuntu" ]]', "installer must stay Ubuntu-only for now")
    require(script, "Containerized installs are not supported right now.", "Docker/Kubernetes installs must stay disabled")
    require(script, "KUBERNETES_SERVICE_HOST", "Kubernetes detection should remain explicit")
    require(script, "/.dockerenv", "Docker detection should remain explicit")
    require(script, 'PKG_MGR="apt"', "Ubuntu apt path should remain the only package manager path")
    require(script, "https://crashpilotx.com/crashpilot-agent.tar.gz", "curl-pipe installs should use the public website bundle")
    require(script, "agent bundle download failed", "bundle failures should be explicit")
    require(script, '[[ ! -t 0 ]]', "curl-pipe installs must not prompt from stdin")
    require(script, "Non-interactive install detected", "non-interactive installs should skip optional package prompts")
    require(script, 'WSL with systemd detected', "WSL2 with systemd should install the heartbeat timer")
    require(script, 'WSL without systemd detected', "WSL without systemd should stay on the manual heartbeat path")
    require(script, "crashpilot-update.timer", "systemd installs should enable verified automatic updates")
    require(script, "Automatic updates: enabled", "installer summary should confirm automatic updates")
    require(script, "crashpilot-snapshot.timer", "systemd installs should enable the flight recorder")
    require(script, "Flight recorder: enabled", "installer summary should confirm the flight recorder")
    require(script, "speedtest-cli", "installer should set up internet capacity checks automatically")
    require(script, "install_speedtest_cli", "speedtest capacity support should be installed without an interactive prompt")
    require(script, "_try_install_pkg speedtest-cli", "installer should try to install the Ubuntu speedtest-cli package")
    require(script, "passive network throughput will still work", "missing speedtest-cli should be non-fatal")
    require(script, "CRASHPILOT_BANDWIDTH_SPEEDTEST_ENABLED=true", "new configs should enable speedtest capacity checks")
    require(script, "ensure_env_default CRASHPILOT_BANDWIDTH_SPEEDTEST_ENABLED true", "existing configs should get speedtest enabled by default")

    for unit in (ROOT / "systemd").glob("crashpilot*.service"):
        text = unit.read_text(encoding="utf-8")
        require(
            text,
            "__CRASHPILOT_BIN__",
            f"{unit.name} should use the installer-rendered crashpilot binary path",
        )

    update_service = (ROOT / "systemd" / "crashpilot-update.service").read_text(encoding="utf-8")
    update_timer = (ROOT / "systemd" / "crashpilot-update.timer").read_text(encoding="utf-8")
    require(update_service, "update --quiet", "update service should call the restricted updater command")
    require(update_timer, "Persistent=true", "missed update checks should run after the machine returns")

    unsupported_managers = ["dnf", "pacman", "zypper", "apk", "xbps"]
    active_installs = [
        manager for manager in unsupported_managers
        if f'{manager})' in script or f'{manager}|' in script
    ]
    if active_installs:
        raise AssertionError(
            "Unsupported package manager install branches should not be active: "
            + ", ".join(active_installs)
        )


if __name__ == "__main__":
    main()
