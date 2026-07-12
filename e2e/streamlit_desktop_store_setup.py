"""Prepare a store-layout deployment for the real WebView2 E2E.

Builds two versions whose ONLY difference is a visible string, so the driver can
prove — from inside the Tauri window — that a restart really switched versions.
v1.1.0 is copied into the tree but left un-pending: the driver promotes it the
way an admin would (`bootstrap.py --set-pending`).

    py -3.11 e2e\\streamlit_desktop_store_setup.py
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from provision_builder.streamlit_desktop import store_builder  # noqa: E402
from provision_builder.streamlit_desktop.device import integrity  # noqa: E402
from provision_builder.streamlit_desktop.models import BuildRequest  # noqa: E402

FIXTURE = ROOT / "e2e" / "fixtures" / "portable-streamlit-smoke"
WORK = ROOT / "dist" / "streamlit-store-webview"
BUILD, DEPLOY = WORK / "build", WORK / "deploy"
APP = "app-portable-streamlit-smoke"


def request_for(label: str) -> BuildRequest:
    (FIXTURE / "app.py").write_text(
        "import streamlit as st\n"
        "st.set_page_config(page_title='Portable Streamlit smoke test', layout='wide')\n"
        "st.title('Portable Streamlit smoke test')\n"
        f"st.write('READY {label}')\n"
        f"st.caption('這個視窗正在執行版本 {label}(store 佈局,共用 runtime)。')\n",
        encoding="utf-8")
    return BuildRequest(
        project_dir=FIXTURE, entrypoint=FIXTURE / "app.py",
        display_name="Portable Streamlit Smoke", output_dir=WORK / "unused",
        shell_exe=Path(r"C:\code\claude\nativeApp\apps\host-tauri\prebuilt\cim-light.exe"),
        runtime_template=ROOT / ".runtime-cache" / "python311",
    )


def main() -> int:
    if WORK.exists():
        shutil.rmtree(WORK)
    WORK.mkdir(parents=True)

    first = store_builder.build_into_store(request_for("v1.0.0"), BUILD, version="v1.0.0",
                                           progress=lambda m: print(f"[build] {m}", flush=True))
    if not first.ok:
        print(f"[setup] FAILED {first.errors}")
        return 1

    # USB-style deployment: the runtime sentinel does NOT travel — the device
    # earns it by verifying every byte on first start.
    shutil.copytree(BUILD, DEPLOY)
    integrity.remove_complete(DEPLOY / "deps" / "runtimes" / first.fingerprint)

    second = store_builder.build_into_store(request_for("v1.1.0"), BUILD, version="v1.1.0",
                                            progress=lambda m: print(f"[build] {m}", flush=True))
    if not second.ok:
        print(f"[setup] FAILED {second.errors}")
        return 1
    # Ship the new version into the deployment, but leave state alone: the
    # driver will do the promote as an operator would.
    shutil.copytree(BUILD / "apps" / APP / "versions" / "v1.1.0",
                    DEPLOY / "apps" / APP / "versions" / "v1.1.0")

    info = {
        "deploy": str(DEPLOY),
        "app_id": APP,
        "fingerprint": first.fingerprint,
        "runtime_reused_for_v1_1_0": second.runtime_reused,
    }
    (WORK / "setup.json").write_text(json.dumps(info, ensure_ascii=False, indent=2),
                                     encoding="utf-8")
    print(json.dumps(info, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
