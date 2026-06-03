"""Tests for SQLite storage layer."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

# We need to point settings at a temp DB before importing store
@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    """Redirect all DB operations to a temporary file."""
    db_file = tmp_path / "test.db"
    monkeypatch.setenv("CRASHPILOT_DB_PATH", str(db_file))
    monkeypatch.setenv("CRASHPILOT_DATA_DIR", str(tmp_path))
    # Reset the settings singleton so the env vars are picked up
    import crashpilot.config as cfg_mod
    cfg_mod._settings = None
    yield db_file
    cfg_mod._settings = None


from crashpilot.storage.store import (
    cleanup_old_reports,
    count_reports,
    count_unpushed,
    delete_report,
    get_meta,
    get_report,
    init_db,
    list_reports,
    list_unpushed,
    mark_pushed,
    save_report,
    set_meta,
    update_analysis,
)


def _make_report(report_id: str = "crash_test001", crash_type: str = "oom_kill",
                 detected_at: str | None = None) -> dict:
    return {
        "id": report_id,
        "boot_id": f"boot_{report_id}",
        "detected_at": detected_at or datetime.now(timezone.utc).isoformat(),
        "crash_time": None,
        "crash_type": crash_type,
        "severity": "high",
        "summary": f"Test report {report_id}",
        "telemetry": {"system": {}, "platform": {"type": "bare_metal"}},
        "analysis": {"ai_analyzed": False, "heuristic": {"confidence": 0.5}},
    }


@pytest.fixture(autouse=True)
def fresh_db(tmp_db):
    init_db()


class TestInitDb:
    def test_init_is_idempotent(self):
        """Calling init_db twice should not raise."""
        init_db()
        init_db()

    def test_tables_exist_after_init(self):
        from crashpilot.config import get_settings
        import sqlite3
        con = sqlite3.connect(str(get_settings().db_path))
        tables = {row[0] for row in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        assert "crash_reports" in tables
        assert "meta" in tables
        con.close()


class TestBackfill:
    def test_new_report_is_unpushed(self):
        save_report(_make_report("crash_bf1"))
        assert count_unpushed() == 1
        ids = [r["id"] for r in list_unpushed()]
        assert "crash_bf1" in ids

    def test_mark_pushed_removes_from_queue(self):
        save_report(_make_report("crash_bf2"))
        mark_pushed("crash_bf2")
        assert count_unpushed() == 0
        assert list_unpushed() == []

    def test_list_unpushed_decodes_json(self):
        save_report(_make_report("crash_bf3"))
        rep = list_unpushed()[0]
        # telemetry/analysis come back as dicts, ready for push_report()
        assert isinstance(rep["telemetry"], dict)
        assert isinstance(rep["analysis"], dict)

    def test_resave_resets_pushed_flag(self):
        """Re-analyzing a boot (INSERT OR REPLACE) should re-queue it for push."""
        save_report(_make_report("crash_bf4"))
        mark_pushed("crash_bf4")
        assert count_unpushed() == 0
        save_report(_make_report("crash_bf4"))  # re-save (e.g. analyze --force)
        assert count_unpushed() == 1

    def test_update_analysis_preserves_pushed(self):
        """Adding AI analysis to an already-pushed report must not re-queue it."""
        save_report(_make_report("crash_bf5"))
        mark_pushed("crash_bf5")
        update_analysis("crash_bf5", {"ai_analyzed": True, "severity": "critical"})
        assert count_unpushed() == 0


class TestSaveAndGet:
    def test_save_and_retrieve(self):
        report = _make_report()
        save_report(report)
        fetched = get_report(report["id"])
        assert fetched is not None
        assert fetched["id"] == report["id"]
        assert fetched["crash_type"] == "oom_kill"
        assert fetched["severity"] == "high"

    def test_telemetry_round_trips(self):
        report = _make_report()
        report["telemetry"]["system"]["memory"] = {"total_gb": 32.0}
        save_report(report)
        fetched = get_report(report["id"])
        assert fetched["telemetry"]["system"]["memory"]["total_gb"] == 32.0

    def test_analysis_round_trips(self):
        report = _make_report()
        report["analysis"] = {"root_cause": "OOM killed python3", "confidence": 0.9}
        save_report(report)
        fetched = get_report(report["id"])
        assert fetched["analysis"]["root_cause"] == "OOM killed python3"

    def test_get_nonexistent_returns_none(self):
        assert get_report("nonexistent_id") is None

    def test_upsert_replaces(self):
        report = _make_report()
        save_report(report)
        report["severity"] = "critical"
        save_report(report)
        fetched = get_report(report["id"])
        assert fetched["severity"] == "critical"


class TestListReports:
    def test_empty_db_returns_empty_list(self):
        assert list_reports() == []

    def test_lists_all_saved_reports(self):
        for i in range(3):
            save_report(_make_report(f"crash_{i:03d}"))
        reports = list_reports()
        assert len(reports) == 3

    def test_limit_respected(self):
        for i in range(10):
            save_report(_make_report(f"crash_{i:03d}"))
        reports = list_reports(limit=5)
        assert len(reports) == 5

    def test_ordered_by_recency(self):
        old = _make_report("crash_old", detected_at="2024-01-01T00:00:00+00:00")
        new = _make_report("crash_new", detected_at="2024-06-01T00:00:00+00:00")
        save_report(old)
        save_report(new)
        reports = list_reports()
        assert reports[0]["id"] == "crash_new"


class TestUpdateAnalysis:
    def test_updates_analysis_and_severity(self):
        save_report(_make_report())
        new_analysis = {
            "root_cause": "kernel panic",
            "severity": "critical",
            "summary": "AI found kernel panic",
        }
        update_analysis("crash_test001", new_analysis)
        fetched = get_report("crash_test001")
        assert fetched["severity"] == "critical"
        assert fetched["analysis"]["root_cause"] == "kernel panic"


class TestDeleteReport:
    def test_delete_existing_returns_true(self):
        save_report(_make_report())
        assert delete_report("crash_test001") is True
        assert get_report("crash_test001") is None

    def test_delete_nonexistent_returns_false(self):
        assert delete_report("does_not_exist") is False

    def test_delete_reduces_count(self):
        save_report(_make_report("crash_a"))
        save_report(_make_report("crash_b"))
        assert count_reports() == 2
        delete_report("crash_a")
        assert count_reports() == 1


class TestCountReports:
    def test_count_zero_initially(self):
        assert count_reports() == 0

    def test_count_matches_saved(self):
        for i in range(7):
            save_report(_make_report(f"crash_{i}"))
        assert count_reports() == 7


class TestCleanupOldReports:
    def test_cleanup_removes_old_reports(self):
        old_date = (datetime.now(timezone.utc) - timedelta(days=100)).isoformat()
        save_report(_make_report("crash_old", detected_at=old_date))
        save_report(_make_report("crash_new"))  # today

        removed = cleanup_old_reports(max_age_days=90)
        assert removed == 1
        assert get_report("crash_old") is None
        assert get_report("crash_new") is not None

    def test_cleanup_zero_days_removes_everything(self):
        save_report(_make_report("crash_a"))
        save_report(_make_report("crash_b"))
        removed = cleanup_old_reports(max_age_days=0)
        assert removed == 2

    def test_cleanup_future_days_removes_nothing(self):
        save_report(_make_report())
        removed = cleanup_old_reports(max_age_days=9999)
        assert removed == 0


class TestMeta:
    def test_set_and_get(self):
        set_meta("last_boot", "abc123")
        assert get_meta("last_boot") == "abc123"

    def test_get_missing_returns_default(self):
        assert get_meta("nonexistent") == ""
        assert get_meta("nonexistent", "fallback") == "fallback"

    def test_overwrite_meta(self):
        set_meta("key", "v1")
        set_meta("key", "v2")
        assert get_meta("key") == "v2"
