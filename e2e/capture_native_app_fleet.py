"""Capture the real Native App -> Management Center -> Fleet journey.

This starts the local provision lab in-process, then delegates WebView2 control
to the Native App Playwright driver. No standalone Fleet browser is opened.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
NATIVE_APP = Path(os.environ.get("NATIVE_APP_REPO", r"C:\code\claude\nativeApp"))
sys.path.insert(0, str(ROOT))

from demo.lab_serve import build_lab  # noqa: E402


def main() -> int:
    output = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "e2e" / "native-app-fleet"
    output.mkdir(parents=True, exist_ok=True)
    driver = NATIVE_APP / "apps" / "host-tauri" / "e2e" / "capture-management-fleet.mjs"
    if not driver.is_file():
        raise FileNotFoundError(f"Native App Fleet driver not found: {driver}")

    cp_port, fleet_port, device_port = 8480, 8490, 8491
    _api, _console, _portal, *servers = build_lab(
        ROOT / ".lab-native-app", cp_port=cp_port, web_port=fleet_port, portal_port=device_port
    )
    for server in servers:
        threading.Thread(target=server.serve_forever, daemon=True).start()

    env = os.environ.copy()
    env["CIM_FLEET_CONSOLE_URL"] = f"http://127.0.0.1:{fleet_port}/"
    env["CIM_APPLICATIONS_URL"] = f"http://127.0.0.1:{device_port}/"
    env.setdefault("CIM_E2E_CDP_PORT", "9339")
    try:
        completed = subprocess.run(
            ["node", str(driver), str(output)], cwd=NATIVE_APP, env=env, check=False
        )
        return completed.returncode
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    raise SystemExit(main())
