"""平台 release 的裝置端安裝器（B-2 phase 1.5：接到別人的電腦）。

「複製資料夾＝部署」的最後一哩：平台 release 資料夾**自帶安裝器**——
目標機雙擊 ``install.bat`` 即安裝；之後拿新的 release 再雙擊同一顆＝更新
（安裝就是第一次更新，同一個動作；user data 永不被觸碰）。啟動用安裝根目錄的
``bin\\start-platform.bat``。

信任模型（金鑰對使用者隱形）：release 內隨附發行者公鑰清單
``trusted_publishers.json``；目標機**首次安裝時把它釘進安裝根目錄**（TOFU），
之後每一次更新都用「釘住的那份」驗章——攻擊者即使替換了新 release 裡的
清單與簽章也過不了關。發布端金鑰由 GUI 自動產生管理，兩端都不需要人碰金鑰。

出貨的 ``tools\\lib\\`` 是 native_agent + provision_builder 必要子集的原始碼
副本（純 Python、stdlib-only 執行），由本模組在 build_release 時複製——
目標機不需要 clone 任何 repo。
"""

from __future__ import annotations

import shutil
from pathlib import Path

_SRC_ROOT = Path(__file__).resolve().parents[1]          # …/src
_REPO_ROOT = _SRC_ROOT.parent

TRUST_STORE_NAME = "trusted_publishers.json"

_PB_FILES = ("__init__.py", "blob_store.py", "package_errors.py", "package_services.py",
             "winfs.py", "_util.py")

_INSTALL_BAT = """@echo off
rem Install or update the CIM platform from THIS release folder.
rem First run = install; run the same file from a newer release = update.
rem Pure ASCII on purpose (CP950 consoles mis-parse non-ASCII bat lines).
setlocal
cd /d "%~dp0"
set "PY="
if exist "%LOCALAPPDATA%\\CIM-Platform\\runtime\\python311\\python.exe" set "PY=%LOCALAPPDATA%\\CIM-Platform\\runtime\\python311\\python.exe"
if not defined PY where py >nul 2>nul && set "PY=py -3.11"
if not defined PY set "PY=python"
%PY% "tools\\device_install.py" %*
rem Always pause: the operator must be able to READ the result line
rem ("UPDATED" or a failure) before the window disappears.
echo.
pause
endlocal
"""

_START_BAT = """@echo off
rem Start the installed CIM platform (reads active.json; cold-start switch).
setlocal
cd /d "%~dp0"
set "PY="
if exist "..\\runtime\\python311\\python.exe" set "PY=..\\runtime\\python311\\python.exe"
if not defined PY where py >nul 2>nul && set "PY=py -3.11"
if not defined PY set "PY=python"
%PY% "launch_platform.py" %*
if errorlevel 1 pause
endlocal
"""

_DEVICE_INSTALL = '''#!/usr/bin/env python3
"""Install/update the CIM platform from the release folder this script ships in.

First run = install (pins the publisher trust store to the install root);
later runs from a newer release = update (verified against the PINNED store).
User data is never touched. Usage:

    install.bat [--root DIR]     (predefined; double-click works)
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

RELEASE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RELEASE_ROOT / "tools" / "lib"))

from native_agent import NativeAgent                      # noqa: E402
from native_agent.file_remote import FileChannelRemote    # noqa: E402
from provision_builder.napp.trust import load_trust_store  # noqa: E402

TRUST_STORE_NAME = "trusted_publishers.json"
APP_ID = "cim-platform"


def main() -> int:
    parser = argparse.ArgumentParser(prog="device_install")
    parser.add_argument("--root", type=Path,
                        default=Path(os.environ.get("CIM_PLATFORM_ROOT",
                                     Path(os.environ.get("LOCALAPPDATA", Path.home()))
                                     / "CIM-Platform")))
    args = parser.parse_args()
    root = args.root

    channel_index = RELEASE_ROOT / "offline-channel" / "channel.json"
    if not channel_index.is_file():
        print("[FAIL] 這不是平台 release 資料夾（缺 offline-channel\\\\channel.json）。")
        return 2
    channel = json.loads(channel_index.read_text(encoding="utf-8"))["channel"]

    shipped_trust = RELEASE_ROOT / TRUST_STORE_NAME
    pinned_trust = root / TRUST_STORE_NAME
    if not pinned_trust.is_file():
        if not shipped_trust.is_file():
            print("[FAIL] release 缺發行者信任清單，無法安裝（重新產一份 release）。")
            return 2
        root.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(shipped_trust, pinned_trust)
        print(f"[install] 首次安裝：發行者已釘住（{pinned_trust}）")
    verifier = load_trust_store(pinned_trust)  # 永遠用釘住的，不信任更新包自帶的

    remote = FileChannelRemote(RELEASE_ROOT / "offline-channel")
    agent = NativeAgent(root, remote, remote.blobs, verifier=verifier)
    outcome = agent.update(APP_ID, channel)
    print(f"[install] 結果:{outcome.state}  啟用版本:{outcome.active}")
    if outcome.error:
        print(f"[install] 訊息:{outcome.error}")
    if outcome.state in ("FAILED", "SKIPPED_FAILED", "SKIPPED_YANKED"):
        return 1
    if outcome.state == "START_CACHED" and outcome.active is None:
        return 1

    # bin\\：讓啟動不依賴 release 資料夾（可以拔走 USB）
    bin_dir = root / "bin"
    lib_src = RELEASE_ROOT / "tools" / "lib"
    lib_dst = bin_dir / "lib"
    if lib_dst.exists():
        shutil.rmtree(lib_dst)
    shutil.copytree(lib_src, lib_dst)
    shutil.copyfile(RELEASE_ROOT / "tools" / "launch_platform.py",
                    bin_dir / "launch_platform.py")
    shutil.copyfile(RELEASE_ROOT / "tools" / "start-platform.bat",
                    bin_dir / "start-platform.bat")

    # 桌面啟動捷徑(小白驗證的 B2:%LOCALAPPDATA% 一般人找不到)。
    # 用一支單行 .bat 代替 .lnk(stdlib 就能建);best-effort,失敗不影響安裝。
    desktop = Path(os.environ.get("CIM_DESKTOP_DIR", "") or
                   Path(os.environ.get("USERPROFILE", Path.home())) / "Desktop")
    if not desktop.is_dir():
        onedrive = os.environ.get("OneDrive")
        if onedrive and (Path(onedrive) / "Desktop").is_dir():
            desktop = Path(onedrive) / "Desktop"
    shortcut = None
    if desktop.is_dir():
        try:
            shortcut = desktop / "啟動 CIM 平台.bat"
            shortcut.write_text(
                "@echo off\\r\\nstart \\"\\" \\"" + str(bin_dir / "start-platform.bat")
                + "\\"\\r\\n", encoding="utf-8")
        except OSError:
            shortcut = None
    print(f"[install] 完成。啟動:{bin_dir / 'start-platform.bat'}")
    if shortcut is not None:
        print(f"[install] 桌面已放好捷徑:「{shortcut.name}」(以後雙擊它啟動)")
    print("[install] 之後更新:拿新的 release 資料夾,雙擊裡面的 install.bat 即可(資料不動)。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''

_LAUNCH_PLATFORM = '''#!/usr/bin/env python3
"""Start the installed CIM platform (thin wrapper over platform_launcher)."""
from __future__ import annotations

import sys
from pathlib import Path

BIN_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BIN_DIR / "lib"))

from native_agent.platform_launcher import main  # noqa: E402

if __name__ == "__main__":
    argv = ["--root", str(BIN_DIR.parent)] + sys.argv[1:]
    raise SystemExit(main(argv))
'''


def write_device_tools(release_root: Path | str, trust_store: Path | str | None) -> None:
    """把安裝器、啟動器、程式庫子集與信任清單放進 release（build 時呼叫）。"""
    release_root = Path(release_root)
    tools = release_root / "tools"
    lib = tools / "lib"

    pb_dst = lib / "provision_builder"
    pb_dst.mkdir(parents=True)
    for name in _PB_FILES:
        shutil.copyfile(_SRC_ROOT / "provision_builder" / name, pb_dst / name)
    shutil.copytree(_SRC_ROOT / "provision_builder" / "napp", pb_dst / "napp",
                    ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(_REPO_ROOT / "native_agent", lib / "native_agent",
                    ignore=shutil.ignore_patterns("__pycache__"))

    (tools / "device_install.py").write_text(_DEVICE_INSTALL, encoding="utf-8")
    (tools / "launch_platform.py").write_text(_LAUNCH_PLATFORM, encoding="utf-8")
    (tools / "start-platform.bat").write_text(_START_BAT, encoding="utf-8", newline="\r\n")
    (release_root / "install.bat").write_text(_INSTALL_BAT, encoding="utf-8", newline="\r\n")

    if trust_store is not None and Path(trust_store).is_file():
        shutil.copyfile(trust_store, release_root / TRUST_STORE_NAME)
