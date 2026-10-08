"""The composite action: valid shape, and its summary script works end to end."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from crashpilot.main import app

yaml = pytest.importorskip("yaml")

ACTION = Path(__file__).resolve().parents[2] / "action.yml"
XID_79 = (
    "[ 1.0] NVRM: Xid (PCI:0000:01:00): 79, pid=0, GPU has fallen off the bus.\n"
    "[ 1.1] NVRM: GPU 0000:01:00.0: GPU has fallen off the bus.\n"
)


def _action() -> dict:
    return yaml.safe_load(ACTION.read_text())


def _summary_script() -> str:
    run = next(s["run"] for s in _action()["runs"]["steps"] if s.get("id") == "classify")
    start = run.index("<<'PY'\n") + len("<<'PY'\n")
    return run[start : run.index("\nPY", start)]


def test_action_shape():
    action = _action()
    assert action["runs"]["using"] == "composite"
    assert {"file", "log", "fail-on-severity"} <= set(action["inputs"])
    assert {"category", "severity", "confidence"} <= set(action["outputs"])
    for step in action["runs"]["steps"]:
        if "run" in step:
            assert step["shell"] == "bash"
    # Untrusted input must never be interpolated into a script.
    for step in action["runs"]["steps"]:
        assert "inputs." not in step.get("run", "")


def _run_script(tmp_path, fail_on=""):
    from typer.testing import CliRunner

    result = CliRunner().invoke(app, ["classify", "--json"], input=XID_79)
    data = tmp_path / "result.json"
    data.write_text(result.output)
    env = {
        **os.environ,
        "GITHUB_OUTPUT": str(tmp_path / "out"),
        "GITHUB_STEP_SUMMARY": str(tmp_path / "summary.md"),
        "CP_FAIL_ON": fail_on,
    }
    proc = subprocess.run(
        [sys.executable, "-", str(data)],
        input=_summary_script(), env=env, capture_output=True, text=True,
    )
    return proc, tmp_path


def test_summary_and_outputs(tmp_path):
    proc, _ = _run_script(tmp_path)
    assert proc.returncode == 0, proc.stderr
    out = (tmp_path / "out").read_text()
    assert "category=gpu_fault" in out and "severity=high" in out
    summary = (tmp_path / "summary.md").read_text()
    assert "gpu_fault" in summary and "HIGH" in summary and "70%" in summary


def test_fail_on_severity(tmp_path):
    (tmp_path / "a").mkdir()
    assert _run_script(tmp_path / "a", "high")[0].returncode == 1
    (tmp_path / "b").mkdir()
    assert _run_script(tmp_path / "b", "critical")[0].returncode == 0
