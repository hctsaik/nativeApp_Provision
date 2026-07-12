// Drive a REAL delivered package: real portable Python, real Streamlit, the real
// prebuilt Tauri shell, driven through WebView2's CDP endpoint.
//
// What it proves (design doc §7.2):
//   1. 8501 is occupied on purpose -> the launcher still comes up on another port
//   2. the Tauri window really RENDERS the app (we read "READY" out of the iframe)
//   3. portal Stop really stops it (the port stops accepting connections)
//   4. portal Start again comes back on a fresh port
//   5. closing the window leaves no Streamlit behind
//
// Usage: node streamlit-desktop-drive.mjs <packageDir> <screenshotDir>
import { spawn, execSync } from "node:child_process";
import { mkdirSync, writeFileSync } from "node:fs";
import { createRequire } from "node:module";
import net from "node:net";
import { join, resolve } from "node:path";

// playwright-core lives in the nativeApp repo (this repo ships no node_modules),
// and ESM resolves from the SCRIPT's location — so resolve it explicitly.
const nativeApp = process.env.NATIVE_APP_REPO ?? "C:/code/claude/nativeApp";
const require = createRequire(`${nativeApp}/package.json`);
const { chromium } = require("playwright-core");

const pkg = resolve(process.argv[2]);
const shots = resolve(process.argv[3] ?? "artifacts/streamlit-desktop");
const cdpPort = Number(process.env.CIM_E2E_CDP_PORT ?? 9351);
const BLOCKED_PORT = 8501; // the package's preferred port — we take it first
// The text that proves THIS app rendered. Override for packages other than the
// smoke fixture (CIM_E2E_EXPECT="CV Viewer"), so the check stays a real one.
const EXPECT = process.env.CIM_E2E_EXPECT ?? "READY";

mkdirSync(shots, { recursive: true });

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const steps = [];
let shotIndex = 0;

async function shot(page, name) {
  const file = `${String(++shotIndex).padStart(2, "0")}-${name}.png`;
  await page.screenshot({ path: join(shots, file), fullPage: true });
  steps.push({ step: shotIndex, name, file });
  return file;
}

function portOpen(port) {
  return new Promise((res) => {
    const sock = net.connect({ port, host: "127.0.0.1" }, () => {
      sock.destroy();
      res(true);
    });
    sock.on("error", () => res(false));
    sock.setTimeout(1500, () => {
      sock.destroy();
      res(false);
    });
  });
}

async function waitPortClosed(port, timeoutMs = 15000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (!(await portOpen(port))) return true;
    await sleep(250);
  }
  return false;
}

function childPid(parentPid, name) {
  const ps = `Get-CimInstance Win32_Process -Filter "ParentProcessId=${parentPid}" | ` +
             `Where-Object { $_.Name -eq '${name}' } | Select-Object -First 1 -ExpandProperty ProcessId`;
  const out = execSync(`powershell -NoProfile -Command "${ps}"`, { encoding: "utf8" }).trim();
  return out ? Number(out) : null;
}

async function appFrame(page, { timeoutMs = 60000 } = {}) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    for (const frame of page.frames()) {
      if (!frame.url().startsWith("http://127.0.0.1")) continue;
      const stApp = await frame.locator('[data-testid="stApp"]').count().catch(() => 0);
      if (stApp > 0) {
        const text = await frame.locator("body").innerText({ timeout: 2000 }).catch(() => "");
        if (text.includes(EXPECT)) return { frame, text };
      }
    }
    await sleep(500);
  }
  return null;
}

async function clickPortal(page, names) {
  for (const name of names) {
    const button = page.locator(`button:has-text("${name}")`).first();
    if (await button.count()) {
      await button.click();
      return name;
    }
  }
  throw new Error(`no portal button matched: ${names.join(" / ")}`);
}

// Hold 8501 for the whole run so the launcher is forced onto a fallback port.
const blocker = net.createServer();
await new Promise((res, rej) => {
  blocker.once("error", rej);
  blocker.listen(BLOCKED_PORT, "127.0.0.1", res);
});

const result = { ok: false, blockedPort: BLOCKED_PORT, steps, checks: {} };
let launcher = null;
let browser = null;

try {
  launcher = spawn(join(pkg, "runtime", "python.exe"), [join(pkg, "launcher", "launch.py")], {
    cwd: pkg,
    env: {
      ...process.env,
      WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS: `--remote-debugging-port=${cdpPort}`,
      WEBVIEW2_USER_DATA_FOLDER: join(shots, "wv2"),
    },
    stdio: ["ignore", "pipe", "pipe"],
  });
  launcher.stdout.on("data", (d) => process.stdout.write(`[launcher] ${d}`));
  launcher.stderr.on("data", (d) => process.stdout.write(`[launcher!] ${d}`));

  // ── 1. the window comes up (which means Streamlit already health-checked) ──
  const deadline = Date.now() + 180_000;
  let page = null;
  while (Date.now() < deadline && !page) {
    try {
      browser = browser ?? (await chromium.connectOverCDP(`http://127.0.0.1:${cdpPort}`));
      const ctx = browser.contexts()[0];
      const candidate = ctx && ctx.pages().find((p) => p.url().includes("tauri.localhost"));
      if (candidate) {
        const ready = await candidate
          .waitForFunction(() => {
            const s = document.querySelector(".toolSelect");
            return s && s.options.length > 0;
          }, { timeout: 5000 })
          .then(() => true)
          .catch(() => false);
        if (ready) page = candidate;
      }
    } catch { /* shell not up yet */ }
    if (!page) await sleep(1500);
  }
  if (!page) throw new Error("Tauri window / portal never became ready");

  const tools = await page.$$eval(".toolSelect option", (els) => els.map((e) => e.value).filter(Boolean));
  result.checks.tools = tools;
  await shot(page, "window-opened");

  // ── 2. press 啟動 once (the known MVP cost of not rebuilding the shell) ────
  await clickPortal(page, ["Start", "啟動"]);
  const rendered = await appFrame(page);
  if (!rendered) throw new Error(`the app never rendered ${EXPECT} inside the WebView2 window`);
  const url = new URL(rendered.frame.url());
  const port = Number(url.port);
  result.checks.renderedPort = port;
  result.checks.fellBackFromPreferred = port !== BLOCKED_PORT;
  await shot(page, "app-rendered");

  if (port === BLOCKED_PORT) throw new Error("launcher used the blocked port 8501");
  if (!(await portOpen(port))) throw new Error(`rendered port ${port} is not listening`);

  // ── 3. Stop must really stop it, not just say so ──────────────────────────
  await clickPortal(page, ["Stop", "停止"]);
  result.checks.portClosedAfterStop = await waitPortClosed(port);
  await shot(page, "after-stop");
  if (!result.checks.portClosedAfterStop) throw new Error(`port ${port} still open after Stop`);

  // ── 4. Start again -> a fresh port, rendered again ────────────────────────
  await clickPortal(page, ["Start", "啟動"]);
  const again = await appFrame(page);
  if (!again) throw new Error("the app did not render again after restart");
  const port2 = Number(new URL(again.frame.url()).port);
  result.checks.restartedPort = port2;
  await shot(page, "restarted");

  // ── 5. close the window like a user would; nothing may survive ────────────
  const shellPid = childPid(launcher.pid, "cim-light.exe");
  result.checks.shellPid = shellPid;
  if (shellPid) execSync(`taskkill /PID ${shellPid} /T /F`, { stdio: "ignore" });

  const exited = await Promise.race([
    new Promise((res) => launcher.once("exit", () => res(true))),
    sleep(30_000).then(() => false),
  ]);
  result.checks.launcherExited = exited;
  result.checks.portClosedAfterClose = await waitPortClosed(port2);

  result.ok = Boolean(
    result.checks.fellBackFromPreferred &&
    result.checks.portClosedAfterStop &&
    result.checks.launcherExited &&
    result.checks.portClosedAfterClose
  );
} catch (error) {
  result.error = String(error && error.message ? error.message : error);
} finally {
  if (browser) await browser.close().catch(() => {});
  if (launcher && launcher.exitCode === null) {
    try { execSync(`taskkill /PID ${launcher.pid} /T /F`, { stdio: "ignore" }); } catch { /* gone */ }
  }
  blocker.close();
  writeFileSync(join(shots, "result.json"), JSON.stringify(result, null, 2));
  process.stdout.write("\nRESULT_JSON " + JSON.stringify(result) + "\n");
}

process.exit(result.ok ? 0 : 1);
