"""Shared ObjectStore contract — filesystem always, MinIO when configured."""

from __future__ import annotations

import hashlib
import io
import os
from pathlib import Path

import pytest

from provision_builder.package_services import FileObjectStore

BACKENDS = ["file", "minio"]


@pytest.fixture(params=BACKENDS)
def store(request, tmp_path: Path):
    if request.param == "file":
        return FileObjectStore(tmp_path / "objects")
    endpoint = os.environ.get("PROVISION_MINIO_ENDPOINT")
    if not endpoint:
        pytest.skip("MinIO contract needs PROVISION_MINIO_ENDPOINT (CI only)")
    try:  # pragma: no cover - CI only
        from remote_adapters.minio_store import MinioObjectStore
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"minio unavailable: {exc}")
    return MinioObjectStore(  # pragma: no cover - CI only
        endpoint,
        os.environ["PROVISION_MINIO_ACCESS_KEY"],
        os.environ["PROVISION_MINIO_SECRET_KEY"],
        os.environ.get("PROVISION_MINIO_BUCKET", "provision-test"),
        secure=os.environ.get("PROVISION_MINIO_SECURE", "0") == "1",
    )


def test_put_returns_true_sha256_and_size(store) -> None:
    payload = b"a package body of some size"
    digest, size = store.put("applications/x/1/x-1.napp", io.BytesIO(payload))
    assert digest == hashlib.sha256(payload).hexdigest()
    assert size == len(payload)


def test_roundtrip_open(store) -> None:
    payload = b"roundtrip"
    store.put("applications/x/1/x-1.napp", io.BytesIO(payload))
    with store.open("applications/x/1/x-1.napp") as fh:
        assert fh.read() == payload


def test_immutable_put(store) -> None:
    store.put("applications/x/1/x-1.napp", io.BytesIO(b"one"))
    with pytest.raises(FileExistsError):
        store.put("applications/x/1/x-1.napp", io.BytesIO(b"two"))


def test_exists_and_iter_and_delete(store) -> None:
    assert store.exists("applications/x/1/x-1.napp") is False
    store.put("applications/x/1/x-1.napp", io.BytesIO(b"one"))
    assert store.exists("applications/x/1/x-1.napp") is True
    assert "applications/x/1/x-1.napp" in set(store.iter_keys())
    store.delete("applications/x/1/x-1.napp")
    assert store.exists("applications/x/1/x-1.napp") is False


def test_missing_open_raises_filenotfound(store) -> None:
    with pytest.raises(FileNotFoundError):
        store.open("applications/nope/1/nope-1.napp")
