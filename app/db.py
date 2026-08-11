"""SQLite persistence (WAL). Small, synchronous, guarded by a lock —
every call is sub-millisecond at this scale."""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    seq         INTEGER PRIMARY KEY,          -- TV bar open time (ms) = idempotency key
    received_at TEXT NOT NULL,
    bar_time    TEXT,
    changed     INTEGER,
    payload     TEXT NOT NULL,
    status      TEXT NOT NULL,                -- accepted | rejected
    reason      TEXT
);
CREATE TABLE IF NOT EXISTS rejected_payloads (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    received_at TEXT NOT NULL,
    payload     TEXT NOT NULL,
    reason      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    trigger     TEXT NOT NULL,
    status      TEXT NOT NULL,                -- ok | partial | paused | skipped | error
    equity      REAL,
    plan        TEXT,
    result      TEXT,
    error       TEXT
);
CREATE TABLE IF NOT EXISTS orders (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       INTEGER,
    ts           TEXT NOT NULL,
    coin         TEXT NOT NULL,
    side         TEXT NOT NULL,               -- buy | sell
    requested_sz REAL NOT NULL,
    filled_sz    REAL,
    avg_px       REAL,
    fee          REAL,
    oid          INTEGER,
    status       TEXT NOT NULL,               -- filled | partial | unfilled | error | dry_run
    error        TEXT
);
CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: str | Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---- kv ----------------------------------------------------------------

    def kv_get(self, key: str) -> str | None:
        with self._lock:
            row = self._conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def kv_set(self, key: str, value: str | None) -> None:
        with self._lock:
            if value is None:
                self._conn.execute("DELETE FROM kv WHERE key=?", (key,))
            else:
                self._conn.execute(
                    "INSERT INTO kv(key,value) VALUES(?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, value),
                )
            self._conn.commit()

    # ---- signals -----------------------------------------------------------

    def insert_signal(self, seq: int, bar_time: str, changed: bool, payload: dict) -> bool:
        """Returns False if seq was already seen (duplicate — dropped)."""
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO signals(seq, received_at, bar_time, changed, payload, status)"
                " VALUES(?,?,?,?,?,'accepted')",
                (seq, utcnow(), bar_time, int(changed), json.dumps(payload)),
            )
            self._conn.commit()
        return cur.rowcount > 0

    def insert_rejected(self, payload: str, reason: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO rejected_payloads(received_at, payload, reason) VALUES(?,?,?)",
                (utcnow(), payload[:8192], reason),
            )
            self._conn.commit()

    def heartbeats_since(self, iso_ts: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM signals WHERE status='accepted' AND received_at >= ?",
                (iso_ts,),
            ).fetchone()
        return int(row["n"])

    # ---- runs / orders -----------------------------------------------------

    def start_run(self, trigger: str) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO runs(started_at, trigger, status) VALUES(?,?,'running')",
                (utcnow(), trigger),
            )
            self._conn.commit()
        return int(cur.lastrowid)

    def finish_run(
        self,
        run_id: int,
        status: str,
        equity: float | None = None,
        plan: list | None = None,
        result: dict | None = None,
        error: str | None = None,
    ) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE runs SET finished_at=?, status=?, equity=?, plan=?, result=?, error=? WHERE id=?",
                (
                    utcnow(),
                    status,
                    equity,
                    json.dumps(plan) if plan is not None else None,
                    json.dumps(result) if result is not None else None,
                    error,
                    run_id,
                ),
            )
            self._conn.commit()

    def insert_order(
        self,
        run_id: int,
        coin: str,
        side: str,
        requested_sz: float,
        status: str,
        filled_sz: float | None = None,
        avg_px: float | None = None,
        fee: float | None = None,
        oid: int | None = None,
        error: str | None = None,
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO orders(run_id, ts, coin, side, requested_sz, filled_sz, avg_px, fee, oid, status, error)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (run_id, utcnow(), coin, side, requested_sz, filled_sz, avg_px, fee, oid, status, error),
            )
            self._conn.commit()

    def fees_since(self, iso_ts: str) -> float:
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(SUM(fee),0) AS f FROM orders WHERE ts >= ?", (iso_ts,)
            ).fetchone()
        return float(row["f"])
