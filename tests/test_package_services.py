from __future__ import annotations

import hashlib
import io
from pathlib import Path

import pytest

from provision_builder.package_errors import (
    ArtifactAlreadyExists,
    ArtifactMissing,
    DuplicateVersion,
    HashMismatch,
    InvalidIdentifier,
    ReleaseNotPublished,
    ReleaseYanked,
    UnknownChannel,
)
from provision_builder.package_services import FileObjectStore, PackageService, SQLiteRegistry


@pytest.fixture
def packages(tmp_path: Path) -> PackageService:
    return PackageService(SQLiteRegistry(tmp_path / "registry.db"), FileObjectStore(tmp_path / "objects"))


def _publish(packages: PackageService, tmp_path: Path, *, app: str = "cv-reviewer",
             version: str = "1.0.0", body: bytes = b"immutable package"):
    source = tmp_path / f"{app}-{version}.napp"
    source.write_bytes(body)
    return packages.publish(app, version, source)


# ── 既有閉環（行為不變） ─────────────────────────────────────────────────────

def test_publish_promote_resolve_and_download(packages: PackageService, tmp_path: Path) -> None:
    source = tmp_path / "cv-reviewer.napp"
    source.write_bytes(b"immutable cv reviewer package")
    release = packages.publish("cv-reviewer", "1.0.0", source)
    packages.promote("cv-reviewer", "production", "1.0.0")
    assert packages.resolve("cv-reviewer", "production") == release
    assert release.sha256 == hashlib.sha256(source.read_bytes()).hexdigest()
    destination = tmp_path / "client" / "package.napp"
    assert packages.download("cv-reviewer", "production", destination) == release
    assert destination.read_bytes() == source.read_bytes()


def test_object_key_cannot_escape_store(tmp_path: Path) -> None:
    store = FileObjectStore(tmp_path / "objects")
    with pytest.raises(ValueError, match="invalid object key"):
        store.open("../secret")


# ── 既有測試升級為 domain error（行為刻意改變） ──────────────────────────────

def test_release_and_object_are_immutable(packages: PackageService, tmp_path: Path) -> None:
    source = tmp_path / "package.napp"
    source.write_bytes(b"v1")
    packages.publish("cv-reviewer", "1.0.0", source)
    source.write_bytes(b"different content")
    with pytest.raises(DuplicateVersion):
        packages.publish("cv-reviewer", "1.0.0", source)


def test_only_published_existing_release_can_be_promoted(packages: PackageService) -> None:
    with pytest.raises(ReleaseNotPublished):
        packages.promote("cv-reviewer", "production", "9.9.9")


def test_download_rejects_tampered_object(packages: PackageService, tmp_path: Path) -> None:
    release = _publish(packages, tmp_path, body=b"trusted")
    packages.promote("cv-reviewer", "production", "1.0.0")
    object_path = packages.objects._path(release.object_key)  # type: ignore[attr-defined]
    object_path.write_bytes(b"tampered")
    destination = tmp_path / "download.napp"
    with pytest.raises(HashMismatch) as exc_info:
        packages.download("cv-reviewer", "production", destination)
    assert exc_info.value.code == "hash_mismatch"
    assert not destination.exists()


# ── Identifier 驗證 ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("bad", ["../evil", "a b", "a:b", "", ".hidden"])
def test_invalid_app_id_or_version_rejected(packages: PackageService, tmp_path: Path, bad: str) -> None:
    source = tmp_path / "p.napp"
    source.write_bytes(b"x")
    with pytest.raises(InvalidIdentifier):
        packages.publish(bad, "1.0.0", source)
    with pytest.raises(InvalidIdentifier):
        packages.publish("cv-reviewer", bad, source)


def test_invalid_channel_rejected_on_promote_and_download(packages: PackageService, tmp_path: Path) -> None:
    _publish(packages, tmp_path)
    with pytest.raises(InvalidIdentifier):
        packages.promote("cv-reviewer", "bad chan", "1.0.0")
    with pytest.raises(InvalidIdentifier):
        packages.download("cv-reviewer", "bad chan", tmp_path / "out.napp")


# ── Duplicate publish 穩定性（不因失敗點漂移） ──────────────────────────────

def test_duplicate_publish_is_stable_across_retries(packages: PackageService, tmp_path: Path) -> None:
    _publish(packages, tmp_path)
    source = tmp_path / "retry.napp"
    source.write_bytes(b"whatever")
    for _ in range(3):
        with pytest.raises(DuplicateVersion):
            packages.publish("cv-reviewer", "1.0.0", source)


def test_orphan_object_reports_artifact_already_exists(packages: PackageService, tmp_path: Path) -> None:
    # object 已存在但 registry 無 release（前一次 publish 上傳成功、寫 DB 前中斷）
    object_key = "applications/cv-reviewer/1.0.0/cv-reviewer-1.0.0.napp"
    packages.objects.put(object_key, io.BytesIO(b"orphan"))
    source = tmp_path / "p.napp"
    source.write_bytes(b"v1")
    with pytest.raises(ArtifactAlreadyExists) as exc_info:
        packages.publish("cv-reviewer", "1.0.0", source)
    assert exc_info.value.code == "artifact_already_exists"


# ── Yank 狀態模型 ───────────────────────────────────────────────────────────

def test_yank_blocks_promote_and_download(packages: PackageService, tmp_path: Path) -> None:
    _publish(packages, tmp_path)
    packages.promote("cv-reviewer", "production", "1.0.0")
    packages.yank("cv-reviewer", "1.0.0")
    with pytest.raises(ReleaseYanked):
        packages.promote("cv-reviewer", "production", "1.0.0")
    with pytest.raises(ReleaseYanked):
        packages.download("cv-reviewer", "production", tmp_path / "out.napp")
    resolved = packages.resolve("cv-reviewer", "production")
    assert resolved is not None and resolved.status == "yanked"


def test_yank_is_idempotent(packages: PackageService, tmp_path: Path) -> None:
    _publish(packages, tmp_path)
    packages.yank("cv-reviewer", "1.0.0")
    packages.yank("cv-reviewer", "1.0.0")  # 不得噴錯


def test_yank_missing_release(packages: PackageService) -> None:
    with pytest.raises(ReleaseNotPublished):
        packages.yank("cv-reviewer", "9.9.9")


# ── Download 失敗分類 ───────────────────────────────────────────────────────

def test_download_missing_object_reports_artifact_missing(packages: PackageService, tmp_path: Path) -> None:
    release = _publish(packages, tmp_path)
    packages.promote("cv-reviewer", "production", "1.0.0")
    packages.objects._path(release.object_key).unlink()  # type: ignore[attr-defined]
    with pytest.raises(ArtifactMissing):
        packages.download("cv-reviewer", "production", tmp_path / "out.napp")


def test_unknown_channel(packages: PackageService, tmp_path: Path) -> None:
    _publish(packages, tmp_path)  # 從未 promote
    with pytest.raises(UnknownChannel):
        packages.download("cv-reviewer", "production", tmp_path / "out.napp")
