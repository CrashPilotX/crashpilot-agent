"""Tests for standalone operational CLI commands."""

from __future__ import annotations

import tarfile

from crashpilot.main import support_bundle


def test_support_bundle_initializes_storage_and_writes_sanitized_files(tmp_path, monkeypatch):
    monkeypatch.setenv("CRASHPILOT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("CRASHPILOT_DB_PATH", str(tmp_path / "crashpilot.db"))
    import crashpilot.config as cfg_mod

    cfg_mod._settings = None
    output = tmp_path / "support.tar.gz"

    support_bundle(output=str(output))

    with tarfile.open(output, "r:gz") as archive:
        assert sorted(archive.getnames()) == [
            "flight-recorder.json",
            "recent-reports.json",
            "system.json",
        ]
    cfg_mod._settings = None
