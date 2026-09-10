"""Tests for self-enrollment: join tokens, identity, the RPCs, and credentials on disk."""

from __future__ import annotations

import base64
import json
import os
import stat

import httpx
import pytest
import respx

from crashpilot import enrollment
from crashpilot.enrollment import (
    EnrollmentError,
    EnrollmentRateLimited,
    JoinToken,
    enroll,
    enroll_once,
    env_file_for_write,
    parse_join_token,
    reenroll_allowed,
    resolve_identity,
    write_env_values,
)

URL = "https://abc.supabase.co"


def _join(url: str = URL, key: str = "anon", secret: str = "s3cret", **extra: str) -> str:
    payload = {"url": url, "key": key, "join": secret, **extra}
    return "cpjoin_" + base64.b64encode(json.dumps(payload).encode()).decode().rstrip("=")


class TestParseJoinToken:
    def test_decodes_url_key_and_secret(self):
        token = parse_join_token(_join())
        assert token == JoinToken(url=URL, anon_key="anon", secret="s3cret")

    def test_accepts_the_quotes_a_shell_snippet_leaves_on(self):
        assert parse_join_token(f"'{_join()}'").secret == "s3cret"

    def test_points_a_cpilot_string_at_configure(self):
        with pytest.raises(EnrollmentError, match="crashpilot configure"):
            parse_join_token("cpilot_abc")

    def test_rejects_undecodable_and_incomplete_tokens(self):
        with pytest.raises(EnrollmentError, match="could not be decoded"):
            parse_join_token("cpjoin_!!!not-base64!!!")
        incomplete = "cpjoin_" + base64.b64encode(json.dumps({"url": URL}).encode()).decode()
        with pytest.raises(EnrollmentError, match="missing: key, join"):
            parse_join_token(incomplete)

    def test_refuses_to_downgrade_to_plaintext(self):
        with pytest.raises(EnrollmentError, match="https://"):
            parse_join_token(_join(url="http://abc.supabase.co"))


class TestResolveIdentity:
    def test_explicit_identity_wins(self, monkeypatch):
        monkeypatch.setattr(enrollment, "_cloud_instance_id", lambda: pytest.fail("should not probe"))
        assert resolve_identity("my-node", "k8s-node") == "my-node"

    def test_kubernetes_node_name_before_cloud_metadata(self, monkeypatch):
        monkeypatch.setattr(enrollment, "_cloud_instance_id", lambda: pytest.fail("should not probe"))
        assert resolve_identity("", "ip-10-0-1-7") == "k8s:ip-10-0-1-7"

    @respx.mock
    def test_aws_instance_id_through_imdsv2(self):
        respx.put("http://169.254.169.254/latest/api/token").respond(200, text="session")
        instance = respx.get("http://169.254.169.254/latest/meta-data/instance-id").respond(
            200, text="i-0123456789abcdef0",
        )
        assert resolve_identity() == "aws:i-0123456789abcdef0"
        assert instance.calls.last.request.headers["X-aws-ec2-metadata-token"] == "session"

    @respx.mock
    def test_unreachable_metadata_falls_through_to_machine_id(self, monkeypatch):
        # Bare metal: every metadata probe times out, quickly, and the machine
        # ID is used instead.
        respx.route().mock(side_effect=httpx.ConnectTimeout("no metadata service here"))
        monkeypatch.setattr(enrollment, "_machine_id", lambda: "0123456789abcdef0123456789abcdef")
        assert resolve_identity() == "machine:0123456789abcdef0123456789abcdef"

    def test_hostname_is_the_last_resort(self, monkeypatch):
        monkeypatch.setattr(enrollment, "_cloud_instance_id", lambda: None)
        monkeypatch.setattr(enrollment, "_machine_id", lambda: None)
        monkeypatch.setattr(enrollment.socket, "gethostname", lambda: "box")
        assert resolve_identity() == "host:box"

    def test_machine_id_must_look_like_one(self, tmp_path):
        good = tmp_path / "good"
        good.write_text("0123456789abcdef0123456789abcdef\n")
        bad = tmp_path / "bad"
        bad.write_text("uninitialized\n")
        assert enrollment._machine_id(good) == "0123456789abcdef0123456789abcdef"
        assert enrollment._machine_id(bad) is None


TOKEN = JoinToken(url=URL, anon_key="anon", secret="s3cret")
ENROLL_URL = f"{URL}/rest/v1/rpc/agent_enroll"


class TestEnrollOnce:
    @respx.mock
    def test_sends_the_contract_the_rpc_expects(self):
        route = respx.post(ENROLL_URL).respond(
            200, json={"system_id": "sys-1", "agent_token": "tok-1", "name": "node-a"},
        )
        result = enroll_once(TOKEN, "k8s:node-a", "node-a", "1.2.3")
        assert result["system_id"] == "sys-1"
        sent = json.loads(route.calls.last.request.content)
        assert sent == {
            "p_join_secret": "s3cret",
            "p_external_id": "k8s:node-a",
            "p_hostname": "node-a",
            "p_version": "1.2.3",
        }
        assert route.calls.last.request.headers["apikey"] == "anon"

    @respx.mock
    def test_explains_an_unusable_token(self):
        respx.post(ENROLL_URL).respond(400, json={"code": "P0001", "message": "Invalid enrollment token"})
        with pytest.raises(EnrollmentError, match="revoked, expired, or used up"):
            enroll_once(TOKEN, "id", None, "1")

    @respx.mock
    def test_rate_limit_is_retryable(self):
        respx.post(ENROLL_URL).respond(
            400, json={"message": "Too many enrollments with this token in the last minute. Retry shortly."},
        )
        with pytest.raises(EnrollmentRateLimited):
            enroll_once(TOKEN, "id", None, "1")

    @respx.mock
    def test_missing_function_means_the_schema_is_old(self):
        respx.post(ENROLL_URL).respond(404, json={"code": "PGRST202", "message": "Could not find the function"})
        with pytest.raises(EnrollmentError, match="schema.sql"):
            enroll_once(TOKEN, "id", None, "1")

    @respx.mock
    def test_passes_other_server_messages_through(self):
        respx.post(ENROLL_URL).respond(
            400, json={"message": "This account has reached its limit of 50 active systems."},
        )
        with pytest.raises(EnrollmentError, match="limit of 50 active systems"):
            enroll_once(TOKEN, "id", None, "1")


class TestEnrollRetries:
    @respx.mock
    def test_backs_off_while_rate_limited_then_succeeds(self):
        limited = httpx.Response(400, json={"message": "Too many enrollments with this token"})
        ok = httpx.Response(200, json={"system_id": "sys-1", "agent_token": "tok-1"})
        respx.post(ENROLL_URL).mock(side_effect=[limited, limited, ok])
        pauses: list[float] = []
        result = enroll(TOKEN, "id", None, "1", sleep=pauses.append)
        assert result["agent_token"] == "tok-1"
        assert len(pauses) == 2 and all(p > 0 for p in pauses)

    @respx.mock
    def test_gives_up_after_the_budget(self):
        respx.post(ENROLL_URL).respond(400, json={"message": "Too many enrollments with this token"})
        with pytest.raises(EnrollmentError, match="rate-limited"):
            enroll(TOKEN, "id", None, "1", max_wait_seconds=5, sleep=lambda _s: None)


class TestWriteEnvValues:
    def test_updates_in_place_and_keeps_other_settings(self, tmp_path):
        env = tmp_path / ".env"
        env.write_text("CRASHPILOT_ANTHROPIC_API_KEY=sk-ant\nCRASHPILOT_SUPABASE_TOKEN=old\n")
        write_env_values(env, {"CRASHPILOT_SUPABASE_TOKEN": "new", "CRASHPILOT_SUPABASE_URL": URL})
        text = env.read_text()
        assert "CRASHPILOT_ANTHROPIC_API_KEY=sk-ant" in text
        assert "CRASHPILOT_SUPABASE_TOKEN=new" in text and "=old" not in text
        assert f"CRASHPILOT_SUPABASE_URL={URL}" in text

    def test_file_is_private_and_nothing_is_left_behind(self, tmp_path):
        if os.name == "nt":
            pytest.skip("POSIX mode bits")
        env = tmp_path / ".env"
        write_env_values(env, {"CRASHPILOT_SUPABASE_TOKEN": "t"})
        assert stat.S_IMODE(env.stat().st_mode) == 0o600
        assert sorted(p.name for p in tmp_path.iterdir()) == [".env"]

    def test_values_are_written_literally(self, tmp_path):
        env = tmp_path / ".env"
        env.write_text("K=old\n")
        write_env_values(env, {"K": r"a\1b\g<0>"})
        assert env.read_text() == "K=a\\1b\\g<0>\n"

    def test_refuses_values_that_would_inject_lines(self, tmp_path):
        with pytest.raises(ValueError):
            write_env_values(tmp_path / ".env", {"K": "a\nCRASHPILOT_SUPABASE_URL=http://evil"})


class TestHelpers:
    def test_config_dir_is_used_before_the_file_exists(self, monkeypatch, tmp_path):
        # A Kubernetes pod's first boot: the hostPath .env does not exist yet,
        # and ~/.config would not survive a restart.
        monkeypatch.setenv("CRASHPILOT_CONFIG_DIR", str(tmp_path / "persist"))
        missing = tmp_path / "home" / ".config" / "crashpilot" / ".env"
        assert env_file_for_write(missing) == tmp_path / "persist" / ".env"

    def test_reenrollment_is_rate_limited(self, tmp_path):
        marker = tmp_path / "reenroll-attempt"
        assert reenroll_allowed(marker, now=1000.0)
        assert not reenroll_allowed(marker, now=1000.0 + enrollment.REENROLL_COOLDOWN_SECONDS - 1)
        assert reenroll_allowed(marker, now=1000.0 + enrollment.REENROLL_COOLDOWN_SECONDS + 1)
