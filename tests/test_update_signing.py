"""P3.2：Store desktop 通道的發行者簽章（device/update_signing + sign_version_dir）。

威脅模型：能寫入 update source 的攻擊者可以重生一份「雜湊自洽」的
payload + files.json + release.json。簽章把「內容沒壞」升級成「內容是誰發的」。

驗的契約：
- 簽章對象 = files.json 的 canonical digest；改任何 payload byte 後**重生 files.json**
  仍會被抓（digest 與簽章不符）——這正是簽章存在的理由。
- require_signed_updates=true：未簽章／缺信任清單 → staging 直接拒絕，版本不落地。
- 未強制但信任清單在：未簽章放行（過渡期），**壞簽章永不放行**。
- 兩者皆未配置：no-op（既有裝置不受影響）。
- `--set-pending` 同受政策管（繞過 updater 的手動路徑不是後門）。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from provision_builder.napp.signing import Ed25519Signer
from provision_builder.napp.trust import add_to_trust_store, generate_keypair, trust_entry
from provision_builder.streamlit_desktop.device import integrity, update_signing
from provision_builder.streamlit_desktop.device.paths import MANIFEST_NAME, AppPaths
from provision_builder.streamlit_desktop.device.provider import FolderUpdateProvider
from provision_builder.streamlit_desktop.device import updater
from provision_builder.streamlit_desktop.store_builder import StoreBuildError, sign_version_dir

LOG = logging.getLogger("test-update-signing")
APP = "app-demo"
VERSION = "1.1.0"
FP = "cp311-testfp"


def _signer(key_id: str = "team-a") -> Ed25519Signer:
    doc = generate_keypair(key_id)
    return Ed25519Signer(bytes.fromhex(doc["private_seed"]), key_id)


def make_version_dir(parent: Path, *, version: str = VERSION, content: str = "print('v1')\n") -> Path:
    vdir = parent / version
    vdir.mkdir(parents=True)
    (vdir / "app.py").write_text(content, encoding="utf-8")
    (vdir / MANIFEST_NAME).write_text(json.dumps({
        "schema_version": 2, "app_id": APP, "version": version,
        "runtime_fingerprint": FP,
    }), encoding="utf-8")
    integrity.write_files_json(vdir)
    return vdir


def write_trust(path: Path, signer: Ed25519Signer, *, retired: bool = False) -> Path:
    add_to_trust_store(path, trust_entry(signer, retired=retired))
    return path


# ---------------------------------------------------------------------------
# 單元：sign_version_dir + check_version_signature
# ---------------------------------------------------------------------------

def test_sign_and_verify_roundtrip(tmp_path: Path) -> None:
    vdir = make_version_dir(tmp_path)
    signer = _signer()
    bundle = sign_version_dir(vdir, signer)
    assert bundle["algorithm"] == "ed25519"
    trust = write_trust(tmp_path / "trusted_publishers.json", signer)
    outcome = update_signing.check_version_signature(
        vdir, config={"require_signed_updates": True}, trust_path=trust)
    assert outcome == "verified"
    # files.json 完全不受影響（signature.json 是 integrity 豁免的 meta 檔）
    assert integrity.verify_tree(vdir) == []


def test_sign_refuses_double_signing(tmp_path: Path) -> None:
    vdir = make_version_dir(tmp_path)
    sign_version_dir(vdir, _signer())
    with pytest.raises(StoreBuildError, match="已有發行者簽章"):
        sign_version_dir(vdir, _signer("team-b"))


def test_rehashed_payload_is_caught(tmp_path: Path) -> None:
    """攻擊者改 payload 後「重生 files.json」讓逐檔驗證通過——簽章必須抓到。"""
    vdir = make_version_dir(tmp_path)
    signer = _signer()
    sign_version_dir(vdir, signer)
    trust = write_trust(tmp_path / "trusted_publishers.json", signer)

    (vdir / "app.py").write_text("print('evil')\n", encoding="utf-8")
    integrity.write_files_json(vdir)          # 攻擊者能重生 manifest…
    assert integrity.verify_tree(vdir) == []  # …逐檔驗證因此照樣通過
    with pytest.raises(update_signing.SignaturePolicyError, match="簽章之後被改過"):
        update_signing.check_version_signature(
            vdir, config={}, trust_path=trust)  # …但簽章對象變了


def test_untrusted_key_rejected_and_retired_still_verifies(tmp_path: Path) -> None:
    vdir = make_version_dir(tmp_path)
    signer = _signer("old-key")
    sign_version_dir(vdir, signer)

    retired = write_trust(tmp_path / "retired.json", signer, retired=True)
    assert update_signing.check_version_signature(
        vdir, config={}, trust_path=retired) == "verified"  # retired 仍可驗舊版

    other = write_trust(tmp_path / "other.json", _signer("new-key"))
    with pytest.raises(update_signing.SignaturePolicyError, match="不在這台機器的信任清單"):
        update_signing.check_version_signature(vdir, config={}, trust_path=other)


def test_policy_matrix(tmp_path: Path) -> None:
    unsigned = make_version_dir(tmp_path / "a")
    signer = _signer()
    trust = write_trust(tmp_path / "trusted_publishers.json", signer)

    # 皆未配置 → no-op
    assert update_signing.check_version_signature(
        unsigned, config={}, trust_path=tmp_path / "absent.json") == "not-configured"
    # 信任清單在、未強制、未簽章 → 過渡期放行
    assert update_signing.check_version_signature(
        unsigned, config={}, trust_path=trust) == "unsigned-allowed"
    # 強制 + 未簽章 → 拒絕
    with pytest.raises(update_signing.SignaturePolicyError, match="沒有發行者簽章"):
        update_signing.check_version_signature(
            unsigned, config={"require_signed_updates": True}, trust_path=trust)
    # 強制 + 缺信任清單 → 設定錯誤（可行動訊息）
    with pytest.raises(update_signing.SignaturePolicyError, match="找不到信任清單"):
        update_signing.check_version_signature(
            unsigned, config={"require_signed_updates": True},
            trust_path=tmp_path / "absent.json")


def test_garbage_signature_never_accepted_even_when_not_required(tmp_path: Path) -> None:
    vdir = make_version_dir(tmp_path)
    signer = _signer()
    trust = write_trust(tmp_path / "trusted_publishers.json", signer)
    digest = update_signing.version_digest(integrity.load_files_json(vdir))
    (vdir / update_signing.SIGNATURE_NAME).write_text(json.dumps({
        "algorithm": "ed25519", "key_id": "team-a",
        "canonical_digest": digest, "signature": "ab" * 64,
    }), encoding="utf-8")
    with pytest.raises(update_signing.SignaturePolicyError, match="驗證失敗"):
        update_signing.check_version_signature(vdir, config={}, trust_path=trust)


# ---------------------------------------------------------------------------
# 整合：updater._stage_version（背景更新與 --install 共用的那條路）
# ---------------------------------------------------------------------------

def _device(tmp_path: Path, *, require: bool, signer: Ed25519Signer | None) -> AppPaths:
    root = tmp_path / "device"
    paths = AppPaths(root, APP)
    paths.app_dir.mkdir(parents=True)
    paths.deps_dir.mkdir(parents=True)
    if require:
        (paths.app_dir / "config.json").write_text(
            json.dumps({"require_signed_updates": True}), encoding="utf-8")
    if signer is not None:
        write_trust(paths.app_dir / update_signing.TRUST_STORE_NAME, signer)
    return paths


def _payload(tmp_path: Path, *, sign_with: Ed25519Signer | None) -> Path:
    payload_app = tmp_path / "payload" / APP
    vdir = make_version_dir(payload_app / "versions")
    if sign_with is not None:
        sign_version_dir(vdir, sign_with)
    (payload_app / "release.json").write_text(json.dumps({
        "schema": 1, "app_id": APP, "version": VERSION,
        "revision": "rev-1", "runtime_fingerprint": FP, "shell_fingerprint": None,
    }), encoding="utf-8")
    return payload_app


def test_stage_version_rejects_unsigned_when_required(tmp_path: Path) -> None:
    signer = _signer()
    paths = _device(tmp_path, require=True, signer=signer)
    payload = _payload(tmp_path, sign_with=None)
    provider = FolderUpdateProvider.from_payload_dir(payload)
    release = provider.get_latest_release(APP, None)

    with pytest.raises(updater.UpdateError, match="發行者簽章檢查未過"):
        updater._stage_version(paths, provider, release, LOG)
    assert not paths.version_dir(VERSION).exists()      # 沒落地
    assert not any(paths.staging_dir.iterdir()) if paths.staging_dir.is_dir() else True


def test_stage_version_accepts_signed(tmp_path: Path) -> None:
    signer = _signer()
    paths = _device(tmp_path, require=True, signer=signer)
    payload = _payload(tmp_path, sign_with=signer)
    provider = FolderUpdateProvider.from_payload_dir(payload)
    release = provider.get_latest_release(APP, None)

    updater._stage_version(paths, provider, release, LOG)
    installed = paths.version_dir(VERSION)
    assert integrity.is_complete(installed)
    # 簽章隨版本落地，之後 --set-pending 的複驗才有東西可驗
    assert (installed / update_signing.SIGNATURE_NAME).is_file()


def test_stage_version_untouched_devices_keep_working(tmp_path: Path) -> None:
    """沒配置簽章政策的既有裝置：未簽章 payload 照常安裝（向後相容）。"""
    paths = _device(tmp_path, require=False, signer=None)
    payload = _payload(tmp_path, sign_with=None)
    provider = FolderUpdateProvider.from_payload_dir(payload)
    release = provider.get_latest_release(APP, None)
    updater._stage_version(paths, provider, release, LOG)
    assert integrity.is_complete(paths.version_dir(VERSION))
