"""files.json manifests and the .complete sentinel.

The install order is the whole safety story: stage → verify every byte →
move into place → write .complete LAST. Anything without the sentinel is
invisible to the rest of the system, so a yanked USB stick or a crash can
never produce a version that half-exists.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat as stat_mod
import tempfile
from pathlib import Path

if __package__:
    from .identifiers import is_safe_relpath
else:
    from identifiers import is_safe_relpath

FILES_NAME = "files.json"
SENTINEL = ".complete"
# Excluded from manifests: the manifest itself, the sentinel, and runtime.json
# (runtime metadata is written store-side after the payload manifest is fixed).
_EXCLUDED = {FILES_NAME, SENTINEL}
_CHUNK = 1024 * 1024


class IntegrityError(Exception):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def _is_reparse_point(path: Path) -> bool:
    if os.name != "nt":  # pragma: no cover
        return path.is_symlink()
    try:
        attrs = os.lstat(path).st_file_attributes
    except OSError:
        return False
    return bool(attrs & stat_mod.FILE_ATTRIBUTE_REPARSE_POINT)


def build_files_json(root: Path, *, extra_excluded: set[str] | None = None) -> dict:
    """Hash every file under root (except the metadata files themselves)."""
    root = Path(root)
    excluded = _EXCLUDED | (extra_excluded or set())
    entries = []
    for base, dirs, files in os.walk(root):
        base_path = Path(base)
        if _is_reparse_point(base_path):
            raise IntegrityError(f"reparse point in payload: {base_path}")
        for name in files:
            path = base_path / name
            rel = path.relative_to(root).as_posix()
            if rel in excluded:
                continue
            if _is_reparse_point(path):
                raise IntegrityError(f"reparse point in payload: {path}")
            entries.append({"path": rel, "size": path.stat().st_size, "sha256": _sha256(path)})
    entries.sort(key=lambda e: e["path"])
    return {"schema": 1, "files": entries}


def write_files_json(root: Path, manifest: dict | None = None) -> dict:
    manifest = manifest or build_files_json(root)
    (Path(root) / FILES_NAME).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


def load_files_json(root: Path) -> dict:
    path = Path(root) / FILES_NAME
    try:
        data = json.loads(path.read_text("utf-8"))
    except FileNotFoundError as exc:
        raise IntegrityError(f"missing {FILES_NAME}: {path}") from exc
    except ValueError as exc:
        raise IntegrityError(f"corrupt {FILES_NAME}: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("files"), list):
        raise IntegrityError(f"malformed {FILES_NAME}: {path}")
    return data


def verify_tree(root: Path, *, manifest: dict | None = None,
                extra_excluded: set[str] | None = None,
                progress=None) -> list[str]:
    """Every declared file present with the right bytes; nothing undeclared.
    Returns a list of problems — empty means the tree is exactly the manifest."""
    root = Path(root)
    manifest = manifest or load_files_json(root)
    excluded = _EXCLUDED | (extra_excluded or set())
    problems: list[str] = []

    declared: dict[str, dict] = {}
    for entry in manifest["files"]:
        rel = entry.get("path")
        if not is_safe_relpath(rel):
            problems.append(f"unsafe path in manifest: {rel!r}")
            continue
        declared[rel] = entry

    seen: set[str] = set()
    for base, _dirs, files in os.walk(root):
        for name in files:
            path = Path(base) / name
            rel = path.relative_to(root).as_posix()
            if rel in excluded:
                continue
            seen.add(rel)
            if _is_reparse_point(path):
                problems.append(f"reparse point: {rel}")
                continue
            entry = declared.get(rel)
            if entry is None:
                problems.append(f"undeclared file: {rel}")
                continue
            size = path.stat().st_size
            if size != entry["size"]:
                problems.append(f"size mismatch: {rel} ({size} != {entry['size']})")
                continue
            if _sha256(path) != entry["sha256"]:
                problems.append(f"hash mismatch: {rel}")
            if progress is not None:
                progress(rel)

    for rel in declared:
        if rel not in seen:
            problems.append(f"missing file: {rel}")
    return problems


# ── sentinel ─────────────────────────────────────────────────────────────────

def is_complete(root: Path) -> bool:
    return (Path(root) / SENTINEL).is_file()


def write_complete(root: Path) -> None:
    """Atomic: the sentinel appears whole or not at all."""
    root = Path(root)
    fd, tmp = tempfile.mkstemp(dir=root, prefix=".complete-", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write("ok\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, root / SENTINEL)


def remove_complete(root: Path) -> None:
    """First step of any deletion: without the sentinel the tree is invisible,
    so an interrupted delete fails closed instead of leaving a poisoned install."""
    try:
        os.remove(Path(root) / SENTINEL)
    except FileNotFoundError:
        pass
