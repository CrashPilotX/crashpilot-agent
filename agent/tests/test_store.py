"""Tests for SQLite storage layer."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

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


from crashpilot.storage.store import (  # noqa: E402
    MAX_PUSH_REJECTIONS,
    cleanup_old_reports,
    count_reports,
    count_set_aside,
    count_unpushed,
    delete_report,
    get_meta,
    get_report,
    init_db,
    list_flight_snapshots,
    list_reports,
    list_unpushed,
    mark_push_rejected,
    mark_pushed,
    save_flight_snapshot,
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
        con = sqlite3.connect(str(get_settings().db_path))
        tables = {row[0] for row in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        assert "crash_reports" in tables
        assert "meta" in tables
        assert "flight_snapshots" in tables
        assert "webhook_deliveries" in tables
        con.close()

    def test_an_older_database_gains_the_new_columns(self, tmp_path, monkeypatch):
        old = tmp_path / "old.db"
        con = sqlite3.connect(str(old))
        con.execute(
            "CREATE TABLE crash_reports (id TEXT PRIMARY KEY, boot_id TEXT NOT NULL, "
            "detected_at TEXT NOT NULL, crash_time TEXT, crash_type TEXT NOT NULL, "
            "severity TEXT NOT NULL DEFAULT 'unknown', summary TEXT, telemetry TEXT NOT NULL, "
            "analysis TEXT, created_at TEXT NOT NULL DEFAULT '')"
        )
        con.execute(
            "INSERT INTO crash_reports (id, boot_id, detected_at, crash_type, telemetry) "
            "VALUES ('crash_old', 'b', '2026-01-01T00:00:00Z', 'oom_kill', '{}')"
        )
        con.commit()
        con.close()
        monkeypatch.setenv("CRASHPILOT_DB_PATH", str(old))
        import crashpilot.config as cfg_mod
        cfg_mod._settings = None

        init_db()
        mark_push_rejected("crash_old")

        assert [r["id"] for r in list_unpushed()] == ["crash_old"]


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


class TestFlightSnapshots:
    def test_snapshot_round_trip(self):
        captured_at = datetime.now(timezone.utc).isoformat()
        save_flight_snapshot({"captured_at": captured_at, "memory": {"used_pct": 42}})
        snapshots = list_flight_snapshots(hours=1)
        assert snapshots == [{"captured_at": captured_at, "memory": {"used_pct": 42}}]

    def test_snapshot_requires_timestamp(self):
        with pytest.raises(ValueError, match="captured_at"):
            save_flight_snapshot({"memory": {"used_pct": 42}})

    def test_a_full_window_returns_the_newest_samples_oldest_first(self):
        # Six hours of one-minute snapshots is 360 rows. Taking the first 240
        # of them left the heartbeat's "latest" two hours stale. Same shape,
        # scaled down: 36 ten-minute samples, 24 wanted.
        now = datetime.now(timezone.utc)
        for tens_ago in range(35, -1, -1):
            save_flight_snapshot({
                "captured_at": (now - timedelta(minutes=10 * tens_ago, seconds=5)).isoformat(),
                "n": tens_ago,
            })

        snapshots = list_flight_snapshots(hours=6, limit=24)

        assert len(snapshots) == 24
        assert [s["n"] for s in snapshots] == list(range(23, -1, -1))

    def test_snapshots_from_a_clock_that_ran_ahead_are_not_the_latest(self):
        now = datetime.now(timezone.utc)
        save_flight_snapshot({"captured_at": (now + timedelta(hours=3)).isoformat(), "n": "future"})
        save_flight_snapshot({"captured_at": (now - timedelta(minutes=1)).isoformat(), "n": "now"})

        assert [s["n"] for s in list_flight_snapshots(hours=1)] == ["now"]


class TestRejectedReports:
    def test_a_report_rejected_repeatedly_is_set_aside(self):
        save_report(_make_report("crash_bad", detected_at="2026-01-01T00:00:00+00:00"))
        for _ in range(MAX_PUSH_REJECTIONS - 1):
            mark_push_rejected("crash_bad")
        assert [r["id"] for r in list_unpushed()] == ["crash_bad"]

        mark_push_rejected("crash_bad")

        assert list_unpushed() == []
        assert count_unpushed() == 0
        assert count_set_aside() == 1
        # Still stored locally.
        assert get_report("crash_bad") is not None

    def test_set_aside_reports_no_longer_hold_up_newer_ones(self):
        save_report(_make_report("crash_bad", detected_at="2026-01-01T00:00:00+00:00"))
        save_report(_make_report("crash_new", detected_at="2026-01-02T00:00:00+00:00"))
        assert [r["id"] for r in list_unpushed(limit=1)] == ["crash_bad"]

        for _ in range(MAX_PUSH_REJECTIONS):
            mark_push_rejected("crash_bad")

        assert [r["id"] for r in list_unpushed(limit=1)] == ["crash_new"]

    def test_analyzing_again_gives_a_report_a_fresh_start(self):
        save_report(_make_report("crash_bad"))
        for _ in range(MAX_PUSH_REJECTIONS):
            mark_push_rejected("crash_bad")
        save_report(_make_report("crash_bad"))  # analyze --force
        assert count_unpushed() == 1
