"""Tests for standalone operational CLI commands."""

from __future__ import annotations

import base64
import json
import tarfile

import pytest
import typer

from crashpilot.main import configure, support_bundle


def _connection_string(url: str, key: str = "anon-key", system_id: str = "sys-1", token: str = "tok-1") -> str:
    payload = json.dumps({"url": url, "key": key, "system_id": system_id, "token": token})
    return "cpilot_" + base64.b64encode(payload.encode()).decode().rstrip("=")


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


def test_configure_rejects_non_https_supabase_url(tmp_path, monkeypatch):
    monkeypatch.setenv("CRASHPILOT_CONFIG_DIR", str(tmp_path))
    import crashpilot.config as cfg_mod

    cfg_mod._settings = None
    env_path = tmp_path / ".env"

    conn = _connection_string(url="http://insecure.example.com")

    with pytest.raises(typer.Exit):
        configure(connection_string=conn)

    # Must fail before writing anything - a rejected connection string
    # should never leave a partially-configured .env behind.
    assert not env_path.exists()
    cfg_mod._settings = None


def test_configure_accepts_https_supabase_url(tmp_path, monkeypatch):
    monkeypatch.setenv("CRASHPILOT_CONFIG_DIR", str(tmp_path))
    import crashpilot.config as cfg_mod

    cfg_mod._settings = None
    env_path = tmp_path / ".env"
    # _find_env_file() only honors CRASHPILOT_CONFIG_DIR if a .env already
    # exists there - for a from-scratch directory it falls through to
    # ~/.config/crashpilot/.env, which would silently write into (and
    # clobber) a real user config on the machine running this test. Pre-touch
    # an empty file so the CRASHPILOT_CONFIG_DIR candidate exists first.
    env_path.touch()

    # configure() sends a real heartbeat after writing .env (best-effort,
    # wrapped in try/except) - stub it out so this test doesn't make a live
    # network call to a placeholder domain.
    import crashpilot.cloud_push as cloud_push_mod

    async def _fake_push_heartbeat(**_kwargs):
        return {}

    monkeypatch.setattr(cloud_push_mod, "push_heartbeat", _fake_push_heartbeat)

    conn = _connection_string(url="https://project.supabase.co")

    configure(connection_string=conn)

    assert env_path.exists()
    contents = env_path.read_text()
    assert "CRASHPILOT_SUPABASE_URL=https://project.supabase.co" in contents
    cfg_mod._settings = None
