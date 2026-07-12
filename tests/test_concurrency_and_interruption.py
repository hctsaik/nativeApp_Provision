"""Slice 3 durability: concurrent promotion + interrupted upload."""

from __future__ import annotations

import io
import threading
from pathlib import Path

import pytest

from provision_builder.package_errors import DuplicateVersion
from provision_builder.package_services import FileObjectStore, PackageService, SQLiteRegistry


@pytest.fixture
def service(tmp_path: Path) -> PackageService:
    return PackageService(SQLiteRegistry(tmp_path / "r.db"), FileObjectStore(tmp_path / "obj"))


def test_concurrent_publish_of_distinct_versions_all_succeed(service: PackageService, tmp_path: Path) -> None:
    versions = [f"1.0.{i}" for i in range(12)]
    errors: list[Exception] = []

    def publish(version: str) -> None:
        pkg = tmp_path / f"{version}.napp"
        pkg.write_bytes(f"body {version}".encode())
        try:
            service.publish("cv-reviewer", version, pkg)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=publish, args=(v,)) for v in versions]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert {r.version for r in service.list_releases("cv-reviewer")} == set(versions)


def test_concurrent_publish_same_version_yields_exactly_one_release(service: PackageService, tmp_path: Path) -> None:
    from provision_builder.package_errors import PackageDomainError

    outcomes: list[str] = []
    lock = threading.Lock()

    def publish(n: int) -> None:
        pkg = tmp_path / f"same-{n}.napp"
        pkg.write_bytes(b"identical target version")
        try:
            service.publish("cv-reviewer", "1.0.0", pkg)
            with lock:
                outcomes.append("ok")
        except PackageDomainError as exc:  # a well-typed loser outcome is fine
            with lock:
                outcomes.append(exc.code)
        except Exception as exc:  # a raw sqlite/OS error is a durability bug
            with lock:
                outcomes.append(f"bug:{type(exc).__name__}")

    threads = [threading.Thread(target=publish, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert outcomes.count("ok") == 1                          # exactly one winner
    assert not any(o.startswith("bug:") for o in outcomes)    # no raw exceptions leaked
    losers = [o for o in outcomes if o != "ok"]
    assert all(o in {"duplicate_version", "artifact_already_exists"} for o in losers)
    assert len(service.list_releases("cv-reviewer")) == 1     # registry has a single release


def test_concurrent_put_never_overwrites_immutable_object(tmp_path: Path) -> None:
    store = FileObjectStore(tmp_path / "obj")
    key = "applications/x/1.0.0/x-1.0.0.napp"
    winners: list[int] = []
    lock = threading.Lock()

    def put(n: int) -> None:
        try:
            store.put(key, io.BytesIO(f"content from writer {n}".encode()))
            with lock:
                winners.append(n)
        except FileExistsError:
            pass

    threads = [threading.Thread(target=put, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(winners) == 1                                  # exactly one writer materialised the object
    with store.open(key) as fh:
        stored = fh.read()
    assert stored == f"content from writer {winners[0]}".encode()  # winner's bytes, not corrupted/mixed


def test_concurrent_promotion_leaves_channel_consistent(service: PackageService, tmp_path: Path) -> None:
    versions = [f"2.0.{i}" for i in range(6)]
    for v in versions:
        pkg = tmp_path / f"{v}.napp"
        pkg.write_bytes(v.encode())
        service.publish("cv-reviewer", v, pkg)

    def promote(v: str) -> None:
        service.promote("cv-reviewer", "production", v)

    threads = [threading.Thread(target=promote, args=(v,)) for v in versions]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    resolved = service.resolve("cv-reviewer", "production")
    assert resolved is not None and resolved.version in versions  # one winner, no corruption


class _ExplodingStream:
    """A source that yields some bytes then fails, to simulate a dropped upload."""

    def __init__(self, chunks: int):
        self._left = chunks

    def read(self, _size: int = -1) -> bytes:
        if self._left <= 0:
            raise ConnectionError("upload interrupted")
        self._left -= 1
        return b"x" * 1024


def test_interrupted_upload_leaves_no_object_or_temp(service: PackageService, tmp_path: Path) -> None:
    store = service.objects
    with pytest.raises(ConnectionError):
        store.put("applications/cv-reviewer/1.0.0/cv-reviewer-1.0.0.napp", _ExplodingStream(3))
    # No committed object, and no leftover .upload-* temp file.
    assert not store.exists("applications/cv-reviewer/1.0.0/cv-reviewer-1.0.0.napp")
    leftovers = [p for p in (tmp_path / "obj").rglob("*") if p.is_file()]
    assert leftovers == [], f"temp files leaked: {leftovers}"
