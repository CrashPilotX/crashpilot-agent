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
    EnrollmentRetired,
    EnrollmentUnavailable,
    JoinToken,
    copied_identity,
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

    @respx.mock
    def test_a_node_retired_on_purpose_is_recognised(self):
        respx.post(ENROLL_URL).respond(400, json={
            "code": "P0001",
            "message": "This node was retired from the dashboard or the API. "
                       "Restore it in the dashboard before it can enroll again.",
            "hint": "retired_manually",
            "details": None,
        })
        with pytest.raises(EnrollmentRetired, match="Restore it in the dashboard"):
            enroll_once(TOKEN, "id", None, "1")

    @respx.mock
    def test_a_server_error_is_retryable(self):
        respx.post(ENROLL_URL).respond(503, json={"message": "upstream unavailable"})
        with pytest.raises(EnrollmentUnavailable):
            enroll_once(TOKEN, "id", None, "1")


class _FakeClock:
    """Time that passes only when the code under test sleeps."""

    def __init__(self) -> None:
        self.now = 0.0
        self.pauses: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.pauses.append(seconds)
        self.now += seconds


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
        clock = _FakeClock()
        with pytest.raises(EnrollmentError, match="rate-limited"):
            enroll(TOKEN, "id", None, "1", max_wait_seconds=5, sleep=clock.sleep, clock=clock)

    @respx.mock
    def test_retries_while_the_network_is_not_up_yet(self):
        # cloud-init runs the installer before the network is up; one refused
        # connection used to be the end of it.
        ok = httpx.Response(200, json={"system_id": "sys-1", "agent_token": "tok-1"})
        respx.post(ENROLL_URL).mock(side_effect=[
            httpx.ConnectError("Network is unreachable"),
            httpx.ConnectTimeout("timed out"),
            httpx.Response(502, text="Bad Gateway"),
            ok,
        ])
        clock = _FakeClock()
        result = enroll(TOKEN, "id", None, "1", sleep=clock.sleep, clock=clock)
        assert result["agent_token"] == "tok-1"
        assert len(clock.pauses) == 3

    @respx.mock
    def test_gives_up_on_an_unreachable_dashboard_after_the_budget(self):
        respx.post(ENROLL_URL).mock(side_effect=httpx.ConnectError("Network is unreachable"))
        clock = _FakeClock()
        with pytest.raises(EnrollmentError, match="could not reach the dashboard"):
            enroll(TOKEN, "id", None, "1", max_wait_seconds=120, sleep=clock.sleep, clock=clock)
        assert clock.now <= 120

    @respx.mock
    def test_the_end_of_the_budget_is_not_a_burst(self):
        # The pause used to be cut to whatever was left of the budget, then
        # jittered below it, so the last seconds became dozens of requests a
        # fraction of a millisecond apart.
        route = respx.post(ENROLL_URL).respond(400, json={"message": "Too many enrollments with this token"})
        clock = _FakeClock()
        with pytest.raises(EnrollmentError):
            enroll(TOKEN, "id", None, "1", max_wait_seconds=300, sleep=clock.sleep, clock=clock)
        assert min(clock.pauses) >= 1.0
        assert route.call_count <= 15
        assert clock.now <= 300

    @respx.mock
    def test_an_unusable_token_is_not_retried(self):
        route = respx.post(ENROLL_URL).respond(400, json={"message": "Invalid enrollment token"})
        with pytest.raises(EnrollmentError, match="revoked"):
            enroll(TOKEN, "id", None, "1", sleep=lambda _s: pytest.fail("should not retry"))
        assert route.call_count == 1


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

    @pytest.mark.parametrize("value", [
        "rack-4 #2",            # python-dotenv drops " #..." as a comment
        "build-${CI_JOB}",      # and expands ${...}, even inside quotes
        "${CI_JOB}${CI_JOB}}",
        "cost$5 and $HOME",
        "it's",
        'say "hi"',
        "both ' and \"",
        "trailing backslash \\",
        "back\\slash ${X} #3 'q' \"dq\"",
        "  padded  ",
        "#leading-hash",
        "plain-value_1.2:3/4",
    ])
    def test_values_read_back_exactly_through_settings(self, tmp_path, monkeypatch, value):
        # Found in review: --external-id 'rack-4 #2' pinned rack-4, and
        # build-${CI_JOB} pinned build-, so re-enrollment used an identity
        # nobody chose.
        import crashpilot.config as cfg_mod

        monkeypatch.setenv("CI_JOB", "EXPANDED")
        monkeypatch.setenv("CRASHPILOT_CONFIG_DIR", str(tmp_path))
        env = tmp_path / ".env"
        env.write_text("CRASHPILOT_EXTERNAL_ID=old\n")
        write_env_values(env, {"CRASHPILOT_EXTERNAL_ID": value, "CRASHPILOT_NODE_NAME": value})
        cfg_mod._settings = None

        settings = cfg_mod.get_settings()

        assert settings.external_id == value
        assert settings.node_name == value
        cfg_mod._settings = None

    def test_simple_values_stay_unquoted(self, tmp_path):
        env = tmp_path / ".env"
        write_env_values(env, {"CRASHPILOT_SUPABASE_URL": URL, "CRASHPILOT_EXTERNAL_ID": "machine:0a1b"})
        assert env.read_text() == f"CRASHPILOT_SUPABASE_URL={URL}\nCRASHPILOT_EXTERNAL_ID=machine:0a1b\n"


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

    def test_an_attempt_recorded_in_the_future_does_not_block(self, tmp_path):
        # Written while the clock ran ahead; once it is corrected backwards the
        # attempt must not hold off enrollment until the clock catches up.
        marker = tmp_path / "reenroll-attempt"
        marker.write_text(str(1000.0 + 86400))
        assert reenroll_allowed(marker, now=1000.0)


class TestCopiedIdentity:
    """A machine image made after enrolling carries the build machine's
    pinned identity and credentials; each copy must enroll as itself."""

    def test_a_copy_with_its_own_instance_id_is_noticed(self, tmp_path, monkeypatch):
        monkeypatch.setattr(enrollment, "_aws_instance_id", lambda client: "i-0copy000000000001")
        monkeypatch.setattr(enrollment, "resolve_identity", lambda explicit="", node_name="": "aws:i-0copy000000000001")
        state = tmp_path / "identity-check"

        moved, fresh = copied_identity("aws:i-0build00000000001", "", state, boot_id="boot-1")

        assert (moved, fresh) == ("aws:i-0copy000000000001", True)

    def test_the_same_machine_is_left_alone(self, tmp_path, monkeypatch):
        monkeypatch.setattr(enrollment, "_machine_id", lambda: "0123456789abcdef0123456789abcdef")
        moved, _ = copied_identity(
            "machine:0123456789abcdef0123456789abcdef", "", tmp_path / "identity-check", boot_id="boot-1",
        )
        assert moved is None

    def test_a_metadata_service_that_does_not_answer_is_not_a_copy(self, tmp_path, monkeypatch):
        # The flaky-metadata case the pinning exists for: no answer this time
        # is not a different machine.
        monkeypatch.setattr(enrollment, "_aws_instance_id", lambda client: None)
        monkeypatch.setattr(enrollment, "resolve_identity", lambda *a, **k: pytest.fail("no re-detection"))
        moved, _ = copied_identity("aws:i-0build00000000001", "", tmp_path / "identity-check", boot_id="b")
        assert moved is None

    def test_a_different_kind_of_identity_is_not_compared(self, tmp_path, monkeypatch):
        monkeypatch.setattr(enrollment, "_machine_id", lambda: "f" * 32)
        monkeypatch.setattr(enrollment, "_aws_instance_id", lambda client: pytest.fail("aws not probed"))
        # Pinned from a hostname: renames are legitimate, so nothing is probed.
        assert copied_identity("host:build-box", "", tmp_path / "s1", boot_id="b") == (None, True)

    def test_checked_once_per_boot(self, tmp_path, monkeypatch):
        probes: list[int] = []
        monkeypatch.setattr(enrollment, "_machine_id", lambda: probes.append(1) or "b" * 32)
        monkeypatch.setattr(enrollment, "resolve_identity", lambda explicit="", node_name="": "machine:" + "b" * 32)
        state = tmp_path / "identity-check"
        pinned = "machine:" + "a" * 32

        first = copied_identity(pinned, "", state, boot_id="boot-1")
        again = copied_identity(pinned, "", state, boot_id="boot-1")
        next_boot = copied_identity(pinned, "", state, boot_id="boot-2")

        assert first == ("machine:" + "b" * 32, True)
        assert again == ("machine:" + "b" * 32, False)
        assert next_boot == ("machine:" + "b" * 32, True)
        assert len(probes) == 2

    def test_a_new_pinned_identity_is_checked_again(self, tmp_path, monkeypatch):
        monkeypatch.setattr(enrollment, "_machine_id", lambda: "b" * 32)
        monkeypatch.setattr(enrollment, "resolve_identity", lambda explicit="", node_name="": "machine:" + "b" * 32)
        state = tmp_path / "identity-check"
        copied_identity("machine:" + "a" * 32, "", state, boot_id="boot-1")

        # After enrolling as itself, the same boot must not keep "moving".
        assert copied_identity("machine:" + "b" * 32, "", state, boot_id="boot-1") == (None, True)

    def test_an_unanswered_check_is_tried_again_later_in_the_boot(self, tmp_path, monkeypatch):
        # A metadata service slow at boot must not leave a copy reporting as
        # the build machine until the next reboot.
        answers = iter([None, "i-0copy000000000001"])
        probes: list[int] = []
        monkeypatch.setattr(
            enrollment, "_aws_instance_id", lambda client: probes.append(1) or next(answers),
        )
        state = tmp_path / "identity-check"
        pinned = "aws:i-0build00000000001"

        assert copied_identity(pinned, "", state, boot_id="b", now=1000.0) == (None, True)
        # Not every minute, though.
        assert copied_identity(pinned, "", state, boot_id="b", now=1060.0) == (None, False)
        assert len(probes) == 1
        later = 1000.0 + enrollment.REENROLL_COOLDOWN_SECONDS + 1
        assert copied_identity(pinned, "", state, boot_id="b", now=later) == ("aws:i-0copy000000000001", True)

    def test_the_answer_is_what_it_enrolls_under(self, tmp_path, monkeypatch):
        # Detecting again could miss the metadata service this time and fall
        # through to a machine ID every copy of the image shares.
        monkeypatch.setattr(enrollment, "_aws_instance_id", lambda client: "i-0copy000000000001")
        monkeypatch.setattr(enrollment, "resolve_identity", lambda *a, **k: pytest.fail("detected again"))
        moved, _ = copied_identity("aws:i-0build00000000001", "", tmp_path / "s", boot_id="b")
        assert moved == "aws:i-0copy000000000001"

    def test_no_boot_id_means_no_check(self, tmp_path, monkeypatch):
        monkeypatch.setattr(enrollment, "_machine_id", lambda: pytest.fail("not probed"))
        assert copied_identity("machine:" + "a" * 32, "", tmp_path / "s", boot_id=None) == (None, False)
