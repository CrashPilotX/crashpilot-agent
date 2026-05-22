"""SQLite-backed crash report storage."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Generator

from ..config import get_settings


SCHEMA = """
CREATE TABLE IF NOT EXISTS crash_reports (
    id          TEXT PRIMARY KEY,
    boot_id     TEXT NOT NULL,
    detected_at TEXT NOT NULL,
    crash_time  TEXT,
    crash_type  TEXT NOT NULL,
    severity    TEXT NOT NULL DEFAULT 'unknown',
    summary     TEXT,
    telemetry   TEXT NOT NULL,   -- JSON blob
    analysis    TEXT,            -- JSON blob, NULL until analyzed
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_crash_boot ON crash_reports(boot_id);
CREATE INDEX IF NOT EXISTS idx_crash_time ON crash_reports(crash_time DESC);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


@contextmanager
def _conn() -> Generator[sqlite3.Connection, None, None]:
    cfg = get_settings()
    # str() required for Python 3.10 compatibility (Path accepted natively in 3.11+)
    con = sqlite3.connect(str(cfg.db_path), timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def init_db() -> None:
    with _conn() as con:
        con.executescript(SCHEMA)


def save_report(report: dict) -> str:
    """Insert or replace a crash report. Returns the report id."""
    with _conn() as con:
        con.execute(
            """
            INSERT OR REPLACE INTO crash_reports
                (id, boot_id, detected_at, crash_time, crash_type, severity,
                 summary, telemetry, analysis)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                report["id"],
                report["boot_id"],
                report["detected_at"],
                report.get("crash_time"),
                report["crash_type"],
                report.get("severity", "unknown"),
                report.get("summary"),
                json.dumps(report.get("telemetry", {})),
                json.dumps(report.get("analysis")) if report.get("analysis") else None,
            ),
        )
    return report["id"]


def update_analysis(report_id: str, analysis: dict) -> None:
    with _conn() as con:
        con.execute(
            "UPDATE crash_reports SET analysis=?, summary=?, severity=? WHERE id=?",
            (
                json.dumps(analysis),
                analysis.get("summary"),
                analysis.get("severity", "unknown"),
                report_id,
            ),
        )


def list_reports(limit: int = 50) -> list[dict]:
    with _conn() as con:
        rows = con.execute(
            """SELECT id, boot_id, detected_at, crash_time, crash_type,
                      severity, summary, analysis
               FROM crash_reports
               ORDER BY COALESCE(crash_time, detected_at) DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()
    result = []
    for row in rows:
        r = dict(row)
        if r["analysis"]:
            r["analysis"] = json.loads(r["analysis"])
        result.append(r)
    return result


def get_report(report_id: str) -> dict | None:
    with _conn() as con:
        row = con.execute(
            "SELECT * FROM crash_reports WHERE id=?", (report_id,)
        ).fetchone()
    if row is None:
        return None
    r = dict(row)
    r["telemetry"] = json.loads(r["telemetry"])
    if r["analysis"]:
        r["analysis"] = json.loads(r["analysis"])
    return r


def delete_report(report_id: str) -> bool:
    """Delete a single report. Returns True if a row was deleted."""
    with _conn() as con:
        cur = con.execute("DELETE FROM crash_reports WHERE id=?", (report_id,))
    return cur.rowcount > 0


def cleanup_old_reports(max_age_days: int) -> int:
    """Delete reports older than *max_age_days*. Returns number of rows deleted."""
    with _conn() as con:
        cur = con.execute(
            """DELETE FROM crash_reports
               WHERE COALESCE(crash_time, detected_at) <
                     strftime('%Y-%m-%dT%H:%M:%SZ',
                              datetime('now', ? || ' days'))""",
            (f"-{max_age_days}",),
        )
    return cur.rowcount


def count_reports() -> int:
    """Return total number of stored reports without loading rows."""
    with _conn() as con:
        row = con.execute("SELECT COUNT(*) FROM crash_reports").fetchone()
    return row[0] if row else 0


def set_meta(key: str, value: str) -> None:
    with _conn() as con:
        con.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, value)
        )


def get_meta(key: str, default: str = "") -> str:
    with _conn() as con:
        row = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else default
