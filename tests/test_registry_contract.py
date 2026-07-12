"""Shared Registry contract — one suite, every backend (Slice 3).

SQLite runs everywhere. PostgreSQL runs only when PROVISION_PG_DSN is set and
``psycopg`` is importable (CI); otherwise it reports a visible skip so the
coverage gap is never silent.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from provision_builder.package_errors import DuplicateVersion, ReleaseNotPublished, ReleaseYanked
from provision_builder.package_services import Release, SQLiteRegistry

BACKENDS = ["sqlite", "postgres"]


def _now() -> str:
    return "2026-07-11T00:00:00+00:00"


def _rel(app: str = "cv-reviewer", version: str = "1.0.0", status: str = "published") -> Release:
    key = f"applications/{app}/{version}/{app}-{version}.napp"
    return Release(app, version, key, "0" * 64, 10, status, _now())


@pytest.fixture(params=BACKENDS)
def registry(request, tmp_path: Path):
    backend = request.param
    if backend == "sqlite":
        yield SQLiteRegistry(tmp_path / "registry.db")
        return
    dsn = os.environ.get("PROVISION_PG_DSN")
    if not dsn:
        pytest.skip("PostgreSQL contract needs PROVISION_PG_DSN (CI only)")
    try:
        from remote_adapters.postgres import PostgreSQLRegistry
    except Exception as exc:  # pragma: no cover - optional driver
        pytest.skip(f"psycopg unavailable: {exc}")
    reg = PostgreSQLRegistry(dsn)
    with reg._connect() as conn, conn.cursor() as cur:  # pragma: no cover - CI only
        cur.execute("DELETE FROM application_channels")
        cur.execute("DELETE FROM application_releases")
        cur.execute("DELETE FROM applications")
        conn.commit()
    yield reg


def test_create_get_and_list(registry) -> None:
    registry.create_release(_rel())
    got = registry.get_release("cv-reviewer", "1.0.0")
    assert got is not None and got.version == "1.0.0"
    assert [r.version for r in registry.list_releases("cv-reviewer")] == ["1.0.0"]
    assert registry.list_applications() == ["cv-reviewer"]
    assert registry.all_object_keys() == {"applications/cv-reviewer/1.0.0/cv-reviewer-1.0.0.napp"}


def test_duplicate_create_raises(registry) -> None:
    registry.create_release(_rel())
    with pytest.raises(DuplicateVersion):
        registry.create_release(_rel())


def test_promote_and_resolve(registry) -> None:
    registry.create_release(_rel())
    registry.promote("cv-reviewer", "production", "1.0.0")
    resolved = registry.resolve("cv-reviewer", "production")
    assert resolved is not None and resolved.version == "1.0.0"


def test_promote_missing_raises(registry) -> None:
    with pytest.raises(ReleaseNotPublished):
        registry.promote("cv-reviewer", "production", "9.9.9")


def test_repromote_moves_channel_pointer(registry) -> None:
    registry.create_release(_rel(version="1.0.0"))
    registry.create_release(_rel(version="2.0.0"))
    registry.promote("cv-reviewer", "production", "2.0.0")
    registry.promote("cv-reviewer", "production", "1.0.0")  # rollback by pointer
    assert registry.resolve("cv-reviewer", "production").version == "1.0.0"


def test_yank_then_promote_raises(registry) -> None:
    registry.create_release(_rel())
    registry.yank("cv-reviewer", "1.0.0")
    with pytest.raises(ReleaseYanked):
        registry.promote("cv-reviewer", "production", "1.0.0")


def test_yank_is_idempotent(registry) -> None:
    registry.create_release(_rel())
    registry.yank("cv-reviewer", "1.0.0")
    registry.yank("cv-reviewer", "1.0.0")
    assert registry.get_release("cv-reviewer", "1.0.0").status == "yanked"
