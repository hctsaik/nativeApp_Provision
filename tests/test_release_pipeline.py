"""P0 Release Pipeline：唯一交付來源 + 防誤包 gate（release_pipeline.py / release.py）。

驗的是「機器可驗的出貨契約」：
- 輸出永遠是全新目錄；歷史輸出不可就地增補。
- gate 拒絕開發殘留（fail loud，不是靜默排除）。
- production channel 拒絕未簽章／驗不過章的 artifact。
- verify 抓得出：改一個 byte、多一個檔、少一個檔、channel.json 不一致、blob 損壞。
- offline-channel 可直接被 native_agent 的 FileChannelRemote 消費（不發明第三種格式）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import run_python
from native_agent.file_remote import FileChannelRemote
from provision_builder import release_pipeline
from provision_builder.blob_store import FileBlobStore
from provision_builder.napp import AppManifest, DevHmacSigner, build_napp
from provision_builder.release_pipeline import (
    ReleaseError,
    build_release,
    scan_payload_tree,
    verify_release,
)

CLI = Path(__file__).resolve().parents[1] / "release.py"


def make_napp(tmp_path: Path, app_id: str = "cv-viewer", version: str = "1.0.0", *,
              signer=None, big_deps=None, blob_store=None, filename: str | None = None) -> Path:
    src = tmp_path / f"src-{app_id}-{version}"
    src.mkdir(parents=True, exist_ok=True)
    (src / "app.py").write_text(f"VERSION = '{version}'\n", encoding="utf-8")
    manifest = AppManifest.from_dict({
        "id": app_id, "version": version, "entrypoint": "app:main",
        "requires": ["numpy==1.26.0"],
    })
    out = tmp_path / (filename or f"{app_id}-{version}.napp")
    build_napp(manifest, src, out, signer=signer,
               big_deps=big_deps, blob_store=blob_store, source_commit="deadbeef")
    return out


# ---------------------------------------------------------------------------
# happy path
# ---------------------------------------------------------------------------

def test_build_and_verify_roundtrip(tmp_path: Path) -> None:
    blobs = FileBlobStore(tmp_path / "blobstore")
    big = tmp_path / "torch.whl"
    big.write_bytes(b"pretend big wheel" * 100)
    napp_a = make_napp(tmp_path, "cv-viewer", "1.2.0",
                       big_deps={"torch.whl": big}, blob_store=blobs,
                       filename="weird-name.napp")  # 輸入檔名不影響出貨檔名
    napp_b = make_napp(tmp_path, "ai4bi", "2.0.0")

    result = build_release(
        tmp_path / "out", [napp_a, napp_b],
        channel="internal", release_id="r1",
        blob_root=tmp_path / "blobstore",
        extras={"docs": _clean_extra(tmp_path)},
    )
    root = result.path
    assert root == tmp_path / "out" / "r1"
    # 佈局：契約規定的每個檔案都在
    assert (root / "offline-channel" / "artifacts" / "cv-viewer-1.2.0.napp").is_file()
    assert (root / "offline-channel" / "artifacts" / "ai4bi-2.0.0.napp").is_file()
    assert (root / "offline-channel" / "channel.json").is_file()
    assert (root / "release-manifest.json").is_file()
    assert (root / "SBOM.json").is_file()
    assert (root / "RELEASE-REPORT.md").is_file()
    assert (root / "checksums.sha256").is_file()
    assert (root / "extras" / "docs" / "readme.txt").is_file()
    # staging 不殘留
    assert not list((tmp_path / "out").glob(".release-staging-*"))
    # manifest totals 落在磁碟上（不是只在回傳值）
    disk_manifest = json.loads((root / "release-manifest.json").read_text(encoding="utf-8"))
    assert disk_manifest["totals"] == result.manifest["totals"]
    assert disk_manifest["totals"]["files"] > 0
    # SBOM 誠實：宣告什麼列什麼
    sbom = json.loads((root / "SBOM.json").read_text(encoding="utf-8"))
    apps = {a["app_id"]: a for a in sbom["apps"]}
    assert apps["cv-viewer"]["requires"] == ["numpy==1.26.0"]
    assert len(apps["cv-viewer"]["blobs"]) == 1
    # 驗證通過
    assert verify_release(root) == []


def test_channel_consumable_by_native_agent(tmp_path: Path) -> None:
    """offline-channel 必須能直接被 FileChannelRemote 吃——不發明第三種格式。"""
    blobs = FileBlobStore(tmp_path / "blobstore")
    big = tmp_path / "model.bin"
    big.write_bytes(b"weights" * 1000)
    napp = make_napp(tmp_path, "cv-viewer", "1.2.0",
                     big_deps={"model.bin": big}, blob_store=blobs)
    result = build_release(tmp_path / "out", [napp], channel="internal",
                           release_id="r1", blob_root=tmp_path / "blobstore")

    remote = FileChannelRemote(result.path / "offline-channel")
    assert remote.list_applications() == ["cv-viewer"]
    release = remote.resolve("cv-viewer", "internal")
    assert release is not None and release.version == "1.2.0"
    with remote.open_artifact(release) as fh:
        assert len(fh.read()) == release.size_bytes
    digest = next(iter(remote.blobs.iter_digests()))
    remote.blobs.verify(digest)


def _clean_extra(tmp_path: Path) -> Path:
    extra = tmp_path / "extra-docs"
    extra.mkdir(exist_ok=True)
    (extra / "readme.txt").write_text("hi", encoding="utf-8")
    return extra


# ---------------------------------------------------------------------------
# 全新輸出目錄
# ---------------------------------------------------------------------------

def test_output_must_be_fresh(tmp_path: Path) -> None:
    napp = make_napp(tmp_path)
    build_release(tmp_path / "out", [napp], release_id="r1")
    before = sorted((tmp_path / "out" / "r1").rglob("*"))
    with pytest.raises(ReleaseError, match="全新目錄"):
        build_release(tmp_path / "out", [napp], release_id="r1")
    assert sorted((tmp_path / "out" / "r1").rglob("*")) == before  # 舊 release 一個 byte 都沒動


# ---------------------------------------------------------------------------
# 防誤包 gate
# ---------------------------------------------------------------------------

def test_gate_rejects_dev_residue_in_extras(tmp_path: Path) -> None:
    dirty = tmp_path / "payload"
    (dirty / "__pycache__").mkdir(parents=True)
    (dirty / "__pycache__" / "x.cpython-311.pyc").write_bytes(b"x")
    (dirty / "_run").mkdir()
    (dirty / "logs").mkdir()
    (dirty / "app.py").write_text("ok", encoding="utf-8")
    napp = make_napp(tmp_path)
    with pytest.raises(ReleaseError) as exc:
        build_release(tmp_path / "out", [napp], release_id="r1", extras={"tools": dirty})
    message = str(exc.value)
    assert "__pycache__" in message and "_run" in message and "logs" in message
    assert not (tmp_path / "out" / "r1").exists()
    assert not list((tmp_path / "out").glob(".release-staging-*")) if (tmp_path / "out").exists() else True


def test_gate_root_names_do_not_kill_nested_data(tmp_path: Path) -> None:
    """§17.4 的教訓：根層的 dist 是垃圾，巢狀的 frontend/dist 是元件本身。"""
    payload = tmp_path / "payload"
    (payload / "component" / "frontend" / "dist").mkdir(parents=True)
    (payload / "component" / "frontend" / "dist" / "bundle.js").write_text("js", encoding="utf-8")
    assert scan_payload_tree(payload) == []
    (payload / "dist").mkdir()
    problems = scan_payload_tree(payload)
    assert len(problems) == 1 and problems[0].startswith("dist")


def test_gate_prunes_flagged_dirs(tmp_path: Path) -> None:
    payload = tmp_path / "payload"
    cache = payload / "pkg" / "__pycache__"
    cache.mkdir(parents=True)
    for i in range(50):
        (cache / f"m{i}.pyc").write_bytes(b"x")
    problems = scan_payload_tree(payload)
    assert len(problems) == 1  # 一個目錄一行，不是 50 行 pyc


# ---------------------------------------------------------------------------
# 簽章政策
# ---------------------------------------------------------------------------

def test_production_rejects_unsigned(tmp_path: Path) -> None:
    napp = make_napp(tmp_path)  # 未簽章
    with pytest.raises(ReleaseError, match="未簽章|信任金鑰"):
        build_release(tmp_path / "out", [napp], channel="production",
                      release_id="r1", verifier=DevHmacSigner())


def test_production_requires_keys_even_for_signed(tmp_path: Path) -> None:
    napp = make_napp(tmp_path, signer=DevHmacSigner())
    with pytest.raises(ReleaseError, match="信任金鑰"):
        build_release(tmp_path / "out", [napp], channel="production", release_id="r1")


def test_production_signed_roundtrip_and_wrong_key(tmp_path: Path) -> None:
    good = DevHmacSigner(b"secret-A", "team-a")
    napp = make_napp(tmp_path, signer=good)
    result = build_release(tmp_path / "out", [napp], channel="production",
                           release_id="r1", verifier=good)
    assert verify_release(result.path, verifier=good) == []
    # 沒帶金鑰 → production 不能宣稱驗證通過
    problems = verify_release(result.path)
    assert any("信任金鑰" in p for p in problems)
    # 錯的金鑰 → build 直接拒絕
    with pytest.raises(ReleaseError, match="簽章驗證失敗|untrusted|key"):
        build_release(tmp_path / "out", [napp], channel="production",
                      release_id="r2", verifier=DevHmacSigner(b"secret-B", "team-b"))


def test_internal_still_rejects_invalid_signature(tmp_path: Path) -> None:
    """簽了但驗不過，internal 也不放行——壞簽章永遠不是可接受狀態。"""
    napp = make_napp(tmp_path, signer=DevHmacSigner(b"secret-A", "team-a"))
    with pytest.raises(ReleaseError, match="簽章驗證失敗"):
        build_release(tmp_path / "out", [napp], channel="internal",
                      release_id="r1", verifier=DevHmacSigner(b"secret-B", "team-a"))


# ---------------------------------------------------------------------------
# 輸入健全性
# ---------------------------------------------------------------------------

def test_duplicate_app_id_rejected(tmp_path: Path) -> None:
    one = make_napp(tmp_path, "cv-viewer", "1.0.0")
    two = make_napp(tmp_path, "cv-viewer", "1.1.0")
    with pytest.raises(ReleaseError, match="重複的 app"):
        build_release(tmp_path / "out", [one, two], release_id="r1")


def test_missing_blob_fails_actionably(tmp_path: Path) -> None:
    blobs = FileBlobStore(tmp_path / "blobstore")
    big = tmp_path / "torch.whl"
    big.write_bytes(b"big" * 100)
    napp = make_napp(tmp_path, big_deps={"torch.whl": big}, blob_store=blobs)

    with pytest.raises(ReleaseError, match="--blobs"):
        build_release(tmp_path / "out", [napp], release_id="r1")  # 忘了給 blob 來源

    digest = next(iter(blobs.iter_digests()))
    (blobs.prefix / digest).unlink()  # blob 被抽走
    with pytest.raises(ReleaseError) as exc:
        build_release(tmp_path / "out", [napp], release_id="r1",
                      blob_root=tmp_path / "blobstore")
    assert digest[:16] in str(exc.value) and "cv-viewer" in str(exc.value)
    assert not (tmp_path / "out" / "r1").exists()


def test_staging_cleaned_on_late_failure(tmp_path: Path, monkeypatch) -> None:
    napp = make_napp(tmp_path)

    def boom(*args, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(release_pipeline, "_write_checksums_and_report", boom)
    with pytest.raises(RuntimeError):
        build_release(tmp_path / "out", [napp], release_id="r1")
    assert not (tmp_path / "out" / "r1").exists()
    assert not list((tmp_path / "out").glob(".release-staging-*"))


# ---------------------------------------------------------------------------
# verify 抓竄改
# ---------------------------------------------------------------------------

def _built(tmp_path: Path) -> Path:
    napp = make_napp(tmp_path)
    return build_release(tmp_path / "out", [napp], release_id="r1").path


def test_verify_catches_modified_file(tmp_path: Path) -> None:
    root = _built(tmp_path)
    report = root / "RELEASE-REPORT.md"
    report.write_text(report.read_text(encoding="utf-8") + "tampered", encoding="utf-8")
    assert any("雜湊不符" in p for p in verify_release(root))


def test_verify_catches_stray_and_missing_files(tmp_path: Path) -> None:
    root = _built(tmp_path)
    (root / "extra.txt").write_text("smuggled", encoding="utf-8")
    problems = verify_release(root)
    assert any("未列入 manifest" in p and "extra.txt" in p for p in problems)
    (root / "extra.txt").unlink()
    (root / "SBOM.json").unlink()
    assert any("缺失" in p and "SBOM.json" in p for p in verify_release(root))


def test_verify_catches_channel_manifest_divergence(tmp_path: Path) -> None:
    root = _built(tmp_path)
    index_path = root / "offline-channel" / "channel.json"
    index = json.loads(index_path.read_text(encoding="utf-8"))
    index["releases"][0]["version"] = "9.9.9"
    index_path.write_text(json.dumps(index, indent=2), encoding="utf-8")
    problems = verify_release(root)
    assert any("channel.json 與 manifest 不一致" in p for p in problems)


def test_verify_catches_corrupted_blob(tmp_path: Path) -> None:
    blobs = FileBlobStore(tmp_path / "blobstore")
    big = tmp_path / "model.bin"
    big.write_bytes(b"weights" * 100)
    napp = make_napp(tmp_path, big_deps={"model.bin": big}, blob_store=blobs)
    root = build_release(tmp_path / "out", [napp], release_id="r1",
                         blob_root=tmp_path / "blobstore").path
    blob_path = next((root / "offline-channel" / "blobs" / "sha256").iterdir())
    # hardlink 可能與來源共身：先斷開再改，避免竄改到來源 store
    data = blob_path.read_bytes()
    blob_path.unlink()
    blob_path.write_bytes(data[:-1] + b"X")
    problems = verify_release(root)
    assert any("blob" in p and ("損壞" in p or "缺失" in p) for p in problems)


def test_verify_refuses_non_release_directory(tmp_path: Path) -> None:
    workspace = tmp_path / "dist"
    workspace.mkdir()
    (workspace / "something.napp").write_bytes(b"x")
    problems = verify_release(workspace)
    assert len(problems) == 1 and "不是 release 目錄" in problems[0]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def test_cli_build_verify_and_tamper(tmp_path: Path) -> None:
    napp = make_napp(tmp_path)
    out = tmp_path / "out"

    built = run_python([str(CLI), "build", "--out", str(out), "--napp", str(napp),
                        "--release-id", "r1"])
    assert built.returncode == 0, built.stdout + built.stderr
    assert "release 已建立" in built.stdout

    ok = run_python([str(CLI), "verify", str(out / "r1")])
    assert ok.returncode == 0 and "可出貨" in ok.stdout

    # 就地增補被拒
    again = run_python([str(CLI), "build", "--out", str(out), "--napp", str(napp),
                        "--release-id", "r1"])
    assert again.returncode == 2 and "全新目錄" in again.stdout

    # 竄改後 verify 非零
    target = out / "r1" / "SBOM.json"
    target.write_text(target.read_text(encoding="utf-8") + " ", encoding="utf-8")
    bad = run_python([str(CLI), "verify", str(out / "r1")])
    assert bad.returncode == 1 and "不可" in bad.stdout


def test_cli_production_flow(tmp_path: Path) -> None:
    napp = make_napp(tmp_path, signer=DevHmacSigner(b"s3cret", "rel-key"))
    out = tmp_path / "out"
    built = run_python([str(CLI), "build", "--out", str(out), "--napp", str(napp),
                        "--channel", "production", "--release-id", "r1",
                        "--trust", "rel-key:s3cret"])
    assert built.returncode == 0, built.stdout + built.stderr
    ok = run_python([str(CLI), "verify", str(out / "r1"), "--trust", "rel-key:s3cret"])
    assert ok.returncode == 0
    no_key = run_python([str(CLI), "verify", str(out / "r1")])
    assert no_key.returncode == 1 and "信任金鑰" in no_key.stdout
