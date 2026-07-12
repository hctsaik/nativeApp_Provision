"""Local package registry and object-store contracts for the update demo.

The public surface is :class:`PackageService`: the single use case shared by the
lab CLI, a future HTTP Control Plane and GUI (architecture decision #10). It only
ever raises the stable domain errors in :mod:`provision_builder.package_errors`;
storage adapters translate their own exceptions into those errors so callers can
map failures by ``code`` without inspecting SQLite or the filesystem.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO, Iterable, Protocol

from provision_builder.package_errors import (
    ArtifactAlreadyExists,
    ArtifactMissing,
    DuplicateVersion,
    HashMismatch,
    ReleaseNotPublished,
    ReleaseYanked,
    UnknownChannel,
    validate_identifier,
)

PUBLISHED = "published"
YANKED = "yanked"

_CHUNK = 1024 * 1024


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class Release:
    app_id: str
    version: str
    object_key: str
    sha256: str
    size_bytes: int
    status: str
    created_at: str


class Registry(Protocol):
    def create_release(self, release: Release) -> None: ...
    def get_release(self, app_id: str, version: str) -> Release | None: ...
    def list_releases(self, app_id: str) -> list[Release]: ...
    def list_applications(self) -> list[str]: ...
    def all_object_keys(self) -> set[str]: ...
    def promote(self, app_id: str, channel: str, version: str) -> None: ...
    def yank(self, app_id: str, version: str) -> None: ...
    def resolve(self, app_id: str, channel: str) -> Release | None: ...


class ObjectStore(Protocol):
    def put(self, object_key: str, source: BinaryIO) -> tuple[str, int]: ...
    def open(self, object_key: str) -> BinaryIO: ...
    def exists(self, object_key: str) -> bool: ...
    def iter_keys(self) -> "Iterable[str]": ...


class SQLiteRegistry:
    """Oracle-like relational registry for local development.

    Adapter contract (every Registry implementation must honour it):
    a duplicate ``(app_id, version)`` insert raises
    :class:`~provision_builder.package_errors.DuplicateVersion`; promoting or
    yanking a missing release raises ``ReleaseNotPublished``; promoting a yanked
    release raises ``ReleaseYanked``.
    """

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")  # tolerate concurrent writers
        return connection

    def _initialize(self) -> None:
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS applications (
                    app_id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS releases (
                    app_id TEXT NOT NULL,
                    version TEXT NOT NULL,
                    object_key TEXT NOT NULL UNIQUE,
                    sha256 TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
                    status TEXT NOT NULL CHECK (status IN ('published', 'yanked')),
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (app_id, version),
                    FOREIGN KEY (app_id) REFERENCES applications(app_id)
                );
                CREATE TABLE IF NOT EXISTS channels (
                    app_id TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    version TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (app_id, channel),
                    FOREIGN KEY (app_id, version) REFERENCES releases(app_id, version)
                );
                """
            )

    @staticmethod
    def _release(row: sqlite3.Row | None) -> Release | None:
        return Release(**dict(row)) if row is not None else None

    @staticmethod
    def _status(db: sqlite3.Connection, app_id: str, version: str) -> str | None:
        row = db.execute(
            "SELECT status FROM releases WHERE app_id = ? AND version = ?",
            (app_id, version),
        ).fetchone()
        return None if row is None else row["status"]

    def create_release(self, release: Release) -> None:
        validate_identifier(release.app_id, "app_id")
        validate_identifier(release.version, "version")
        try:
            with self._connect() as db:
                db.execute(
                    "INSERT OR IGNORE INTO applications(app_id, created_at) VALUES (?, ?)",
                    (release.app_id, release.created_at),
                )
                db.execute(
                    """INSERT INTO releases
                       (app_id, version, object_key, sha256, size_bytes, status, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (release.app_id, release.version, release.object_key, release.sha256,
                     release.size_bytes, release.status, release.created_at),
                )
        except sqlite3.IntegrityError as exc:
            # The DB constraint is the final authority on uniqueness even if a
            # concurrent publisher slipped past the service-layer pre-check.
            raise DuplicateVersion(
                f"release already exists: {release.app_id}@{release.version}"
            ) from exc

    def get_release(self, app_id: str, version: str) -> Release | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM releases WHERE app_id = ? AND version = ?", (app_id, version)
            ).fetchone()
        return self._release(row)

    def list_releases(self, app_id: str) -> list[Release]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM releases WHERE app_id = ? ORDER BY created_at DESC", (app_id,)
            ).fetchall()
        return [Release(**dict(row)) for row in rows]

    def list_applications(self) -> list[str]:
        with self._connect() as db:
            rows = db.execute("SELECT app_id FROM applications ORDER BY app_id").fetchall()
        return [row["app_id"] for row in rows]

    def all_object_keys(self) -> set[str]:
        with self._connect() as db:
            rows = db.execute("SELECT object_key FROM releases").fetchall()
        return {row["object_key"] for row in rows}

    def promote(self, app_id: str, channel: str, version: str) -> None:
        validate_identifier(channel, "channel")
        with self._connect() as db:
            status = self._status(db, app_id, version)
            if status is None:
                raise ReleaseNotPublished(f"release not found: {app_id}@{version}")
            if status == YANKED:
                raise ReleaseYanked(f"release is yanked: {app_id}@{version}")
            if status != PUBLISHED:
                raise ReleaseNotPublished(f"release not published: {app_id}@{version}")
            db.execute(
                """INSERT INTO channels(app_id, channel, version, updated_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(app_id, channel) DO UPDATE SET
                     version = excluded.version, updated_at = excluded.updated_at""",
                (app_id, channel, version, _utc_now()),
            )

    def yank(self, app_id: str, version: str) -> None:
        with self._connect() as db:
            status = self._status(db, app_id, version)
            if status is None:
                raise ReleaseNotPublished(f"release not found: {app_id}@{version}")
            if status == YANKED:
                return  # idempotent: yanking an already-yanked release is a no-op
            db.execute(
                "UPDATE releases SET status = ? WHERE app_id = ? AND version = ?",
                (YANKED, app_id, version),
            )

    def resolve(self, app_id: str, channel: str) -> Release | None:
        with self._connect() as db:
            row = db.execute(
                """SELECT r.* FROM channels c JOIN releases r
                     ON r.app_id = c.app_id AND r.version = c.version
                     WHERE c.app_id = ? AND c.channel = ?""",
                (app_id, channel),
            ).fetchone()
        return self._release(row)


class FileObjectStore:
    """Filesystem implementation with MinIO-like immutable object keys."""

    def __init__(self, root: Path | str):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, object_key: str) -> Path:
        parts = object_key.replace("\\", "/").split("/")
        if not parts or any(not p or p in {".", ".."} or ":" in p for p in parts):
            raise ValueError(f"invalid object key: {object_key!r}")
        # Containment is checked logically (no per-call Path.resolve(), which is
        # race-fragile on Windows while the tree is being created concurrently).
        # Rejecting '', '.', '..' and drive markers above means the join can't
        # escape; commonpath confirms it without touching the filesystem.
        root_n = os.path.normpath(self.root)
        path_n = os.path.normpath(self.root.joinpath(*parts))
        if os.path.commonpath([root_n, path_n]) != root_n:
            raise ValueError(f"object key escapes store: {object_key!r}")
        return Path(path_n)

    def put(self, object_key: str, source: BinaryIO) -> tuple[str, int]:
        destination = self._path(object_key)
        if destination.exists():
            raise FileExistsError(f"immutable object already exists: {object_key}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        size = 0
        fd, temp_name = tempfile.mkstemp(prefix=".upload-", dir=destination.parent)
        try:
            with os.fdopen(fd, "wb") as target:
                while chunk := source.read(_CHUNK):
                    target.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
            # Materialise atomically AND exclusively: os.link fails with
            # FileExistsError if a concurrent writer already created the object,
            # so an immutable object is never silently overwritten (os.replace
            # would clobber it).
            try:
                os.link(temp_name, destination)
            except FileExistsError:
                raise
            except (OSError, NotImplementedError):
                # No hardlink support: fall back to a guarded replace. Re-check
                # first to preserve immutability on the common path.
                if destination.exists():
                    raise FileExistsError(f"immutable object already exists: {object_key}")
                os.replace(temp_name, destination)
        except BaseException:
            Path(temp_name).unlink(missing_ok=True)
            raise
        finally:
            if destination.exists():
                Path(temp_name).unlink(missing_ok=True)  # drop the temp hardlink
        return digest.hexdigest(), size

    def open(self, object_key: str) -> BinaryIO:
        return self._path(object_key).open("rb")

    def exists(self, object_key: str) -> bool:
        return self._path(object_key).is_file()

    def iter_keys(self) -> Iterable[str]:
        for path in sorted(self.root.rglob("*")):
            if not path.is_file() or path.name.startswith("."):
                continue  # skip in-flight .upload-* / .download-* temp files
            yield path.relative_to(self.root).as_posix()

    def delete(self, object_key: str) -> None:
        self._path(object_key).unlink(missing_ok=True)


class PackageService:
    """Use case shared by a future GUI, HTTP API, and CI client."""

    def __init__(self, registry: Registry, objects: ObjectStore):
        self.registry = registry
        self.objects = objects

    @staticmethod
    def _object_key(app_id: str, version: str) -> str:
        return f"applications/{app_id}/{version}/{app_id}-{version}.napp"

    def publish(self, app_id: str, version: str, package: Path | str) -> Release:
        validate_identifier(app_id, "app_id")
        validate_identifier(version, "version")
        # Pre-checks only improve the error experience; the DB constraint and the
        # immutable object key remain the real authority (both raise below on a
        # race). Publishing the same version any number of times therefore always
        # fails with the same DuplicateVersion / ArtifactAlreadyExists code.
        if self.registry.get_release(app_id, version) is not None:
            raise DuplicateVersion(f"release already exists: {app_id}@{version}")
        object_key = self._object_key(app_id, version)
        if self.objects.exists(object_key):
            raise ArtifactAlreadyExists(
                f"orphan artifact exists for {app_id}@{version}; "
                "run object-store GC or publish a new version"
            )
        try:
            with Path(package).open("rb") as source:
                sha256, size = self.objects.put(object_key, source)
        except FileExistsError as exc:
            # We saw no object at the pre-check but lost the exclusive-create race
            # to a concurrent publisher of the SAME version — coherent answer is
            # DuplicateVersion (a pre-existing orphan is caught by the check above).
            raise DuplicateVersion(f"release already exists: {app_id}@{version}") from exc
        release = Release(app_id, version, object_key, sha256, size, PUBLISHED, _utc_now())
        # If this write loses a race the object is left unreferenced for a safe GC
        # to reclaim — we never publish half of the metadata.
        self.registry.create_release(release)
        return release

    def promote(self, app_id: str, channel: str, version: str) -> Release | None:
        self.registry.promote(app_id, channel, version)
        return self.registry.resolve(app_id, channel)

    def yank(self, app_id: str, version: str) -> Release | None:
        self.registry.yank(app_id, version)
        return self.registry.get_release(app_id, version)

    def resolve(self, app_id: str, channel: str) -> Release | None:
        return self.registry.resolve(app_id, channel)

    def get_release(self, app_id: str, version: str) -> Release | None:
        return self.registry.get_release(app_id, version)

    def list_releases(self, app_id: str) -> list[Release]:
        return self.registry.list_releases(app_id)

    def list_applications(self) -> list[str]:
        return self.registry.list_applications()

    def open_artifact(self, release: Release) -> BinaryIO:
        try:
            return self.objects.open(release.object_key)
        except FileNotFoundError as exc:
            raise ArtifactMissing(
                f"artifact missing for {release.app_id}@{release.version}"
            ) from exc

    def download(self, app_id: str, channel: str, destination: Path | str) -> Release:
        validate_identifier(app_id, "app_id")
        validate_identifier(channel, "channel")
        release = self.registry.resolve(app_id, channel)
        if release is None:
            raise UnknownChannel(f"channel not found: {app_id}/{channel}")
        if release.status == YANKED:
            raise ReleaseYanked(f"release is yanked: {app_id}@{release.version}")
        try:
            source = self.objects.open(release.object_key)
        except FileNotFoundError as exc:
            raise ArtifactMissing(f"artifact missing for {app_id}@{release.version}") from exc
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        fd, temp_name = tempfile.mkstemp(prefix=".download-", dir=destination.parent)
        try:
            with source, os.fdopen(fd, "wb") as target:
                while chunk := source.read(_CHUNK):
                    target.write(chunk)
                    digest.update(chunk)
            if digest.hexdigest() != release.sha256:
                raise HashMismatch(
                    f"downloaded artifact SHA-256 mismatch for {app_id}@{release.version}"
                )
            os.replace(temp_name, destination)
        except BaseException:
            Path(temp_name).unlink(missing_ok=True)
            raise
        return release

    @staticmethod
    def as_json(release: Release) -> str:
        return json.dumps(asdict(release), ensure_ascii=False, indent=2)
