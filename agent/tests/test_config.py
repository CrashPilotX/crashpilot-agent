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

    def test_data_dir_is_private(self, tmp_path, monkeypatch):
        # The crash database and dmesg caches in it were readable by every
        # local user: the directory was created 0755 and the files 0644.
        import os
        import stat

        data_dir = tmp_path / "crashpilot"
        monkeypatch.setenv("CRASHPILOT_DATA_DIR", str(data_dir))
        import crashpilot.config as cfg_mod
        cfg_mod._settings = None
        old_umask = os.umask(0o022)
        try:
            cfg_mod.get_settings()
        finally:
            os.umask(old_umask)
        assert stat.S_IMODE(data_dir.stat().st_mode) == 0o700

    def test_an_existing_open_data_dir_is_closed(self, tmp_path, monkeypatch):
        # A Kubernetes hostPath (DirectoryOrCreate) or an older install made it 0755.
        import stat

        data_dir = tmp_path / "var" / "lib" / "crashpilot"
        data_dir.mkdir(parents=True)
        data_dir.chmod(0o755)
        monkeypatch.setenv("CRASHPILOT_DATA_DIR", str(data_dir))
        import crashpilot.config as cfg_mod
        cfg_mod._settings = None
        cfg_mod.get_settings()
        assert stat.S_IMODE(data_dir.stat().st_mode) == 0o700

    def test_a_shared_directory_is_not_closed(self, tmp_path, monkeypatch):
        # Pointed at a shared directory by mistake: never lock others out of it.
        import stat

        shared = tmp_path / "shared"
        shared.mkdir()
        shared.chmod(0o755)
        monkeypatch.setenv("CRASHPILOT_DATA_DIR", str(shared))
        import crashpilot.config as cfg_mod
        cfg_mod._settings = None
        cfg_mod.get_settings()
        assert stat.S_IMODE(shared.stat().st_mode) == 0o755

    def test_default_data_dir_follows_the_install(self, tmp_path, monkeypatch):
        # The .deb's services keep their data in /var/lib/crashpilot; a CLI run
        # by hand on that machine must read the same database.
        import crashpilot.config as cfg_mod

        installer = tmp_path / "opt" / "crashpilot" / "data"
        package = tmp_path / "var" / "lib" / "crashpilot"
        monkeypatch.setattr(cfg_mod, "_INSTALLER_DATA_DIR", installer)
        monkeypatch.setattr(cfg_mod, "_PACKAGE_DATA_DIR", package)
        home = Path.home() / ".local" / "share" / "crashpilot"

        assert cfg_mod._default_data_dir() == home
        package.mkdir(parents=True)
        assert cfg_mod._default_data_dir() == package
        installer.parent.mkdir(parents=True)
        assert cfg_mod._default_data_dir() == installer

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

    def test_resets_settings_re_discover_the_env_file_when_config_dir_changes(self, tmp_path, monkeypatch):
        """Regression: Settings.model_config's env_file=_find_env_file() is
        only evaluated once, when the Settings class body first executes -
        it does not automatically re-run just because _settings is reset to
        None. get_settings() must explicitly re-discover the .env path on
        every call, or a CRASHPILOT_CONFIG_DIR change made after this
        module's first import would be silently ignored forever, even
        though "reset _settings and call get_settings() again" looks like
        (and is documented/used everywhere else as) a full reload."""
        import crashpilot.config as cfg_mod

        dir_a = tmp_path / "a"
        dir_b = tmp_path / "b"
        dir_a.mkdir()
        dir_b.mkdir()
        (dir_a / ".env").write_text("CRASHPILOT_ANTHROPIC_API_KEY=from-dir-a\n")
        (dir_b / ".env").write_text("CRASHPILOT_ANTHROPIC_API_KEY=from-dir-b\n")

        # A real env var takes precedence over the .env file, which would
        # mask the very thing this test is checking.
        monkeypatch.delenv("CRASHPILOT_ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setenv("CRASHPILOT_CONFIG_DIR", str(dir_a))
        cfg_mod._settings = None
        first = cfg_mod.get_settings()
        assert first.anthropic_api_key == "from-dir-a"

        monkeypatch.setenv("CRASHPILOT_CONFIG_DIR", str(dir_b))
        cfg_mod._settings = None
        second = cfg_mod.get_settings()
        assert second.anthropic_api_key == "from-dir-b"

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

    def test_a_plaintext_supabase_url_is_never_sent_to(self, monkeypatch):
        # configure and join tokens already insist on https://, but a URL from
        # the environment (a Kubernetes Secret, a docker .env) was used as is,
        # sending the agent token in plaintext. It is refused where requests
        # are made, so settings still load and local analysis still runs.
        import asyncio

        import crashpilot.config as cfg_mod
        from crashpilot.cloud_push import push_heartbeat, push_report
        from crashpilot.config import InsecureSupabaseURL
        from crashpilot.enrollment import sign_off

        monkeypatch.setenv("CRASHPILOT_SUPABASE_URL", "http://abc.supabase.co")
        cfg_mod._settings = None
        cfg = cfg_mod.get_settings()
        assert cfg.supabase_url == "http://abc.supabase.co"

        def _no_network(*_a, **_k):
            raise AssertionError("a request was made to a plaintext URL")

        monkeypatch.setattr("httpx.post", _no_network)
        monkeypatch.setattr("httpx.AsyncClient.post", _no_network)
        url = cfg.supabase_url
        with pytest.raises(InsecureSupabaseURL, match="must start with https://"):
            asyncio.run(push_heartbeat(supabase_url=url, anon_key="a", system_id="s", agent_token="t"))
        with pytest.raises(InsecureSupabaseURL):
            asyncio.run(push_report(supabase_url=url, anon_key="a", system_id="s", agent_token="t", report={}))
        with pytest.raises(InsecureSupabaseURL):
            sign_off(url, "a", "s", "t")

    def test_an_https_or_empty_supabase_url_is_fine(self, monkeypatch):
        import crashpilot.config as cfg_mod

        monkeypatch.setenv("CRASHPILOT_SUPABASE_URL", "https://abc.supabase.co")
        cfg_mod._settings = None
        assert cfg_mod.get_settings().supabase_url == "https://abc.supabase.co"
        monkeypatch.setenv("CRASHPILOT_SUPABASE_URL", "")
        cfg_mod._settings = None
        assert cfg_mod.get_settings().supabase_url == ""

    def test_db_path_is_not_current_directory(self):
        """Regression: Path('') == PosixPath('.') bug: db_path must not be '.'"""
        from crashpilot.config import get_settings
        cfg = get_settings()
        assert cfg.db_path != Path(".")
        assert cfg.db_path != Path("")
        assert cfg.data_dir != Path(".")
        assert cfg.data_dir != Path("")
        assert cfg.db_path is not None
        assert cfg.data_dir is not None
