"""SQLite record of transactions already handled, so repeated runs don't
re-evaluate or re-write them."""
from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone


class StateStore:
    def __init__(self, path: str = "state.db"):
        # The webhook server calls this from worker threads, so allow
        # cross-thread use and serialize access with a lock.
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._lock = threading.Lock()
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS processed (
                   transaction_id TEXT PRIMARY KEY,
                   outcome        TEXT NOT NULL,
                   option_id      TEXT,
                   processed_at   TEXT NOT NULL
               )""")
        self._conn.commit()

    def get(self, transaction_id: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT outcome FROM processed WHERE transaction_id = ?", (transaction_id,)).fetchone()
        return row[0] if row else None

    def record(self, transaction_id: str, outcome: str, option_id: str | None = None) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO processed VALUES (?, ?, ?, ?)",
                (transaction_id, outcome, option_id, datetime.now(timezone.utc).isoformat()))
            self._conn.commit()

    def close(self) -> None:
        self._conn.close()
