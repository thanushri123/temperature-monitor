"""SQLite storage (WAL mode). Swappable for a managed time-series store at scale.

Tables
  readings(device_id, ts, temp_c, breach)  PK (device_id, ts) -> idempotent inserts
  alerts(id, device_id, opened_ts, last_ts, resolved_ts, peak_c, breach_count, last_notified_ts)
  rejected(id, received_ts, code, reason, payload)  -> audit trail for bad data
  device_state(device_id, last_ts, last_temp_c, in_breach)  -> O(1) evaluation state
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from typing import Iterable

import pandas as pd

SCHEMA = """
CREATE TABLE IF NOT EXISTS readings(
    device_id TEXT NOT NULL,
    ts INTEGER NOT NULL,
    temp_c REAL NOT NULL,
    breach INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (device_id, ts)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_readings_ts ON readings(ts);

CREATE TABLE IF NOT EXISTS alerts(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id TEXT NOT NULL,
    opened_ts INTEGER NOT NULL,
    last_ts INTEGER NOT NULL,
    resolved_ts INTEGER,
    peak_c REAL NOT NULL,
    breach_count INTEGER NOT NULL DEFAULT 1,
    last_notified_ts INTEGER NOT NULL,
    threshold_c REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_alerts_open ON alerts(device_id, resolved_ts);
CREATE INDEX IF NOT EXISTS idx_alerts_opened ON alerts(opened_ts);

CREATE TABLE IF NOT EXISTS rejected(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    received_ts INTEGER NOT NULL,
    code TEXT NOT NULL,
    reason TEXT NOT NULL,
    payload TEXT
);
CREATE INDEX IF NOT EXISTS idx_rejected_ts ON rejected(received_ts);

CREATE TABLE IF NOT EXISTS device_state(
    device_id TEXT PRIMARY KEY,
    last_ts INTEGER NOT NULL,
    last_temp_c REAL NOT NULL,
    in_breach INTEGER NOT NULL DEFAULT 0
);
"""


class Store:
    def __init__(self, path: str = ":memory:"):
        self.path = path
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        if path != ":memory:":
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(SCHEMA)

    @contextmanager
    def tx(self):
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def close(self):
        with self._lock:
            self._conn.close()

    # ---- writes -------------------------------------------------------
    def insert_reading(self, conn, device_id: str, ts: int, temp_c: float, breach: bool) -> bool:
        """Insert; returns False if (device_id, ts) already exists (duplicate delivery)."""
        cur = conn.execute(
            "INSERT OR IGNORE INTO readings(device_id, ts, temp_c, breach) VALUES (?,?,?,?)",
            (device_id, ts, temp_c, int(breach)),
        )
        return cur.rowcount == 1

    def log_rejected(self, received_ts: int, code: str, reason: str, payload) -> None:
        try:
            body = json.dumps(payload, default=str)[:2000]
        except Exception:
            body = repr(payload)[:2000]
        with self._lock:
            self._conn.execute(
                "INSERT INTO rejected(received_ts, code, reason, payload) VALUES (?,?,?,?)",
                (received_ts, code, reason, body),
            )

    def purge_older_than(self, cutoff_ts: int) -> dict:
        """Retention: delete data older than cutoff. Open alerts are never purged."""
        with self.tx() as c:
            r = c.execute("DELETE FROM readings WHERE ts < ?", (cutoff_ts,)).rowcount
            a = c.execute(
                "DELETE FROM alerts WHERE resolved_ts IS NOT NULL AND resolved_ts < ?", (cutoff_ts,)
            ).rowcount
            j = c.execute("DELETE FROM rejected WHERE received_ts < ?", (cutoff_ts,)).rowcount
            c.execute("DELETE FROM device_state WHERE last_ts < ?", (cutoff_ts,))
        return {"readings": r, "alerts": a, "rejected": j}

    def clear_all(self):
        """Delete every row (used by the dashboard's Clear all data button)."""
        with self.tx() as c:
            for table in ("readings", "alerts", "rejected", "device_state"):
                c.execute(f"DELETE FROM {table}")

    # ---- reads --------------------------------------------------------
    def _df(self, sql: str, params: Iterable = ()) -> pd.DataFrame:
        with self._lock:
            rows = self._conn.execute(sql, tuple(params)).fetchall()
        return pd.DataFrame([dict(r) for r in rows])

    def get_state(self, conn, device_id: str):
        return conn.execute("SELECT * FROM device_state WHERE device_id=?", (device_id,)).fetchone()

    def history(self, device_id: str | None, start_ts: int, end_ts: int, limit: int = 200_000) -> pd.DataFrame:
        sql = "SELECT device_id, ts, temp_c, breach FROM readings WHERE ts BETWEEN ? AND ?"
        params: list = [start_ts, end_ts]
        if device_id:
            sql += " AND device_id=?"
            params.append(device_id)
        sql += " ORDER BY ts LIMIT ?"
        params.append(limit)
        return self._df(sql, params)

    def devices(self) -> pd.DataFrame:
        return self._df("SELECT * FROM device_state ORDER BY device_id")

    def alerts(self, start_ts: int = 0, only_open: bool = False) -> pd.DataFrame:
        sql = "SELECT * FROM alerts WHERE opened_ts >= ?"
        if only_open:
            sql += " AND resolved_ts IS NULL"
        return self._df(sql + " ORDER BY opened_ts DESC", [start_ts])

    def rejected(self, start_ts: int = 0, limit: int = 500) -> pd.DataFrame:
        return self._df(
            "SELECT * FROM rejected WHERE received_ts >= ? ORDER BY id DESC LIMIT ?", [start_ts, limit]
        )

    def counts(self) -> dict:
        with self._lock:
            c = self._conn
            return {
                "readings": c.execute("SELECT COUNT(*) FROM readings").fetchone()[0],
                "alerts": c.execute("SELECT COUNT(*) FROM alerts").fetchone()[0],
                "rejected": c.execute("SELECT COUNT(*) FROM rejected").fetchone()[0],
                "oldest_ts": c.execute("SELECT MIN(ts) FROM readings").fetchone()[0],
                "newest_ts": c.execute("SELECT MAX(ts) FROM readings").fetchone()[0],
            }