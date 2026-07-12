// 單一工具的正式離線包驗證：apply → warmup → Tauri → Start → UI/engine 證據。
import { spawnSync } from "node:child_process";
import { existsSync, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { join, resolve } from "node:path";
import { pathToFileURL } from "node:url";

const args = Object.fromEntries(process.argv.slice(2).map((v, i, all) =>
  v.startsWith("--") ? [v.slice(2), all[i + 1]] : null).filter(Boolean));
const required = ["provision", "validation-dir", "project", "tool"];
for (const key of required) {
  if (!args[key]) throw new Error(`缺少 --${key}`);
}

const provision = resolve(args.provision);
const workDir = resolve(args["validation-dir"]);
const project = resolve(args.project);
const toolId = args.tool;
const python = args.python || process.env.CIM_VALIDATION_PYTHON || process.execPath;
const exe = args.exe || join(project, "apps", "host-tauri", "prebuilt", "cim-light.exe");
const enginePy = join(project, "sidecar", "python-engine", "engine.py");
const harnessPath = join(project, "apps", "host-tauri", "e2e", "lib.mjs");
if (!existsSync(harnessPath)) throw new Error(`找不到 Tauri E2E harness：${harnessPath}`);
const { spawnShell, waitEngineReady, getReadyPage, selectAndStart, teardown } =
  await import(pathToFileURL(harnessPath).href);
const cacheDir = join(workDir, "deppack-cache");
const venvDir = join(workDir, "tool-venvs");
const shotsDir = join(workDir, "screenshots");
const DEAD_INDEX = "http://127.0.0.1:1/simple";
const ERROR_MARKERS = /ModuleNotFoundError|ImportError|Traceback \(most recent call last\)|No module named/;
const log = (s) => console.log(`[驗證] ${s}`);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function runPy(argv, env = {}) {
  const p = spawnSync(python, argv, {
    encoding: "utf8", timeout: 30 * 60 * 1000,
    env: { ...process.env, PYTHONIOENCODING: "utf-8", ...env },
  });
  if (p.status !== 0) throw new Error(`${argv[0]} 失敗\n${p.stdout || ""}\n${p.stderr || ""}`);
  return (p.stdout || "").trim();
}

async function probeFrame(page) {
  let best = { stApp: 0, bodyLen: 0, sample: "", errorPage: false, notFound: false, url: null };
  for (const frame of page.frames().filter((f) => f.url().startsWith("http://127.0.0.1"))) {
    const body = await frame.locator("body").innerText({ timeout: 3000 }).catch(() => "");
    const stApp = await frame.locator('[data-testid="stApp"]').count().catch(() => 0);
    if (body.length > best.bodyLen) {
      best = { stApp, bodyLen: body.trim().length, sample: body.trim().slice(0, 300).replace(/\s+/g, " "),
        errorPage: ERROR_MARKERS.test(body), notFound: /Not Found|^404/.test(body.trim()), url: frame.url() };
    }
  }
  return best;
}

async function main() {
  for (const path of [provision, project, exe, enginePy]) {
    if (!existsSync(path)) throw new Error(`找不到必要路徑：${path}`);
  }
  mkdirSync(workDir, { recursive: true });
  rmSync(cacheDir, { recursive: true, force: true });
  rmSync(venvDir, { recursive: true, force: true });
  rmSync(join(workDir, "logs"), { recursive: true, force: true });
  rmSync(join(workDir, "wv2"), { recursive: true, force: true });
  mkdirSync(cacheDir, { recursive: true });
  mkdirSync(venvDir, { recursive: true });
  mkdirSync(shotsDir, { recursive: true });

  const sourceManifestPath = join(provision, "source-packages", toolId, "source-manifest.json");
  const sourceManifest = existsSync(sourceManifestPath)
    ? JSON.parse(readFileSync(sourceManifestPath, "utf8")) : null;
  const depManifestPath = join(provision, "packs", toolId, "deppack.json");
  let applyOutput = "", warmupOutput = "", warmupMs = 0;
  if (existsSync(depManifestPath)) {
    log(`套用補給包到 ${cacheDir}`);
    applyOutput = runPy([join(provision, "apply.py"), "--deppack-cache", cacheDir, "--tools", toolId]);
    log(`建立 ${toolId} 的離線執行環境`);
    const warmupStart = Date.now();
    warmupOutput = runPy([
      join(provision, "warmup.py"), "--project", project,
      "--deppack-cache", cacheDir, "--tool-venvs", venvDir, "--tools", toolId,
    ], { PIP_INDEX_URL: DEAD_INDEX, PIP_NO_INPUT: "1", PIP_RETRIES: "1", PIP_TIMEOUT: "5" });
    warmupMs = Date.now() - warmupStart;
    log(`暖機完成（${Math.round(warmupMs / 1000)} 秒）`);
  } else {
    log(`${toolId} 沒有 dependency pack；跳過 Apply/Warmup`);
  }
  let category = sourceManifest?.category;
  if (!category && existsSync(join(provision, "source-packages", toolId, "source", "plugin.yaml"))) {
    const yaml = readFileSync(join(provision, "source-packages", toolId, "source", "plugin.yaml"), "utf8");
    category = yaml.match(/^category:\s*([^#\r\n]+)/m)?.[1]?.trim();
  }
  category ||= "app";
  if (category === "module") {
    log(`驗證平台能載入 ${toolId} 的 process 原始碼`);
    runPy(["-c", [
      "import sys", `sys.path.insert(0, r'${join(project, "sidecar", "python-engine").replaceAll("\\", "\\\\")}')`,
      `from plugin_loader import PluginLoader`, `PluginLoader.load_module_dev('${toolId}', 'process')`,
      `print('MODULE_LOAD_OK ${toolId}')`,
    ].join(";")]);
  }
  log(`啟動 Tauri`);

  const overrides = { CIM_DEPPACK_CACHE: cacheDir, CIM_TOOL_VENVS_DIR: venvDir,
    PIP_INDEX_URL: DEAD_INDEX, PIP_NO_INPUT: "1", PIP_RETRIES: "1", PIP_TIMEOUT: "5" };
  const saved = {};
  for (const [k, v] of Object.entries(overrides)) { saved[k] = process.env[k]; process.env[k] = v; }
  let child = null, browser = null;
  const result = { toolId, provision, project, workDir, applyOutput, warmupOutput, warmupMs,
    category, engineReady: false, portalReady: false, frame: null, depsReady: false,
    moduleLoadReady: category === "module", pass: false, error: null };
  try {
    const spawned = spawnShell(9471, workDir, { exe, repo: project, python, enginePy });
    child = spawned.child;
    result.engineReady = !!(await waitEngineReady(child, spawned.wd, 90000));
    if (!result.engineReady) throw new Error("Tauri engine 90 秒內未就緒");
    const got = await getReadyPage(9471, 60000);
    browser = got.browser;
    const page = got.page;
    if (!page) throw new Error("Tauri Portal 60 秒內未就緒");
    result.portalReady = true;
    await page.screenshot({ path: join(shotsDir, "01-portal.png") });
    if (category !== "module") {
      await selectAndStart(page, toolId);
      log(`已在 Tauri 選擇 ${toolId} 並按下 Start`);
      const deadline = Date.now() + 120000;
      while (Date.now() < deadline) {
        result.frame = await probeFrame(page);
        if (result.frame.stApp > 0 && result.frame.bodyLen >= 120) break;
        await sleep(2500);
      }
      await page.screenshot({ path: join(shotsDir, "02-final.png") });
    } else {
      log(`${toolId} 是 Sheet 內的 Module；Tauri Portal 已啟動，原始碼載入契約已通過`);
      result.frame = { mode: "module-load-contract", bodyLen: 0, errorPage: false, notFound: false };
    }
    const engineLogPath = join(workDir, "logs", "engine.log");
    const engineLog = existsSync(engineLogPath) ? readFileSync(engineLogPath, "utf8") : "";
    result.depsReady = !existsSync(depManifestPath) || engineLog.includes(`Per-tool deps ready for ${toolId}`);
    result.pass = category === "module"
      ? result.engineReady && result.portalReady && result.moduleLoadReady && result.depsReady
      : !!result.frame && result.frame.stApp > 0 && result.frame.bodyLen >= 120
        && !result.frame.errorPage && !result.frame.notFound && result.depsReady;
    if (!result.pass) result.error = "Tauri 有啟動，但 UI 或相依證據未通過";
  } catch (e) {
    result.error = String(e?.message || e);
  } finally {
    if (browser && !(result.pass && args["keep-open"] === "true")) await browser.close().catch(() => {});
    if (child && !(result.pass && args["keep-open"] === "true")) teardown(child);
    for (const [k, v] of Object.entries(saved)) {
      if (v === undefined) delete process.env[k]; else process.env[k] = v;
    }
  }
  writeFileSync(join(workDir, "validation-result.json"), JSON.stringify(result, null, 2), "utf8");
  if (result.pass && args["keep-open"] === "true") log("Tauri 已保留開啟，可直接操作視窗");
  log(result.pass ? "PASS：Source／相依與 Tauri 驗證通過" : `FAIL：${result.error}`);
  return result.pass ? 0 : 1;
}

process.exit(await main());
