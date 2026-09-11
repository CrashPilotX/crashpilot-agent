"""Tests for `crashpilot enroll`, `crashpilot sign-off`, and heartbeat self-enrollment."""

from __future__ import annotations

import base64
import json

import pytest
from typer.testing import CliRunner

from crashpilot.main import app

runner = CliRunner()
URL = "https://abc.supabase.co"


def _join(secret: str = "s3cret") -> str:
    payload = json.dumps({"url": URL, "key": "anon", "join": secret})
    return "cpjoin_" + base64.b64encode(payload.encode()).decode()


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("CRASHPILOT_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("CRASHPILOT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CRASHPILOT_DB_PATH", str(tmp_path / "data" / "test.db"))
    for name in ("CRASHPILOT_ENROLL_TOKEN", "CRASHPILOT_NODE_NAME", "CRASHPILOT_EXTERNAL_ID"):
        monkeypatch.delenv(name, raising=False)
    import crashpilot.config as cfg_mod
    cfg_mod._settings = None
    monkeypatch.setattr("shutil.which", lambda _name: None)
    monkeypatch.setattr("crashpilot.enrollment.resolve_identity", lambda explicit="", node_name="": explicit or "machine:abc")
    yield
    cfg_mod._settings = None


@pytest.fixture()
def fake_enroll(monkeypatch):
    """Stub the agent_enroll RPC; each call issues a new agent token."""
    calls: list[dict] = []

    def _enroll(token, external_id, hostname, version, **_):
        calls.append({"secret": token.secret, "external_id": external_id})
        return {"system_id": "sys-1", "agent_token": f"tok-{len(calls)}", "name": hostname or "node"}

    monkeypatch.setattr("crashpilot.enrollment.enroll", _enroll)
    return calls


@pytest.fixture()
def heartbeats(monkeypatch):
    sent: list[str] = []

    async def _hb(**kwargs):
        sent.append(kwargs["agent_token"])

    monkeypatch.setattr("crashpilot.cloud_push.push_heartbeat", _hb)
    return sent


class TestEnrollCommand:
    def test_saves_credentials_and_the_join_token(self, tmp_path, fake_enroll, heartbeats):
        result = runner.invoke(app, ["enroll", _join()])
        assert result.exit_code == 0, result.output
        env = (tmp_path / ".env").read_text()
        assert f"CRASHPILOT_SUPABASE_URL={URL}" in env
        assert "CRASHPILOT_SUPABASE_SYSTEM_ID=sys-1" in env
        assert "CRASHPILOT_SUPABASE_TOKEN=tok-1" in env
        # Kept so the node can enroll again if its credentials are rejected.
        assert "CRASHPILOT_ENROLL_TOKEN=cpjoin_" in env
        assert heartbeats == ["tok-1"]
        assert fake_enroll == [{"secret": "s3cret", "external_id": "machine:abc"}]

    def test_explicit_identity_is_passed_through(self, fake_enroll, heartbeats):
        result = runner.invoke(app, ["enroll", _join(), "--external-id", "rack-7/slot-3"])
        assert result.exit_code == 0, result.output
        assert fake_enroll[0]["external_id"] == "rack-7/slot-3"

    def test_no_op_when_already_enrolled(self, monkeypatch, fake_enroll, heartbeats):
        monkeypatch.setenv("CRASHPILOT_SUPABASE_URL", URL)
        monkeypatch.setenv("CRASHPILOT_SUPABASE_ANON_KEY", "anon")
        monkeypatch.setenv("CRASHPILOT_SUPABASE_SYSTEM_ID", "sys-0")
        monkeypatch.setenv("CRASHPILOT_SUPABASE_TOKEN", "tok-0")
        monkeypatch.setattr("crashpilot.main._credentials_work", lambda *_: True)
        result = runner.invoke(app, ["enroll", _join()])
        assert result.exit_code == 0, result.output
        assert "Already enrolled" in result.output
        assert fake_enroll == []

    def test_explains_a_missing_token(self):
        result = runner.invoke(app, ["enroll"])
        assert result.exit_code == 1
        assert "No join token" in result.output

    def test_reports_enrollment_errors(self, monkeypatch):
        from crashpilot.enrollment import EnrollmentError

        def _fail(*_a, **_k):
            raise EnrollmentError("This join token is not valid")

        monkeypatch.setattr("crashpilot.enrollment.enroll", _fail)
        result = runner.invoke(app, ["enroll", _join()])
        assert result.exit_code == 1
        assert "not valid" in result.output


class TestHeartbeatEnrollment:
    def test_a_node_with_only_a_join_token_enrolls_on_first_heartbeat(
        self, monkeypatch, tmp_path, fake_enroll, heartbeats,
    ):
        # The Kubernetes case: the token comes from the environment, not disk.
        monkeypatch.setenv("CRASHPILOT_ENROLL_TOKEN", _join())
        result = runner.invoke(app, ["heartbeat", "--quiet"])
        assert result.exit_code == 0, result.output
        assert heartbeats == ["tok-1"]
        env = (tmp_path / ".env").read_text()
        assert "CRASHPILOT_SUPABASE_TOKEN=tok-1" in env
        # A token handed over through the environment is not copied to disk.
        assert "CRASHPILOT_ENROLL_TOKEN" not in env

    def test_rejected_credentials_trigger_one_re_enrollment(self, monkeypatch, fake_enroll):
        from crashpilot.cloud_push import CredentialsRejected

        monkeypatch.setenv("CRASHPILOT_ENROLL_TOKEN", _join())
        monkeypatch.setenv("CRASHPILOT_SUPABASE_URL", URL)
        monkeypatch.setenv("CRASHPILOT_SUPABASE_ANON_KEY", "anon")
        monkeypatch.setenv("CRASHPILOT_SUPABASE_SYSTEM_ID", "sys-1")
        sent: list[str] = []

        async def _hb(**kwargs):
            sent.append(kwargs["agent_token"])
            if kwargs["agent_token"] == "stale":
                raise CredentialsRejected("Invalid system_id or agent_token")

        monkeypatch.setattr("crashpilot.cloud_push.push_heartbeat", _hb)
        # Stale credentials in the environment would outrank the new ones in
        # .env, so drop them the way a real restore would leave only .env.
        monkeypatch.setenv("CRASHPILOT_SUPABASE_TOKEN", "stale")

        def _enroll_and_drop_env(*args, **kwargs):
            monkeypatch.delenv("CRASHPILOT_SUPABASE_TOKEN", raising=False)
            return original(*args, **kwargs)

        import crashpilot.main as main_mod
        original = main_mod._enroll_and_store
        monkeypatch.setattr(main_mod, "_enroll_and_store", _enroll_and_drop_env)

        result = runner.invoke(app, ["heartbeat", "--quiet"])
        assert result.exit_code == 0, result.output
        assert len(fake_enroll) == 1
        assert sent == ["stale", "tok-1"]

    def test_re_enrollment_respects_the_cooldown(self, monkeypatch, tmp_path, fake_enroll):
        from crashpilot.cloud_push import CredentialsRejected

        monkeypatch.setenv("CRASHPILOT_ENROLL_TOKEN", _join())
        monkeypatch.setenv("CRASHPILOT_SUPABASE_URL", URL)
        monkeypatch.setenv("CRASHPILOT_SUPABASE_ANON_KEY", "anon")
        monkeypatch.setenv("CRASHPILOT_SUPABASE_SYSTEM_ID", "sys-1")
        monkeypatch.setenv("CRASHPILOT_SUPABASE_TOKEN", "stale")

        async def _always_rejected(**_):
            raise CredentialsRejected("Invalid system_id or agent_token")

        monkeypatch.setattr("crashpilot.cloud_push.push_heartbeat", _always_rejected)
        (tmp_path / "data").mkdir(exist_ok=True)
        import time
        (tmp_path / "data" / "reenroll-attempt").write_text(str(time.time()))

        result = runner.invoke(app, ["heartbeat", "--quiet"])
        assert result.exit_code == 1
        assert fake_enroll == []

    def test_without_a_join_token_rejection_is_just_a_failure(self, monkeypatch, fake_enroll):
        from crashpilot.cloud_push import CredentialsRejected

        monkeypatch.setenv("CRASHPILOT_SUPABASE_URL", URL)
        monkeypatch.setenv("CRASHPILOT_SUPABASE_ANON_KEY", "anon")
        monkeypatch.setenv("CRASHPILOT_SUPABASE_SYSTEM_ID", "sys-1")
        monkeypatch.setenv("CRASHPILOT_SUPABASE_TOKEN", "stale")

        async def _rejected(**_):
            raise CredentialsRejected("Invalid system_id or agent_token")

        monkeypatch.setattr("crashpilot.cloud_push.push_heartbeat", _rejected)
        result = runner.invoke(app, ["heartbeat", "--quiet"])
        assert result.exit_code == 1
        assert fake_enroll == []


class TestSignOff:
    def _configured(self, monkeypatch):
        monkeypatch.setenv("CRASHPILOT_SUPABASE_URL", URL)
        monkeypatch.setenv("CRASHPILOT_SUPABASE_ANON_KEY", "anon")
        monkeypatch.setenv("CRASHPILOT_SUPABASE_SYSTEM_ID", "sys-1")
        monkeypatch.setenv("CRASHPILOT_SUPABASE_TOKEN", "tok-1")

    def test_calls_the_rpc_with_this_nodes_credentials(self, monkeypatch):
        self._configured(monkeypatch)
        calls: list[tuple] = []
        monkeypatch.setattr("crashpilot.enrollment.sign_off", lambda *args: calls.append(args))
        result = runner.invoke(app, ["sign-off"])
        assert result.exit_code == 0, result.output
        assert calls == [(URL, "anon", "sys-1", "tok-1")]

    def test_a_failure_never_fails_the_shutdown(self, monkeypatch):
        self._configured(monkeypatch)

        def _boom(*_):
            raise RuntimeError("network is down")

        monkeypatch.setattr("crashpilot.enrollment.sign_off", _boom)
        result = runner.invoke(app, ["sign-off", "--quiet"])
        assert result.exit_code == 0
        assert "Could not sign off" in result.output

    def test_unconfigured_is_a_silent_no_op(self, monkeypatch):
        called: list[int] = []
        monkeypatch.setattr("crashpilot.enrollment.sign_off", lambda *a: called.append(1))
        result = runner.invoke(app, ["sign-off"])
        assert result.exit_code == 0
        assert called == []


class TestIdentityIsPinned:
    def test_re_enrollment_reuses_the_identity_enrolled_under(self, monkeypatch, tmp_path, fake_enroll, heartbeats):
        # Found end to end: a node enrolled with --external-id came back after
        # a retire-by-hand as a new system, because re-enrollment detected a
        # different identity instead of reusing the one it enrolled under.
        result = runner.invoke(app, ["enroll", _join(), "--external-id", "rack-7/slot-3"])
        assert result.exit_code == 0, result.output
        assert "CRASHPILOT_EXTERNAL_ID=rack-7/slot-3" in (tmp_path / ".env").read_text()

        import crashpilot.config as cfg_mod
        import crashpilot.main as main_mod

        cfg_mod._settings = None
        main_mod._enroll_and_store(cfg_mod.get_settings().enroll_token, "", persist_token=False)
        assert [call["external_id"] for call in fake_enroll] == ["rack-7/slot-3", "rack-7/slot-3"]
