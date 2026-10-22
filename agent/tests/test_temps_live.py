"""Component temperatures for the heartbeat: what is read from hwmon, what is left out, how it is attached."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from crashpilot import cloud_push
from crashpilot.collectors.temps_live import collect_temperatures


def chip(root: Path, index: int, name: str, temps: dict[int, tuple[str | None, int | str]] | None = None, **extra):
    """One /sys/class/hwmon/hwmonN. temps maps N -> (label, millidegrees); extra files are written as given."""
    path = root / "class" / "hwmon" / f"hwmon{index}"
    path.mkdir(parents=True, exist_ok=True)
    (path / "name").write_text(name + "\n")
    for n, (label, value) in (temps or {}).items():
        (path / f"temp{n}_input").write_text(f"{value}\n")
        if label is not None:
            (path / f"temp{n}_label").write_text(label + "\n")
    for filename, content in extra.items():
        (path / filename).write_text(f"{content}\n")
    return path


def link_device(hwmon: Path, root: Path, target: str, **files: str) -> Path:
    """Point hwmon/device at a directory named like the real one (nvme0, or a SCSI device with a block/ child)."""
    device = root / "devices" / target
    device.mkdir(parents=True, exist_ok=True)
    for filename, content in files.items():
        (device / filename).write_text(content + "\n")
    (hwmon / "device").symlink_to(device)
    return device


class TestServer2Layout:
    """The layout read from server2 (Ryzen 9 9950X, two DIMM sensors, one NVMe drive, three AMD GPUs)."""

    @pytest.fixture
    def root(self, tmp_path):
        chip(tmp_path, 0, "r8169_0_b00:00", {1: (None, 45000)})
        chip(tmp_path, 2, "nvme", {1: ("Composite", 27800), 2: ("Sensor 2", 32800)},
             temp1_max=84800, temp1_crit=85800)
        nvme = tmp_path / "class" / "hwmon" / "hwmon2"
        link_device(nvme, tmp_path, "nvme0")
        (tmp_path / "class" / "nvme" / "nvme0").mkdir(parents=True)
        (tmp_path / "class" / "nvme" / "nvme0" / "model").write_text("WD_BLACK SN850X 2000GB\n")
        chip(tmp_path, 3, "k10temp", {1: ("Tctl", 93000), 3: ("Tccd2", 75000), 2: ("Tccd1", 88000)})
        chip(tmp_path, 4, "spd5118", {1: (None, 35000)}, temp1_max=55000, temp1_crit=85000)
        chip(tmp_path, 5, "spd5118", {1: (None, 34200)})
        chip(tmp_path, 6, "asus")
        chip(tmp_path, 10, "amdgpu", {1: ("edge", 56000)})
        return tmp_path

    def test_cpu_leads_with_its_package_reading(self, root):
        cpu = collect_temperatures(root)["cpu"]
        assert cpu["package_c"] == 93.0
        assert cpu["max_c"] == 93.0
        assert [(s["label"], s["temp_c"]) for s in cpu["sensors"]] == [
            ("Tctl", 93.0), ("Tccd1", 88.0), ("Tccd2", 75.0),
        ]
        assert {s["chip"] for s in cpu["sensors"]} == {"k10temp"}

    def test_each_dimm_is_numbered_and_carries_its_limits(self, root):
        memory = collect_temperatures(root)["memory"]
        assert [(s["label"], s["temp_c"]) for s in memory["sensors"]] == [("DIMM 1", 35.0), ("DIMM 2", 34.2)]
        assert memory["sensors"][0]["high_c"] == 55.0
        assert memory["sensors"][0]["crit_c"] == 85.0
        assert "high_c" not in memory["sensors"][1]
        assert memory["max_c"] == 35.0

    def test_the_nvme_drive_is_named_with_its_model(self, root):
        disk = collect_temperatures(root)["disk"]
        composite, extra = disk["sensors"]
        assert (composite["label"], composite["temp_c"]) == ("Composite", 27.8)
        assert composite["device"] == "nvme0"
        assert composite["model"] == "WD_BLACK SN850X 2000GB"
        assert composite["high_c"] == 84.8 and composite["crit_c"] == 85.8
        assert extra["label"] == "Sensor 2"
        assert disk["max_c"] == 32.8

    def test_gpus_network_cards_and_other_chips_are_not_components(self, root):
        assert set(collect_temperatures(root)) == {"cpu", "memory", "disk"}
        everything = json.dumps(collect_temperatures(root))
        assert "amdgpu" not in everything and "r8169" not in everything and "asus" not in everything

    def test_the_whole_document_is_json_and_small(self, root):
        document = json.dumps(collect_temperatures(root))
        assert len(document) < 1500


class TestCpuReading:
    def test_intel_package_beats_the_cores_and_cores_sort_naturally(self, tmp_path):
        temps = {1: ("Package id 0", 61000)}
        temps.update({n + 2: (f"Core {n}", 50000 + n * 1000) for n in range(12)})
        chip(tmp_path, 0, "coretemp", temps)
        cpu = collect_temperatures(tmp_path)["cpu"]
        labels = [s["label"] for s in cpu["sensors"]]
        assert cpu["package_c"] == 61.0
        assert labels[0] == "Package id 0"
        assert labels[1:4] == ["Core 0", "Core 1", "Core 2"]  # not Core 0, Core 1, Core 10

    def test_the_cap_drops_cores_but_max_counts_every_sensor(self, tmp_path):
        temps = {1: ("Package id 0", 60000)}
        temps.update({n + 2: (f"Core {n}", 40000 + n * 1000) for n in range(30)})
        temps[40] = ("Core 99", 99000)
        chip(tmp_path, 0, "coretemp", temps)
        cpu = collect_temperatures(tmp_path)["cpu"]
        assert len(cpu["sensors"]) == 16
        assert cpu["max_c"] == 99.0  # the hottest core was cut from the list, not from the maximum
        assert cpu["package_c"] == 60.0

    def test_tdie_is_preferred_to_tctl(self, tmp_path):
        chip(tmp_path, 0, "k10temp", {1: ("Tctl", 85000), 2: ("Tdie", 65000)})
        assert collect_temperatures(tmp_path)["cpu"]["package_c"] == 65.0

    def test_a_chip_with_unlabelled_sensors_reports_its_hottest(self, tmp_path):
        chip(tmp_path, 0, "cpu_thermal", {1: (None, 48000), 2: (None, 52000)})
        cpu = collect_temperatures(tmp_path)["cpu"]
        assert cpu["package_c"] == 52.0
        assert cpu["sensors"][0]["label"] == "temp1"

    def test_the_board_sensor_is_not_mistaken_for_the_cpu(self, tmp_path):
        chip(tmp_path, 0, "acpitz", {1: (None, 40000)})
        assert collect_temperatures(tmp_path) == {}


class TestDisks:
    def test_a_sata_drive_is_named_by_its_block_device(self, tmp_path):
        hwmon = chip(tmp_path, 0, "drivetemp", {1: (None, 38000)}, temp1_max=60000)
        device = link_device(hwmon, tmp_path, "pci0000:00/host0/target0:0:0/0:0:0:0", model="Samsung SSD 860 EVO")
        (device / "block" / "sda").mkdir(parents=True)
        (sensor,) = collect_temperatures(tmp_path)["disk"]["sensors"]
        assert (sensor["device"], sensor["label"], sensor["temp_c"]) == ("sda", "Drive", 38.0)
        assert sensor["model"] == "Samsung SSD 860 EVO"
        assert sensor["high_c"] == 60.0

    def test_two_nvme_drives_are_told_apart(self, tmp_path):
        for index, name in enumerate(("nvme0", "nvme1")):
            hwmon = chip(tmp_path, index, "nvme", {1: ("Composite", 30000 + index * 5000)})
            link_device(hwmon, tmp_path, name)
        sensors = collect_temperatures(tmp_path)["disk"]["sensors"]
        assert [(s["device"], s["temp_c"]) for s in sensors] == [("nvme0", 30.0), ("nvme1", 35.0)]

    def test_a_disk_without_a_device_link_still_reports(self, tmp_path):
        chip(tmp_path, 0, "nvme", {1: ("Composite", 41000)})
        (sensor,) = collect_temperatures(tmp_path)["disk"]["sensors"]
        assert sensor["temp_c"] == 41.0
        assert "model" not in sensor


class TestWhatIsLeftOut:
    def test_nothing_to_read_gives_nothing(self, tmp_path):
        assert collect_temperatures(tmp_path) == {}  # no /sys/class/hwmon at all (a container, WSL)
        (tmp_path / "class" / "hwmon").mkdir(parents=True)
        assert collect_temperatures(tmp_path) == {}

    @pytest.mark.parametrize("value", [-273150, 255000, 150000, -40000, "n/a", ""])
    def test_a_nonsense_reading_is_dropped_not_reported(self, tmp_path, value):
        chip(tmp_path, 0, "k10temp", {1: ("Tctl", value), 2: ("Tccd1", 50000)})
        cpu = collect_temperatures(tmp_path)["cpu"]
        assert [s["label"] for s in cpu["sensors"]] == ["Tccd1"]

    def test_a_component_whose_only_sensor_is_unusable_is_absent(self, tmp_path):
        chip(tmp_path, 0, "spd5118", {1: (None, 255000)})
        assert "memory" not in collect_temperatures(tmp_path)

    def test_a_sensor_the_kernel_refuses_to_read_is_skipped(self, tmp_path):
        hwmon = chip(tmp_path, 0, "k10temp", {1: ("Tctl", 70000)})
        (hwmon / "temp2_input").mkdir()  # reading a directory fails like EIO/EBUSY does
        (hwmon / "temp2_label").write_text("Tccd1\n")
        cpu = collect_temperatures(tmp_path)["cpu"]
        assert [s["label"] for s in cpu["sensors"]] == ["Tctl"]

    def test_absurd_limits_are_ignored(self, tmp_path):
        chip(tmp_path, 0, "nvme", {1: ("Composite", 40000)}, temp1_max=-5000, temp1_crit=900000)
        (sensor,) = collect_temperatures(tmp_path)["disk"]["sensors"]
        assert "high_c" not in sensor and "crit_c" not in sensor

    def test_a_chip_with_no_name_is_skipped(self, tmp_path):
        path = tmp_path / "class" / "hwmon" / "hwmon0"
        path.mkdir(parents=True)
        (path / "temp1_input").write_text("50000\n")
        assert collect_temperatures(tmp_path) == {}

    def test_labels_cannot_carry_control_characters(self, tmp_path):
        chip(tmp_path, 0, "k10temp", {1: ("Tc\x1b[31mtl\x00", 70000)})
        assert collect_temperatures(tmp_path)["cpu"]["sensors"][0]["label"] == "Tc[31mtl"


class TestHeartbeat:
    """The readings ride inside the section of the component they describe, and only there."""

    @pytest.fixture(autouse=True)
    def _isolated_collectors(self, monkeypatch, tmp_path):
        monkeypatch.setattr(cloud_push, "_section_ttl_path", lambda: tmp_path / "section_ttl.json")
        monkeypatch.setattr(cloud_push.os, "getloadavg", lambda: (0.5, 0.4, 0.3), raising=False)
        monkeypatch.setattr(cloud_push, "_read_meminfo", lambda: {"MemTotal": 16777216, "MemAvailable": 8388608})
        monkeypatch.setattr(cloud_push, "_collect_disk_usage", lambda: {"primary": {"used_pct": 10}})
        monkeypatch.setattr(cloud_push, "_collect_network_usage", lambda: {"rx_mbps": 1.0, "tx_mbps": 1.0})
        monkeypatch.setattr(cloud_push, "collect_amd_gpus", lambda: [])
        monkeypatch.setattr(cloud_push.shutil, "which", lambda name: None)

    READINGS = {
        "cpu": {"package_c": 60.0, "max_c": 60.0, "sensors": [{"chip": "k10temp", "label": "Tctl", "temp_c": 60.0}]},
        "memory": {"max_c": 35.0, "sensors": [{"chip": "spd5118", "label": "DIMM 1", "temp_c": 35.0}]},
        "disk": {"max_c": 30.0, "sensors": [{"chip": "nvme", "label": "Composite", "temp_c": 30.0, "device": "nvme0"}]},
    }

    def test_each_component_carries_its_own_readings(self, monkeypatch):
        monkeypatch.setattr(cloud_push, "collect_temperatures", lambda: self.READINGS)
        metrics = cloud_push._build_live_metrics(slim=True)
        assert metrics["cpu"]["temperatures"] == self.READINGS["cpu"]
        assert metrics["memory"]["temperatures"] == self.READINGS["memory"]
        assert metrics["disk"]["temperatures"] == self.READINGS["disk"]
        assert "temperatures" not in metrics  # nothing at the top level
        assert metrics["cpu"]["load_1m"] == 0.5  # the load figures are untouched

    def test_a_machine_with_no_sensors_sends_nothing_extra(self, monkeypatch):
        monkeypatch.setattr(cloud_push, "collect_temperatures", lambda: {})
        metrics = cloud_push._build_live_metrics(slim=True)
        assert all("temperatures" not in metrics[c] for c in ("cpu", "memory", "disk"))

    def test_a_reading_never_creates_a_section_that_has_no_usage_data(self, monkeypatch):
        monkeypatch.setattr(cloud_push, "_read_meminfo", lambda: {})  # meminfo unreadable: no memory section
        monkeypatch.setattr(cloud_push, "collect_temperatures", lambda: self.READINGS)
        metrics = cloud_push._build_live_metrics(slim=True)
        assert "memory" not in metrics
        assert metrics["cpu"]["temperatures"] == self.READINGS["cpu"]

    def test_a_failing_reader_costs_the_readings_not_the_heartbeat(self, monkeypatch):
        def broken():
            raise RuntimeError("hwmon went away")

        monkeypatch.setattr(cloud_push, "collect_temperatures", broken)
        metrics = cloud_push._build_live_metrics(slim=True)
        assert "temperatures" not in metrics["cpu"]
        assert "load_pct" in metrics["cpu"] and "gpu" in metrics
