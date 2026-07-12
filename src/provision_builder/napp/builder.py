"""Assemble a ``.napp`` from an application source tree (Slice 4).

Big dependencies are offloaded to a content-addressed blob store and recorded as
sha256 references — their bytes never enter the ``.napp``. A source-only change
therefore keeps the same ``dependency_fingerprint`` and reuses every blob.
"""

from __future__ import annotations

import io
import json
import shutil
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from provision_builder.blob_store import FileBlobStore
from provision_builder.napp import _layout as L
from provision_builder.napp.errors import InvalidManifest
from provision_builder.napp.manifest import AppManifest, validate_package_manifest
from provision_builder.napp.signing import Signer, sign_digest


@dataclass
class NappBuildResult:
    path: Path
    package: dict
    checksums: dict[str, str]
    canonical_digest: str
    blob_references: list[dict]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _copy_source(app_source: Path, staged_app: Path) -> int:
    if not app_source.is_dir():
        raise InvalidManifest(f"source_root is not a directory: {app_source}")
    count = 0
    for item in sorted(app_source.rglob("*")):
        if item.is_dir():
            continue
        rel = item.relative_to(app_source)
        target = staged_app / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(item, target)
        count += 1
    if count == 0:
        raise InvalidManifest(f"no source files under {app_source}")
    return count


def _stage_big_deps(big_deps: dict[str, Path], blobs: FileBlobStore | None) -> list[dict]:
    if not big_deps:
        return []
    if blobs is None:
        raise InvalidManifest("big_deps given but no blob store to offload them into")
    references: list[dict] = []
    for name, path in sorted(big_deps.items()):
        digest, size = blobs.put_file(path)
        references.append({"name": name, "sha256": digest, "size": size})
    return references


def build_napp(
    manifest: AppManifest,
    app_source: Path | str,
    out_path: Path | str,
    *,
    dependency_manifest: dict | None = None,
    dependency_wheels_dir: Path | str | None = None,
    dependency_fingerprint: str | None = None,
    big_deps: dict[str, Path] | None = None,
    blob_store: FileBlobStore | None = None,
    migrations_dir: Path | str | None = None,
    signer: Signer | None = None,
    source_commit: str = "",
    platform: dict | None = None,
    work_dir: Path | str | None = None,
) -> NappBuildResult:
    out_path = Path(out_path)
    app_source = Path(app_source)
    staging_parent = Path(work_dir) if work_dir else out_path.parent
    build_root = staging_parent / f".napp-build-{manifest.id}-{manifest.version}"
    if build_root.exists():
        shutil.rmtree(build_root)
    build_root.mkdir(parents=True)
    try:
        source_files = _copy_source(app_source, build_root / L.APPLICATION_DIR)

        dep_manifest = dependency_manifest or {"schema": 1, "requires": list(manifest.requires), "wheels": []}
        dep_bytes = json.dumps(dep_manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")
        (build_root / L.DEPENDENCY_MANIFEST).write_bytes(dep_bytes)
        import hashlib

        dependency_fingerprint = dependency_fingerprint or hashlib.sha256(dep_bytes).hexdigest()
        if dependency_wheels_dir is not None:
            wheels_source = Path(dependency_wheels_dir)
            if not wheels_source.is_dir():
                raise InvalidManifest(f"dependency wheels directory not found: {wheels_source}")
            shutil.copytree(wheels_source, build_root / L.WHEELS_DIR)

        references = _stage_big_deps(big_deps or {}, blob_store)
        (build_root / L.BLOB_REFERENCES).write_text(
            json.dumps({"blobs": references}, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        if migrations_dir is not None:
            src = Path(migrations_dir)
            if src.is_dir():
                shutil.copytree(src, build_root / L.MIGRATIONS_DIR)

        package = {
            "app_id": manifest.id,
            "version": manifest.version,
            "entrypoint": manifest.entrypoint,
            "category": manifest.category,
            "source_commit": source_commit,
            "dependency_fingerprint": dependency_fingerprint,
            "platform": platform or {"os": "windows", "arch": "amd64", "python": "3.11", "abi": "cp311"},
            "artifact": {
                "format_version": L.FORMAT_VERSION,
                "source_files": source_files,
                "blob_references": len(references),
            },
            "healthcheck": manifest.healthcheck,
            "created_at": _utc_now(),
        }
        validate_package_manifest(package)
        (build_root / L.PACKAGE_JSON).write_text(
            json.dumps(package, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        checksums = L.compute_checksums(build_root)
        digest = L.canonical_digest(checksums)
        (build_root / L.CHECKSUMS_JSON).write_text(
            json.dumps({"algorithm": "sha256", "canonical_digest": digest, "files": checksums},
                       ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        if signer is not None:
            bundle = sign_digest(signer, digest)
            (build_root / L.SIGNATURE_JSON).write_text(
                json.dumps(bundle.to_json(), ensure_ascii=False, indent=2), encoding="utf-8"
            )

        _zip_dir(build_root, out_path)
        return NappBuildResult(out_path, package, checksums, digest, references)
    finally:
        shutil.rmtree(build_root, ignore_errors=True)


def _zip_dir(root: Path, out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(out_path.suffix + ".tmp")
    with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(root.rglob("*")):
            if path.is_file():
                zf.write(path, path.relative_to(root).as_posix())
    tmp.replace(out_path)


# Convenience for tests / smoke: build straight from an in-memory manifest dict.
def build_from_app_json(app_json: dict, app_source: Path | str, out_path: Path | str, **kwargs) -> NappBuildResult:
    return build_napp(AppManifest.from_dict(app_json), app_source, out_path, **kwargs)


def bytes_source(data: bytes) -> io.BytesIO:
    return io.BytesIO(data)
