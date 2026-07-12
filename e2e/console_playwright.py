"""Playwright E2E over the Web Console GUI — drives the whole flow through the
browser: list apps → open cv-reviewer → promote a version → start a rollout, and
asserts the rendered result each time.

Prerequisites (one-time; needs network for the browser download):
    py -3.11 -m pip install playwright
    py -3.11 -m playwright install chromium

Run the lab first (separate terminal), then this script:
    py -3.11 demo/lab_serve.py            # terminal 1  (serves :8090 / :8080)
    py -3.11 e2e/console_playwright.py    # terminal 2

Note: unlike the rest of this project, Playwright is a third-party dependency and
downloads a Chromium build, so it runs on a dev/CI machine, not the locked-down
offline box. The Console itself is plain server-rendered HTML, so any browser or
even `curl` can drive it — Playwright just makes the GUI assertions explicit.
"""

from __future__ import annotations

import sys

BASE = "http://127.0.0.1:8090"


def run() -> int:
    try:
        from playwright.sync_api import expect, sync_playwright
    except ImportError:
        print("playwright not installed — see the header of this file", file=sys.stderr)
        return 2

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()

        # 1. Applications index lists the app.
        page.goto(f"{BASE}/")
        expect(page.get_by_text("Applications")).to_be_visible()
        page.get_by_role("link", name="cv-reviewer").click()

        # 2. App page shows releases, channels, builds, rollout and action forms.
        expect(page.get_by_role("heading", name="cv-reviewer")).to_be_visible()
        expect(page.get_by_text("Releases")).to_be_visible()
        expect(page.get_by_text("Actions")).to_be_visible()

        # 3. Promote 1.1.0 to production via the form; page redirects back.
        promote = page.locator("form[action$='/promote']")
        promote.locator("select[name=version]").select_option("1.1.0")
        promote.locator("select[name=channel]").select_option("production")
        promote.locator("button").click()
        expect(page.get_by_role("cell", name="production")).to_be_visible()
        assert "1.1.0" in page.content()

        # 4. Start a 10% rollout of 1.1.0.
        rollout = page.locator("form[action$='/rollout']")
        rollout.locator("select[name=version]").select_option("1.1.0")
        rollout.locator("input[name=stage_percent]").fill("10")
        rollout.locator("button").click()
        expect(page.get_by_text("Rollout")).to_be_visible()
        assert "10" in page.content()

        browser.close()
    print("Console E2E passed: promote + rollout drove through the GUI.")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
