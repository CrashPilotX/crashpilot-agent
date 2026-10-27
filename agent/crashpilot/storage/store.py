"""SQLite-backed crash report storage."""

from __future__ import annotations

import json
import random
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Callable, Generator, TypeVar

from ..config import get_settings

T = TypeVar("T")

# A report the cloud refuses this many times (an HTTP 4xx, not an outage) is
# set aside: kept locally, but no longer re-sent every minute or left at the
# head of the oldest-first backfill queue, where it held up newer reports.
MAX_PUSH_REJECTIONS = 5

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
    push_rejections INTEGER NOT NULL DEFAULT 0,  -- times the cloud refused it (HTTP 4xx)
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

-- One row per heartbeat tick, kept for about a week: enough for the network verdict to see a
-- whole week's pattern, small enough (about 1 MB) to forget about. See collectors/net_health.py.
CREATE TABLE IF NOT EXISTS network_samples (
    ts              REAL PRIMARY KEY,   -- epoch seconds
    iface           TEXT NOT NULL,
    medium          TEXT,
    link_speed_mbps INTEGER,
    duplex          TEXT,
    rx_bytes        INTEGER NOT NULL,   -- cumulative counters: rates come from differences
    tx_bytes        INTEGER NOT NULL,
    rx_errs         INTEGER,
    rx_drops        INTEGER,
    tx_errs         INTEGER,
    tx_drops        INTEGER,
    tcp_out         INTEGER,
    tcp_retrans     INTEGER,
    gw_rtt_ms       REAL,
    gw_loss_pct     REAL,
    inet_rtt_ms     REAL
);

-- Bytes moved per calendar month (UTC), added up from the counters as they grow, so a reboot
-- (counters back to zero) does not lose what came before it. last_* is the previous reading.
CREATE TABLE IF NOT EXISTS network_usage (
    month     TEXT PRIMARY KEY,         -- 'YYYY-MM'
    rx_bytes  INTEGER NOT NULL DEFAULT 0,
    tx_bytes  INTEGER NOT NULL DEFAULT 0,
    since     TEXT NOT NULL,
    last_ts   REAL,
    last_iface TEXT,
    last_rx   INTEGER,
    last_tx   INTEGER
);

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


# Generous on purpose: every commit of first-time setup waits on an fsync, and on a
# busy disk (a CI host extracting images, a spinning drive) a second of that per
# commit queues up behind each contender. Waiting is better than not starting.
_LOCK_TIMEOUT_SECONDS = 30.0


def _while_locked(
    operation: Callable[[], T],
    timeout: float = _LOCK_TIMEOUT_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> T:
    """Run a step of database setup, retrying while another process holds the lock.

    The container starts the heartbeat loop and `crashpilot serve` together, and
    on a new database both create it at once. When two connections contend for
    the lock during that first write, SQLite sometimes answers "database is
    locked" straight away, without consulting the connection's busy timeout,
    which used to stop the server at startup (a container then exited with code
    1 and, without a restart policy, stayed down). Every step of setup can be
    run again safely, and the process that got there first is finished in
    milliseconds, so the loser waits briefly and retries.
    """
    deadline = clock() + timeout
    while True:
        try:
            return operation()
        except sqlite3.OperationalError as exc:
            message = str(exc).lower()
            if ("locked" not in message and "busy" not in message) or clock() >= deadline:
                raise
        sleep(0.02 + random.random() * 0.08)


def _enable_wal(con: sqlite3.Connection, **retry: Any) -> None:
    """Put the database in WAL mode.

    The mode is stored in the database file, so once any process has switched
    it every later connection finds WAL already on, and reading it takes no
    write lock.
    """

    def switch() -> None:
        mode = con.execute("PRAGMA journal_mode").fetchone()
        if mode and str(mode[0]).lower() == "wal":
            return
        con.execute("PRAGMA journal_mode=WAL")

    _while_locked(switch, **retry)


@contextmanager
def _conn() -> Generator[sqlite3.Connection, None, None]:
    cfg = get_settings()
    # str() required for Python 3.10 compatibility (Path accepted natively in 3.11+)
    con = sqlite3.connect(str(cfg.db_path), timeout=10)
    try:
        con.row_factory = sqlite3.Row
        _enable_wal(con)
        con.execute("PRAGMA foreign_keys=ON")
    except BaseException:
        con.close()
        raise
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
        _while_locked(lambda: con.executescript(SCHEMA))
        # Migration: add columns to databases created before they existed
        # (`pushed` came with cloud backfill, `push_rejections` later).
        cols = {row[1] for row in con.execute("PRAGMA table_info(crash_reports)")}
        for column in ("pushed", "push_rejections"):
            if column in cols:
                continue
            def add_column(name: str = column) -> sqlite3.Cursor:
                return con.execute(
                    f"ALTER TABLE crash_reports ADD COLUMN {name} INTEGER NOT NULL DEFAULT 0"
                )

            try:
                _while_locked(add_column)
            except sqlite3.OperationalError as exc:
                # Another process (e.g. the heartbeat loop starting alongside the
                # boot analysis) added the column first: that's fine. Anything
                # else, a lock that never cleared included, must not be passed
                # off as success: the column would then be missing.
                if "duplicate column" not in str(exc).lower():
                    raise


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


def mark_push_rejected(report_id: str) -> None:
    """Count one refusal of this report by the cloud (see MAX_PUSH_REJECTIONS)."""
    with _conn() as con:
        con.execute(
            "UPDATE crash_reports SET push_rejections = push_rejections + 1 WHERE id=?",
            (report_id,),
        )


def list_unpushed(limit: int = 50) -> list[dict]:
    """Return reports not yet confirmed in the cloud (oldest first), with
    telemetry and analysis decoded: ready to hand to push_report()."""
    with _conn() as con:
        rows = con.execute(
            """SELECT * FROM crash_reports
               WHERE pushed = 0 AND push_rejections < ?
               ORDER BY COALESCE(crash_time, detected_at) ASC
               LIMIT ?""",
            (MAX_PUSH_REJECTIONS, limit),
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
            "SELECT COUNT(*) FROM crash_reports WHERE pushed = 0 AND push_rejections < ?",
            (MAX_PUSH_REJECTIONS,),
        ).fetchone()
    return row[0] if row else 0


def count_set_aside() -> int:
    """Number of reports the cloud kept refusing, no longer retried."""
    with _conn() as con:
        row = con.execute(
            "SELECT COUNT(*) FROM crash_reports WHERE pushed = 0 AND push_rejections >= ?",
            (MAX_PUSH_REJECTIONS,),
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
    """The newest `limit` snapshots in the window, oldest first.

    Rows stamped in the future (taken while the clock ran ahead) are left
    out: newest-first, they would otherwise pose as the latest sample.
    """
    with _conn() as con:
        rows = con.execute(
            """SELECT snapshot FROM flight_snapshots
               WHERE datetime(captured_at) >= datetime('now', ? || ' hours')
                 AND datetime(captured_at) <= datetime('now')
               ORDER BY captured_at DESC
               LIMIT ?""",
            (f"-{hours}", limit),
        ).fetchall()
    return [json.loads(row["snapshot"]) for row in reversed(rows)]


_SAMPLE_COLUMNS = (
    "ts", "iface", "medium", "link_speed_mbps", "duplex", "rx_bytes", "tx_bytes", "rx_errs", "rx_drops",
    "tx_errs", "tx_drops", "tcp_out", "tcp_retrans", "gw_rtt_ms", "gw_loss_pct", "inet_rtt_ms",
)
# A gap this long between readings is a machine that was off or asleep, not traffic to count.
_USAGE_MAX_GAP_S = 6 * 3600


def save_network_sample(sample: dict, retention_days: int = 8) -> None:
    """Record one connection reading, add its bytes to the month's usage, and drop old readings."""
    values = tuple(sample.get(column) for column in _SAMPLE_COLUMNS)
    if values[0] is None or values[1] is None or values[5] is None or values[6] is None:
        raise ValueError("a network sample needs ts, iface, rx_bytes and tx_bytes")
    with _conn() as con:
        con.execute(
            f"INSERT OR REPLACE INTO network_samples({', '.join(_SAMPLE_COLUMNS)}) "
            f"VALUES ({', '.join('?' for _ in _SAMPLE_COLUMNS)})",
            values,
        )
        con.execute("DELETE FROM network_samples WHERE ts < ?", (sample["ts"] - retention_days * 86400,))
        _add_to_usage(con, sample)


def _add_to_usage(con: sqlite3.Connection, sample: dict) -> None:
    moment = datetime.fromtimestamp(sample["ts"], tz=timezone.utc)
    month = moment.strftime("%Y-%m")
    con.execute(
        "INSERT OR IGNORE INTO network_usage(month, since) VALUES (?, ?)",
        (month, moment.strftime("%Y-%m-%dT%H:%M:%SZ")),
    )
    # The baseline is the latest reading of any month, so the interval that crosses midnight on the
    # 1st is counted (in the new month) instead of being lost.
    row = con.execute(
        "SELECT last_ts, last_iface, last_rx, last_tx FROM network_usage "
        "WHERE last_ts IS NOT NULL ORDER BY last_ts DESC LIMIT 1"
    ).fetchone()
    rx, tx = sample["rx_bytes"], sample["tx_bytes"]
    add_rx = add_tx = 0
    if (
        row is not None and row["last_iface"] == sample["iface"]
        and 0 < sample["ts"] - row["last_ts"] <= _USAGE_MAX_GAP_S
    ):
        # Counters that went backwards were reset by a reboot: what is there now was moved since.
        add_rx = rx - row["last_rx"] if rx >= row["last_rx"] else rx
        add_tx = tx - row["last_tx"] if tx >= row["last_tx"] else tx
    con.execute(
        """UPDATE network_usage SET rx_bytes = rx_bytes + ?, tx_bytes = tx_bytes + ?,
               last_ts = ?, last_iface = ?, last_rx = ?, last_tx = ? WHERE month = ?""",
        (add_rx, add_tx, sample["ts"], sample["iface"], rx, tx, month),
    )


def list_network_samples(hours: float = 168, limit: int = 20000) -> list[dict]:
    """The readings in the window, oldest first. Readings stamped in the future are left out."""
    now = time.time()
    with _conn() as con:
        rows = con.execute(
            "SELECT * FROM network_samples WHERE ts >= ? AND ts <= ? ORDER BY ts DESC LIMIT ?",
            (now - hours * 3600, now + 60, limit),
        ).fetchall()
    return [dict(row) for row in reversed(rows)]


def get_network_usage(month: str | None = None) -> dict | None:
    """Bytes moved this month (or the month given as 'YYYY-MM'), or None if nothing was recorded."""
    month = month or datetime.now(timezone.utc).strftime("%Y-%m")
    with _conn() as con:
        row = con.execute("SELECT month, rx_bytes, tx_bytes, since FROM network_usage WHERE month=?", (month,)).fetchone()
    return dict(row) if row else None


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
