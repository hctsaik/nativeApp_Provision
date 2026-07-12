// Real WebView2 E2E for the STORE layout.
//
// The headless store E2E already proved the state machine; this proves the half
// a user actually sees. Two full cold starts through the real chain:
//
//   start.bat → bootstrap (any runtime) → launch.py (the version's OWN runtime)
//              → Streamlit → cim-light.exe → WebView2
//
// Run 1 renders v1.0.0. We then promote v1.1.0 the way an operator would
// (bootstrap --set-pending) and cold start again: run 2 must render v1.1.0 —
// read out of the iframe, not inferred from state.json.
//
// Usage: node streamlit-desktop-store-drive.mjs <deployDir> <shotsDir>
import { spawn, execSync, execFileSync } from "node:child_process";
import { mkdirSync, readFileSync, readdirSync, writeFileSync, existsSync } from "node:fs";
import { createRequire } from "node:module";
import net from "node:net";
import { join, resolve } from "node:path";

const nativeApp = process.env.NATIVE_APP_REPO ?? "C:/code/claude/nativeApp";
const require = createRequire(`${nativeApp}/package.json`);
const { chromium } = require("playwright-core");

const deploy = resolve(process.argv[2]);
const shots = resolve(process.argv[3] ?? "artifacts/streamlit-store");
const APP = "app-portable-streamlit-smoke";
const BLOCKED_PORT = 8501;
mkdirSync(shots, { recursive: true });

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const steps = [];
let shotIndex = 0;

const statePath = join(deploy, "apps", APP, "state", "state.json");
const readState = () => JSON.parse(readFileSync(statePath, "utf8"));

function anyRuntimePython() {
  const runtimes = join(deploy, "deps", "runtimes");
  for (const name of readdirSync(runtimes)) {
    const exe = join(runtimes, name, "python.exe");
    if (existsSync(exe)) return exe;
  }
  throw new Error("no runtime in deps/runtimes");
}

async function shot(page, name) {
  const file = `${String(++shotIndex).padStart(2, "0")}-${name}.png`;
  await page.screenshot({ path: join(shots, file), fullPage: true });
  steps.push({ step: shotIndex, name, file });
  return file;
}

function portOpen(port) {
  return new Promise((res) => {
    const sock = net.connect({ port, host: "127.0.0.1" }, () => { sock.destroy(); res(true); });
    sock.on("error", () => res(false));
    sock.setTimeout(1500, () => { sock.destroy(); res(false); });
  });
}

async function waitPortClosed(port, timeoutMs = 20000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (!(await portOpen(port))) return true;
    await sleep(250);
  }
  return false;
}

function descendantPid(rootPid, name) {
  // The shell is a grandchild (bootstrap → launch.py → cim-light.exe), so we
  // walk the subtree. PowerShell only dumps the table — `$pid` is a read-only
  // automatic variable there, so doing the walk in PS is a trap.
  const csv = execSync(
    "powershell -NoProfile -Command \"Get-CimInstance Win32_Process | " +
    "Select-Object ProcessId,ParentProcessId,Name | ConvertTo-Csv -NoTypeInformation\"",
    { encoding: "utf8" });
  const byParent = new Map();
  for (const line of csv.split(/\r?\n/).slice(1)) {
    const cells = line.split(",").map((c) => c.replace(/^"|"$/g, ""));
    if (cells.length < 3) continue;
    const [pid, ppid, procName] = [Number(cells[0]), Number(cells[1]), cells[2]];
    if (!byParent.has(ppid)) byParent.set(ppid, []);
    byParent.get(ppid).push({ pid, name: procName });
  }
  const queue = [rootPid];
  const seen = new Set();
  while (queue.length) {
    for (const child of byParent.get(queue.shift()) ?? []) {
      if (seen.has(child.pid)) continue;
      seen.add(child.pid);
      if (child.name.toLowerCase() === name.toLowerCase()) return child.pid;
      queue.push(child.pid);
    }
  }
  return null;
}

function startBootstrap(runIndex) {
  const child = spawn(anyRuntimePython(), [join(deploy, "bootstrap", "bootstrap.py")], {
    cwd: deploy,
    env: {
      ...process.env,
      PYTHONDONTWRITEBYTECODE: "1",
      PYTHONIOENCODING: "utf-8",
      WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS: `--remote-debugging-port=${9360 + runIndex}`,
      WEBVIEW2_USER_DATA_FOLDER: join(shots, `wv2-${runIndex}`),
    },
    stdio: ["ignore", "pipe", "pipe"],
  });
  child.stdout.on("data", (d) => process.stdout.write(`[boot${runIndex}] ${d}`));
  child.stderr.on("data", (d) => process.stdout.write(`[boot${runIndex}!] ${d}`));
  return child;
}

async function attachPortal(cdpPort, timeoutMs = 300000) {
  // First start deep-verifies ~450MB of runtime before the window ever appears,
  // so this wait is generous on purpose.
  const deadline = Date.now() + timeoutMs;
  let browser = null;
  while (Date.now() < deadline) {
    try {
      browser = browser ?? (await chromium.connectOverCDP(`http://127.0.0.1:${cdpPort}`));
      const ctx = browser.contexts()[0];
      const page = ctx && ctx.pages().find((p) => p.url().includes("tauri.localhost"));
      if (page) {
        const ready = await page
          .waitForFunction(() => {
            const s = document.querySelector(".toolSelect");
            return s && s.options.length > 0;
          }, { timeout: 5000 })
          .then(() => true).catch(() => false);
        if (ready) return { browser, page };
      }
    } catch { /* shell not up yet */ }
    await sleep(2000);
  }
  return { browser, page: null };
}

async function appFrame(page, timeoutMs = 90000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    for (const frame of page.frames()) {
      if (!frame.url().startsWith("http://127.0.0.1")) continue;
      if (!(await frame.locator('[data-testid="stApp"]').count().catch(() => 0))) continue;
      const text = await frame.locator("body").innerText({ timeout: 2000 }).catch(() => "");
      if (text.includes("READY")) return { frame, text };
    }
    await sleep(500);
  }
  return null;
}

async function clickPortal(page, names) {
  for (const name of names) {
    const button = page.locator(`button:has-text("${name}")`).first();
    if (await button.count()) { await button.click(); return name; }
  }
  throw new Error(`no portal button matched: ${names.join(" / ")}`);
}

const result = { ok: false, blockedPort: BLOCKED_PORT, steps, checks: {} };
const blocker = net.createServer();
await new Promise((res, rej) => { blocker.once("error", rej); blocker.listen(BLOCKED_PORT, "127.0.0.1", res); });

const children = [];
let browser = null;

async function coldStart(runIndex, { expect, shotPrefix }) {
  const child = startBootstrap(runIndex);
  children.push(child);
  const attached = await attachPortal(9360 + runIndex);
  browser = attached.browser;
  const page = attached.page;
  if (!page) throw new Error(`run ${runIndex}: the Tauri window never became ready`);

  await shot(page, `${shotPrefix}-window`);
  await clickPortal(page, ["Start", "啟動"]);
  const rendered = await appFrame(page);
  if (!rendered) throw new Error(`run ${runIndex}: the app never rendered READY`);
  if (!rendered.text.includes(expect)) {
    throw new Error(`run ${runIndex}: window shows the wrong version — expected ${expect}`);
  }
  const port = Number(new URL(rendered.frame.url()).port);
  await shot(page, `${shotPrefix}-rendered`);
  return { child, page, port };
}

async function closeShell(child) {
  const shellPid = descendantPid(child.pid, "cim-light.exe");
  if (shellPid) execSync(`taskkill /PID ${shellPid} /T /F`, { stdio: "ignore" });
  await Promise.race([
    new Promise((res) => child.once("exit", res)),
    sleep(30000),
  ]);
  if (child.exitCode === null) {
    try { execSync(`taskkill /PID ${child.pid} /T /F`, { stdio: "ignore" }); } catch { /* gone */ }
  }
  if (browser) { await browser.close().catch(() => {}); browser = null; }
}

try {
  // ── run 1: first start ever — deep verify, render v1.0.0, prove Stop works ──
  const before = readState();
  result.checks.initialState = { current: before.current, lkg: before.last_known_good };

  const run1 = await coldStart(1, { expect: "READY v1.0.0", shotPrefix: "run1" });
  result.checks.renderedPort = run1.port;
  result.checks.fellBackFromPreferred = run1.port !== BLOCKED_PORT;

  const afterHealthy = readState();
  result.checks.lkgCommittedAfterHealthyStart = afterHealthy.last_known_good === "v1.0.0";

  await clickPortal(run1.page, ["Stop", "停止"]);
  result.checks.portClosedAfterStop = await waitPortClosed(run1.port);
  await shot(run1.page, "run1-after-stop");

  await closeShell(run1.child);
  result.checks.portClosedAfterClose = await waitPortClosed(run1.port);

  // ── operator stages v1.1.0 (as the admin path does) ────────────────────────
  execFileSync(anyRuntimePython(),
               [join(deploy, "bootstrap", "bootstrap.py"), "--set-pending", "v1.1.0"],
               { cwd: deploy, stdio: "inherit",
                 env: { ...process.env, PYTHONDONTWRITEBYTECODE: "1", PYTHONIOENCODING: "utf-8" } });
  result.checks.pendingSet = readState().pending === "v1.1.0";

  // ── run 2: cold start promotes and the WINDOW shows the new version ────────
  const run2 = await coldStart(2, { expect: "READY v1.1.0", shotPrefix: "run2" });
  result.checks.windowShowsPromotedVersion = true;
  const promoted = readState();
  result.checks.promoted = {
    current: promoted.current, previous: promoted.previous,
    pending: promoted.pending, lkg: promoted.last_known_good,
  };
  await closeShell(run2.child);

  result.ok = Boolean(
    result.checks.fellBackFromPreferred &&
    result.checks.lkgCommittedAfterHealthyStart &&
    result.checks.portClosedAfterStop &&
    result.checks.portClosedAfterClose &&
    result.checks.pendingSet &&
    result.checks.windowShowsPromotedVersion &&
    promoted.current === "v1.1.0" && promoted.previous === "v1.0.0" &&
    promoted.pending === null && promoted.last_known_good === "v1.1.0"
  );
} catch (error) {
  result.error = String(error && error.message ? error.message : error);
} finally {
  if (browser) await browser.close().catch(() => {});
  for (const child of children) {
    if (child.exitCode === null) {
      try { execSync(`taskkill /PID ${child.pid} /T /F`, { stdio: "ignore" }); } catch { /* gone */ }
    }
  }
  blocker.close();
  writeFileSync(join(shots, "result.json"), JSON.stringify(result, null, 2));
  process.stdout.write("\nRESULT_JSON " + JSON.stringify(result) + "\n");
}

process.exit(result.ok ? 0 : 1);
