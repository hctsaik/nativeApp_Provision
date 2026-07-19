"""裝置端安裝器（device_payload + release 內的 install 流程）。

「接到別人的電腦」那段的契約：
- 平台 release 自帶 install.bat / tools / lib / trusted_publishers.json。
- 目標機跑 device_install.py：首次＝安裝並**釘住**發行者清單（TOFU）、
  再跑新 release＝更新（user data 不動）、啟動走安裝根的 bin\\。
- 攻擊者換掉新 release 裡的簽章與信任清單也沒用——驗的是釘住那份。

全部用 subprocess 跑 release 裡實際出貨的腳本（不是 import），
證明它在「只有 release 資料夾」的世界裡自足。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from provision_builder.blob_store import FileBlobStore
from provision_builder.napp import AppManifest, build_napp
from provision_builder.napp.signing import Ed25519Signer
from provision_builder.napp.trust import (add_to_trust_store, generate_keypair,
                                          sign_napp, trust_entry)
from provision_builder.platform_store import build_platform_napp
from provision_builder.release_pipeline import build_release

SHELL_BYTES = b"MZ-fake-shell " * 200


def make_platform(tmp_path: Path, marker: str) -> Path:
    root = tmp_path / f"platform-{marker}"
    engine = root / "sidecar" / "python-engine"
    engine.mkdir(parents=True)
    (engine / "engine.py").write_text(f"MARKER = '{marker}'\n", encoding="utf-8")
    shell = root / "apps" / "host-tauri" / "prebuilt"
    shell.mkdir(parents=True)
    (shell / "cim-light.exe").write_bytes(SHELL_BYTES)
    return root


def _signer(tmp_path: Path, key_id: str = "fab-team") -> tuple[Ed25519Signer, Path]:
    doc = generate_keypair(key_id)
    signer = Ed25519Signer(bytes.fromhex(doc["private_seed"]), key_id)
    store = tmp_path / f"trust-{key_id}.json"
    add_to_trust_store(store, trust_entry(signer))
    return signer, store


def make_release(tmp_path: Path, marker: str, version: str, release_id: str,
                 signer: Ed25519Signer, trust_store: Path) -> Path:
    blobs = FileBlobStore(tmp_path / "blobstore")
    platform = make_platform(tmp_path, marker)
    napp = build_platform_napp(platform, version,
                               tmp_path / f"cim-platform-{version}-{marker}.napp",
                               blob_store=blobs)
    sign_napp(napp.path, signer)
    release = build_release(tmp_path / "releases", [napp.path], channel="production",
                            release_id=release_id, blob_root=tmp_path / "blobstore",
                            verifier=_verifier_for(trust_store),
                            trust_store_file=trust_store)
    return release.path


def _verifier_for(trust_store: Path):
    from provision_builder.napp.trust import load_trust_store
    return load_trust_store(trust_store)


def run_install(release_dir: Path, root: Path,
                desktop: Path | None = None) -> subprocess.CompletedProcess:
    import os

    # 剔除 PYTHONPATH：安裝器必須只靠 release 內的 tools\lib 自足
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env["PYTHONIOENCODING"] = "utf-8"
    env["CIM_DESKTOP_DIR"] = str(desktop) if desktop else str(release_dir / "no-desktop")
    return subprocess.run(
        [sys.executable, str(release_dir / "tools" / "device_install.py"),
         "--root", str(root)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", env=env,
    )


# ---------------------------------------------------------------------------
# release 形狀
# ---------------------------------------------------------------------------

def test_platform_release_carries_installer_and_trust(tmp_path: Path) -> None:
    signer, trust = _signer(tmp_path)
    release = make_release(tmp_path, "v1", "1.0.0", "r1", signer, trust)
    assert (release / "install.bat").is_file()
    assert (release / "tools" / "device_install.py").is_file()
    assert (release / "tools" / "lib" / "native_agent" / "agent.py").is_file()
    assert (release / "tools" / "lib" / "provision_builder" / "napp" / "ed25519.py").is_file()
    assert (release / "trusted_publishers.json").is_file()
    # 安裝器也在 checksums 覆蓋範圍內（release 完整性包含它）
    listed = (release / "checksums.sha256").read_text(encoding="utf-8")
    assert "install.bat" in listed and "tools/device_install.py" in listed


def test_non_platform_release_has_no_installer(tmp_path: Path) -> None:
    src = tmp_path / "src-app"
    src.mkdir()
    (src / "app.py").write_text("x = 1\n", encoding="utf-8")
    manifest = AppManifest.from_dict({"id": "cv-viewer", "version": "1.0.0"})
    napp = tmp_path / "cv-viewer-1.0.0.napp"
    build_napp(manifest, src, napp, source_commit="x")
    release = build_release(tmp_path / "releases", [napp], release_id="r1")
    assert not (release.path / "install.bat").exists()
    assert not (release.path / "tools").exists()


# ---------------------------------------------------------------------------
# 安裝 → 更新 → TOFU（全走出貨腳本的 subprocess）
# ---------------------------------------------------------------------------

def test_install_update_and_tofu_pinning(tmp_path: Path) -> None:
    signer, trust = _signer(tmp_path)
    root = tmp_path / "target-machine"

    # ── 首次安裝：釘住發行者 ──
    desktop = tmp_path / "fake-desktop"
    desktop.mkdir()
    release1 = make_release(tmp_path, "v1", "1.0.0", "r1", signer, trust)
    result = run_install(release1, root, desktop=desktop)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "首次安裝" in result.stdout and "釘住" in result.stdout
    # 桌面捷徑（小白救星）：單行 bat 指向安裝根的啟動器
    shortcut = desktop / "啟動 CIM 平台.bat"
    assert shortcut.is_file()
    assert str(root / "bin" / "start-platform.bat") in shortcut.read_text(encoding="utf-8")
    pinned = root / "trusted_publishers.json"
    assert pinned.is_file()
    active = json.loads((root / "applications" / "cim-platform" / "active.json")
                        .read_text(encoding="utf-8"))
    assert active["version"] == "1.0.0"
    assert (root / "bin" / "start-platform.bat").is_file()
    assert (root / "bin" / "launch_platform.py").is_file()

    # 啟動器（bin\，不依賴 release 資料夾）dry-run 可解析
    launch = subprocess.run(
        [sys.executable, str(root / "bin" / "launch_platform.py"), "--dry-run"],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert launch.returncode == 0, launch.stdout + launch.stderr
    plan = json.loads(launch.stdout)
    assert plan["version"] == "1.0.0"

    # 使用者資料落地（更新後必須原封不動）
    user_file = Path(plan["cwd"]) / "logs" / "precious.log"
    user_file.parent.mkdir(parents=True, exist_ok=True)
    user_file.write_text("現場資料", encoding="utf-8")
    pinned_bytes = pinned.read_bytes()

    # ── 更新：拿新 release 再跑同一個動作 ──
    release2 = make_release(tmp_path, "v2", "1.1.0", "r2", signer, trust)
    result = run_install(release2, root)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "首次安裝" not in result.stdout            # 不重複釘
    active = json.loads((root / "applications" / "cim-platform" / "active.json")
                        .read_text(encoding="utf-8"))
    assert active["version"] == "1.1.0"
    assert user_file.read_text(encoding="utf-8") == "現場資料"   # data 不動
    assert pinned.read_bytes() == pinned_bytes                    # 釘住的清單不動

    # ── TOFU：攻擊者用自己的鑰簽新版、連信任清單一起換掉 ──
    attacker, attacker_trust = _signer(tmp_path, "attacker")
    evil = make_release(tmp_path, "evil", "9.9.9", "r-evil", attacker, attacker_trust)
    result = run_install(evil, root)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "untrusted" in result.stdout or "SKIPPED" in result.stdout \
        or "FAILED" in result.stdout
    active = json.loads((root / "applications" / "cim-platform" / "active.json")
                        .read_text(encoding="utf-8"))
    assert active["version"] == "1.1.0"               # 裝置不動如山
    assert pinned.read_bytes() == pinned_bytes        # 釘住的清單沒被換


def test_install_refuses_release_without_trust_when_unpinned(tmp_path: Path) -> None:
    """release 沒帶信任清單、裝置也沒釘過 → 明確拒絕（不是靜默裝成未驗證）。"""
    signer, trust = _signer(tmp_path)
    release = make_release(tmp_path, "v1", "1.0.0", "r1", signer, trust)
    (release / "trusted_publishers.json").unlink()
    result = run_install(release, tmp_path / "target")
    assert result.returncode == 2
    assert "信任清單" in result.stdout