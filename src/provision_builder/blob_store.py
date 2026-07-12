"""Content-addressed blob store for big dependencies and models (Slice 3).

Large wheels / model files are stored once under ``blobs/sha256/<hash>`` and
referenced by hash from a ``.napp`` (see 02_ARCHITECTURE.md §3). Identical
content deduplicates naturally, so a source-only update never re-uploads or
re-downloads torch.

Both the MinIO side and the device side use the same layout; the Native_App
agent downloads only the hashes it is missing.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path
from typing import BinaryIO, Iterable

from provision_builder.package_errors import ArtifactCorrupted, HashMismatch

_CHUNK = 1024 * 1024
_HEX = 64


def _is_sha256(value: str) -> bool:
    return len(value) == _HEX and all(c in "0123456789abcdef" for c in value)


class FileBlobStore:
    """Filesystem ``blobs/sha256/<hash>`` store with atomic, immutable writes."""

    def __init__(self, root: Path | str):
        self.root = Path(root).resolve()
        self.prefix = self.root / "sha256"
        self.prefix.mkdir(parents=True, exist_ok=True)

    def _path(self, digest: str) -> Path:
        if not _is_sha256(digest):
            raise ValueError(f"not a sha256 digest: {digest!r}")
        return self.prefix / digest

    def has(self, digest: str) -> bool:
        return self._path(digest).is_file()

    def put(self, source: BinaryIO) -> tuple[str, int]:
        """Stream ``source`` into the store; return ``(sha256, size)``.

        Writing an already-present blob is a no-op (content addressing makes it
        idempotent, unlike the immutable package objects).
        """
        digester = hashlib.sha256()
        size = 0
        fd, temp_name = tempfile.mkstemp(prefix=".blob-", dir=self.prefix)
        try:
            with os.fdopen(fd, "wb") as target:
                while chunk := source.read(_CHUNK):
                    target.write(chunk)
                    digester.update(chunk)
                    size += len(chunk)
            digest = digester.hexdigest()
            destination = self.prefix / digest
            if destination.exists():
                Path(temp_name).unlink(missing_ok=True)
            else:
                os.replace(temp_name, destination)
        except BaseException:
            Path(temp_name).unlink(missing_ok=True)
            raise
        return digest, size

    def put_file(self, path: Path | str) -> tuple[str, int]:
        with Path(path).open("rb") as source:
            return self.put(source)

    def open(self, digest: str) -> BinaryIO:
        return self._path(digest).open("rb")

    def verify(self, digest: str) -> None:
        """Re-hash a stored blob; raise if it is missing or corrupted on disk."""
        path = self._path(digest)
        if not path.is_file():
            raise ArtifactCorrupted(f"blob missing: {digest}")
        digester = hashlib.sha256()
        with path.open("rb") as source:
            while chunk := source.read(_CHUNK):
                digester.update(chunk)
        if digester.hexdigest() != digest:
            raise HashMismatch(f"blob content does not match its address: {digest}")

    def iter_digests(self) -> Iterable[str]:
        for path in sorted(self.prefix.iterdir()):
            if path.is_file() and _is_sha256(path.name):
                yield path.name

    def link_into(self, digest: str, destination: Path | str) -> Path:
        """Materialise a blob at ``destination`` via hardlink, copy as fallback.

        Used to assemble a per-tool wheelhouse without duplicating 2 GB files.
        """
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        source = self._path(digest)
        destination.unlink(missing_ok=True)
        try:
            os.link(source, destination)
        except (OSError, NotImplementedError):
            import shutil

            shutil.copyfile(source, destination)
        return destination
