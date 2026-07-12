"""Rollout and governance (Slice 8).

Desired state is expressed as *staged rollouts*, never one-shot "install now"
commands (decision #9). A rollout targets a version and widens by percentage
(10 → 50 → 100); a device is included by a deterministic hash bucket, so its
membership is stable and only ever grows as the stage advances. Reported device
failures above a threshold auto-pause the rollout. Every mutating action is
authorized (RBAC hook) and written to an append-only audit log.

OIDC/token verification is out of scope for the offline box — the ``Authorizer``
protocol is the seam where a real identity provider plugs in (it maps a verified
actor to roles). ``RoleBasedAuthorizer`` is a concrete, testable policy;
``AllowAll`` is the lab default.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from provision_builder.package_errors import PackageDomainError


class Unauthorized(PackageDomainError):
    code = "unauthorized"


class RolloutError(PackageDomainError):
    code = "rollout_error"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def bucket(app_id: str, device_id: str) -> int:
    """Deterministic 0..99 bucket for a device within an application's rollout."""
    digest = hashlib.sha256(f"{app_id}:{device_id}".encode("utf-8")).hexdigest()
    return int(digest[:8], 16) % 100


# ── authorization ───────────────────────────────────────────────────────────

class Authorizer(Protocol):
    def check(self, actor: str, action: str) -> None: ...


class AllowAll:
    def check(self, actor: str, action: str) -> None:
        return None


class RoleBasedAuthorizer:
    def __init__(self, actor_roles: dict[str, set[str]], role_actions: dict[str, set[str]]):
        self._actor_roles = actor_roles
        self._role_actions = role_actions

    def check(self, actor: str, action: str) -> None:
        roles = self._actor_roles.get(actor, set())
        if not any(action in self._role_actions.get(role, set()) for role in roles):
            raise Unauthorized(f"{actor!r} may not {action}")


# ── data ────────────────────────────────────────────────────────────────────

@dataclass
class Rollout:
    rollout_id: int
    app_id: str
    version: str
    baseline: str | None
    stage_percent: int
    status: str  # active | paused | completed
    approved: bool = False


class RolloutStore:
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
                CREATE TABLE IF NOT EXISTS devices (
                    device_id TEXT PRIMARY KEY,
                    group_name TEXT NOT NULL DEFAULT 'default',
                    registered_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS rollouts (
                    rollout_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    app_id TEXT NOT NULL,
                    version TEXT NOT NULL,
                    baseline TEXT,
                    stage_percent INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    approved INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS rollout_reports (
                    rollout_id INTEGER NOT NULL,
                    device_id TEXT NOT NULL,
                    success INTEGER NOT NULL,
                    reported_at TEXT NOT NULL,
                    PRIMARY KEY (rollout_id, device_id)
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    actor TEXT NOT NULL,
                    action TEXT NOT NULL,
                    target TEXT NOT NULL,
                    detail_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );
                """
            )


# ── service ─────────────────────────────────────────────────────────────────

class RolloutService:
    def __init__(
        self,
        store: RolloutStore,
        *,
        authorizer: Authorizer | None = None,
        failure_threshold: float = 0.2,
        min_samples: int = 5,
        approval_threshold: int = 100,
    ):
        self.store = store
        self.authz = authorizer or AllowAll()
        self.failure_threshold = failure_threshold
        self.min_samples = min_samples
        # Advancing a rollout beyond this stage percent requires explicit
        # approval. Default 100 → approval optional (never blocks); set lower
        # (e.g. 10) to gate wide rollouts behind a human sign-off.
        self.approval_threshold = approval_threshold

    def _audit(self, db: sqlite3.Connection, actor: str, action: str, target: str, **detail) -> None:
        db.execute(
            "INSERT INTO audit_events(actor, action, target, detail_json, created_at) VALUES (?,?,?,?,?)",
            (actor, action, target, json.dumps(detail, ensure_ascii=False), _utc_now()),
        )

    # devices ---------------------------------------------------------------

    def register_device(self, device_id: str, group: str = "default", *, actor: str = "system") -> None:
        self.authz.check(actor, "device.register")
        with self.store._connect() as db:
            db.execute(
                "INSERT INTO devices(device_id, group_name, registered_at) VALUES (?,?,?) "
                "ON CONFLICT(device_id) DO UPDATE SET group_name = excluded.group_name",
                (device_id, group, _utc_now()),
            )
            self._audit(db, actor, "device.register", device_id, group=group)

    def list_devices(self, group: str | None = None) -> list[str]:
        with self.store._connect() as db:
            if group is None:
                rows = db.execute("SELECT device_id FROM devices ORDER BY device_id").fetchall()
            else:
                rows = db.execute(
                    "SELECT device_id FROM devices WHERE group_name = ? ORDER BY device_id", (group,)
                ).fetchall()
        return [r["device_id"] for r in rows]

    # rollouts --------------------------------------------------------------

    def start_rollout(self, app_id: str, version: str, *, stage_percent: int = 10,
                     baseline: str | None = None, actor: str = "system") -> Rollout:
        self.authz.check(actor, "rollout.start")
        if not 0 < stage_percent <= 100:
            raise RolloutError(f"stage_percent must be in 1..100, got {stage_percent}")
        with self.store._connect() as db:
            if baseline is None:
                row = db.execute(
                    "SELECT version FROM rollouts WHERE app_id = ? AND status = 'completed' "
                    "ORDER BY rollout_id DESC LIMIT 1", (app_id,)
                ).fetchone()
                baseline = row["version"] if row else None
            now = _utc_now()
            cur = db.execute(
                "INSERT INTO rollouts(app_id, version, baseline, stage_percent, status, created_at, updated_at)"
                " VALUES (?,?,?,?, 'active', ?, ?)",
                (app_id, version, baseline, stage_percent, now, now),
            )
            rollout_id = int(cur.lastrowid)
            self._audit(db, actor, "rollout.start", f"{app_id}@{version}",
                        stage_percent=stage_percent, baseline=baseline)
        return self.get_rollout(rollout_id)

    def advance(self, rollout_id: int, stage_percent: int, *, actor: str = "system") -> Rollout:
        self.authz.check(actor, "rollout.advance")
        rollout = self.get_rollout(rollout_id)
        if rollout.status != "active":
            raise RolloutError(f"cannot advance a {rollout.status} rollout")
        if stage_percent <= rollout.stage_percent:
            raise RolloutError("rollout can only widen, never narrow")
        if stage_percent > self.approval_threshold and not rollout.approved:
            raise Unauthorized(
                f"advancing beyond {self.approval_threshold}% requires approval"
            )
        status = "completed" if stage_percent >= 100 else "active"
        with self.store._connect() as db:
            db.execute(
                "UPDATE rollouts SET stage_percent = ?, status = ?, updated_at = ? WHERE rollout_id = ?",
                (min(stage_percent, 100), status, _utc_now(), rollout_id),
            )
            self._audit(db, actor, "rollout.advance", f"{rollout.app_id}@{rollout.version}",
                        stage_percent=min(stage_percent, 100), status=status)
        return self.get_rollout(rollout_id)

    def approve(self, rollout_id: int, *, actor: str = "system") -> Rollout:
        self.authz.check(actor, "rollout.approve")
        rollout = self.get_rollout(rollout_id)
        with self.store._connect() as db:
            db.execute(
                "UPDATE rollouts SET approved = 1, updated_at = ? WHERE rollout_id = ?",
                (_utc_now(), rollout_id),
            )
            self._audit(db, actor, "rollout.approve", f"{rollout.app_id}@{rollout.version}")
        return self.get_rollout(rollout_id)

    def pause(self, rollout_id: int, *, actor: str = "system", reason: str = "manual") -> Rollout:
        self.authz.check(actor, "rollout.pause")
        return self._set_status(rollout_id, "paused", actor, "rollout.pause", reason=reason)

    def resume(self, rollout_id: int, *, actor: str = "system") -> Rollout:
        self.authz.check(actor, "rollout.resume")
        rollout = self.get_rollout(rollout_id)
        if rollout.status != "paused":
            raise RolloutError(f"cannot resume a {rollout.status} rollout")
        return self._set_status(rollout_id, "active", actor, "rollout.resume")

    def _set_status(self, rollout_id: int, status: str, actor: str, action: str, **detail) -> Rollout:
        rollout = self.get_rollout(rollout_id)
        with self.store._connect() as db:
            db.execute(
                "UPDATE rollouts SET status = ?, updated_at = ? WHERE rollout_id = ?",
                (status, _utc_now(), rollout_id),
            )
            self._audit(db, actor, action, f"{rollout.app_id}@{rollout.version}", **detail)
        return self.get_rollout(rollout_id)

    def get_rollout(self, rollout_id: int) -> Rollout:
        with self.store._connect() as db:
            row = db.execute("SELECT * FROM rollouts WHERE rollout_id = ?", (rollout_id,)).fetchone()
        if row is None:
            raise RolloutError(f"no such rollout: {rollout_id}")
        return Rollout(row["rollout_id"], row["app_id"], row["version"], row["baseline"],
                       row["stage_percent"], row["status"], bool(row["approved"]))

    def latest_rollout(self, app_id: str) -> Rollout | None:
        with self.store._connect() as db:
            row = db.execute(
                "SELECT * FROM rollouts WHERE app_id = ? ORDER BY rollout_id DESC LIMIT 1", (app_id,)
            ).fetchone()
        if row is None:
            return None
        return Rollout(row["rollout_id"], row["app_id"], row["version"], row["baseline"],
                       row["stage_percent"], row["status"], bool(row["approved"]))

    # desired state ---------------------------------------------------------

    def desired_for_device(self, app_id: str, device_id: str) -> str | None:
        rollout = self.latest_rollout(app_id)
        if rollout is None:
            return None
        if rollout.status == "completed":
            return rollout.version
        included = bucket(app_id, device_id) < rollout.stage_percent
        return rollout.version if included else rollout.baseline

    # observation + auto-pause ---------------------------------------------

    def report(self, rollout_id: int, device_id: str, success: bool, *, actor: str = "device") -> Rollout:
        self.authz.check(actor, "rollout.report")
        with self.store._connect() as db:
            db.execute(
                "INSERT INTO rollout_reports(rollout_id, device_id, success, reported_at) VALUES (?,?,?,?) "
                "ON CONFLICT(rollout_id, device_id) DO UPDATE SET success = excluded.success, "
                "reported_at = excluded.reported_at",
                (rollout_id, device_id, 1 if success else 0, _utc_now()),
            )
            total = db.execute(
                "SELECT COUNT(*) AS n FROM rollout_reports WHERE rollout_id = ?", (rollout_id,)
            ).fetchone()["n"]
            failures = db.execute(
                "SELECT COUNT(*) AS n FROM rollout_reports WHERE rollout_id = ? AND success = 0", (rollout_id,)
            ).fetchone()["n"]
            row = db.execute("SELECT * FROM rollouts WHERE rollout_id = ?", (rollout_id,)).fetchone()
            status = row["status"]
            if (status == "active" and total >= self.min_samples
                    and failures / total > self.failure_threshold):
                db.execute(
                    "UPDATE rollouts SET status = 'paused', updated_at = ? WHERE rollout_id = ?",
                    (_utc_now(), rollout_id),
                )
                self._audit(db, "system", "rollout.autopause", f"{row['app_id']}@{row['version']}",
                            total=total, failures=failures,
                            failure_rate=round(failures / total, 3))
        return self.get_rollout(rollout_id)

    def failure_rate(self, rollout_id: int) -> float:
        with self.store._connect() as db:
            total = db.execute(
                "SELECT COUNT(*) AS n FROM rollout_reports WHERE rollout_id = ?", (rollout_id,)
            ).fetchone()["n"]
            if total == 0:
                return 0.0
            failures = db.execute(
                "SELECT COUNT(*) AS n FROM rollout_reports WHERE rollout_id = ? AND success = 0", (rollout_id,)
            ).fetchone()["n"]
        return failures / total

    # audit -----------------------------------------------------------------

    def audit_log(self) -> list[dict]:
        with self.store._connect() as db:
            rows = db.execute(
                "SELECT actor, action, target, detail_json, created_at FROM audit_events ORDER BY event_id"
            ).fetchall()
        return [
            {"actor": r["actor"], "action": r["action"], "target": r["target"],
             "detail": json.loads(r["detail_json"]), "created_at": r["created_at"]}
            for r in rows
        ]
