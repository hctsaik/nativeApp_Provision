// GUI E2E:在**真的斷網**的條件下，用真的 Tauri 殼把有相依的工具（app-lv）點開。
//
// 回答一個 CLI 回答不了的問題：「補給包套用之後，使用者在畫面上按下 Start，到底會看到什麼？」
//
//   A. no-provision   沒有補給包 + 斷網 → 工具開得起來，但畫面是 ModuleNotFoundError
//   B. cold-no-warmup 有補給包 + 斷網 + 直接按 Start → 第一次按會失敗（見下），再按一次才成功
//   C. warmup-first   有補給包 + 斷網 + 先跑 warmup.py → 第一次按就成功
//
// 「斷網」怎麼做到：PIP_INDEX_URL 指向 http://127.0.0.1:1/simple（死位址）。
// 若安裝路徑漏掉 --no-index，pip 會去連那個位址並失敗 → B/C 就會紅。
//
// ⚠ 兩個實測學到的事，都寫進判準裡：
//
// 1) 既有 harness 的 `verifyRendered` 只看 `[data-testid="stApp"]` 存在且沒有 "Not Found"。
//    但 Streamlit script 在 import 階段崩潰時，stApp 容器**仍然存在**，body 是一段
//    ModuleNotFoundError → 會誤判成 RENDERED。所以這裡要求「有內容且不是 traceback」。
//
// 2) Tauri 殼的 HTTP bridge（bridge.rs::api_post）對 engine 有 **30 秒逾時**，
//    而 engine 在 POST /tools/<id>/start 裡**同步**安裝相依（_prewarm_deps_and_timeout）。
//    torch 級相依實測 76 秒 → 殼先放棄，portal 顯示「Failed to start tool: undefined」，
//    但 engine 其實把相依裝完、Streamlit 也起來了。B 就是在量這件事。
//
// 判準用三個互相獨立的證據：
//   (1) iframe 有實質內容且不是 traceback
//   (2) engine.log 出現 "Per-tool deps ready for app-lv"
//   (3) 直接用該工具 venv 的 python.exe `import torch` 成功
//
// 用法：node e2e/gui_offline_e2e.mjs <provision目錄> <輸出目錄> [A|B|C ...]

import {
  DEFAULTS, spawnShell, waitEngineReady, getReadyPage, teardown,
} from "file:///C:/code/claude/nativeApp/apps/host-tauri/e2e/lib.mjs";
import { spawnSync } from "node:child_process";
import { existsSync, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { join, resolve } from "node:path";

const TOOL_ID = "app-lv";
const PROVISION_DIR = resolve(process.argv[2] ?? "dist/provision");
const OUT_DIR = resolve(process.argv[3] ?? "e2e/out");
const WANTED = process.argv.slice(4);

const DEAD_INDEX = "http://127.0.0.1:1/simple";
const MIN_BODY_LEN = 120;
const ERROR_MARKERS = /ModuleNotFoundError|ImportError|Traceback \(most recent call last\)|No module named/;
const PORTAL_START_FAILED = /Failed to start tool/;

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const log = (msg) => console.log(`[${new Date().toTimeString().slice(0, 8)}] ${msg}`);

const SCENARIOS = [
  {
    key: "A-no-provision",
    title: "沒有補給包 + 斷網",
    why: "離線機忘了套用補給包時，使用者實際上會看到什麼。",
    expect: { working: false, errorPage: true, depsReady: false, torch: false },
    cdpPort: 9411, applyProvision: false, warmup: false, freshVenv: true,
    waitMs: 3 * 60 * 1000, allowRetry: false,
  },
  {
    key: "B-cold-no-warmup",
    title: "有補給包 + 斷網 + 直接按 Start",
    why: "首次相依安裝（76 秒）超過殼的 30 秒 HTTP 逾時 → 第一次按 Start 會顯示失敗。",
    expect: { working: true, errorPage: false, depsReady: true, torch: true, firstStartFailed: true },
    cdpPort: 9412, applyProvision: true, warmup: false, freshVenv: true,
    waitMs: 12 * 60 * 1000, allowRetry: true,
  },
  {
    key: "C-warmup-first",
    title: "有補給包 + 斷網 + 先跑 warmup.py",
    why: "把安裝成本移出「按下 Start」那一刻 → 第一次按就成功。這是建議的離線流程。",
    expect: { working: true, errorPage: false, depsReady: true, torch: true, firstStartFailed: false },
    cdpPort: 9413, applyProvision: true, warmup: true, freshVenv: true,
    waitMs: 6 * 60 * 1000, allowRetry: false,
  },
];

// ── 外部程序 ─────────────────────────────────────────────────────────────────
function runPy(args, extraEnv = {}) {
  return spawnSync(DEFAULTS.python, args, {
    encoding: "utf8", timeout: 30 * 60 * 1000,
    env: { ...process.env, PYTHONIOENCODING: "utf-8", ...extraEnv },
  });
}

function applyProvision(cacheDir) {
  const proc = runPy([join(PROVISION_DIR, "apply.py"), "--deppack-cache", cacheDir]);
  if (proc.status !== 0) throw new Error(`apply.py 失敗：\n${proc.stdout}\n${proc.stderr}`);
  return proc.stdout.trim();
}

/** 先把相依裝好（用平台會用的那顆 Python，ABI 才對得上）。 */
function runWarmup(cacheDir, venvDir) {
  const proc = runPy([
    join(PROVISION_DIR, "warmup.py"),
    "--project", DEFAULTS.repo,
    "--deppack-cache", cacheDir,
    "--tool-venvs", venvDir,
  ], { PIP_INDEX_URL: DEAD_INDEX, PIP_NO_INPUT: "1", PIP_RETRIES: "1", PIP_TIMEOUT: "5" });
  if (proc.status !== 0) throw new Error(`warmup.py 失敗：\n${proc.stdout}\n${proc.stderr}`);
  return proc.stdout.trim();
}

/** 證據 3：直接用該工具 venv 的直譯器 import torch。 */
function probeVenv(venvDir) {
  const python = join(venvDir, TOOL_ID, "Scripts", "python.exe");
  if (!existsSync(python)) return { venv: false, torch: false, detail: "venv 不存在" };
  const proc = spawnSync(python, ["-c", "import torch, transformers; print(torch.__version__)"],
    { encoding: "utf8", timeout: 180000 });
  const ok = proc.status === 0;
  return { venv: true, torch: ok,
           detail: ok ? `torch ${proc.stdout.trim()}`
                      : (proc.stderr || "").trim().split("\n").pop() || "import 失敗" };
}

const readEngineLog = (workDir) => {
  const path = join(workDir, "logs", "engine.log");
  return existsSync(path) ? readFileSync(path, "utf8") : "";
};

/** 證據 2：engine 對相依的最終判決。 */
function depVerdict(workDir) {
  const lines = readEngineLog(workDir).split(/\r?\n/);
  const ready = lines.filter((l) => /Per-tool deps ready for app-lv/.test(l));
  const bad = lines.filter((l) => /Per-tool deps for app-lv unavailable|dependency handling skipped/.test(l));
  return { depsReady: ready.length > 0,
           cached: ready.some((l) => l.includes("(cached)")),
           lines: [...ready, ...bad].map((l) => l.trim().slice(0, 200)) };
}

const streamlitStarted = (workDir) => /Starting Streamlit app for app-lv/.test(readEngineLog(workDir));

/** 證據 1：iframe 到底畫出了什麼。 */
async function probeFrame(page) {
  const frames = page.frames().filter((f) => f.url().startsWith("http://127.0.0.1"));
  const out = { stApp: 0, bodyLen: 0, sample: "", notFound: false, errorPage: false, url: null };
  for (const frame of frames) {
    const body = await frame.locator("body").innerText({ timeout: 3000 }).catch(() => "");
    const stApp = await frame.locator('[data-testid="stApp"]').count().catch(() => 0);
    if (stApp > 0 || body.length > out.bodyLen) {
      out.stApp = stApp;
      out.bodyLen = body.trim().length;
      out.sample = body.trim().slice(0, 240).replace(/\s+/g, " ");
      out.notFound = body.includes("Not Found") || body.trim().startsWith("404");
      out.errorPage = ERROR_MARKERS.test(body);
      out.url = frame.url();
    }
  }
  return out;
}

/** portal 的狀態列（不是 iframe）——殼的 HTTP 逾時會寫在這裡。 */
async function portalSaysStartFailed(page) {
  const text = await page.locator("body").innerText({ timeout: 2000 }).catch(() => "");
  return PORTAL_START_FAILED.test(text);
}

const hasContent = (p) => p.stApp > 0 && !p.notFound && p.bodyLen >= MIN_BODY_LEN;
const isWorking = (p) => hasContent(p) && !p.errorPage;

// ── 單一情境 ─────────────────────────────────────────────────────────────────
async function runScenario(scenario) {
  const runDir = join(OUT_DIR, scenario.key);
  const shots = join(OUT_DIR, "screenshots");
  rmSync(runDir, { recursive: true, force: true });
  mkdirSync(runDir, { recursive: true });
  mkdirSync(shots, { recursive: true });

  const cacheDir = join(runDir, "deppack-cache");
  const venvDir = join(runDir, "tool-venvs");
  mkdirSync(cacheDir, { recursive: true });
  mkdirSync(venvDir, { recursive: true });

  const result = {
    key: scenario.key, title: scenario.title, why: scenario.why, expect: scenario.expect,
    applyOutput: null, warmupOutput: null, warmupMs: null,
    frame: null, depsReady: false, cached: false, depLines: [], venv: null,
    firstStartFailed: false, firstFailureMs: null, elapsedMs: null,
    screenshots: [], pass: false, note: "",
  };

  log(`──── ${scenario.key}：${scenario.title} ────`);

  if (scenario.applyProvision) {
    result.applyOutput = applyProvision(cacheDir);
    log(`  apply.py 已套用補給包`);
  } else {
    log(`  刻意不套用補給包（deppack-cache 空的）`);
  }
  if (scenario.warmup) {
    const t0 = Date.now();
    result.warmupOutput = runWarmup(cacheDir, venvDir);
    result.warmupMs = Date.now() - t0;
    log(`  warmup.py 完成（${Math.round(result.warmupMs / 1000)}s，全程斷網）`);
  }

  const overrides = {
    CIM_DEPPACK_CACHE: cacheDir,
    CIM_TOOL_VENVS_DIR: venvDir,
    PIP_INDEX_URL: DEAD_INDEX,
    PIP_NO_INPUT: "1",
    PIP_DISABLE_PIP_VERSION_CHECK: "1",
    PIP_RETRIES: "1",
    PIP_TIMEOUT: "5",
  };
  const saved = {};
  for (const [k, v] of Object.entries(overrides)) { saved[k] = process.env[k]; process.env[k] = v; }

  let child = null, browser = null;
  try {
    const spawned = spawnShell(scenario.cdpPort, runDir);
    child = spawned.child;
    const enginePort = await waitEngineReady(child, spawned.wd, 90000);
    if (!enginePort) { result.note = "ENGINE_NOT_READY"; return result; }

    const got = await getReadyPage(scenario.cdpPort, 60000);
    browser = got.browser;
    const page = got.page;
    if (!page) { result.note = "PORTAL_NOT_READY"; return result; }

    const shot = async (name) => {
      const file = join(shots, `${scenario.key}-${name}.png`);
      await page.screenshot({ path: file }).catch(() => {});
      result.screenshots.push(file);
    };

    await shot("01-portal");
    await page.selectOption(".toolSelect", TOOL_ID);
    await shot("02-selected");
    log(`  按下 Start…`);
    const clickedAt = Date.now();
    await page.click('button:has-text("Start")');
    await sleep(2500);
    await shot("03-starting");

    let retried = false;
    let shotIndex = 0, lastShot = Date.now();
    const deadline = clickedAt + scenario.waitMs;

    while (Date.now() < deadline) {
      const probe = await probeFrame(page);
      if (isWorking(probe)) { result.frame = probe; break; }
      if (probe.errorPage) { result.frame = probe; result.note = "Streamlit 畫出 Python 例外"; break; }

      // 殼的 30 秒 HTTP 逾時：portal 說失敗，但 engine 還在裝
      if (!result.firstStartFailed && await portalSaysStartFailed(page)) {
        result.firstStartFailed = true;
        result.firstFailureMs = Date.now() - clickedAt;
        log(`  portal 顯示「Failed to start tool」（${Math.round(result.firstFailureMs / 1000)}s）`
          + `——殼的 30s bridge 逾時，engine 仍在背景安裝`);
        await shot("04-start-failed");
        if (!scenario.allowRetry) break;
      }

      // 等 engine 真的把 Streamlit 拉起來，再按第二次 Start（= 真實使用者的動作）
      if (result.firstStartFailed && !retried && streamlitStarted(runDir)) {
        await sleep(4000);
        log(`  engine 已完成安裝並啟動 Streamlit，再按一次 Start`);
        await page.click('button:has-text("Start")').catch(() => {});
        retried = true;
        await sleep(3000);
        await shot("05-retry");
      }

      if (Date.now() - lastShot > 60000) {
        lastShot = Date.now();
        await page.screenshot({ path: join(shots, `${scenario.key}-progress-${String(++shotIndex).padStart(2, "0")}.png`) })
          .catch(() => {});
      }
      await sleep(3000);
    }

    result.elapsedMs = Date.now() - clickedAt;
    await sleep(2500);
    result.frame = await probeFrame(page);
    await shot("06-final");

    const secs = Math.round(result.elapsedMs / 1000);
    if (isWorking(result.frame)) log(`  工具算繪出真正的 UI（${secs}s，bodyLen=${result.frame.bodyLen}）`);
    else if (result.frame.errorPage) log(`  iframe 是錯誤頁（${secs}s）`);
    else log(`  未算繪（${secs}s，bodyLen=${result.frame.bodyLen}）`);
  } catch (exc) {
    result.note = "DRIVER_ERROR: " + String(exc.message || exc).slice(0, 200);
    log(`  ${result.note}`);
  } finally {
    if (browser) await browser.close().catch(() => {});
    if (child) teardown(child);
    for (const [k, v] of Object.entries(saved)) {
      if (v === undefined) delete process.env[k]; else process.env[k] = v;
    }
  }

  const verdict = depVerdict(runDir);
  result.depsReady = verdict.depsReady;
  result.cached = verdict.cached;
  result.depLines = verdict.lines;
  await sleep(1500);
  result.venv = probeVenv(venvDir);

  result.actual = {
    working: isWorking(result.frame || {}),
    errorPage: !!(result.frame && result.frame.errorPage),
    depsReady: result.depsReady,
    torch: result.venv.torch,
    firstStartFailed: result.firstStartFailed,
  };
  result.pass = Object.entries(scenario.expect).every(([k, v]) => result.actual[k] === v);

  log(`  證據：可用=${result.actual.working} 錯誤頁=${result.actual.errorPage} `
    + `相依ready=${result.actual.depsReady} torch=${result.actual.torch}（${result.venv.detail}）`);
  log(`  ${result.pass ? "✔ 符合預期" : "✘ 不符預期"}`);
  return result;
}

// ── 主流程 ───────────────────────────────────────────────────────────────────
(async () => {
  mkdirSync(OUT_DIR, { recursive: true });
  const chosen = WANTED.length
    ? SCENARIOS.filter((s) => WANTED.some((w) => s.key.startsWith(w)))
    : SCENARIOS;

  const results = [];
  for (const scenario of chosen) results.push(await runScenario(scenario));

  // 只跑部分情境時，把結果併回既有的 result.json（冷啟動要 76 秒，不該白重跑）
  const reportPath = join(OUT_DIR, "result.json");
  let merged = results;
  if (WANTED.length && existsSync(reportPath)) {
    const prev = JSON.parse(readFileSync(reportPath, "utf8")).results || [];
    const byKey = new Map(prev.map((r) => [r.key, r]));
    for (const r of results) byKey.set(r.key, r);
    merged = SCENARIOS.map((s) => byKey.get(s.key)).filter(Boolean);
  }
  writeFileSync(reportPath, JSON.stringify(
    { tool: TOOL_ID, provisionDir: PROVISION_DIR, deadIndex: DEAD_INDEX,
      minBodyLen: MIN_BODY_LEN, results: merged }, null, 2), "utf8");

  console.log("\n" + "=".repeat(88));
  console.log("情境                可用    錯誤頁  相依ready  torch   首次失敗  秒數   結論");
  for (const r of merged) {
    const a = r.actual || {};
    console.log(
      `${r.key.padEnd(18)} ${String(a.working).padEnd(7)} ${String(a.errorPage).padEnd(7)} `
      + `${String(a.depsReady).padEnd(10)} ${String(a.torch).padEnd(7)} `
      + `${String(a.firstStartFailed).padEnd(9)} ${String(Math.round((r.elapsedMs || 0) / 1000)).padEnd(6)} `
      + `${r.pass ? "PASS" : "FAIL"} ${r.note}`);
  }
  console.log("=".repeat(88));
  console.log(`報告：${reportPath}`);
  process.exit(merged.every((r) => r.pass) ? 0 : 1);
})();
