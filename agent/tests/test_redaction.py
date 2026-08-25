"""Tests for automatic secret detection and redaction (redaction.py).

This is the mechanism behind the "automatic secret redaction" claim on the
Data Handling page - these tests exist to keep that claim true.
"""

from __future__ import annotations

from crashpilot.redaction import redact_telemetry, redact_text, redact_value


class TestRedactText:
    def test_redacts_aws_access_key(self):
        redacted, counts = redact_text("AWS key: AKIAIOSFODNN7EXAMPLE in config")
        assert "AKIAIOSFODNN7EXAMPLE" not in redacted
        assert "[REDACTED:aws_access_key]" in redacted
        assert counts == {"aws_access_key": 1}

    def test_redacts_github_token(self):
        redacted, counts = redact_text("export GITHUB_TOKEN=ghp_1234567890abcdef1234567890abcdef1234")
        assert "ghp_" not in redacted
        assert counts == {"github_token": 1}

    def test_redacts_anthropic_key(self):
        redacted, counts = redact_text("Authorization: Bearer sk-ant-api03-abcdefghijklmnopqrstuvwxyz1234567890")
        assert "sk-ant-" not in redacted
        assert counts.get("anthropic_key") == 1

    def test_redacts_basic_auth_credential_in_url_but_keeps_host(self):
        redacted, counts = redact_text("DATABASE_URL=postgres://myuser:SuperSecret123@db.example.com:5432/app")
        assert "SuperSecret123" not in redacted
        assert "db.example.com" in redacted
        assert "myuser" in redacted
        assert counts == {"basic_auth_url": 1}

    def test_redacts_labeled_password_but_keeps_the_key_name(self):
        redacted, counts = redact_text("password: hunter2isnotenoughchars")
        assert "hunter2isnotenoughchars" not in redacted
        assert "password:" in redacted
        assert counts == {"labeled_credential": 1}

    def test_redacts_private_key_block(self):
        text = "-----BEGIN RSA PRIVATE KEY-----\nMIIEpAIBAAKCAQEA...\n-----END RSA PRIVATE KEY-----"
        redacted, counts = redact_text(text)
        assert "MIIEpAIBAAKCAQEA" not in redacted
        assert counts == {"private_key_block": 1}

    def test_redacts_jwt(self):
        jwt = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dGVzdHNpZ25hdHVyZQ"
        redacted, counts = redact_text(jwt)
        assert redacted == "[REDACTED:jwt]"
        assert counts == {"jwt": 1}

    def test_does_not_redact_short_placeholder_values(self):
        # "null" is 4 chars, below the 6-char minimum - a common non-secret
        # placeholder that shouldn't trigger a false positive.
        redacted, counts = redact_text("token: null")
        assert redacted == "token: null"
        assert counts == {}

    def test_does_not_redact_normal_log_lines(self):
        line = "systemd service normal-line kernel: Out of memory: Killed process 1234 (python3)"
        redacted, counts = redact_text(line)
        assert redacted == line
        assert counts == {}

    def test_redacts_labeled_password_containing_special_characters(self):
        # Regression: the value character class used to be alnum-only, so a
        # password with @/!/# etc. failed the whole match and passed through
        # completely unredacted instead of being caught.
        redacted, counts = redact_text("password: P@ssw0rd!123")
        assert "P@ssw0rd!123" not in redacted
        assert "password:" in redacted
        assert counts == {"labeled_credential": 1}

    def test_redacts_labeled_secret_with_hash_and_ampersand(self):
        redacted, counts = redact_text("password=Sn0w!Fall#42&more")
        assert "Sn0w!Fall#42" not in redacted
        assert counts == {"labeled_credential": 1}

    def test_does_not_double_redact_another_patterns_mask(self):
        # Regression: a broadened labeled_credential value class must not
        # re-match another pattern's [REDACTED:...] mask as if it were the
        # secret itself once that pattern has already run.
        redacted, counts = redact_text("token=AKIAIOSFODNN7EXAMPLE failed")
        assert redacted == "token=[REDACTED:aws_access_key] failed"
        assert counts == {"aws_access_key": 1}

    def test_counts_multiple_distinct_secrets_in_one_string(self):
        text = "AKIAIOSFODNN7EXAMPLE and password=hunter22isnotenough"
        _, counts = redact_text(text)
        assert counts.get("aws_access_key") == 1
        assert counts.get("labeled_credential") == 1


class TestRedactValue:
    def test_redacts_strings_nested_in_dicts_and_lists(self):
        value = {
            "journal": {"previous_boot_errors": "token=AKIAIOSFODNN7EXAMPLE failed"},
            "lines": ["clean line", "password=SuperSecret123isfine"],
        }
        redacted, counts = redact_value(value)
        assert "AKIAIOSFODNN7EXAMPLE" not in redacted["journal"]["previous_boot_errors"]
        assert "SuperSecret123isfine" not in redacted["lines"][1]
        assert redacted["lines"][0] == "clean line"
        assert counts.get("aws_access_key") == 1
        assert counts.get("labeled_credential") == 1

    def test_skips_identifier_fields_like_boot_id(self):
        # An id/boot_id is an opaque identifier, not a log line - never worth
        # scanning, and skipping it avoids a false positive if an id happens
        # to start with characters that resemble a pattern.
        value = {"boot_id": "AKIAIOSFODNN7EXAMPLE-looking-but-not-a-secret"}
        redacted, counts = redact_value(value)
        assert redacted["boot_id"] == value["boot_id"]
        assert counts == {}

    def test_leaves_non_string_values_untouched(self):
        value = {"count": 5, "enabled": True, "ratio": 0.5, "nothing": None}
        redacted, counts = redact_value(value)
        assert redacted == value
        assert counts == {}


class TestRedactTelemetry:
    def test_returns_summary_with_count_and_categories(self):
        telemetry = {
            "journal": {"oom_events": "Out of memory: password=hunter22isnotenoughcredential"},
            "dmesg": {"full_tail": "AKIAIOSFODNN7EXAMPLE appeared in dmesg"},
        }
        redacted, summary = redact_telemetry(telemetry)
        assert summary["count"] == 2
        assert summary["categories"] == ["aws_access_key", "labeled_credential"]
        assert "hunter22isnotenoughcredential" not in redacted["journal"]["oom_events"]
        assert "AKIAIOSFODNN7EXAMPLE" not in redacted["dmesg"]["full_tail"]

    def test_zero_count_when_nothing_looks_like_a_secret(self):
        telemetry = {"journal": {"previous_boot_errors": "Kernel panic - not syncing"}}
        redacted, summary = redact_telemetry(telemetry)
        assert summary == {"count": 0, "categories": []}
        assert redacted == telemetry
