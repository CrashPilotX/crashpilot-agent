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


def test_safe_extract_rejects_path_traversal(tmp_path):
    bundle_path = tmp_path / "bad.tar.gz"
    bundle_path.write_bytes(_bundle_bytes("../../outside"))

    with pytest.raises(ValueError, match="unsafe bundle path"):
        updater._safe_extract(bundle_path, tmp_path / "extract")
