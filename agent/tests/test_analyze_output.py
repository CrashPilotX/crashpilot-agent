"""What `crashpilot analyze` prints: the evidence table must show the evidence."""

from __future__ import annotations

from typer.testing import CliRunner

from crashpilot.main import app


def _report(evidence: list[dict]) -> dict:
    return {
        "id": "crash_test",
        "crash_type": "power_loss",
        "severity": "high",
        "crash_time": "2026-10-22T14:12:39Z",
        "analysis": {"root_cause": "The system stopped suddenly.", "confidence": 0.8, "evidence": evidence, "remediation": []},
    }


def _run(monkeypatch, evidence: list[dict]) -> str:
    async def fake_check_and_analyze(force: bool = False) -> dict:
        return _report(evidence)

    monkeypatch.setattr("crashpilot.monitor.check_and_analyze", fake_check_and_analyze)
    result = CliRunner().invoke(app, ["analyze"])
    assert result.exit_code == 0, result.output
    return result.output


def test_a_heuristic_finding_is_shown_not_a_blank_cell(monkeypatch):
    # Heuristic evidence has an empty "interpretation" next to the real text in "excerpt". The table
    # asked for the interpretation with a fallback to the excerpt, and a key that exists but is empty
    # is not "missing", so every heuristic report printed a table of blank findings.
    output = _run(monkeypatch, [{"source": "journal", "excerpt": "gpu-burn started", "interpretation": "", "weight": 0.5}])
    assert "gpu-burn started" in output


def test_an_interpretation_is_preferred_when_there_is_one(monkeypatch):
    output = _run(monkeypatch, [{"source": "journal", "excerpt": "raw log line", "interpretation": "the GPU was reset", "weight": 0.9}])
    assert "the GPU was reset" in output and "raw log line" not in output


def test_evidence_with_neither_does_not_break_the_report(monkeypatch):
    assert "Key Evidence" in _run(monkeypatch, [{"source": "system", "weight": 0.5}])
