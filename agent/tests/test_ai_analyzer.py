from __future__ import annotations

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
