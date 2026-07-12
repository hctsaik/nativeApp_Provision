from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest

from provision_builder.blob_store import FileBlobStore
from provision_builder.napp import (
    AppManifest,
    DevHmacSigner,
    build_napp,
    install_source,
    read_package_json,
    verify_napp,
)
from provision_builder.napp.errors import InvalidManifest, SignatureInvalid
from provision_builder.package_errors import HashMismatch

APP_JSON = {
    "id": "cv-reviewer",
    "version": "1.4.2",
    "entrypoint": "app:main",
    "category": "review",
    "requires": ["numpy==1.26.0"],
    "healthcheck": {"cmd": "python -c 'import app'"},
}


@pytest.fixture
def app_source(tmp_path: Path) -> Path:
    root = tmp_path / "src_app"
    (root / "pkg").mkdir(parents=True)
    (root / "app.py").write_text("def main():\n    return 'ok'\n", encoding="utf-8")
    (root / "pkg" / "util.py").write_text("VALUE = 1\n", encoding="utf-8")
    return root


def _build(tmp_path: Path, app_source: Path, *, signer=None, big_deps=None, blob_store=None):
    manifest = AppManifest.from_dict(APP_JSON)
    out = tmp_path / "cv-reviewer-1.4.2.napp"
    return build_napp(
        manifest, app_source, out,
        signer=signer, big_deps=big_deps, blob_store=blob_store,
        source_commit="deadbeef",
    )


def test_build_and_verify_roundtrip(tmp_path: Path, app_source: Path) -> None:
    result = _build(tmp_path, app_source, signer=DevHmacSigner())
    assert result.path.is_file()
    package = read_package_json(result.path)
    assert package["app_id"] == "cv-reviewer" and package["version"] == "1.4.2"
    assert package["artifact"]["source_files"] == 2
    contents = verify_napp(result.path, verifier=DevHmacSigner())
    assert contents.signature is not None
    assert contents.canonical_digest == result.canonical_digest


def test_big_deps_offloaded_to_blob_store_not_embedded(tmp_path: Path, app_source: Path) -> None:
    blobs = FileBlobStore(tmp_path / "blobs")
    big = tmp_path / "torch.whl"
    big.write_bytes(b"pretend 2GB wheel")
    result = _build(tmp_path, app_source, signer=DevHmacSigner(),
                    big_deps={"torch.whl": big}, blob_store=blobs)
    assert len(result.blob_references) == 1
    ref = result.blob_references[0]
    assert blobs.has(ref["sha256"])  # body is in the blob store …
    with zipfile.ZipFile(result.path) as zf:
        names = zf.namelist()
    assert not any(n.endswith("torch.whl") for n in names)  # … not in the .napp


def test_source_only_change_keeps_dependency_fingerprint(tmp_path: Path, app_source: Path) -> None:
    first = _build(tmp_path, app_source)
    (app_source / "app.py").write_text("def main():\n    return 'changed'\n", encoding="utf-8")
    second = build_napp(AppManifest.from_dict(APP_JSON), app_source, tmp_path / "v2.napp")
    assert first.package["dependency_fingerprint"] == second.package["dependency_fingerprint"]


def test_install_source_extracts_after_verify(tmp_path: Path, app_source: Path) -> None:
    result = _build(tmp_path, app_source, signer=DevHmacSigner())
    dest = tmp_path / "versions" / "1.4.2.staging"
    install_source(result.path, dest, verifier=DevHmacSigner())
    assert (dest / "app.py").read_text(encoding="utf-8").startswith("def main")
    assert (dest / "pkg" / "util.py").read_text(encoding="utf-8") == "VALUE = 1\n"


def test_tampered_file_fails_verification(tmp_path: Path, app_source: Path) -> None:
    result = _build(tmp_path, app_source, signer=DevHmacSigner())
    _rewrite_zip_member(result.path, "application/app.py", b"def main():\n    return 'evil'\n")
    with pytest.raises(HashMismatch):
        verify_napp(result.path, verifier=DevHmacSigner())


def test_wrong_key_fails_signature(tmp_path: Path, app_source: Path) -> None:
    result = _build(tmp_path, app_source, signer=DevHmacSigner(secret=b"publisher-key"))
    with pytest.raises(SignatureInvalid):
        verify_napp(result.path, verifier=DevHmacSigner(secret=b"attacker-key"))


def test_unsigned_package_rejected_when_verifier_required(tmp_path: Path, app_source: Path) -> None:
    result = _build(tmp_path, app_source)  # no signer
    verify_napp(result.path)  # ok without a verifier
    with pytest.raises(SignatureInvalid):
        verify_napp(result.path, verifier=DevHmacSigner())


def test_key_rotation_trust_store(tmp_path: Path, app_source: Path) -> None:
    from provision_builder.napp import MultiKeyVerifier

    k1, k2 = DevHmacSigner(secret=b"key-one", key_id="k1"), DevHmacSigner(secret=b"key-two", key_id="k2")
    result = _build(tmp_path, app_source, signer=k1)  # signed by k1

    trust = MultiKeyVerifier({"k1": k1, "k2": k2})
    verify_napp(result.path, verifier=trust)          # both keys trusted → ok

    trust.retire("k1")                                 # rotate k1 out
    with pytest.raises(SignatureInvalid):
        verify_napp(result.path, verifier=trust)       # k1 no longer trusted


def test_untrusted_key_id_rejected(tmp_path: Path, app_source: Path) -> None:
    from provision_builder.napp import MultiKeyVerifier

    signer = DevHmacSigner(secret=b"rogue", key_id="rogue")
    result = _build(tmp_path, app_source, signer=signer)
    trust = MultiKeyVerifier({"k1": DevHmacSigner(secret=b"key-one", key_id="k1")})
    with pytest.raises(SignatureInvalid):
        verify_napp(result.path, verifier=trust)


def test_invalid_manifest_rejected() -> None:
    with pytest.raises(InvalidManifest):
        AppManifest.from_dict({"version": "1.0.0"})  # no identity


def test_entrypoint_is_optional_launch_hint() -> None:
    manifest = AppManifest.from_dict({"id": "app-ai4bi", "version": "1.0.0"})
    assert manifest.entrypoint == ""
    with pytest.raises(InvalidManifest):
        AppManifest.from_dict({"id": "bad id", "version": "1.0.0", "entrypoint": "x"})


def _rewrite_zip_member(zip_path: Path, member: str, data: bytes) -> None:
    tmp = zip_path.with_suffix(".rebuilt")
    with zipfile.ZipFile(zip_path) as src, zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as dst:
        for item in src.namelist():
            dst.writestr(item, data if item == member else src.read(item))
    tmp.replace(zip_path)
