"""SQLite-backed crash report storage."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
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
    pushed      INTEGER NOT NULL DEFAULT 0,  -- 1 once confirmed in the cloud
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_crash_boot ON crash_reports(boot_id);
CREATE INDEX IF NOT EXISTS idx_crash_time ON crash_reports(crash_time DESC);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS flight_snapshots (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    captured_at TEXT NOT NULL,
    snapshot    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_flight_snapshots_captured
    ON flight_snapshots(captured_at DESC);

CREATE TABLE IF NOT EXISTS webhook_deliveries (
    id              TEXT PRIMARY KEY,
    event_type      TEXT NOT NULL,
    webhook_url     TEXT NOT NULL,
    payload         TEXT NOT NULL,
    attempts        INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT NOT NULL,
    delivered_at    TEXT,
    last_error      TEXT,
    created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_webhook_deliveries_due
    ON webhook_deliveries(delivered_at, next_attempt_at);
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
        # Migration: add `pushed` to databases created before cloud backfill existed.
        cols = {row[1] for row in con.execute("PRAGMA table_info(crash_reports)")}
        if "pushed" not in cols:
            try:
                con.execute(
                    "ALTER TABLE crash_reports ADD COLUMN pushed INTEGER NOT NULL DEFAULT 0"
                )
            except sqlite3.OperationalError:
                # Another process (e.g. the heartbeat loop starting alongside the
                # boot analysis) added the column first — that's fine.
                pass


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


def mark_pushed(report_id: str) -> None:
    """Mark a report as confirmed-delivered to the cloud."""
    with _conn() as con:
        con.execute("UPDATE crash_reports SET pushed=1 WHERE id=?", (report_id,))


def list_unpushed(limit: int = 50) -> list[dict]:
    """Return reports not yet confirmed in the cloud (oldest first), with
    telemetry and analysis decoded — ready to hand to push_report()."""
    with _conn() as con:
        rows = con.execute(
            """SELECT * FROM crash_reports
               WHERE pushed = 0
               ORDER BY COALESCE(crash_time, detected_at) ASC
               LIMIT ?""",
            (limit,),
        ).fetchall()
    result = []
    for row in rows:
        r = dict(row)
        r["telemetry"] = json.loads(r["telemetry"]) if r.get("telemetry") else {}
        if r.get("analysis"):
            r["analysis"] = json.loads(r["analysis"])
        result.append(r)
    return result


def count_unpushed() -> int:
    """Number of reports still waiting to reach the cloud."""
    with _conn() as con:
        row = con.execute(
            "SELECT COUNT(*) FROM crash_reports WHERE pushed = 0"
        ).fetchone()
    return row[0] if row else 0


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


def save_flight_snapshot(snapshot: dict, retention_hours: int = 48) -> None:
    captured_at = snapshot.get("captured_at")
    if not captured_at:
        raise ValueError("flight snapshot requires captured_at")
    with _conn() as con:
        con.execute(
            "INSERT INTO flight_snapshots(captured_at, snapshot) VALUES (?, ?)",
            (captured_at, json.dumps(snapshot)),
        )
        con.execute(
            """DELETE FROM flight_snapshots
               WHERE datetime(captured_at) < datetime('now', ? || ' hours')""",
            (f"-{retention_hours}",),
        )


def list_flight_snapshots(hours: int = 1, limit: int = 240) -> list[dict]:
    with _conn() as con:
        rows = con.execute(
            """SELECT snapshot FROM flight_snapshots
               WHERE datetime(captured_at) >= datetime('now', ? || ' hours')
               ORDER BY captured_at ASC
               LIMIT ?""",
            (f"-{hours}", limit),
        ).fetchall()
    return [json.loads(row["snapshot"]) for row in rows]


def enqueue_webhook_delivery(delivery: dict) -> str:
    with _conn() as con:
        con.execute(
            """INSERT OR IGNORE INTO webhook_deliveries
               (id, event_type, webhook_url, payload, next_attempt_at, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                delivery["id"],
                delivery["event_type"],
                delivery["webhook_url"],
                json.dumps(delivery["payload"], separators=(",", ":"), sort_keys=True),
                delivery["next_attempt_at"],
                delivery["created_at"],
            ),
        )
    return delivery["id"]


def list_due_webhook_deliveries(limit: int = 20) -> list[dict]:
    with _conn() as con:
        rows = con.execute(
            """SELECT * FROM webhook_deliveries
               WHERE delivered_at IS NULL
                 AND attempts < 8
                 AND datetime(next_attempt_at) <= datetime('now')
               ORDER BY datetime(next_attempt_at) ASC
               LIMIT ?""",
            (limit,),
        ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        result.append(item)
    return result


def mark_webhook_delivered(delivery_id: str, delivered_at: str) -> None:
    with _conn() as con:
        con.execute(
            """UPDATE webhook_deliveries
               SET delivered_at=?, last_error=NULL
               WHERE id=?""",
            (delivered_at, delivery_id),
        )


def mark_webhook_failed(
    delivery_id: str,
    *,
    attempts: int,
    next_attempt_at: str,
    error: str,
) -> None:
    with _conn() as con:
        con.execute(
            """UPDATE webhook_deliveries
               SET attempts=?, next_attempt_at=?, last_error=?
               WHERE id=?""",
            (attempts, next_attempt_at, error[:1000], delivery_id),
        )


def webhook_delivery_status() -> dict[str, int]:
    with _conn() as con:
        row = con.execute(
            """SELECT
                 SUM(CASE WHEN delivered_at IS NULL AND attempts < 8 THEN 1 ELSE 0 END) AS pending,
                 SUM(CASE WHEN delivered_at IS NULL AND attempts >= 8 THEN 1 ELSE 0 END) AS failed,
                 SUM(CASE WHEN delivered_at IS NOT NULL THEN 1 ELSE 0 END) AS delivered
               FROM webhook_deliveries"""
        ).fetchone()
    return {
        "pending": int(row["pending"] or 0) if row else 0,
        "failed": int(row["failed"] or 0) if row else 0,
        "delivered": int(row["delivered"] or 0) if row else 0,
    }
