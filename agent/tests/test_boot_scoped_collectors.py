"""Journal queries that feed crash analysis must look at the boot being analyzed.

Analysis explains how the previous boot ended. Without a boot filter, an NVIDIA
Xid or a PCIe error from weeks ago matched in every later analysis, and the
detector blamed the GPU for crashes it had nothing to do with.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

import crashpilot.collectors.gpu as gpu_module
import crashpilot.collectors.system as system_module
from crashpilot.collectors.gpu import GpuCollector
from crashpilot.collectors.system import SystemCollector


def _journalctl_calls(run: AsyncMock) -> list[tuple[str, ...]]:
    return [call.args for call in run.call_args_list if call.args[0] == "journalctl"]


@pytest.mark.asyncio
async def test_nvidia_xid_errors_come_from_the_previous_boot_only(monkeypatch):
    run = AsyncMock(return_value=("", "", 0))
    monkeypatch.setattr(gpu_module, "run_cmd", run)
    monkeypatch.setattr(gpu_module, "cmd_available", lambda name: name == "nvidia-smi")

    await GpuCollector()._collect_nvidia()

    (xid_query,) = _journalctl_calls(run)
    assert "--grep=NVRM.*Xid" in xid_query
    assert "--boot=-1" in xid_query


@pytest.mark.asyncio
async def test_pcie_errors_come_from_the_previous_boot_only(monkeypatch):
    run = AsyncMock(return_value=("", "", 0))
    monkeypatch.setattr(system_module, "run_cmd", run)

    await SystemCollector()._collect_pcie()

    (pcie_query,) = _journalctl_calls(run)
    assert "-k" in pcie_query
    assert "--boot=-1" in pcie_query
