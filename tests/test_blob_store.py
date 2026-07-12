from __future__ import annotations

import hashlib
import io
from pathlib import Path

import pytest

from provision_builder.blob_store import FileBlobStore
from provision_builder.package_errors import ArtifactCorrupted, HashMismatch


def test_put_is_content_addressed_and_dedupes(tmp_path: Path) -> None:
    store = FileBlobStore(tmp_path / "blobs")
    payload = b"torch-like 2GB stand-in"
    digest, size = store.put(io.BytesIO(payload))
    assert digest == hashlib.sha256(payload).hexdigest()
    assert size == len(payload)
    # Same content again → same digest, still exactly one blob on disk.
    again, _ = store.put(io.BytesIO(payload))
    assert again == digest
    assert list(store.iter_digests()) == [digest]


def test_has_and_open(tmp_path: Path) -> None:
    store = FileBlobStore(tmp_path / "blobs")
    digest, _ = store.put(io.BytesIO(b"data"))
    assert store.has(digest)
    with store.open(digest) as fh:
        assert fh.read() == b"data"


def test_verify_detects_corruption(tmp_path: Path) -> None:
    store = FileBlobStore(tmp_path / "blobs")
    digest, _ = store.put(io.BytesIO(b"trusted"))
    store.verify(digest)  # ok
    (store.prefix / digest).write_bytes(b"tampered")
    with pytest.raises(HashMismatch):
        store.verify(digest)


def test_verify_missing_blob(tmp_path: Path) -> None:
    store = FileBlobStore(tmp_path / "blobs")
    with pytest.raises(ArtifactCorrupted):
        store.verify("a" * 64)


def test_link_into_materialises_blob(tmp_path: Path) -> None:
    store = FileBlobStore(tmp_path / "blobs")
    digest, _ = store.put(io.BytesIO(b"wheel bytes"))
    dest = tmp_path / "wheelhouse" / "pkg.whl"
    store.link_into(digest, dest)
    assert dest.read_bytes() == b"wheel bytes"


def test_rejects_non_sha256_name(tmp_path: Path) -> None:
    store = FileBlobStore(tmp_path / "blobs")
    with pytest.raises(ValueError):
        store.open("not-a-digest")
