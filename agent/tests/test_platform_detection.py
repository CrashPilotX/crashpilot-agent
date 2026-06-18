from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from crashpilot.collectors import platform as platform_module
from crashpilot.collectors.platform import Distro, InitSystem, PlatformType


def _common(monkeypatch):
    monkeypatch.setattr(platform_module, "_kernel_version", AsyncMock(return_value="6.8.0"))
    monkeypatch.setattr(
        platform_module,
        "run_cmd",
        AsyncMock(side_effect=[
            ("x86_64\n", "", 0),
            ("ci-host\n", "", 0),
        ]),
    )
    monkeypatch.setattr(
        platform_module,
        "_detect_distro",
        AsyncMock(return_value=(Distro.UBUNTU, "24.04")),
    )
    monkeypatch.setattr(
        platform_module,
        "_detect_init",
        AsyncMock(return_value=InitSystem.SYSTEMD),
    )
    monkeypatch.setattr(platform_module, "_detect_wsl", lambda kernel: None)


@pytest.mark.asyncio
async def test_detects_supported_virtual_machine(monkeypatch):
    _common(monkeypatch)
    monkeypatch.setattr(platform_module, "_detect_container_runtime", lambda: None)
    monkeypatch.setattr(
        platform_module,
        "_detect_vm_hypervisor",
        AsyncMock(return_value="kvm"),
    )

    info = await platform_module.detect_platform()

    assert info.platform is PlatformType.VIRTUAL_MACHINE
    assert info.hypervisor == "kvm"
    assert info.is_virtual is True
    assert info.supported is True


@pytest.mark.asyncio
async def test_detects_supported_kubernetes_container(monkeypatch):
    _common(monkeypatch)
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.96.0.1")
    monkeypatch.setattr(platform_module, "_detect_container_runtime", lambda: "kubernetes")
    monkeypatch.setattr(
        platform_module,
        "_detect_vm_hypervisor",
        AsyncMock(return_value=None),
    )

    info = await platform_module.detect_platform()

    assert info.platform is PlatformType.CONTAINER
    assert info.hypervisor == "kubernetes"
    assert info.extra["orchestration"] == "kubernetes"
    assert info.supported is True


@pytest.mark.asyncio
async def test_container_detection_takes_precedence_over_wsl_host_kernel(monkeypatch):
    _common(monkeypatch)
    monkeypatch.setattr(platform_module, "_detect_wsl", lambda kernel: 2)
    monkeypatch.setattr(platform_module, "_detect_container_runtime", lambda: "docker")
    monkeypatch.setattr(
        platform_module,
        "_detect_vm_hypervisor",
        AsyncMock(return_value=None),
    )

    info = await platform_module.detect_platform()

    assert info.platform is PlatformType.CONTAINER
    assert info.hypervisor == "docker"
    assert info.wsl_version is None


def test_wsl_version_detection():
    assert platform_module._detect_wsl("5.15.90.1-microsoft-standard-WSL2") == 2
    assert platform_module._detect_wsl("4.4.0-19041-Microsoft") == 1
