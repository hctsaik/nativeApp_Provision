"""Authoritative device-side state and operation journal (SQLite)."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from native_agent.operations import (
    CANCELLABLE_STAGES,
    PERCENT_BY_STAGE,
    STAGE_BY_FINAL_STATUS,
    STAGE_BY_STEP,
    OperationCancelled,
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Operation:
    op_id: int
    app_id: str
    from_version: str | None
    to_version: str
    current_step: str
    previous_active: str | None
    desired_identity: str
    status: str


class AgentState:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 10000")
        return conn

    def _initialize(self) -> None:
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS applications (
                    app_id TEXT PRIMARY KEY,
                    active_version TEXT,
                    last_known_good TEXT,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS operations (
                    op_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    app_id TEXT NOT NULL,
                    from_version TEXT,
                    to_version TEXT NOT NULL,
                    current_step TEXT NOT NULL,
                    previous_active TEXT,
                    desired_identity TEXT NOT NULL,
                    status TEXT NOT NULL,
                    kind TEXT NOT NULL DEFAULT 'update',
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    started_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_error TEXT
                );
                CREATE TABLE IF NOT EXISTS operation_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    op_id INTEGER NOT NULL,
                    stage TEXT NOT NULL,
                    state TEXT NOT NULL,
                    percent INTEGER NOT NULL,
                    message_key TEXT NOT NULL,
                    detail_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS failed_versions (
                    app_id TEXT NOT NULL,
                    version TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    failed_at TEXT NOT NULL,
                    PRIMARY KEY (app_id, version)
                );
                """
            )
            # Best-effort migration for pre-existing .lab/.device DBs whose
            # operations table predates the kind/cancel_requested columns.
            for ddl in ("ALTER TABLE operations ADD COLUMN kind TEXT NOT NULL DEFAULT 'update'",
                        "ALTER TABLE operations ADD COLUMN cancel_requested INTEGER NOT NULL DEFAULT 0"):
                try:
                    db.execute(ddl)
                except sqlite3.OperationalError:
                    pass  # column already exists

    # ── application pointers ────────────────────────────────────────────────

    def _ensure_app(self, db: sqlite3.Connection, app_id: str) -> None:
        db.execute(
            "INSERT OR IGNORE INTO applications(app_id, updated_at) VALUES (?, ?)",
            (app_id, _utc_now()),
        )

    def known_apps(self) -> list[str]:
        with self._connect() as db:
            rows = db.execute("SELECT app_id FROM applications ORDER BY app_id").fetchall()
        return [r["app_id"] for r in rows]

    def active_version(self, app_id: str) -> str | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT active_version FROM applications WHERE app_id = ?", (app_id,)
            ).fetchone()
        return row["active_version"] if row else None

    def last_known_good(self, app_id: str) -> str | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT last_known_good FROM applications WHERE app_id = ?", (app_id,)
            ).fetchone()
        return row["last_known_good"] if row else None

    def set_active(self, app_id: str, version: str | None) -> None:
        with self._connect() as db:
            self._ensure_app(db, app_id)
            db.execute(
                "UPDATE applications SET active_version = ?, updated_at = ? WHERE app_id = ?",
                (version, _utc_now(), app_id),
            )

    def set_last_known_good(self, app_id: str, version: str) -> None:
        with self._connect() as db:
            self._ensure_app(db, app_id)
            db.execute(
                "UPDATE applications SET last_known_good = ?, updated_at = ? WHERE app_id = ?",
                (version, _utc_now(), app_id),
            )

    # ── operation journal ───────────────────────────────────────────────────

    def begin_operation(self, app_id: str, *, from_version: str | None, to_version: str,
                        previous_active: str | None, desired_identity: str,
                        kind: str = "update") -> int:
        now = _utc_now()
        with self._connect() as db:
            self._ensure_app(db, app_id)
            cur = db.execute(
                """INSERT INTO operations
                   (app_id, from_version, to_version, current_step, previous_active,
                    desired_identity, status, kind, started_at, updated_at)
                   VALUES (?, ?, ?, 'CHECKING', ?, ?, 'running', ?, ?, ?)""",
                (app_id, from_version, to_version, previous_active, desired_identity, kind, now, now),
            )
            op_id = int(cur.lastrowid)
        self.add_event(op_id, "QUEUED", "QUEUED", f"application.{kind}.queued")
        return op_id

    def update_step(self, op_id: int, step: str) -> None:
        """Advance the journal, emit a structured RUNNING event, and honour cancel.

        Cancellation is cooperative and observed at stage boundaries: if a cancel
        was requested and the stage being entered is cancellable, raise
        OperationCancelled for the operation runner to catch.
        """
        stage = STAGE_BY_STEP.get(step, step)
        with self._connect() as db:
            db.execute(
                "UPDATE operations SET current_step = ?, updated_at = ? WHERE op_id = ?",
                (step, _utc_now(), op_id),
            )
        self.add_event(op_id, stage, "RUNNING", f"application.update.{stage.lower()}")
        if stage in CANCELLABLE_STAGES and self.cancel_requested(op_id):
            raise OperationCancelled(op_id)

    def finish_operation(self, op_id: int, status: str, last_error: str | None = None) -> None:
        with self._connect() as db:
            db.execute(
                "UPDATE operations SET status = ?, last_error = ?, updated_at = ? WHERE op_id = ?",
                (status, last_error, _utc_now(), op_id),
            )
        stage = STAGE_BY_FINAL_STATUS.get(status, "FAILED")
        self.add_event(op_id, stage, stage, f"application.update.{stage.lower()}",
                       detail={"error": last_error} if last_error else None)

    # ── operation events + cancellation ─────────────────────────────────────

    def add_event(self, op_id: int, stage: str, state: str, message_key: str,
                  detail: dict | None = None) -> int:
        with self._connect() as db:
            cur = db.execute(
                """INSERT INTO operation_events
                   (op_id, stage, state, percent, message_key, detail_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (op_id, stage, state, PERCENT_BY_STAGE.get(stage, 0), message_key,
                 json.dumps(detail or {}, ensure_ascii=False), _utc_now()),
            )
            return int(cur.lastrowid)

    def events_after(self, op_id: int, after_sequence: int = 0) -> list[dict]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM operation_events WHERE op_id = ? AND sequence > ? ORDER BY sequence",
                (op_id, after_sequence),
            ).fetchall()
        return [{"sequence": r["sequence"], "op_id": r["op_id"], "stage": r["stage"],
                 "state": r["state"], "percent": r["percent"], "message_key": r["message_key"],
                 "detail": json.loads(r["detail_json"]), "created_at": r["created_at"]}
                for r in rows]

    def operation_dict(self, op_id: int) -> dict | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM operations WHERE op_id = ?", (op_id,)).fetchone()
        return dict(row) if row is not None else None

    def request_cancel(self, op_id: int) -> None:
        with self._connect() as db:
            db.execute(
                "UPDATE operations SET cancel_requested = 1, updated_at = ? WHERE op_id = ?",
                (_utc_now(), op_id),
            )

    def cancel_requested(self, op_id: int) -> bool:
        with self._connect() as db:
            row = db.execute(
                "SELECT cancel_requested FROM operations WHERE op_id = ?", (op_id,)
            ).fetchone()
        return bool(row and row["cancel_requested"])

    def running_operations(self, app_id: str | None = None) -> list[Operation]:
        query = "SELECT * FROM operations WHERE status = 'running'"
        params: tuple = ()
        if app_id is not None:
            query += " AND app_id = ?"
            params = (app_id,)
        query += " ORDER BY op_id"
        with self._connect() as db:
            rows = db.execute(query, params).fetchall()
        return [self._op(row) for row in rows]

    @staticmethod
    def _op(row: sqlite3.Row) -> Operation:
        return Operation(
            op_id=row["op_id"], app_id=row["app_id"], from_version=row["from_version"],
            to_version=row["to_version"], current_step=row["current_step"],
            previous_active=row["previous_active"], desired_identity=row["desired_identity"],
            status=row["status"],
        )

    # ── failed-version memory (avoid infinite retry) ────────────────────────

    def record_failure(self, app_id: str, version: str, reason: str) -> None:
        with self._connect() as db:
            db.execute(
                """INSERT INTO failed_versions(app_id, version, reason, failed_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(app_id, version) DO UPDATE SET
                     reason = excluded.reason, failed_at = excluded.failed_at""",
                (app_id, version, reason, _utc_now()),
            )

    def is_failed(self, app_id: str, version: str) -> bool:
        with self._connect() as db:
            row = db.execute(
                "SELECT 1 FROM failed_versions WHERE app_id = ? AND version = ?", (app_id, version)
            ).fetchone()
        return row is not None

    def clear_failure(self, app_id: str, version: str) -> None:
        with self._connect() as db:
            db.execute(
                "DELETE FROM failed_versions WHERE app_id = ? AND version = ?", (app_id, version)
            )
