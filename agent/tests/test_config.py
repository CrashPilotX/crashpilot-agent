"""Tests for configuration loading and path resolution."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def reset_settings(monkeypatch, tmp_path):
    """Ensure each test starts with a clean Settings singleton."""
    monkeypatch.setenv("CRASHPILOT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CRASHPILOT_DB_PATH", str(tmp_path / "data" / "test.db"))
    import crashpilot.config as cfg_mod
    cfg_mod._settings = None
    yield
    cfg_mod._settings = None


class TestSettings:
    def test_defaults_are_sane(self):
        from crashpilot.config import get_settings
        cfg = get_settings()
        assert cfg.api_host == "127.0.0.1"
        assert cfg.api_port == 7878
        assert cfg.journal_lines == 5000
        assert cfg.dmesg_lines == 2000
        assert cfg.confidence_threshold == 0.4
        assert cfg.analysis_timeout == 120
        assert cfg.egress_soft_limit_mb == 16
        assert cfg.egress_daily_limit_mb == 32

    def test_data_dir_created(self, tmp_path, monkeypatch):
        data_dir = tmp_path / "crashpilot_data"
        monkeypatch.setenv("CRASHPILOT_DATA_DIR", str(data_dir))
        import crashpilot.config as cfg_mod
        cfg_mod._settings = None
        from crashpilot.config import get_settings
        cfg = get_settings()
        assert cfg.data_dir == data_dir
        assert data_dir.exists()

    def test_db_path_defaults_inside_data_dir(self, tmp_path, monkeypatch):
        monkeypatch.delenv("CRASHPILOT_DB_PATH", raising=False)
        data_dir = tmp_path / "mydata"
        monkeypatch.setenv("CRASHPILOT_DATA_DIR", str(data_dir))
        import crashpilot.config as cfg_mod
        cfg_mod._settings = None
        from crashpilot.config import get_settings
        cfg = get_settings()
        assert cfg.db_path == data_dir / "crashpilot.db"

    def test_explicit_db_path_respected(self, tmp_path, monkeypatch):
        custom_db = tmp_path / "custom.db"
        monkeypatch.setenv("CRASHPILOT_DB_PATH", str(custom_db))
        import crashpilot.config as cfg_mod
        cfg_mod._settings = None
        from crashpilot.config import get_settings
        cfg = get_settings()
        assert cfg.db_path == custom_db

    def test_api_key_from_env(self, monkeypatch):
        monkeypatch.setenv("CRASHPILOT_ANTHROPIC_API_KEY", "sk-ant-test-key")
        import crashpilot.config as cfg_mod
        cfg_mod._settings = None
        from crashpilot.config import get_settings
        cfg = get_settings()
        assert cfg.anthropic_api_key == "sk-ant-test-key"

    def test_empty_api_key_is_falsy(self, monkeypatch):
        monkeypatch.setenv("CRASHPILOT_ANTHROPIC_API_KEY", "")
        import crashpilot.config as cfg_mod
        cfg_mod._settings = None
        from crashpilot.config import get_settings
        cfg = get_settings()
        assert not cfg.anthropic_api_key

    def test_singleton_returns_same_object(self):
        from crashpilot.config import get_settings
        s1 = get_settings()
        s2 = get_settings()
        assert s1 is s2

    def test_db_path_is_not_current_directory(self):
        """Regression: Path('') == PosixPath('.') bug — db_path must not be '.'"""
        from crashpilot.config import get_settings
        cfg = get_settings()
        assert cfg.db_path != Path(".")
        assert cfg.db_path != Path("")
        assert cfg.data_dir != Path(".")
        assert cfg.data_dir != Path("")
        assert cfg.db_path is not None
        assert cfg.data_dir is not None
