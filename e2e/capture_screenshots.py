"""Capture step-by-step screenshots of the real Console + Device Portal.

Starts the lab in-process (no ports collide with a running lab), drives the GUI
with Playwright, and saves numbered PNGs — the source images for the visual
"看圖照做" guide. Re-run after any Console/Portal change to refresh the guide.

    py -3.11 -m pip install playwright   &&   py -3.11 -m playwright install chromium
    py -3.11 e2e/capture_screenshots.py [output_dir]

Playwright + Chromium are a dev/CI dependency (network for the browser download);
the rest of the project stays stdlib-only.
"""

from __future__ import annotations

import shutil
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]

import importlib.util

_spec = importlib.util.spec_from_file_location("lab_serve", ROOT / "demo" / "lab_serve.py")
lab = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lab)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    out = Path(argv[0]) if argv else ROOT / "e2e" / "screenshots"
    out.mkdir(parents=True, exist_ok=True)
    for old in out.glob("*.png"):
        old.unlink()

    lab_root = out / "_labstate"
    if lab_root.exists():
        shutil.rmtree(lab_root)
    cp, web, portal = 8380, 8390, 8391
    _api, _console, _portal, cps, webs, ps = lab.build_lab(lab_root, cp_port=cp, web_port=web, portal_port=portal)
    for s in (cps, webs, ps):
        threading.Thread(target=s.serve_forever, daemon=True).start()
    time.sleep(0.5)

    console, device = f"http://127.0.0.1:{web}", f"http://127.0.0.1:{portal}"
    app = f"{console}/applications/cv-reviewer"

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("playwright not installed — see the header of this file", file=sys.stderr)
        return 2

    def full(page, name):
        page.screenshot(path=str(out / f"{name}.png"), full_page=True); print("shot", name)

    def fieldset(page, legend, name):
        page.locator(f"fieldset:has-text('{legend}')").first.screenshot(path=str(out / f"{name}.png")); print("shot", name)

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1200, "height": 820})

        page.goto(console); page.wait_for_load_state("networkidle"); full(page, "01_console_home")
        page.goto(app); page.wait_for_load_state("networkidle"); full(page, "02_app_overview")

        bf = page.locator("form[action='/applications/cv-reviewer/build']")
        bf.locator("input[name=version]").fill("2.0.0")
        bf.locator("select[name=channel]").select_option("")
        fieldset(page, "Build & publish", "03_build_form")
        bf.locator("button").click(); page.wait_for_load_state("networkidle"); full(page, "04_after_build")

        pf = page.locator("form[action='/applications/cv-reviewer/promote']")
        pf.locator("select[name=version]").select_option("2.0.0")
        pf.locator("select[name=channel]").select_option("production")
        fieldset(page, "Promote", "05_promote_form")
        pf.locator("button").click(); page.wait_for_load_state("networkidle"); full(page, "06_after_promote")

        rf = page.locator("form[action='/applications/cv-reviewer/rollout']")
        rf.locator("select[name=version]").select_option("2.0.0")
        rf.locator("input[name=stage_percent]").fill("10")
        fieldset(page, "Start rollout", "07_rollout_start")
        rf.locator("button").click(); page.wait_for_load_state("networkidle")
        af = page.locator("form[action='/applications/cv-reviewer/rollout/advance']")
        af.locator("input[name=stage_percent]").fill("50")
        af.locator("button").click(); page.wait_for_load_state("networkidle"); full(page, "08_rollout_controls")

        df = page.locator("form[action='/applications/cv-reviewer/device']")
        df.locator("input[name=device_id]").fill("device-42")
        fieldset(page, "Register device", "09_register_device")

        page.goto(device); page.wait_for_load_state("networkidle"); full(page, "10_portal_available")
        page.locator("form[action='/update'] button").click(); page.wait_for_load_state("networkidle")
        full(page, "11_portal_updated")

        browser.close()

    for s in (cps, webs, ps):
        s.shutdown(); s.server_close()
    print(f"done → {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
