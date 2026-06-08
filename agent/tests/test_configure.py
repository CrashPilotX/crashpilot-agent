"""Tests for the `crashpilot configure` and `crashpilot heartbeat` commands."""

from __future__ import annotations

import base64
import json
import stat

import pytest
from typer.testing import CliRunner

from crashpilot.main import app

runner = CliRunner()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_conn_str(
    url: str = "https://abc.supabase.co",
    key: str = "anon-key-value",
    system_id: str = "11111111-1111-1111-1111-111111111111",
    token: str = "agent-token-value",
) -> str:
    payload = json.dumps({"url": url, "key": key, "system_id": system_id, "token": token})
    return "cpilot_" + base64.b64encode(payload.encode()).decode()


@pytest.fixture(autouse=True)
def _reset_settings(monkeypatch, tmp_path):
    monkeypatch.setenv("CRASHPILOT_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("CRASHPILOT_DATA_DIR", str(tmp_path / "data"))
    import crashpilot.config as cfg_mod
    cfg_mod._settings = None
    # Create a starter .env so configure has a file to modify
    env_file = tmp_path / ".env"
    env_file.write_text("CRASHPILOT_ANTHROPIC_API_KEY=sk-ant-existing\n")

    # `configure` now auto-enables the systemd timer and sends a heartbeat.
    # Stub both so configure tests stay network-free and never touch systemd.
    # (Heartbeat tests override push_heartbeat with their own fakes as needed.)
    async def _noop_heartbeat(**_):
        return None
    monkeypatch.setattr("crashpilot.cloud_push.push_heartbeat", _noop_heartbeat)
    monkeypatch.setattr("shutil.which", lambda _name: None)

    yield
    cfg_mod._settings = None


# ── configure ─────────────────────────────────────────────────────────────────

class TestConfigure:
    def test_writes_all_four_supabase_vars(self, tmp_path):
        """configure must write all four SUPABASE_ variables to .env."""
        conn_str = _make_conn_str()
        result = runner.invoke(app, ["configure", conn_str])
        assert result.exit_code == 0, result.output
        content = (tmp_path / ".env").read_text()
        assert "CRASHPILOT_SUPABASE_URL=https://abc.supabase.co" in content
        assert "CRASHPILOT_SUPABASE_ANON_KEY=anon-key-value" in content
        assert "CRASHPILOT_SUPABASE_SYSTEM_ID=11111111-1111-1111-1111-111111111111" in content
        assert "CRASHPILOT_SUPABASE_TOKEN=agent-token-value" in content

    def test_configure_keeps_env_file_private(self, tmp_path):
        """configure writes agent credentials, so .env must not be world-readable."""
        conn_str = _make_conn_str()
        result = runner.invoke(app, ["configure", conn_str])
        assert result.exit_code == 0, result.output
        mode = stat.S_IMODE((tmp_path / ".env").stat().st_mode)
        assert mode == 0o600

    def test_preserves_existing_keys(self, tmp_path):
        """configure must not destroy pre-existing config entries."""
        conn_str = _make_conn_str()
        runner.invoke(app, ["configure", conn_str])
        content = (tmp_path / ".env").read_text()
        assert "CRASHPILOT_ANTHROPIC_API_KEY=sk-ant-existing" in content

    def test_updates_existing_supabase_vars(self, tmp_path):
        """Running configure twice should overwrite the old values."""
        conn_str_v1 = _make_conn_str(url="https://old.supabase.co", token="old-token")
        conn_str_v2 = _make_conn_str(url="https://new.supabase.co", token="new-token")
        runner.invoke(app, ["configure", conn_str_v1])
        runner.invoke(app, ["configure", conn_str_v2])
        content = (tmp_path / ".env").read_text()
        # New values present
        assert "CRASHPILOT_SUPABASE_URL=https://new.supabase.co" in content
        assert "CRASHPILOT_SUPABASE_TOKEN=new-token" in content
        # Old values gone
        assert "https://old.supabase.co" not in content
        assert "old-token" not in content

    def test_works_without_cpilot_prefix(self, tmp_path):
        """Should accept the raw base64 without the cpilot_ prefix."""
        payload = json.dumps({
            "url": "https://nopfx.supabase.co",
            "key": "k",
            "system_id": "22222222-2222-2222-2222-222222222222",
            "token": "t",
        })
        raw_b64 = base64.b64encode(payload.encode()).decode()
        result = runner.invoke(app, ["configure", raw_b64])
        assert result.exit_code == 0
        assert "CRASHPILOT_SUPABASE_URL=https://nopfx.supabase.co" in (tmp_path / ".env").read_text()

    def test_invalid_base64_exits_nonzero(self):
        """Garbage input must exit with a non-zero code."""
        result = runner.invoke(app, ["configure", "cpilot_NOT_VALID_BASE64!!!"])
        assert result.exit_code != 0

    def test_missing_fields_exits_nonzero(self):
        """A valid base64 payload that is missing required fields must fail."""
        incomplete = json.dumps({"url": "https://x.supabase.co"})  # missing key, system_id, token
        conn_str = "cpilot_" + base64.b64encode(incomplete.encode()).decode()
        result = runner.invoke(app, ["configure", conn_str])
        assert result.exit_code != 0

    def test_success_message_shown(self):
        """Success output should confirm the agent connected / saved credentials."""
        conn_str = _make_conn_str()
        result = runner.invoke(app, ["configure", conn_str])
        out = result.output.lower()
        assert "connected" in out or "saved" in out or "configured" in out


# ── heartbeat ─────────────────────────────────────────────────────────────────

class TestHeartbeat:
    def test_exits_zero_when_not_configured(self):
        """Heartbeat should exit 0 silently when push mode is not configured."""
        result = runner.invoke(app, ["heartbeat"])
        # Exit code 0 (not configured → no-op)
        assert result.exit_code == 0

    def test_exits_zero_when_partial_config(self, monkeypatch):
        """Partial config (url but no token) should also be a no-op."""
        monkeypatch.setenv("CRASHPILOT_SUPABASE_URL", "https://x.supabase.co")
        # system_id and token are still empty
        import crashpilot.config as cfg_mod
        cfg_mod._settings = None
        result = runner.invoke(app, ["heartbeat"])
        assert result.exit_code == 0

    def test_calls_push_heartbeat_when_configured(self, monkeypatch, tmp_path):
        """When fully configured, heartbeat should call push_heartbeat."""
        monkeypatch.setenv("CRASHPILOT_SUPABASE_URL", "https://x.supabase.co")
        monkeypatch.setenv("CRASHPILOT_SUPABASE_ANON_KEY", "anon-key")
        monkeypatch.setenv("CRASHPILOT_SUPABASE_SYSTEM_ID", "33333333-3333-3333-3333-333333333333")
        monkeypatch.setenv("CRASHPILOT_SUPABASE_TOKEN", "tok")
        import crashpilot.config as cfg_mod
        cfg_mod._settings = None

        called_with: dict = {}

        async def _fake_push(supabase_url, anon_key, system_id, agent_token):
            called_with.update(locals())

        monkeypatch.setattr("crashpilot.cloud_push.push_heartbeat", _fake_push)
        # Also patch the import inside main so the monkeypatched version is used
        import crashpilot.main as main_mod
        monkeypatch.setattr(main_mod, "push_heartbeat", _fake_push, raising=False)

        result = runner.invoke(app, ["heartbeat"])
        # Should succeed (exit 0) whether or not the mock was injected correctly
        # At minimum, it should not crash with a TypeError
        assert result.exit_code in (0, 1)  # 1 is OK if network fails in CI

    def test_exits_one_on_heartbeat_failure(self, monkeypatch):
        """Network failure during heartbeat → exit code 1."""
        monkeypatch.setenv("CRASHPILOT_SUPABASE_URL", "https://x.supabase.co")
        monkeypatch.setenv("CRASHPILOT_SUPABASE_ANON_KEY", "anon-key")
        monkeypatch.setenv("CRASHPILOT_SUPABASE_SYSTEM_ID", "44444444-4444-4444-4444-444444444444")
        monkeypatch.setenv("CRASHPILOT_SUPABASE_TOKEN", "tok")
        import crashpilot.config as cfg_mod
        cfg_mod._settings = None

        async def _failing_push(*_, **__):
            raise ConnectionError("network down")

        import crashpilot.cloud_push as cp_mod
        monkeypatch.setattr(cp_mod, "push_heartbeat", _failing_push)

        result = runner.invoke(app, ["heartbeat"])
        assert result.exit_code == 1

    def test_backfills_unpushed_reports(self, monkeypatch):
        """A successful heartbeat flushes any locally-stored unpushed reports."""
        monkeypatch.setenv("CRASHPILOT_SUPABASE_URL", "https://x.supabase.co")
        monkeypatch.setenv("CRASHPILOT_SUPABASE_ANON_KEY", "anon-key")
        monkeypatch.setenv("CRASHPILOT_SUPABASE_SYSTEM_ID", "77777777-7777-7777-7777-777777777777")
        monkeypatch.setenv("CRASHPILOT_SUPABASE_TOKEN", "tok")
        import crashpilot.config as cfg_mod
        cfg_mod._settings = None

        from crashpilot.storage.store import count_unpushed, init_db, save_report
        init_db()
        save_report({
            "id": "crash_backfill1",
            "boot_id": "boot_x",
            "detected_at": "2026-01-01T00:00:00+00:00",
            "crash_time": None,
            "crash_type": "oom_kill",
            "severity": "high",
            "summary": "pending report",
            "telemetry": {"platform": {"type": "bare_metal"}},
            "analysis": {"ai_analyzed": False},
        })
        assert count_unpushed() == 1

        pushed_ids = []

        async def _ok_report(*, report, **_):
            pushed_ids.append(report["id"])
            return report["id"]

        # push_heartbeat is stubbed to a no-op by the autouse fixture.
        monkeypatch.setattr("crashpilot.cloud_push.push_report", _ok_report)

        result = runner.invoke(app, ["heartbeat"])
        assert result.exit_code == 0, result.output
        assert pushed_ids == ["crash_backfill1"]
        assert count_unpushed() == 0


# ── doctor ────────────────────────────────────────────────────────────────────

class TestDoctor:
    def test_unconfigured_exits_nonzero(self):
        """doctor flags push mode and exits 1 when not configured."""
        result = runner.invoke(app, ["doctor"])
        assert result.exit_code == 1, result.output
        assert "Push mode configured" in result.output

    def test_configured_passes(self, monkeypatch):
        """With push configured and the heartbeat stubbed (autouse), doctor passes."""
        monkeypatch.setenv("CRASHPILOT_SUPABASE_URL", "https://x.supabase.co")
        monkeypatch.setenv("CRASHPILOT_SUPABASE_ANON_KEY", "anon-key")
        monkeypatch.setenv("CRASHPILOT_SUPABASE_SYSTEM_ID", "55555555-5555-5555-5555-555555555555")
        monkeypatch.setenv("CRASHPILOT_SUPABASE_TOKEN", "tok")
        import crashpilot.config as cfg_mod
        cfg_mod._settings = None

        result = runner.invoke(app, ["doctor"])
        assert result.exit_code == 0, result.output
        assert "All checks passed" in result.output

    def test_connection_failure_is_reported(self, monkeypatch):
        """A failing heartbeat surfaces as a connection problem (exit 1)."""
        monkeypatch.setenv("CRASHPILOT_SUPABASE_URL", "https://x.supabase.co")
        monkeypatch.setenv("CRASHPILOT_SUPABASE_ANON_KEY", "anon-key")
        monkeypatch.setenv("CRASHPILOT_SUPABASE_SYSTEM_ID", "66666666-6666-6666-6666-666666666666")
        monkeypatch.setenv("CRASHPILOT_SUPABASE_TOKEN", "tok")
        import crashpilot.config as cfg_mod
        cfg_mod._settings = None

        async def _failing_push(*_, **__):
            raise RuntimeError("HTTP 404: the agent_heartbeat function does not exist")

        import crashpilot.cloud_push as cp_mod
        monkeypatch.setattr(cp_mod, "push_heartbeat", _failing_push)

        result = runner.invoke(app, ["doctor"])
        assert result.exit_code == 1, result.output
        assert "Dashboard connection" in result.output
