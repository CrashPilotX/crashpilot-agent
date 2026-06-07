"""Tests for the keyless (heuristic) remediation knowledge base."""

from __future__ import annotations

from crashpilot.analyzers.crash_detector import CrashType
from crashpilot.analyzers.heuristic_advice import advice_for


class TestAdviceFor:
    def test_known_type_has_remediation(self):
        advice = advice_for("oom_kill")
        assert advice["root_cause"]
        assert len(advice["remediation"]) >= 1
        assert advice["monitoring_suggestions"]

    def test_unknown_type_falls_back_to_default(self):
        advice = advice_for("something_we_dont_know")
        # Default still gives actionable guidance, not an empty stub.
        assert advice["root_cause"]
        assert len(advice["remediation"]) >= 1

    def test_every_crash_type_resolves(self):
        """Every CrashType the detector can emit must map to usable advice."""
        for ct in CrashType:
            advice = advice_for(ct.value)
            assert "root_cause" in advice
            assert "remediation" in advice
            assert "monitoring_suggestions" in advice
            # remediation entries are well-formed
            for step in advice["remediation"]:
                assert {"priority", "action", "rationale"} <= step.keys()

    def test_clean_shutdown_has_no_remediation(self):
        advice = advice_for("clean_shutdown")
        assert advice["remediation"] == []


class TestNoKeyResult:
    def test_keyless_result_is_actionable(self):
        from crashpilot.analyzers.ai_analyzer import _no_api_key_result

        detection = {
            "crash_type": "oom_kill",
            "severity": "high",
            "confidence": 0.7,
            "evidence": ["Out of memory: Killed process 1234 (python3)"],
        }
        result = _no_api_key_result(detection)
        assert result["ai_analyzed"] is False
        assert result["crash_type"] == "oom_kill"
        assert len(result["remediation"]) >= 1          # the key win: real steps, not empty
        assert result["monitoring_suggestions"]
        assert "memory" in result["root_cause"].lower()
        assert result["evidence"]                         # heuristic evidence preserved
