"""Shared ``.napp`` layout constants and checksum/digest helpers."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

PACKAGE_JSON = "package.json"
CHECKSUMS_JSON = "checksums.json"
SIGNATURE_JSON = "signature.json"
DEPENDENCY_MANIFEST = "dependency-manifest.json"
BLOB_REFERENCES = "blob-references.json"
APPLICATION_DIR = "application"
MIGRATIONS_DIR = "migrations"
WHEELS_DIR = "wheels"

FORMAT_VERSION = 1

# Files excluded from checksums.json (checksums cannot cover itself; the
# signature is computed *from* checksums, so it is covered indirectly).
_UNCHECKED = {CHECKSUMS_JSON, SIGNATURE_JSON}

_CHUNK = 1024 * 1024


def sha256_file(path: Path) -> str:
    digester = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(_CHUNK):
            digester.update(chunk)
    return digester.hexdigest()


def compute_checksums(root: Path) -> dict[str, str]:
    """Map every packaged file (posix relpath) → sha256, excluding meta files."""
    files: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if rel in _UNCHECKED:
            continue
        files[rel] = sha256_file(path)
    return files


def canonical_digest(checksums: dict[str, str]) -> str:
    """Deterministic sha256 over the checksum set — what the signature commits to."""
    payload = json.dumps(checksums, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
