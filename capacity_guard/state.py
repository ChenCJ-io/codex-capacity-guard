"""Small private journal shared by command hooks and the recovery process."""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


class Store:
    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = directory / "state.sqlite3"
        self.db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        self.path.chmod(0o600)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS recoveries (
                thread_id TEXT PRIMARY KEY, turn_id TEXT NOT NULL, model TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0, next_at REAL NOT NULL,
                status TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '',
                token TEXT, submitted_turn TEXT, updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS cancelled_threads (
                thread_id TEXT PRIMARY KEY, timestamp REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp REAL NOT NULL,
                thread_id TEXT, action TEXT NOT NULL, detail TEXT NOT NULL
            );
        """)

    def close(self) -> None:
        self.db.close()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        else:
            self.db.execute("COMMIT")

    def get(self, key: str, default=None):
        row = self.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key: str, value) -> None:
        self.db.execute("INSERT INTO settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, json.dumps(value)))

    def audit(self, action: str, thread_id: str | None = None, detail: str = "") -> None:
        self.db.execute("INSERT INTO audit(timestamp,thread_id,action,detail) VALUES (?,?,?,?)", (time.time(), thread_id, action, detail))
        self.db.execute("DELETE FROM audit WHERE id < (SELECT COALESCE(MAX(id),0)-999 FROM audit)")

    def recovery(self, thread_id: str) -> dict | None:
        row = self.db.execute("SELECT * FROM recoveries WHERE thread_id=?", (thread_id,)).fetchone()
        return dict(row) if row else None

    def eligible(self, thread_id: str, timestamp: float) -> bool:
        if not self.get("enabled", False):
            return False
        allowed = self.get("threads", [])
        if allowed and thread_id not in allowed:
            return False
        row = self.db.execute("SELECT timestamp FROM cancelled_threads WHERE thread_id=?", (thread_id,)).fetchone()
        return not row or timestamp > row[0]

    def schedule(self, event, next_at: float) -> None:
        previous = self.recovery(event.thread_id)
        if previous and previous["turn_id"] == event.turn_id:
            return
        attempts = previous["attempts"] if previous and previous["status"] in {"submitted", "uncertain"} and previous["model"] == event.model else 0
        self.db.execute("""INSERT INTO recoveries(thread_id,turn_id,model,attempts,next_at,status,updated_at)
            VALUES (?,?,?,?,?,'waiting',?) ON CONFLICT(thread_id) DO UPDATE SET
            turn_id=excluded.turn_id,model=excluded.model,attempts=excluded.attempts,
            next_at=excluded.next_at,status='waiting',reason='',token=NULL,
            submitted_turn=NULL,updated_at=excluded.updated_at""",
            (event.thread_id, event.turn_id, event.model, attempts, next_at, time.time()))
        self.audit("capacity_detected", event.thread_id)

    def update(self, thread_id: str, **fields) -> None:
        allowed = {"attempts", "next_at", "status", "reason", "token", "submitted_turn"}
        if not fields or not set(fields) <= allowed:
            raise ValueError("Invalid recovery state fields.")
        fields["updated_at"] = time.time()
        self.db.execute("UPDATE recoveries SET " + ",".join(f"{k}=?" for k in fields) + " WHERE thread_id=?", [*fields.values(), thread_id])

    def cancel(self, thread_id: str, reason: str, timestamp: float | None = None) -> None:
        self.update(thread_id, status="cancelled", reason=reason)
        self.db.execute("INSERT INTO cancelled_threads VALUES (?,?) ON CONFLICT(thread_id) DO UPDATE SET timestamp=excluded.timestamp", (thread_id, timestamp or time.time()))
        self.audit(reason, thread_id)

    def pending(self, now: float) -> list[dict]:
        return [dict(row) for row in self.db.execute("SELECT * FROM recoveries WHERE status IN ('waiting','uncertain','submitted') AND next_at<=? ORDER BY next_at", (now,))]

    def status(self) -> dict:
        return {"enabled": self.get("enabled", False), "threads": self.get("threads", []), "pid": self.get("pid"), "heartbeat": self.get("heartbeat"), "last_issue": self.get("last_issue"), "recoveries": [dict(row) for row in self.db.execute("SELECT thread_id,turn_id,model,attempts,next_at,status,reason FROM recoveries ORDER BY updated_at DESC LIMIT 50")]}
