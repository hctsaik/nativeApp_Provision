"""P3：trust store、napp 補簽、release promote（internal → production）。

驗的契約：
- keygen/sign/trust store 全流程能讓 production channel 出貨且裝置端可驗。
- retired 金鑰仍可驗舊包（ADR 0001 §4）；不在 store 的 key_id 一律不信。
- promote 是「同一批 bytes 換通道全程重驗」：未簽章的內部包晉升 production 必失敗，
  晉升成功時 manifest 記 promoted_from。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import run_python
from provision_builder.napp import AppManifest, build_napp, verify_napp
from provision_builder.napp.errors import SignatureInvalid
from provision_builder.napp.signing import Ed25519Signer
from provision_builder.napp.trust import (
    TrustStoreError,
    add_to_trust_store,
    generate_keypair,
    load_private_key,
    load_trust_store,
    sign_napp,
    trust_entry,
)
from provision_builder.release_pipeline import (
    ReleaseError,
    build_release,
    promote_release,
    verify_release,
)

CLI = Path(__file__).resolve().parents[1] / "release.py"


def make_unsigned_napp(tmp_path: Path, app_id: str = "cv-viewer", version: str = "1.0.0") -> Path:
    src = tmp_path / f"src-{app_id}-{version}"
    src.mkdir(parents=True, exist_ok=True)
    (src / "app.py").write_text(f"VERSION = '{version}'\n", encoding="utf-8")
    manifest = AppManifest.from_dict({"id": app_id, "version": version, "entrypoint": "app:main"})
    out = tmp_path / f"{app_id}-{version}.napp"
    build_napp(manifest, src, out, source_commit="deadbeef")
    return out


def _signer(tmp_path: Path, key_id: str = "team-a") -> Ed25519Signer:
    doc = generate_keypair(key_id)
    key_file = tmp_path / f"{key_id}.private.json"
    key_file.write_text(json.dumps(doc), encoding="utf-8")
    return load_private_key(key_file)


# ---------------------------------------------------------------------------
# trust store 與補簽
# ---------------------------------------------------------------------------

def test_sign_napp_then_verify_with_trust_store(tmp_path: Path) -> None:
    napp = make_unsigned_napp(tmp_path)
    signer = _signer(tmp_path)
    store_path = tmp_path / "trusted_publishers.json"
    add_to_trust_store(store_path, trust_entry(signer))

    bundle = sign_napp(napp, signer)
    assert bundle.algorithm == "ed25519"
    contents = verify_napp(napp, verifier=load_trust_store(store_path))
    assert contents.signature is not None and contents.signature.key_id == "team-a"


def test_sign_refuses_double_signing(tmp_path: Path) -> None:
    napp = make_unsigned_napp(tmp_path)
    signer = _signer(tmp_path)
    sign_napp(napp, signer)
    with pytest.raises(SignatureInvalid, match="已有簽章"):
        sign_napp(napp, signer)


def test_retired_key_still_verifies_untrusted_key_rejected(tmp_path: Path) -> None:
    napp = make_unsigned_napp(tmp_path)
    signer = _signer(tmp_path, "old-key")
    sign_napp(napp, signer)
    store_path = tmp_path / "store.json"
    add_to_trust_store(store_path, trust_entry(signer, retired=True))

    verify_napp(napp, verifier=load_trust_store(store_path))  # retired 仍可驗舊包

    other_store = tmp_path / "other.json"
    add_to_trust_store(other_store, trust_entry(_signer(tmp_path, "new-key")))
    with pytest.raises(SignatureInvalid, match="untrusted"):
        verify_napp(napp, verifier=load_trust_store(other_store))  # 撤銷 = 移除 = 不信


def test_tampered_private_key_file_rejected(tmp_path: Path) -> None:
    doc = generate_keypair("team-a")
    doc["public_key"] = "00" * 32  # 檔案被動過手腳
    key_file = tmp_path / "k.json"
    key_file.write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(TrustStoreError, match="不符"):
        load_private_key(key_file)


def test_trust_store_rejects_duplicate_key_id(tmp_path: Path) -> None:
    store = tmp_path / "store.json"
    signer = _signer(tmp_path)
    add_to_trust_store(store, trust_entry(signer))
    with pytest.raises(TrustStoreError, match="已有 key_id"):
        add_to_trust_store(store, trust_entry(signer))


# ---------------------------------------------------------------------------
# production build 與 promote
# ---------------------------------------------------------------------------

def _trusted(tmp_path: Path):
    signer = _signer(tmp_path)
    store_path = tmp_path / "trusted_publishers.json"
    add_to_trust_store(store_path, trust_entry(signer))
    return signer, load_trust_store(store_path)


def test_production_build_with_ed25519(tmp_path: Path) -> None:
    signer, verifier = _trusted(tmp_path)
    napp = make_unsigned_napp(tmp_path)
    sign_napp(napp, signer)
    result = build_release(tmp_path / "out", [napp], channel="production",
                           release_id="r1", verifier=verifier)
    assert verify_release(result.path, verifier=verifier) == []


def test_promote_internal_to_production(tmp_path: Path) -> None:
    signer, verifier = _trusted(tmp_path)
    napp = make_unsigned_napp(tmp_path)
    sign_napp(napp, signer)
    internal = build_release(tmp_path / "out", [napp], channel="internal", release_id="int-1")

    result = promote_release(internal.path, tmp_path / "out",
                             to_channel="production", release_id="prod-1", verifier=verifier)
    assert result.channel == "production"
    assert result.manifest["promoted_from"] == "int-1"
    assert verify_release(result.path, verifier=verifier) == []
    disk = json.loads((result.path / "release-manifest.json").read_text(encoding="utf-8"))
    assert disk["promoted_from"] == "int-1"


def test_promote_unsigned_to_production_fails(tmp_path: Path) -> None:
    _, verifier = _trusted(tmp_path)
    napp = make_unsigned_napp(tmp_path)  # 沒簽章
    internal = build_release(tmp_path / "out", [napp], channel="internal", release_id="int-1")
    with pytest.raises(ReleaseError, match="未簽章"):
        promote_release(internal.path, tmp_path / "out",
                        to_channel="production", release_id="prod-1", verifier=verifier)
    assert not (tmp_path / "out" / "prod-1").exists()


def test_promote_tampered_source_fails(tmp_path: Path) -> None:
    signer, verifier = _trusted(tmp_path)
    napp = make_unsigned_napp(tmp_path)
    sign_napp(napp, signer)
    internal = build_release(tmp_path / "out", [napp], channel="internal", release_id="int-1")
    sbom = internal.path / "SBOM.json"
    sbom.write_text(sbom.read_text(encoding="utf-8") + " ", encoding="utf-8")
    with pytest.raises(ReleaseError, match="不能晉升"):
        promote_release(internal.path, tmp_path / "out",
                        to_channel="production", release_id="prod-1", verifier=verifier)


def test_promote_carries_blobs_and_extras(tmp_path: Path) -> None:
    from provision_builder.blob_store import FileBlobStore

    signer, verifier = _trusted(tmp_path)
    blobs = FileBlobStore(tmp_path / "blobstore")
    big = tmp_path / "model.bin"
    big.write_bytes(b"weights" * 500)
    src = tmp_path / "src-app"
    src.mkdir()
    (src / "app.py").write_text("VERSION='1.0.0'\n", encoding="utf-8")
    manifest = AppManifest.from_dict({"id": "cv-viewer", "version": "1.0.0", "entrypoint": "app:main"})
    napp = tmp_path / "cv-viewer-1.0.0.napp"
    build_napp(manifest, src, napp, big_deps={"model.bin": big}, blob_store=blobs,
               source_commit="x")
    sign_napp(napp, signer)
    extra = tmp_path / "docs"
    extra.mkdir()
    (extra / "readme.txt").write_text("hi", encoding="utf-8")

    internal = build_release(tmp_path / "out", [napp], channel="internal", release_id="int-1",
                             blob_root=tmp_path / "blobstore", extras={"docs": extra})
    result = promote_release(internal.path, tmp_path / "out",
                             to_channel="production", release_id="prod-1", verifier=verifier)
    assert (result.path / "extras" / "docs" / "readme.txt").is_file()
    assert result.manifest["blobs"] and verify_release(result.path, verifier=verifier) == []


# ---------------------------------------------------------------------------
# CLI 全流程
# ---------------------------------------------------------------------------

def test_cli_keygen_sign_build_promote(tmp_path: Path) -> None:
    napp = make_unsigned_napp(tmp_path)
    key = tmp_path / "team-a.private.json"
    store = tmp_path / "trusted_publishers.json"

    kg = run_python([str(CLI), "keygen", "--key-id", "team-a",
                     "--out", str(key), "--trust-store", str(store)])
    assert kg.returncode == 0, kg.stdout + kg.stderr
    kg2 = run_python([str(CLI), "keygen", "--key-id", "x", "--out", str(key)])
    assert kg2.returncode == 2 and "拒絕覆蓋" in kg2.stdout

    signed = run_python([str(CLI), "sign", str(napp), "--key", str(key)])
    assert signed.returncode == 0 and "已簽章" in signed.stdout

    out = tmp_path / "out"
    built = run_python([str(CLI), "build", "--out", str(out), "--napp", str(napp),
                        "--channel", "internal", "--release-id", "int-1"])
    assert built.returncode == 0, built.stdout + built.stderr

    promoted = run_python([str(CLI), "promote", str(out / "int-1"),
                           "--to-channel", "production", "--out", str(out),
                           "--release-id", "prod-1", "--trust-store", str(store)])
    assert promoted.returncode == 0, promoted.stdout + promoted.stderr
    assert "已晉升" in promoted.stdout

    ok = run_python([str(CLI), "verify", str(out / "prod-1"), "--trust-store", str(store)])
    assert ok.returncode == 0 and "可出貨" in ok.stdout
    no_key = run_python([str(CLI), "verify", str(out / "prod-1")])
    assert no_key.returncode == 1
