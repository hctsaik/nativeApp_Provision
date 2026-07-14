#!/usr/bin/env python3
"""GUI 內建流程的端到端測試:Source Package(原始碼獨立打包)+ Tauri 驗證。

對每個工具走**真實的 GUI 後端程式碼路徑**——不是另寫一份、而是呼叫 GUI「開始打包」
與「Tauri 驗證」兩顆按鈕背後的同一批函式:

  1. discover_source_modules + BuildProcess.run
       → 原子性建立 source-packages/<id>(即使沒有 requires 也打包原始碼);
         有 requires 的工具再跑 provision.py build(增量命中既有包 → 不連網)。
  2. validate_package.mjs(apply → warmup → 啟動真 Tauri 殼 → Portal Start / 載入契約
       → iframe 算繪與 engine log 雙重證據)。
  3. 讀 validation-result.json,斷言 pass。

刻意覆蓋 validate_package.mjs 的兩條分支:
  * category=app    → 真的在 Portal 選工具、按 Start,並要求 iframe 畫出非 traceback 的 UI。
  * category=module → module-load-contract(Sheet 內元件不會出現在 Portal 工具選單)。

驗證期間 PIP_INDEX_URL 指向死位址(validate_package.mjs 內建),所以「離線可裝」
是被證明的性質,而不是宣稱。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from provision_builder.gui_backend import BuildOptions, BuildProcess  # noqa: E402
from provision_builder.scan import make_subprocess_loader  # noqa: E402
from provision_builder.source_pack import discover_source_modules  # noqa: E402


def find_module_folder(project: Path, tool_id: str) -> Path:
    """在平台 engine 的 plugin 樹裡,找出這個工具 id 的 plugin.yaml 所在資料夾。"""
    engine = project / "sidecar" / "python-engine"
    candidates = sorted(engine.glob("plugins/**/plugin.yaml"))
    loader = make_subprocess_loader([sys.executable])
    loaded = loader(candidates)
    for path in candidates:
        entry = loaded.get(str(path)) or {}
        data = entry.get("data") or {}
        if str(data.get("id") or "").strip() == tool_id:
            return path.parent
    raise SystemExit(f"在 {engine}\\plugins 找不到 id={tool_id} 的 plugin.yaml")


def build_source_and_deps(project: Path, dest: Path, tool_id: str) -> None:
    """走 BuildProcess.run(GUI「開始打包」按鈕背後的後端)。"""
    folder = find_module_folder(project, tool_id)
    # 掃「裝著各個模組的那一層」(= GUI 的「Module 資料夾」),再挑出要的那一個。
    # 以前這裡直接把單一模組目錄餵進去;source_pack 現在會擋——擋的正是操作員在檔案
    # 選擇器裡往下多點一層、結果安靜地只打包到 1 個模組的那個手誤。
    modules = discover_source_modules(folder.parent, [sys.executable])
    picked = [m for m in modules if m.tool_id == tool_id] or modules
    options = BuildOptions(
        project_root=project, dest=dest, tool_ids=(),
        source_modules=tuple(picked),
    )
    rc = BuildProcess().run(options, on_line=lambda line: print(f"  [打包] {line}", flush=True))
    if rc != 0:
        raise SystemExit(f"{tool_id} 原始碼/相依打包失敗(rc={rc})")


def validate(project: Path, dest: Path, work: Path, tool_id: str) -> dict:
    """走 validate_package.mjs(GUI「Tauri 驗證」按鈕背後的 driver)。

    E2E 自動化不帶 --keep-open,好讓它在 pass 後自行 teardown、換下一個工具並乾淨退出;
    keep-open 只是 GUI 給人續操作用的便利旗標,不屬於驗證邏輯本身。
    """
    validation_dir = work / tool_id
    cmd = [
        "node", str(REPO / "e2e" / "validate_package.mjs"),
        "--provision", str(dest.resolve()),
        "--validation-dir", str(validation_dir.resolve()),
        "--project", str(project.resolve()),
        "--tool", tool_id,
        "--python", sys.executable,
    ]
    print(f"  [驗證] {' '.join(cmd)}", flush=True)
    proc = subprocess.run(cmd, cwd=str(REPO), text=True)
    result_path = validation_dir / "validation-result.json"
    if not result_path.is_file():
        raise SystemExit(f"{tool_id} 沒有產出 validation-result.json(rc={proc.returncode})")
    return json.loads(result_path.read_text(encoding="utf-8"))


def main() -> int:
    ap = argparse.ArgumentParser(description="Source Package + Tauri 驗證的 E2E")
    ap.add_argument("--project", default=r"C:\code\claude\nativeApp")
    ap.add_argument("--dest", default=str(REPO / "dist" / "provision"),
                    help="provision 輸出根(可指向既有包 → 增量命中,不連網)")
    ap.add_argument("--work", default=str(REPO / "e2e" / "gui-flow-validation"))
    ap.add_argument("--tools", default="app-lv,module_001",
                    help="逗號分隔的工具 id;至少各含一個 app 類與 module 類最有代表性")
    args = ap.parse_args()

    project = Path(args.project).resolve()
    dest = Path(args.dest).resolve()
    work = Path(args.work).resolve()
    tool_ids = [t.strip() for t in args.tools.split(",") if t.strip()]

    results: list[dict] = []
    for tool_id in tool_ids:
        print(f"\n=== {tool_id} ===", flush=True)
        build_source_and_deps(project, dest, tool_id)
        source_pack = dest / "source-packages" / tool_id / "source-manifest.json"
        if not source_pack.is_file():
            raise SystemExit(f"{tool_id} 沒有產出 source-manifest.json")
        print(f"  [打包] 原始碼包就緒:{source_pack.relative_to(dest)}", flush=True)
        result = validate(project, dest, work, tool_id)
        results.append(result)
        state = "PASS" if result.get("pass") else "FAIL"
        print(f"  [{state}] category={result.get('category')} "
              f"engineReady={result.get('engineReady')} portalReady={result.get('portalReady')} "
              f"depsReady={result.get('depsReady')} error={result.get('error')}", flush=True)

    print("\n=== 總結 ===", flush=True)
    all_pass = True
    for r in results:
        ok = bool(r.get("pass"))
        all_pass = all_pass and ok
        frame = r.get("frame") or {}
        detail = frame.get("mode") or f"bodyLen={frame.get('bodyLen')} stApp={frame.get('stApp')}"
        print(f"  {'PASS' if ok else 'FAIL'}  {r.get('toolId'):12} "
              f"[{r.get('category')}] {detail}", flush=True)
    print(f"\n{'全部通過' if all_pass else '有失敗'}", flush=True)
    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
