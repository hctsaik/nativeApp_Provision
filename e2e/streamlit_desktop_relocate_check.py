"""Copy the delivered package to an awkward path and prove it still starts.

Spaces and CJK in the path are where naive launchers break (quoting, encoding).
We run the launcher with --no-shell so this stays a headless check of the half
that matters here: manifest resolution, port pick, Streamlit spawn, health.

    py -3.11 e2e\\streamlit_desktop_relocate_check.py [package_dir]
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PKG = ROOT / "dist" / "streamlit-apps" / "portable-streamlit-smoke"
TARGET = ROOT / "dist" / "relocate check 中文 資料夾" / "portable-streamlit-smoke"


def main() -> int:
    source = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PKG
    if not (source / "app-package.json").is_file():
        print(f"[relocate] 找不到交付包:{source}")
        return 2

    if TARGET.exists():
        shutil.rmtree(TARGET, ignore_errors=True)
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    print(f"[relocate] 複製到:{TARGET}")
    shutil.copytree(source, TARGET)

    proc = subprocess.Popen(
        [str(TARGET / "runtime" / "python.exe"), str(TARGET / "launcher" / "launch.py"), "--no-shell"],
        cwd=str(TARGET), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace",
    )
    url = None
    try:
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            line = proc.stdout.readline()
            if not line:
                break
            print("  " + line.rstrip())
            if "ready at http" in line:
                url = line.split("ready at ")[-1].strip()
                break
        if not url:
            print("[relocate] FAILED — launcher 沒有回報 ready")
            return 1

        with urllib.request.urlopen(url + "/_stcore/health", timeout=5) as resp:
            healthy = resp.status == 200
        print(f"[relocate] health {url} -> {resp.status}")
        print("[relocate] PASS — 含空白與中文的路徑可正常啟動" if healthy else "[relocate] FAILED")
        return 0 if healthy else 1
    finally:
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, check=False)
        shutil.rmtree(TARGET.parent, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
