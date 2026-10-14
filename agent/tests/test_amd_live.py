"""AMD GPU readings for the heartbeat, from a fake /sys tree.

The tree mirrors a real amdgpu machine: /sys/class/drm/cardN/device is a symlink to the
PCI device directory, whose `driver` symlink names the bound driver. A sensor file that
is a directory instead of a file raises OSError when read, which is how a sleeping card's
"device or resource busy" looks to the code.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from crashpilot import cloud_push
from crashpilot.collectors import amd_live
from crashpilot.collectors.amd_live import collect_amd_gpus, lspci_device_names

REFUSED = object()  # a sensor the kernel refuses to read


def make_card(
    root: Path, number: int, *, bdf: str, vendor: str = "0x1002", device_id: str = "0x73bf", driver: str | None = "amdgpu",
    status: str = "active", files: dict | None = None, hwmon: dict | None = None, product_name: str | None = None,
) -> Path:
    dev = root / "devices" / "pci0000:00" / bdf
    dev.mkdir(parents=True)
    (dev / "vendor").write_text(vendor + "\n")
    (dev / "device").write_text(device_id + "\n")
    (dev / "power").mkdir()
    (dev / "power" / "runtime_status").write_text(status + "\n")
    if product_name:
        (dev / "product_name").write_text(product_name + "\n")
    if driver:
        drivers = root / "bus" / "pci" / "drivers" / driver
        drivers.mkdir(parents=True, exist_ok=True)
        (dev / "driver").symlink_to(drivers)
    for name, value in (files or {}).items():
        target = dev / name
        if value is REFUSED:
            target.mkdir()
        else:
            target.write_text(f"{value}\n")
    if hwmon is not None:
        hw = dev / "hwmon" / "hwmon7"
        hw.mkdir(parents=True)
        for name, value in hwmon.items():
            target = hw / name
            if value is REFUSED:
                target.mkdir()
            else:
                target.write_text(f"{value}\n")
    drm = root / "class" / "drm"
    drm.mkdir(parents=True, exist_ok=True)
    (drm / f"card{number}").mkdir()
    (drm / f"card{number}" / "device").symlink_to(dev)
    return dev


NO_NAMES = lambda: {}  # noqa: E731


ACTIVE_FILES = {"gpu_busy_percent": 37, "mem_busy_percent": 12, "mem_info_vram_total": 17163091968, "mem_info_vram_used": 3221225472}
ACTIVE_HWMON = {
    "temp1_label": "edge", "temp1_input": 41000, "temp2_label": "junction", "temp2_input": 55000,
    "temp3_label": "mem", "temp3_input": 48000, "power1_average": 123_400_000,
}


class TestAnActiveCard:
    def test_everything_the_kernel_reports_is_read(self, tmp_path):
        make_card(tmp_path, 0, bdf="0000:03:00.0", files=ACTIVE_FILES, hwmon=ACTIVE_HWMON)
        (gpu,) = collect_amd_gpus(tmp_path, NO_NAMES)
        assert gpu == {
            "index": 0, "name": "AMD GPU 1002:73bf", "pci_address": "0000:03:00.0", "power_state": "active",
            "memory_total_mb": 16368, "memory_used_mb": 3072,
            "utilization_gpu_pct": 37, "utilization_memory_pct": 12,
            "temperature_gpu": 41, "temperature_junction": 55, "power_draw_w": 123.4,
        }

    def test_the_result_is_plain_json(self, tmp_path):
        make_card(tmp_path, 0, bdf="0000:03:00.0", files=ACTIVE_FILES, hwmon=ACTIVE_HWMON)
        json.dumps(collect_amd_gpus(tmp_path, NO_NAMES))

    def test_a_value_the_kernel_refuses_is_unknown_not_zero(self, tmp_path):
        make_card(tmp_path, 0, bdf="0000:03:00.0", files={**ACTIVE_FILES, "gpu_busy_percent": REFUSED},
                  hwmon={**ACTIVE_HWMON, "temp1_input": REFUSED, "power1_average": REFUSED})
        (gpu,) = collect_amd_gpus(tmp_path, NO_NAMES)
        assert gpu["utilization_gpu_pct"] is None
        assert gpu["temperature_gpu"] is None
        assert gpu["power_draw_w"] is None
        assert gpu["utilization_memory_pct"] == 12 and gpu["temperature_junction"] == 55

    def test_a_card_that_lacks_a_file_reports_none_for_it(self, tmp_path):
        make_card(tmp_path, 0, bdf="0000:03:00.0", files={"mem_info_vram_total": 1073741824, "mem_info_vram_used": 0})
        (gpu,) = collect_amd_gpus(tmp_path, NO_NAMES)
        assert gpu["utilization_gpu_pct"] is None and gpu["temperature_gpu"] is None and gpu["power_draw_w"] is None
        assert gpu["memory_total_mb"] == 1024 and gpu["memory_used_mb"] == 0

    @pytest.mark.parametrize("busy", ["255", "-1", "abc", ""])
    def test_a_nonsense_utilization_is_unknown(self, tmp_path, busy):
        make_card(tmp_path, 0, bdf="0000:03:00.0", files={"gpu_busy_percent": busy})
        (gpu,) = collect_amd_gpus(tmp_path, NO_NAMES)
        assert gpu["utilization_gpu_pct"] is None

    def test_instantaneous_power_is_used_when_there_is_no_average(self, tmp_path):
        make_card(tmp_path, 0, bdf="0000:11:00.0", hwmon={"power1_input": 9_000_000})
        assert collect_amd_gpus(tmp_path, NO_NAMES)[0]["power_draw_w"] == 9.0

    def test_unlabelled_sensors_fall_back_to_the_first_temperature(self, tmp_path):
        make_card(tmp_path, 0, bdf="0000:11:00.0", hwmon={"temp1_input": 46000})
        gpu = collect_amd_gpus(tmp_path, NO_NAMES)[0]
        assert gpu["temperature_gpu"] == 46 and gpu["temperature_junction"] is None


class TestASleepingCard:
    """The reason this reader exists as it does: a sleeping card must not be woken."""

    def test_it_is_reported_as_suspended_and_idle_with_its_memory(self, tmp_path):
        make_card(tmp_path, 1, bdf="0000:07:00.0", status="suspended",
                  files={**ACTIVE_FILES, "gpu_busy_percent": REFUSED}, hwmon={k: REFUSED for k in ACTIVE_HWMON})
        (gpu,) = collect_amd_gpus(tmp_path, NO_NAMES)
        assert gpu["power_state"] == "suspended"
        assert gpu["utilization_gpu_pct"] == 0 and gpu["utilization_memory_pct"] == 0
        assert gpu["temperature_gpu"] is None and gpu["temperature_junction"] is None and gpu["power_draw_w"] is None
        assert gpu["memory_total_mb"] == 16368

    @pytest.mark.parametrize("status", ["suspended", "suspending", "SUSPENDED"])
    def test_every_asleep_state_counts(self, tmp_path, status):
        make_card(tmp_path, 0, bdf="0000:07:00.0", status=status, files=ACTIVE_FILES, hwmon=ACTIVE_HWMON)
        assert collect_amd_gpus(tmp_path, NO_NAMES)[0]["power_state"] == "suspended"

    @pytest.mark.parametrize("status", ["active", "resuming", "unsupported", ""])
    def test_other_states_are_read_normally(self, tmp_path, status):
        make_card(tmp_path, 0, bdf="0000:07:00.0", status=status, files=ACTIVE_FILES, hwmon=ACTIVE_HWMON)
        gpu = collect_amd_gpus(tmp_path, NO_NAMES)[0]
        assert gpu["power_state"] == "active" and gpu["utilization_gpu_pct"] == 37

    def test_no_sensor_file_is_even_opened_while_it_sleeps(self, tmp_path, monkeypatch):
        make_card(tmp_path, 0, bdf="0000:07:00.0", status="suspended", files=ACTIVE_FILES, hwmon=ACTIVE_HWMON)
        opened: list[str] = []
        real = amd_live._read

        def spy(path: Path):
            opened.append(path.name)
            return real(path)

        monkeypatch.setattr(amd_live, "_read", spy)  # every file read goes through it, _read_int included
        collect_amd_gpus(tmp_path, NO_NAMES)
        assert {"runtime_status", "mem_info_vram_total"} <= set(opened), "the spy sees the reads it should"
        woken_by = {"gpu_busy_percent", "mem_busy_percent", "temp1_input", "temp2_input", "temp3_input", "power1_average", "power1_input"}
        assert woken_by.isdisjoint(opened), sorted(woken_by & set(opened))


class TestWhichCardsCount:
    def test_other_vendors_and_other_drivers_are_left_out(self, tmp_path):
        make_card(tmp_path, 0, bdf="0000:01:00.0", vendor="0x10de", device_id="0x2684", driver="nvidia", files=ACTIVE_FILES)
        make_card(tmp_path, 1, bdf="0000:02:00.0", driver="radeon", files=ACTIVE_FILES)
        make_card(tmp_path, 2, bdf="0000:03:00.0", driver=None, files=ACTIVE_FILES)  # unbound
        make_card(tmp_path, 3, bdf="0000:04:00.0", files=ACTIVE_FILES)
        gpus = collect_amd_gpus(tmp_path, NO_NAMES)
        assert [(g["index"], g["pci_address"]) for g in gpus] == [(0, "0000:04:00.0")]

    def test_connectors_are_not_cards_and_cards_sort_by_number(self, tmp_path):
        for number, bdf in ((10, "0000:0a:00.0"), (2, "0000:02:00.0"), (0, "0000:00:00.0")):
            make_card(tmp_path, number, bdf=bdf, files=ACTIVE_FILES)
        drm = tmp_path / "class" / "drm"
        (drm / "card0-DP-1").mkdir()
        (drm / "renderD128").mkdir()
        gpus = collect_amd_gpus(tmp_path, NO_NAMES)
        assert [g["pci_address"] for g in gpus] == ["0000:00:00.0", "0000:02:00.0", "0000:0a:00.0"]
        assert [g["index"] for g in gpus] == [0, 1, 2]

    def test_index_counts_amd_gpus_only(self, tmp_path):
        make_card(tmp_path, 0, bdf="0000:01:00.0", vendor="0x10de", driver="nvidia")
        make_card(tmp_path, 1, bdf="0000:02:00.0", files=ACTIVE_FILES)
        assert collect_amd_gpus(tmp_path, NO_NAMES)[0]["index"] == 0

    def test_a_machine_without_a_drm_tree_has_no_gpus(self, tmp_path):
        assert collect_amd_gpus(tmp_path, NO_NAMES) == []

    def test_lspci_is_not_asked_when_there_is_nothing_to_name(self, tmp_path):
        def boom():
            raise AssertionError("no AMD card, no lspci")

        make_card(tmp_path, 0, bdf="0000:01:00.0", vendor="0x10de", driver="nvidia")
        assert collect_amd_gpus(tmp_path, boom) == []


class TestNames:
    def test_the_card_says_its_own_name_when_it_does(self, tmp_path):
        make_card(tmp_path, 0, bdf="0000:03:00.0", product_name="AMD Radeon PRO W6800")
        assert collect_amd_gpus(tmp_path, lambda: {"0000:03:00.0": "Navi 21"})[0]["name"] == "AMD Radeon PRO W6800"

    def test_otherwise_the_pci_database_name_is_used(self, tmp_path):
        make_card(tmp_path, 0, bdf="0000:03:00.0")
        name = collect_amd_gpus(tmp_path, lambda: {"0000:03:00.0": "Navi 21 [Radeon RX 6800/6800 XT / 6900 XT]"})[0]["name"]
        assert name == "AMD Navi 21 [Radeon RX 6800/6800 XT / 6900 XT]"

    def test_a_device_the_database_does_not_know_is_named_by_its_ids(self, tmp_path):
        make_card(tmp_path, 0, bdf="0000:11:00.0", device_id="0x13c0")
        assert collect_amd_gpus(tmp_path, lambda: {"0000:11:00.0": "Device 13c0"})[0]["name"] == "AMD GPU 1002:13c0"

    def test_without_lspci_it_is_named_by_its_ids(self, tmp_path):
        make_card(tmp_path, 0, bdf="0000:03:00.0")
        assert collect_amd_gpus(tmp_path, NO_NAMES)[0]["name"] == "AMD GPU 1002:73bf"


class TestLspciNames:
    LINE = '03:00.0 "VGA compatible controller" "Advanced Micro Devices, Inc. [AMD/ATI]" "Navi 21 [Radeon RX 6800/6800 XT / 6900 XT]" -rc3 "Sapphire" "Device e438"\n'

    def runner(self, stdout="", raises=None):
        def run(*args, **kwargs):
            if raises:
                raise raises
            return SimpleNamespace(stdout=stdout, returncode=0)
        return run

    def test_the_real_output_format_is_parsed(self):
        names = lspci_device_names(self.runner(self.LINE + '11:00.0 "VGA compatible controller" "Advanced Micro Devices, Inc. [AMD/ATI]" "Device 13c0" -rc1\n'))
        assert names == {"0000:03:00.0": "Navi 21 [Radeon RX 6800/6800 XT / 6900 XT]", "0000:11:00.0": "Device 13c0"}

    def test_a_full_address_is_kept_as_it_is(self):
        assert lspci_device_names(self.runner(self.LINE.replace("03:00.0", "0000:03:00.0", 1))) == {
            "0000:03:00.0": "Navi 21 [Radeon RX 6800/6800 XT / 6900 XT]"}

    @pytest.mark.parametrize("failure", [FileNotFoundError("lspci"), subprocess.TimeoutExpired("lspci", 10)])
    def test_a_missing_or_stuck_lspci_gives_no_names(self, failure):
        assert lspci_device_names(self.runner(raises=failure)) == {}

    def test_garbage_lines_are_skipped(self):
        assert lspci_device_names(self.runner('unbalanced "quote\n\n' + self.LINE)) == {
            "0000:03:00.0": "Navi 21 [Radeon RX 6800/6800 XT / 6900 XT]"}


class TestInTheHeartbeat:
    @pytest.fixture(autouse=True)
    def _isolated_collectors(self, monkeypatch, tmp_path):
        monkeypatch.setattr(cloud_push, "_section_ttl_path", lambda: tmp_path / "section_ttl.json")
        monkeypatch.setattr(cloud_push.os, "getloadavg", lambda: (0.5, 0.4, 0.3), raising=False)
        monkeypatch.setattr(cloud_push, "_read_meminfo", lambda: {})
        monkeypatch.setattr(cloud_push, "_collect_disk_usage", lambda: {"primary": {"used_pct": 10}})
        monkeypatch.setattr(cloud_push, "_collect_network_usage", lambda: {"rx_mbps": 1.0, "tx_mbps": 1.0})
        monkeypatch.setattr(cloud_push.shutil, "which", lambda name: None)

    def test_amd_gpus_are_reported_beside_the_nvidia_entry(self, monkeypatch):
        gpus = [{"index": 0, "name": "AMD GPU 1002:73bf", "utilization_gpu_pct": 5}]
        monkeypatch.setattr(cloud_push, "collect_amd_gpus", lambda: gpus)
        metrics = cloud_push._build_live_metrics(slim=True)
        assert metrics["gpu"]["amd"] == {"available": True, "gpus": gpus}
        assert metrics["gpu"]["nvidia"] == {"available": False, "gpus": []}

    def test_a_machine_without_amd_gpus_says_so(self, monkeypatch):
        monkeypatch.setattr(cloud_push, "collect_amd_gpus", lambda: [])
        assert cloud_push._build_live_metrics(slim=True)["gpu"]["amd"] == {"available": False, "gpus": []}

    def test_a_failing_reader_costs_the_section_not_the_heartbeat(self, monkeypatch):
        def broken():
            raise RuntimeError("sysfs went away")

        monkeypatch.setattr(cloud_push, "collect_amd_gpus", broken)
        metrics = cloud_push._build_live_metrics(slim=True)
        assert metrics["gpu"]["amd"] == {"available": False, "gpus": []}
        assert "cpu" in metrics and "gpu" in metrics

    def test_the_nvidia_entry_is_untouched_when_the_tool_is_present(self, monkeypatch):
        monkeypatch.setattr(cloud_push.shutil, "which", lambda name: "/usr/bin/nvidia-smi" if name == "nvidia-smi" else None)
        monkeypatch.setattr(
            cloud_push.subprocess, "run",
            lambda *a, **k: SimpleNamespace(returncode=0, stdout="0, NVIDIA RTX 4090, 12, 3, 24564, 1024, 55, 90.5\n"),
        )
        monkeypatch.setattr(cloud_push, "collect_amd_gpus", lambda: [{"index": 0}])
        gpu = cloud_push._build_live_metrics(slim=True)["gpu"]
        assert gpu["nvidia"]["gpus"][0]["name"] == "NVIDIA RTX 4090"
        assert gpu["nvidia"]["gpus"][0]["utilization_gpu_pct"] == 12
        assert gpu["amd"]["available"] is True

    def test_agent_health_lists_rocm_smi_among_the_tools(self, monkeypatch):
        monkeypatch.setattr(cloud_push.shutil, "which", lambda name: "/opt/rocm/bin/rocm-smi" if name == "rocm-smi" else None)
        monkeypatch.setattr(cloud_push, "_systemctl_unit_state", lambda unit: ("active", "enabled"))
        health = cloud_push._build_agent_health({})
        assert health["tools"]["rocm_smi"] is True
        assert health["tools"]["nvidia_smi"] is False
