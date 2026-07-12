// What does the packaged app ACTUALLY show? Launch it in the real shell, press
// Start, wait for the Streamlit script to finish, and dump the iframe's text.
//
// A blank screenshot is not evidence of a broken app — Streamlit paints its
// shell (stApp) before the script's output arrives, so a screenshot taken on
// "stApp exists" can be honestly empty. This probe waits for content.
//
// Usage: node probe-app-text.mjs <packageDir> <outDir>
import { spawn, execSync } from "node:child_process";
import { mkdirSync } from "node:fs";
import { createRequire } from "node:module";
import { join, resolve } from "node:path";

const nativeApp = process.env.NATIVE_APP_REPO ?? "C:/code/claude/nativeApp";
const require = createRequire(`${nativeApp}/package.json`);
const { chromium } = require("playwright-core");

const pkg = resolve(process.argv[2]);
const out = resolve(process.argv[3] ?? "artifacts/probe");
const cdpPort = Number(process.env.CIM_E2E_CDP_PORT ?? 9381);
mkdirSync(out, { recursive: true });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const launcher = spawn(join(pkg, "runtime", "python.exe"), [join(pkg, "launcher", "launch.py")], {
  cwd: pkg,
  env: {
    ...process.env,
    WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS: `--remote-debugging-port=${cdpPort}`,
    WEBVIEW2_USER_DATA_FOLDER: join(out, "wv2"),
  },
  stdio: ["ignore", "pipe", "pipe"],
});
launcher.stdout.on("data", (d) => process.stdout.write(`[launcher] ${d}`));

let browser = null;
try {
  const deadline = Date.now() + 180_000;
  let page = null;
  while (Date.now() < deadline && !page) {
    try {
      browser = browser ?? (await chromium.connectOverCDP(`http://127.0.0.1:${cdpPort}`));
      const ctx = browser.contexts()[0];
      const candidate = ctx && ctx.pages().find((p) => p.url().includes("tauri.localhost"));
      if (candidate) {
        const ok = await candidate
          .waitForFunction(() => {
            const s = document.querySelector(".toolSelect");
            return s && s.options.length > 0;
          }, { timeout: 5000 }).then(() => true).catch(() => false);
        if (ok) page = candidate;
      }
    } catch { /* not up yet */ }
    if (!page) await sleep(1500);
  }
  if (!page) throw new Error("portal never became ready");

  await page.locator('button:has-text("Start")').first().click();

  // Wait for the app frame to actually have text, not merely to exist.
  let frame = null;
  let text = "";
  const contentDeadline = Date.now() + 120_000;
  while (Date.now() < contentDeadline) {
    for (const f of page.frames()) {
      if (!f.url().startsWith("http://127.0.0.1")) continue;
      if (!(await f.locator('[data-testid="stApp"]').count().catch(() => 0))) continue;
      const t = await f.locator("body").innerText({ timeout: 2000 }).catch(() => "");
      if (t.trim().length > 20) { frame = f; text = t; break; }
    }
    if (frame) break;
    await sleep(1000);
  }

  await page.screenshot({ path: join(out, "app.png"), fullPage: true });
  console.log("\n=== iframe url ===\n" + (frame ? frame.url() : "(no frame with content)"));
  console.log("\n=== 使用者實際看到的文字 ===\n" + (text.slice(0, 1500) || "(空白)"));

  // Streamlit renders exceptions into the page; surface them explicitly.
  const failed = /Traceback|Error|Exception|找不到|缺失/.test(text);
  console.log("\n=== 判定 ===\n" + (text.trim() ? (failed ? "有錯誤訊息(見上)" : "有內容,無錯誤字樣") : "空白"));
} finally {
  if (browser) await browser.close().catch(() => {});
  try { execSync(`taskkill /PID ${launcher.pid} /T /F`, { stdio: "ignore" }); } catch { /* gone */ }
}
