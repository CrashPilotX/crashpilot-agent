from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from crashpilot.analyzers import ai_analyzer


@pytest.fixture(autouse=True)
def reset_clients():
    import crashpilot.config as config_module

    config_module._settings = None
    ai_analyzer._async_client = None
    yield
    config_module._settings = None
    ai_analyzer._async_client = None


class _Messages:
    def __init__(self, content):
        self._content = content

    async def create(self, **_kwargs):
        return SimpleNamespace(
            content=self._content,
            usage=SimpleNamespace(input_tokens=12, output_tokens=34),
        )


class _Client:
    def __init__(self, content):
        self.messages = _Messages(content)


@pytest.mark.asyncio
async def test_analyze_crash_ignores_non_text_blocks(monkeypatch, tmp_path):
    monkeypatch.setenv("CRASHPILOT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("CRASHPILOT_DB_PATH", str(tmp_path / "crashpilot.db"))
    monkeypatch.setenv("CRASHPILOT_ANTHROPIC_API_KEY", "test-key")

    content = [
        SimpleNamespace(type="thinking", thinking="private reasoning"),
        SimpleNamespace(
            type="text",
            text='{"root_cause":"OOM","crash_type":"oom_kill","severity":"high","confidence":0.9}',
        ),
    ]
    monkeypatch.setattr(ai_analyzer, "_get_client", lambda _key: _Client(content))

    result = await ai_analyzer.analyze_crash(
        telemetry={},
        detection_result={"crash_type": "oom_kill", "severity": "high", "confidence": 0.8},
        timeline=[],
    )

    assert result["ai_analyzed"] is True
    assert result["root_cause"] == "OOM"
    assert result["usage"] == {"input_tokens": 12, "output_tokens": 34}


@pytest.mark.asyncio
async def test_analyze_crash_falls_back_when_response_has_no_text(monkeypatch, tmp_path):
    monkeypatch.setenv("CRASHPILOT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("CRASHPILOT_DB_PATH", str(tmp_path / "crashpilot.db"))
    monkeypatch.setenv("CRASHPILOT_ANTHROPIC_API_KEY", "test-key")

    monkeypatch.setattr(
        ai_analyzer,
        "_get_client",
        lambda _key: _Client([SimpleNamespace(type="tool_use", name="lookup")]),
    )

    result = await ai_analyzer.analyze_crash(
        telemetry={},
        detection_result={"crash_type": "unknown", "severity": "medium", "confidence": 0.2},
        timeline=[],
    )

    assert result["ai_analyzed"] is False
    assert result["ai_error"] == "Anthropic response contained no text content"


@pytest.mark.asyncio
async def test_analyze_crash_normalizes_explicit_null_alternative_hypotheses(monkeypatch, tmp_path):
    # setdefault is a no-op when the key is already present, so a model that
    # returns "alternative_hypotheses": null (valid JSON, not omitted) needs
    # its own check, or callers that assume an array would break.
    monkeypatch.setenv("CRASHPILOT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("CRASHPILOT_DB_PATH", str(tmp_path / "crashpilot.db"))
    monkeypatch.setenv("CRASHPILOT_ANTHROPIC_API_KEY", "test-key")

    content = [
        SimpleNamespace(
            type="text",
            text=(
                '{"root_cause":"OOM","crash_type":"oom_kill","severity":"high",'
                '"confidence":0.9,"alternative_hypotheses":null}'
            ),
        ),
    ]
    monkeypatch.setattr(ai_analyzer, "_get_client", lambda _key: _Client(content))

    result = await ai_analyzer.analyze_crash(
        telemetry={},
        detection_result={"crash_type": "oom_kill", "severity": "high", "confidence": 0.8},
        timeline=[],
    )

    assert result["alternative_hypotheses"] == []


class TestExtractJson:
    def test_returns_bare_json_unchanged(self):
        text = '{"root_cause": "oom"}'
        assert ai_analyzer._extract_json(text) == text

    def test_extracts_deeply_nested_json_from_a_fenced_block(self):
        # Regression: a non-greedy brace match used to stop at the first
        # nested closing brace, truncating exactly this kind of response
        # (evidence/remediation/timeline are lists of dicts per the real
        # schema) into invalid JSON that silently discarded the whole
        # analysis.
        text = (
            "Here is my analysis:\n"
            "```json\n"
            '{"root_cause": "oom", "evidence": [{"source": "dmesg", "excerpt": "x"}], '
            '"remediation": [{"priority": 1, "action": "y"}]}\n'
            "```\n"
            "Let me know if you need more detail."
        )
        result = ai_analyzer._extract_json(text)
        parsed = json.loads(result)
        assert parsed["evidence"] == [{"source": "dmesg", "excerpt": "x"}]
        assert parsed["remediation"] == [{"priority": 1, "action": "y"}]

    def test_extracts_json_from_fence_without_language_tag(self):
        text = '```\n{"a": {"b": 1}}\n```'
        assert json.loads(ai_analyzer._extract_json(text)) == {"a": {"b": 1}}

    def test_falls_back_to_first_and_last_brace_without_a_fence(self):
        text = 'Sure, here is the JSON: {"a": {"b": 1}} - hope that helps.'
        assert json.loads(ai_analyzer._extract_json(text)) == {"a": {"b": 1}}


class TestTruncateTelemetry:
    def test_leaf_strings_are_bounded_by_max_chars_not_a_hardcoded_constant(self):
        # Regression: the per-field truncation threshold used to be a
        # hardcoded 8000 regardless of max_chars, so a small max_chars budget
        # had no effect on the actual output size.
        telemetry = {"dmesg": "x" * 5000}
        result = ai_analyzer._truncate_telemetry(telemetry, max_chars=2000)
        assert len(result["dmesg"]) == 2000
        assert result["dmesg"] == ("x" * 5000)[-2000:]

    def test_short_strings_are_left_untouched(self):
        telemetry = {"dmesg": "short line"}
        result = ai_analyzer._truncate_telemetry(telemetry, max_chars=2000)
        assert result["dmesg"] == "short line"

    def test_empty_telemetry_returns_empty_dict(self):
        assert ai_analyzer._truncate_telemetry({}, max_chars=2000) == {}


class TestNoApiKeyResultEvidence:
    def test_evidence_carries_real_source_and_extracted_metadata(self):
        detection = {
            "crash_type": "oom_kill",
            "severity": "high",
            "confidence": 0.7,
            "evidence": ["Out of memory: Kill process 38291 (torch_train) score 923"],
            "evidence_sources": ["journal"],
            "alternatives": [],
        }
        result = ai_analyzer._no_api_key_result(detection)
        assert result["evidence"] == [{
            "source": "journal",
            "excerpt": "Out of memory: Kill process 38291 (torch_train) score 923",
            "interpretation": "",
            "weight": 0.5,
            "timestamp": None,
            "process": "torch_train",
            "pid": 38291,
        }]

    def test_evidence_source_falls_back_to_system_when_missing(self):
        detection = {
            "crash_type": "unknown",
            "confidence": 0.3,
            "evidence": ["No clear crash signature detected"],
            "evidence_sources": [],
            "alternatives": [],
        }
        result = ai_analyzer._no_api_key_result(detection)
        assert result["evidence"][0]["source"] == "system"


class TestNoApiKeyResultAlternatives:
    def test_no_alternatives_when_confidence_is_high(self):
        detection = {
            "crash_type": "oom_kill",
            "confidence": 0.9,
            "evidence": [],
            "evidence_sources": [],
            "alternatives": [{"crash_type": "disk_error", "confidence": 0.4, "evidence": ["x"]}],
        }
        result = ai_analyzer._no_api_key_result(detection)
        assert result["alternative_hypotheses"] == []

    def test_surfaces_real_alternatives_when_confidence_is_not_high(self):
        detection = {
            "crash_type": "soft_lockup",
            "confidence": 0.7,
            "evidence": [],
            "evidence_sources": [],
            "alternatives": [
                {"crash_type": "watchdog_reset", "confidence": 0.6, "evidence": ["watchdog: BUG"]},
            ],
        }
        result = ai_analyzer._no_api_key_result(detection)
        assert len(result["alternative_hypotheses"]) == 1
        alt = result["alternative_hypotheses"][0]
        assert "watchdog reset" in alt["hypothesis"]
        assert alt["confidence"] == 0.6
        assert "watchdog: BUG" in alt["why_less_likely"]
