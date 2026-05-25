"""Tests for the FastAPI server endpoints."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    """Redirect DB and data dir to a temp location; reset settings singleton."""
    monkeypatch.setenv("CRASHPILOT_DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("CRASHPILOT_DATA_DIR", str(tmp_path))
    import crashpilot.config as cfg_mod
    cfg_mod._settings = None
    yield
    cfg_mod._settings = None


@pytest.fixture()
def client(tmp_db):
    """Authenticated TestClient — all requests carry the agent Bearer token."""
    from crashpilot.api.server import app, get_agent_token
    from crashpilot.storage.store import init_db
    init_db()
    token = get_agent_token()
    with TestClient(
        app,
        raise_server_exceptions=True,
        headers={"Authorization": f"Bearer {token}"},
    ) as c:
        yield c


@pytest.fixture()
def unauthed_client(tmp_db):
    """Unauthenticated TestClient — no auth headers sent."""
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


@pytest.fixture()
def multi_reports(tmp_db):
    """Insert several reports with different severities and types."""
    from crashpilot.storage.store import init_db, save_report
    init_db()
    ids = []
    specs = [
        ("crash_001", "kernel_panic", "critical"),
        ("crash_002", "oom_kill", "high"),
        ("crash_003", "thermal_shutdown", "medium"),
        ("crash_004", "power_loss", "low"),
        ("crash_005", "disk_io_error", "high"),
    ]
    for rid, ctype, severity in specs:
        report = {
            "id": rid,
            "boot_id": f"boot_{rid}",
            "detected_at": datetime.now(timezone.utc).isoformat(),
            "crash_time": None,
            "crash_type": ctype,
            "severity": severity,
            "summary": f"Test {ctype}",
            "telemetry": {
                "system": {}, "platform": {}, "journal": {},
                "dmesg": {}, "smart": {}, "gpu": {}, "thermal": {}, "docker": {},
            },
            "analysis": {
                "ai_analyzed": False,
                "heuristic": {"confidence": 0.5, "crash_type": ctype},
                "timeline": [],
            },
        }
        save_report(report)
        ids.append(rid)
    return ids


# ---------------------------------------------------------------------------
# Auth tests
# ---------------------------------------------------------------------------

class TestAuth:
    def test_health_requires_no_auth(self, unauthed_client):
        """Public /health endpoint must be accessible without a token."""
        r = unauthed_client.get("/health")
        assert r.status_code == 200

    def test_protected_route_requires_token(self, unauthed_client):
        """/api/v1/* routes must return 401 when no token is provided."""
        r = unauthed_client.get("/api/v1/reports")
        assert r.status_code == 401

    def test_wrong_token_rejected(self, tmp_db):
        """A Bearer token that doesn't match the stored token must be rejected."""
        from crashpilot.api.server import app
        from crashpilot.storage.store import init_db
        init_db()
        with TestClient(
            app,
            headers={"Authorization": "Bearer definitely_wrong_token"},
        ) as c:
            r = c.get("/api/v1/reports")
            assert r.status_code == 401

    def test_401_error_message(self, unauthed_client):
        """The 401 response body should include guidance."""
        r = unauthed_client.get("/api/v1/status")
        assert r.status_code == 401
        body = r.json()
        assert "detail" in body

    def test_token_in_bearer_scheme(self, tmp_db):
        """Non-Bearer auth schemes (e.g. Basic) should also be rejected."""
        from crashpilot.api.server import app, get_agent_token
        from crashpilot.storage.store import init_db
        init_db()
        token = get_agent_token()
        with TestClient(
            app,
            headers={"Authorization": f"Basic {token}"},
        ) as c:
            r = c.get("/api/v1/reports")
            assert r.status_code == 401

    def test_delete_requires_auth(self, unauthed_client, saved_report):
        """DELETE also requires a valid token."""
        r = unauthed_client.delete(f"/api/v1/reports/{saved_report}")
        assert r.status_code == 401


# ---------------------------------------------------------------------------
# /health
# ---------------------------------------------------------------------------

class TestHealth:
    def test_health_ok(self, client):
        r = client.get("/health")
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "ok"
        assert "version" in data


# ---------------------------------------------------------------------------
# GET /api/v1/reports
# ---------------------------------------------------------------------------

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

    def test_limit_param_too_low(self, client):
        r = client.get("/api/v1/reports?limit=0")
        assert r.status_code == 422

    def test_limit_param_too_high(self, client):
        r = client.get("/api/v1/reports?limit=9999")
        assert r.status_code == 422

    def test_limit_param_valid(self, client, multi_reports):
        r = client.get("/api/v1/reports?limit=3")
        assert r.status_code == 200
        assert len(r.json()["reports"]) <= 3

    def test_report_summary_fields(self, client, saved_report):
        r = client.get("/api/v1/reports")
        report = r.json()["reports"][0]
        for field in (
            "id", "boot_id", "detected_at", "crash_type", "severity",
            "ai_analyzed", "confidence",
        ):
            assert field in report, f"Missing field: {field}"

    def test_total_matches_count(self, client, multi_reports):
        r = client.get("/api/v1/reports?limit=100")
        data = r.json()
        assert data["total"] == len(data["reports"])

    def test_no_raw_telemetry_in_list(self, client, saved_report):
        """List endpoint must not expose telemetry — only summary fields."""
        report = client.get("/api/v1/reports").json()["reports"][0]
        assert "telemetry" not in report
        assert "telemetry_summary" not in report


# ---------------------------------------------------------------------------
# GET /api/v1/reports/{id}
# ---------------------------------------------------------------------------

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
        """top_processes_at_crash is too large and should be stripped."""
        r = client.get(f"/api/v1/reports/{saved_report}")
        tel = r.json()["telemetry_summary"]
        assert "top_processes_at_crash" not in tel.get("system", {})

    def test_full_report_fields(self, client, saved_report):
        """Verify all top-level fields are present on a full report."""
        data = client.get(f"/api/v1/reports/{saved_report}").json()
        for field in ("id", "boot_id", "detected_at", "crash_type", "severity",
                      "analysis", "telemetry_summary"):
            assert field in data, f"Missing field: {field}"

    def test_telemetry_summary_structure(self, client, saved_report):
        """Telemetry summary should include expected sub-keys."""
        tel = client.get(f"/api/v1/reports/{saved_report}").json()["telemetry_summary"]
        for key in ("system", "journal_summary", "dmesg_summary"):
            assert key in tel, f"Missing telemetry key: {key}"

    def test_oom_events_snippet_truncated(self, client, saved_report):
        """Journal OOM snippet should be at most 2000 chars."""
        tel = client.get(f"/api/v1/reports/{saved_report}").json()["telemetry_summary"]
        snippet = tel.get("journal_summary", {}).get("oom_events_snippet", "")
        assert len(snippet) <= 2000


# ---------------------------------------------------------------------------
# DELETE /api/v1/reports/{id}
# ---------------------------------------------------------------------------

class TestDeleteReport:
    def test_delete_existing(self, client, saved_report):
        r = client.delete(f"/api/v1/reports/{saved_report}")
        assert r.status_code == 204
        r2 = client.get(f"/api/v1/reports/{saved_report}")
        assert r2.status_code == 404

    def test_delete_nonexistent_404(self, client):
        r = client.delete("/api/v1/reports/does_not_exist")
        assert r.status_code == 404

    def test_delete_idempotent(self, client, saved_report):
        """Deleting twice should give 204 then 404."""
        assert client.delete(f"/api/v1/reports/{saved_report}").status_code == 204
        assert client.delete(f"/api/v1/reports/{saved_report}").status_code == 404

    def test_delete_removes_from_list(self, client, multi_reports):
        """After deletion the report must not appear in the list."""
        target = multi_reports[0]
        client.delete(f"/api/v1/reports/{target}")
        ids = [r["id"] for r in client.get("/api/v1/reports").json()["reports"]]
        assert target not in ids


# ---------------------------------------------------------------------------
# GET /api/v1/status
# ---------------------------------------------------------------------------

class TestStatus:
    def test_status_fields(self, client):
        r = client.get("/api/v1/status")
        assert r.status_code == 200
        data = r.json()
        for field in (
            "agent_version", "reports_count", "api_key_configured",
            "claude_model", "data_dir",
        ):
            assert field in data, f"Missing field: {field}"

    def test_reports_count_zero(self, client):
        assert client.get("/api/v1/status").json()["reports_count"] == 0

    def test_reports_count_accurate(self, client, saved_report):
        assert client.get("/api/v1/status").json()["reports_count"] == 1

    def test_reports_count_multi(self, client, multi_reports):
        assert client.get("/api/v1/status").json()["reports_count"] == len(multi_reports)

    def test_api_key_not_configured(self, client, monkeypatch):
        monkeypatch.setenv("CRASHPILOT_ANTHROPIC_API_KEY", "")
        import crashpilot.config as cfg_mod
        cfg_mod._settings = None
        r = client.get("/api/v1/status")
        assert r.json()["api_key_configured"] is False

    def test_api_key_configured(self, client, monkeypatch):
        monkeypatch.setenv("CRASHPILOT_ANTHROPIC_API_KEY", "sk-ant-fake-key")
        import crashpilot.config as cfg_mod
        cfg_mod._settings = None
        r = client.get("/api/v1/status")
        assert r.json()["api_key_configured"] is True

    def test_recent_crashes_in_status(self, client, multi_reports):
        data = client.get("/api/v1/status").json()
        assert "recent_crashes" in data
        assert isinstance(data["recent_crashes"], list)
        for crash in data["recent_crashes"]:
            for field in ("id", "crash_type", "detected_at", "severity"):
                assert field in crash, f"Missing crash field: {field}"
