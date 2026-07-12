"""PostgreSQL Registry adapter (Slice 3), mirroring SQLiteRegistry semantics.

Driver (``psycopg`` v3) is imported lazily inside ``__init__`` so importing this
module never requires the driver. An ``OracleRegistry`` can follow the same
shape later. Domain-error contract (identical to every Registry): duplicate
``(app_id, version)`` -> DuplicateVersion; missing release on promote/yank ->
ReleaseNotPublished; promoting a yanked release -> ReleaseYanked.

Unverified on the WDAC build box (no local PostgreSQL); the shared contract
tests exercise it in CI when PROVISION_PG_DSN is set.
"""

from __future__ import annotations

from datetime import datetime, timezone

from provision_builder.package_errors import (
    DuplicateVersion,
    RegistryUnavailable,
    ReleaseNotPublished,
    ReleaseYanked,
    validate_identifier,
)
from provision_builder.package_services import PUBLISHED, YANKED, Release

_DDL = """
CREATE TABLE IF NOT EXISTS applications (
    app_id VARCHAR(128) PRIMARY KEY,
    display_name VARCHAR(256),
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS application_releases (
    app_id VARCHAR(128) NOT NULL REFERENCES applications(app_id),
    version VARCHAR(64) NOT NULL,
    object_key VARCHAR(1024) NOT NULL UNIQUE,
    sha256 CHAR(64) NOT NULL,
    size_bytes BIGINT NOT NULL CHECK (size_bytes >= 0),
    status VARCHAR(32) NOT NULL CHECK (status IN ('published', 'yanked')),
    dependency_fingerprint CHAR(64),
    platform_constraint VARCHAR(256),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (app_id, version)
);
CREATE TABLE IF NOT EXISTS application_channels (
    app_id VARCHAR(128) NOT NULL,
    channel VARCHAR(32) NOT NULL,
    version VARCHAR(64) NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (app_id, channel),
    FOREIGN KEY (app_id, version) REFERENCES application_releases(app_id, version)
);
"""

_COLUMNS = "app_id, version, object_key, sha256, size_bytes, status, created_at"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class PostgreSQLRegistry:
    def __init__(self, dsn: str):
        try:
            import psycopg  # noqa: F401
        except ImportError as exc:  # pragma: no cover - depends on optional driver
            raise RegistryUnavailable("psycopg is required for PostgreSQLRegistry") from exc
        self._psycopg = __import__("psycopg")
        self.dsn = dsn
        self._initialize()

    def _connect(self):
        try:
            return self._psycopg.connect(self.dsn)
        except Exception as exc:  # pragma: no cover - network/driver
            raise RegistryUnavailable(f"cannot connect to PostgreSQL: {exc}") from exc

    def _initialize(self) -> None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(_DDL)
            conn.commit()

    @staticmethod
    def _release(row) -> Release | None:
        if row is None:
            return None
        app_id, version, object_key, sha256, size_bytes, status, created_at = row
        created = created_at.isoformat() if hasattr(created_at, "isoformat") else str(created_at)
        return Release(app_id, version, object_key, sha256, int(size_bytes), status, created)

    def create_release(self, release: Release) -> None:
        validate_identifier(release.app_id, "app_id")
        validate_identifier(release.version, "version")
        try:
            with self._connect() as conn, conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO applications(app_id, created_at) VALUES (%s, %s) "
                    "ON CONFLICT (app_id) DO NOTHING",
                    (release.app_id, release.created_at),
                )
                cur.execute(
                    f"INSERT INTO application_releases ({_COLUMNS}) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                    (release.app_id, release.version, release.object_key, release.sha256,
                     release.size_bytes, release.status, release.created_at),
                )
                conn.commit()
        except self._psycopg.errors.UniqueViolation as exc:
            raise DuplicateVersion(
                f"release already exists: {release.app_id}@{release.version}"
            ) from exc

    def get_release(self, app_id: str, version: str) -> Release | None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                f"SELECT {_COLUMNS} FROM application_releases WHERE app_id = %s AND version = %s",
                (app_id, version),
            )
            return self._release(cur.fetchone())

    def list_releases(self, app_id: str) -> list[Release]:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                f"SELECT {_COLUMNS} FROM application_releases WHERE app_id = %s "
                "ORDER BY created_at DESC",
                (app_id,),
            )
            return [self._release(row) for row in cur.fetchall()]  # type: ignore[misc]

    def list_applications(self) -> list[str]:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT app_id FROM applications ORDER BY app_id")
            return [row[0] for row in cur.fetchall()]

    def all_object_keys(self) -> set[str]:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT object_key FROM application_releases")
            return {row[0] for row in cur.fetchall()}

    def _status(self, cur, app_id: str, version: str) -> str | None:
        cur.execute(
            "SELECT status FROM application_releases WHERE app_id = %s AND version = %s",
            (app_id, version),
        )
        row = cur.fetchone()
        return None if row is None else row[0]

    def promote(self, app_id: str, channel: str, version: str) -> None:
        validate_identifier(channel, "channel")
        with self._connect() as conn, conn.cursor() as cur:
            status = self._status(cur, app_id, version)
            if status is None:
                raise ReleaseNotPublished(f"release not found: {app_id}@{version}")
            if status == YANKED:
                raise ReleaseYanked(f"release is yanked: {app_id}@{version}")
            if status != PUBLISHED:
                raise ReleaseNotPublished(f"release not published: {app_id}@{version}")
            cur.execute(
                "INSERT INTO application_channels(app_id, channel, version, updated_at) "
                "VALUES (%s, %s, %s, %s) "
                "ON CONFLICT (app_id, channel) DO UPDATE SET "
                "version = EXCLUDED.version, updated_at = EXCLUDED.updated_at",
                (app_id, channel, version, _utc_now()),
            )
            conn.commit()

    def yank(self, app_id: str, version: str) -> None:
        with self._connect() as conn, conn.cursor() as cur:
            status = self._status(cur, app_id, version)
            if status is None:
                raise ReleaseNotPublished(f"release not found: {app_id}@{version}")
            if status == YANKED:
                return
            cur.execute(
                "UPDATE application_releases SET status = %s WHERE app_id = %s AND version = %s",
                (YANKED, app_id, version),
            )
            conn.commit()

    def resolve(self, app_id: str, channel: str) -> Release | None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                f"SELECT {', '.join('r.' + c for c in _COLUMNS.split(', '))} "
                "FROM application_channels c JOIN application_releases r "
                "ON r.app_id = c.app_id AND r.version = c.version "
                "WHERE c.app_id = %s AND c.channel = %s",
                (app_id, channel),
            )
            return self._release(cur.fetchone())
