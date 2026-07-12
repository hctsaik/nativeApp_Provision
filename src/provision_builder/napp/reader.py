"""Verify and install a ``.napp`` (Slice 4).

Verification recomputes every packaged file's sha256 against ``checksums.json``,
re-derives the canonical digest and (when a verifier is supplied) checks the
detached signature. ``install_source`` extracts ``application/`` into a version
staging directory only after verification passes — it never activates anything;
that is the Native_App agent's job (Slice 7).
"""

from __future__ import annotations

import hashlib
import json
import zipfile
from dataclasses import dataclass
from pathlib import Path

from provision_builder.napp import _layout as L
from provision_builder.napp.errors import InvalidManifest, SignatureInvalid
from provision_builder.napp.signing import SignatureBundle, Verifier
from provision_builder.package_errors import ArtifactCorrupted, HashMismatch


@dataclass
class NappContents:
    package: dict
    checksums: dict[str, str]
    canonical_digest: str
    blob_references: list[dict]
    signature: SignatureBundle | None


def _read_json(zf: zipfile.ZipFile, name: str) -> dict:
    try:
        return json.loads(zf.read(name).decode("utf-8"))
    except KeyError as exc:
        raise ArtifactCorrupted(f"missing {name} in package") from exc
    except (ValueError, UnicodeDecodeError) as exc:
        raise ArtifactCorrupted(f"unreadable {name}: {exc}") from exc


def read_package_json(napp_path: Path | str) -> dict:
    with zipfile.ZipFile(napp_path) as zf:
        return _read_json(zf, L.PACKAGE_JSON)


def verify_napp(napp_path: Path | str, *, verifier: Verifier | None = None) -> NappContents:
    with zipfile.ZipFile(napp_path) as zf:
        names = set(zf.namelist())
        package = _read_json(zf, L.PACKAGE_JSON)
        checksums_doc = _read_json(zf, L.CHECKSUMS_JSON)
        files = checksums_doc.get("files", {})

        # 1. every declared file present with the right hash
        for rel, expected in files.items():
            if rel not in names:
                raise ArtifactCorrupted(f"file listed in checksums missing from package: {rel}")
            actual = hashlib.sha256(zf.read(rel)).hexdigest()
            if actual != expected:
                raise HashMismatch(f"checksum mismatch for {rel}")

        # 2. no unlisted payload smuggled in (meta files are exempt)
        exempt = {L.CHECKSUMS_JSON, L.SIGNATURE_JSON}
        for name in names:
            if name.endswith("/") or name in exempt:
                continue
            if name not in files:
                raise ArtifactCorrupted(f"unlisted file in package: {name}")

        # 3. canonical digest recomputed from the checksum set
        digest = L.canonical_digest(files)
        declared = checksums_doc.get("canonical_digest")
        if declared is not None and declared != digest:
            raise HashMismatch("canonical digest does not match checksum set")

        # 4. signature (optional presence, mandatory validity if a verifier is given)
        signature = None
        if L.SIGNATURE_JSON in names:
            signature = SignatureBundle.from_json(_read_json(zf, L.SIGNATURE_JSON))
        if verifier is not None:
            if signature is None:
                raise SignatureInvalid("package is not signed but a verifier was required")
            verifier.verify(digest, signature)

        blob_refs = _read_json(zf, L.BLOB_REFERENCES).get("blobs", []) if L.BLOB_REFERENCES in names else []
        return NappContents(package, files, digest, blob_refs, signature)


def install_source(
    napp_path: Path | str,
    dest_dir: Path | str,
    *,
    verifier: Verifier | None = None,
) -> NappContents:
    """Verify then extract application source and its offline dependency payload."""
    contents = verify_napp(napp_path, verifier=verifier)
    dest_dir = Path(dest_dir)
    if dest_dir.exists() and any(dest_dir.iterdir()):
        raise InvalidManifest(f"install destination is not empty: {dest_dir}")
    dest_dir.mkdir(parents=True, exist_ok=True)
    prefix = L.APPLICATION_DIR + "/"
    with zipfile.ZipFile(napp_path) as zf:
        members = [n for n in zf.namelist() if n.startswith(prefix) and not n.endswith("/")]
        if not members:
            raise ArtifactCorrupted("package has no application/ payload")
        for name in members:
            rel = name[len(prefix):]
            target = dest_dir / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(zf.read(name))
        for name in zf.namelist():
            if name == L.DEPENDENCY_MANIFEST or name.startswith(L.WHEELS_DIR + "/"):
                if name.endswith("/"):
                    continue
                target = dest_dir / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(zf.read(name))
    return contents
