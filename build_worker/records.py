"""Persisted build results (Slice 5 "register build result").

A small SQLite ledger so operators (and the Web Console Builds page) can see
what was built, its outcome, digest and where the structured log lives — even
for failed jobs.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class BuildRecord:
    build_id: int
    app_id: str
    version: str
    status: str
    digest: str
    commit: str
    log_path: str
    error: str | None
    created_at: str


class BuildRecordStore:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute(
                """CREATE TABLE IF NOT EXISTS builds (
                    build_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    app_id TEXT NOT NULL,
                    version TEXT NOT NULL,
                    status TEXT NOT NULL,
                    digest TEXT NOT NULL DEFAULT '',
                    commit_ref TEXT NOT NULL DEFAULT '',
                    log_path TEXT NOT NULL DEFAULT '',
                    error TEXT,
                    created_at TEXT NOT NULL
                )"""
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 10000")
        return conn

    def record(self, *, app_id: str, version: str, status: str, digest: str = "",
               commit: str = "", log_path: str = "", error: str | None = None) -> int:
        with self._connect() as db:
            cur = db.execute(
                "INSERT INTO builds(app_id, version, status, digest, commit_ref, log_path, error, created_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (app_id, version, status, digest, commit, log_path, error, _utc_now()),
            )
            return int(cur.lastrowid)

    @staticmethod
    def _row(r: sqlite3.Row) -> BuildRecord:
        return BuildRecord(r["build_id"], r["app_id"], r["version"], r["status"], r["digest"],
                           r["commit_ref"], r["log_path"], r["error"], r["created_at"])

    def list_builds(self, app_id: str | None = None) -> list[BuildRecord]:
        with self._connect() as db:
            if app_id is None:
                rows = db.execute("SELECT * FROM builds ORDER BY build_id DESC").fetchall()
            else:
                rows = db.execute(
                    "SELECT * FROM builds WHERE app_id = ? ORDER BY build_id DESC", (app_id,)
                ).fetchall()
        return [self._row(r) for r in rows]
