"""Tests for the FastAPI server endpoints."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    """Redirect DB to a temp file and reset settings singleton."""
    monkeypatch.setenv("CRASHPILOT_DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("CRASHPILOT_DATA_DIR", str(tmp_path))
    import crashpilot.config as cfg_mod
    cfg_mod._settings = None
    yield
    cfg_mod._settings = None


@pytest.fixture()
def client(tmp_db):
    from crashpilot.api.server import app
    from crashpilot.storage.store import init_db
    init_db()
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


@pytest.fixture()
def saved_report(tmp_db):
    """Insert a report and return its ID."""
    from crashpilot.storage.store import init_db, save_report
    init_db()
    report = {
        "id": "crash_testapi001",
        "boot_id": "boot_abc",
        "detected_at": datetime.now(timezone.utc).isoformat(),
        "crash_time": None,
        "crash_type": "oom_kill",
        "severity": "high",
        "summary": "OOM killed python3",
        "telemetry": {
            "system": {"memory": {"total_gb": 16.0}},
            "platform": {"type": "bare_metal"},
            "journal": {"boots": [], "oom_events": "Out of memory: Killed process 1234"},
            "dmesg": {"critical_events": [], "critical_count": 1},
            "smart": {},
            "gpu": {},
            "thermal": {},
            "docker": {},
        },
        "analysis": {
            "ai_analyzed": False,
            "heuristic": {"confidence": 0.7, "crash_type": "oom_kill"},
            "timeline": [],
        },
    }
    save_report(report)
    return report["id"]


class TestHealth:
    def test_health_ok(self, client):
        r = client.get("/health")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"


class TestListReports:
    def test_empty_list(self, client):
        r = client.get("/api/v1/reports")
        assert r.status_code == 200
        data = r.json()
        assert data["reports"] == []
        assert data["total"] == 0

    def test_lists_saved_reports(self, client, saved_report):
        r = client.get("/api/v1/reports")
        assert r.status_code == 200
        data = r.json()
        assert data["total"] == 1
        assert data["reports"][0]["id"] == saved_report

    def test_limit_param_validated(self, client):
        # limit=0 is below ge=1
        r = client.get("/api/v1/reports?limit=0")
        assert r.status_code == 422

    def test_limit_max_capped(self, client):
        # limit=9999 is above le=100 — FastAPI rejects it
        r = client.get("/api/v1/reports?limit=9999")
        assert r.status_code == 422

    def test_report_summary_fields(self, client, saved_report):
        r = client.get("/api/v1/reports")
        report = r.json()["reports"][0]
        for field in ("id", "boot_id", "detected_at", "crash_type", "severity",
                      "ai_analyzed", "confidence"):
            assert field in report


class TestGetReport:
    def test_get_existing(self, client, saved_report):
        r = client.get(f"/api/v1/reports/{saved_report}")
        assert r.status_code == 200
        data = r.json()
        assert data["id"] == saved_report
        assert data["crash_type"] == "oom_kill"
        assert "analysis" in data
        assert "telemetry_summary" in data

    def test_get_nonexistent_404(self, client):
        r = client.get("/api/v1/reports/does_not_exist")
        assert r.status_code == 404

    def test_raw_log_blobs_stripped(self, client, saved_report):
        """Raw logs should not be in the API response (too large)."""
        r = client.get(f"/api/v1/reports/{saved_report}")
        tel = r.json()["telemetry_summary"]
        # system.top_processes_at_crash stripped
        assert "top_processes_at_crash" not in tel.get("system", {})


class TestDeleteReport:
    def test_delete_existing(self, client, saved_report):
        r = client.delete(f"/api/v1/reports/{saved_report}")
        assert r.status_code == 204
        # Confirm gone
        r2 = client.get(f"/api/v1/reports/{saved_report}")
        assert r2.status_code == 404

    def test_delete_nonexistent_404(self, client):
        r = client.delete("/api/v1/reports/does_not_exist")
        assert r.status_code == 404


class TestStatus:
    def test_status_fields(self, client):
        r = client.get("/api/v1/status")
        assert r.status_code == 200
        data = r.json()
        assert "agent_version" in data
        assert "reports_count" in data
        assert "api_key_configured" in data
        assert "claude_model" in data
        assert "data_dir" in data

    def test_reports_count_accurate(self, client, saved_report):
        r = client.get("/api/v1/status")
        assert r.json()["reports_count"] == 1

    def test_api_key_not_configured(self, client, monkeypatch):
        monkeypatch.setenv("CRASHPILOT_ANTHROPIC_API_KEY", "")
        import crashpilot.config as cfg_mod
        cfg_mod._settings = None
        r = client.get("/api/v1/status")
        assert r.json()["api_key_configured"] is False
