"""CIM 平台本身的 Store 化——phase 1：平台變成一個 ``.napp``（B-2）。

nativeApp 的可攜部署（start.bat + engine\\ 整夾替換）是「咬住檔案」風險最後的
殘留點。phase 1 不另寫第三套 updater：**平台就是 Native Agent 底下的一個 app**
（app_id = ``cim-platform``），因此沿用已經過測的整條鏈——

    build_platform_napp()  →  release.py build（P0 通道）
      →  NativeAgent.update("cim-platform", channel)   ← 下載/驗證/不可變版本/切換/回滾
        →  native_agent.platform_launcher              ← 讀 active.json、照 start.bat 契約起殼

payload = ``engine/``（sidecar/python-engine 的內容形狀，濾掉衍生物）；
17MB 的 Tauri 殼以 content-addressed blob 旅行（不進 .napp），
``shell.blobref.json`` 留在 payload 裡讓 launcher 找得到它。

**Phase 1 刻意不含**：可攜 Python runtime 的版本化（launcher 沿用跑它的那顆直譯器，
= start.bat 的 runtime\\python311 慣例）；portal dist 的獨立版本化（跟殼走，
與 start.bat 行為一致）。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from provision_builder._util import sha256_file
from provision_builder.blob_store import FileBlobStore
from provision_builder.napp import AppManifest, NappBuildResult, build_napp

PLATFORM_APP_ID = "cim-platform"
SHELL_BLOB_NAME = "cim-light.exe"
SHELL_BLOBREF = "shell.blobref.json"

# 打包時「排除」（builder 面對的是活的開發樹，殘渣是常態，不是錯誤——
# 這點與 release gate 的 fail-loud 不同，那裡面對的是宣稱乾淨的交付輸入）。
_EXCLUDE_DIRS_ANY = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
                     ".git", ".venv", "node_modules"}
_EXCLUDE_DIRS_ROOT = {".tool-venvs", ".deppack-cache", ".wheel-store", "logs", "tmp"}
_EXCLUDE_SUFFIXES = {".pyc", ".pyo", ".log"}
_EXCLUDE_NAMES = {"tools.sqlite"}


class PlatformPackError(Exception):
    """The platform tree cannot be packaged; message says what to fix."""


def _git_commit(repo: Path) -> str:
    try:
        out = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                             capture_output=True, text=True, timeout=15)
        return out.stdout.strip() if out.returncode == 0 else ""
    except OSError:
        return ""


def _copy_engine_tree(engine_root: Path, dest: Path) -> int:
    copied = 0
    for base, dirs, files in os.walk(engine_root):
        rel_base = Path(base).relative_to(engine_root)
        keep = []
        for name in sorted(dirs):
            if name in _EXCLUDE_DIRS_ANY:
                continue
            if rel_base == Path(".") and name in _EXCLUDE_DIRS_ROOT:
                continue
            keep.append(name)
        dirs[:] = keep
        for name in sorted(files):
            if name in _EXCLUDE_NAMES or Path(name).suffix.lower() in _EXCLUDE_SUFFIXES:
                continue
            source = Path(base) / name
            target = dest / rel_base / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            copied += 1
    if copied == 0:
        raise PlatformPackError(f"engine 樹是空的:{engine_root}")
    return copied


def build_platform_napp(
    platform_root: Path | str,
    version: str,
    out_path: Path | str,
    *,
    blob_store: FileBlobStore,
    shell_exe: Path | str | None = None,
    signer=None,
    work_dir: Path | str | None = None,
) -> NappBuildResult:
    """Package a CIM platform working tree as the ``cim-platform`` app.

    ``platform_root`` is the nativeApp repo/deploy root. The engine payload is
    ``sidecar/python-engine`` (or ``engine/`` in a portable tree); the shell is
    the EXISTING prebuilt ``cim-light.exe`` — never rebuilt here (WDAC).
    """
    platform_root = Path(platform_root)
    engine_root = platform_root / "sidecar" / "python-engine"
    if not (engine_root / "engine.py").is_file():
        engine_root = platform_root / "engine"
    if not (engine_root / "engine.py").is_file():
        raise PlatformPackError(
            f"這不是 CIM 平台專案:{platform_root}\n"
            "  找不到 sidecar\\python-engine\\engine.py（或 engine\\engine.py）")

    shell = Path(shell_exe) if shell_exe else \
        platform_root / "apps" / "host-tauri" / "prebuilt" / "cim-light.exe"
    if not shell.is_file():
        raise PlatformPackError(
            f"找不到 Tauri 殼:{shell}\n"
            "  在非 WDAC 機器跑 scripts\\win\\build-shell.bat 產生後複製就位，"
            "或用 shell_exe 參數指定")

    staging = Path(tempfile.mkdtemp(prefix=".platform-napp-",
                                    dir=str(work_dir) if work_dir else None))
    try:
        _copy_engine_tree(engine_root, staging / "engine")
        # 殼以 blob 旅行;payload 裡留下指標,launcher 據此 materialize。
        digest, size = sha256_file(shell)
        (staging / SHELL_BLOBREF).write_text(
            json.dumps({"schema": 1, "name": SHELL_BLOB_NAME, "sha256": digest, "size": size},
                       indent=2),
            encoding="utf-8",
        )
        manifest = AppManifest.from_dict({
            "id": PLATFORM_APP_ID,
            "version": version,
            "entrypoint": "engine/engine.py",
        })
        return build_napp(
            manifest, staging, out_path,
            big_deps={SHELL_BLOB_NAME: shell},
            blob_store=blob_store,
            signer=signer,
            source_commit=_git_commit(platform_root),
        )
    finally:
        shutil.rmtree(staging, ignore_errors=True)
