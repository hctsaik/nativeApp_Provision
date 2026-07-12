#!/usr/bin/env python3
"""端到端:建置 -> 完整交付 -> 目標機安裝更新 -> 退版 -> 回收。

這支不是單元測試,它跑的是「管理員真的會做的那一串動作」,而且每一步都用
目標機看得到的東西驗證(sentinel、state.json、可執行的 start.bat),不是用
建置機的記憶體物件驗證。第二輪評分裡最貴的四個 blocker 全部在這條路上。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from provision_builder.streamlit_desktop import (  # noqa: E402
    BuildRequest, build_into_store, export_full_tree, export_update,
    find_entrypoint, find_runtime, find_shell, suggest_name,
)

PROJECT = Path(r"C:\code\claude\CV_Viewer")
STORE = ROOT / "dist" / "e2e-store"
DELIVER = ROOT / "dist" / "e2e-deliver"        # 「目標機」:只拿得到匯出物
PAYLOAD = ROOT / "dist" / "e2e-update"


def step(n: int, text: str) -> None:
    print(f"\n[{n}] {text}", flush=True)


def run_bootstrap(tree: Path, *args: str) -> subprocess.CompletedProcess:
    """用交付樹自己的 bootstrap(不是 repo 裡的那份)——目標機只有這個。"""
    cmd = [sys.executable, str(tree / "bootstrap" / "bootstrap.py"), *args]
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", env=dict(os.environ, PYTHONUTF8="1"))


def request(version_note: str) -> BuildRequest:
    entry = find_entrypoint(PROJECT)
    return BuildRequest(
        project_dir=PROJECT,
        entrypoint=entry.value,
        display_name=suggest_name(PROJECT),
        output_dir=STORE,
        shell_exe=find_shell().value,
        runtime_template=find_runtime().value,
        preferred_port=0,
        requirements=PROJECT / "requirements.lock.txt",
    )


def main() -> int:
    for path in (STORE, DELIVER, PAYLOAD):
        shutil.rmtree(path, ignore_errors=True)

    step(1, "建置 v1.0.0(新 store,要新建 runtime)")
    started = time.time()
    r1 = build_into_store(request("v1.0.0"), STORE, version="v1.0.0",
                          progress=lambda line: print("    " + str(line), flush=True))
    assert r1.ok, r1.errors
    print(f"    ok  {r1.version}  版本 {r1.version_mb:.1f} MB  本次新增 {r1.added_mb:.0f} MB"
          f"  ({time.time() - started:.0f}s)")

    step(2, "建置機:共用 runtime 建完之後,必須仍然符合它自己的 files.json")
    # 這一步是實測出來的:import 閘門會執行共用 runtime 的 python.exe 去問「你裝得到
    # 這些套件嗎」,而 python 預設會把 stdlib 的 __pycache__ 寫回那份 runtime——寫在
    # files.json 算完「之後」。於是 runtime 不再等於它自己的清單,交付到任何一台機器
    # 都會被判定為損毀。在這裡擋住,比在目標機上才發現便宜太多。
    sys.path.insert(0, str(STORE / "bootstrap"))
    import integrity as integrity_mod                       # noqa: E402
    import runtime_store as rstore_mod                      # noqa: E402
    manifest = json.loads((STORE / "apps" / r1.app_id / "versions" / "v1.0.0"
                           / "app-package.json").read_text("utf-8"))
    rt_dir = rstore_mod.RuntimeStore(STORE / "deps").path_for(manifest["runtime_fingerprint"])
    stray = [p for p in rt_dir.rglob("*.py[co]")]
    problems = integrity_mod.verify_tree(rt_dir, extra_excluded={"runtime.json"})
    print(f"    runtime 裡的 .pyc:{len(stray)} 個(要 0)")
    print(f"    runtime 對自己的 files.json 驗證:{'通過' if not problems else problems[:3]}")
    assert not stray and not problems, "共用 runtime 被污染了 —— 交付出去必定驗證失敗"

    step(3, "匯出「完整交付」(給沒裝過的新機器)")
    full = export_full_tree(STORE, DELIVER, app_id=r1.app_id)
    tree = Path(full.out_dir)
    must_exist = ["bootstrap/bootstrap.py", "讀我-使用說明.txt", "tools/admin.bat",
                  "tools/gc.bat", f"apps/{r1.app_id}/state/state.json"]
    bats = sorted(p.name for p in tree.glob("start*.bat"))
    missing = [p for p in must_exist if not (tree / p).exists()]
    print(f"    {full.total_mb:.0f} MB   啟動檔:{bats}")
    print(f"    缺少的必要檔案:{missing or '(無)'}")
    assert not missing and bats, "完整交付缺東西 —— 這正是 R2 的 blocker"
    assert not list(tree.glob("apps/*/data")), "把建置機的 data/(log、lease)交付出去了"

    step(4, "目標機:sentinel 該在的在、該不在的不在")
    ver_complete = (tree / "apps" / r1.app_id / "versions" / "v1.0.0" / ".complete").exists()
    rt_complete = [p for p in (tree / "deps" / "runtimes").glob("*/.complete")]
    print(f"    版本 .complete 保留 = {ver_complete}(要 True:拿到就能跑)")
    print(f"    runtime .complete  = {len(rt_complete)} 個(要 0:必須在目標機自己驗過才算數)")
    assert ver_complete and not rt_complete

    step(5, "目標機:bootstrap --status(電話另一頭念得出來的一頁)")
    out = run_bootstrap(tree, "--status")
    print("    " + "\n    ".join(out.stdout.strip().splitlines()[:12]))
    assert out.returncode == 0, out.stderr

    step(6, "建置 v1.1.0(同一份 lock → runtime 應該重用,+0 MB)")
    r2 = build_into_store(request("v1.1.0"), STORE, version="v1.1.0",
                          progress=lambda line: None)
    assert r2.ok, r2.errors
    print(f"    ok  runtime 重用 = {r2.runtime_reused}   本次新增 {r2.added_mb:.1f} MB")
    assert r2.runtime_reused and r2.added_mb < 30

    step(7, "匯出「更新包」(十幾 MB,給已部署的機器)")
    upd = export_update(STORE, r2.app_id, "v1.1.0", PAYLOAD, include_runtime=False)
    print(f"    {upd.total_mb:.1f} MB   ->  {upd.out_dir}")

    step(8, "目標機:bootstrap --install(R2 最貴的 blocker:以前這裡無路可走)")
    out = run_bootstrap(tree, "--install", str(upd.out_dir))
    print("    " + "\n    ".join((out.stdout + out.stderr).strip().splitlines()[-6:]))
    assert out.returncode == 0, "安裝更新包失敗"
    state = json.loads((tree / "apps" / r2.app_id / "state" / "state.json").read_text("utf-8"))
    print(f"    state: current={state.get('current')}  pending={state.get('pending')}")
    assert state.get("pending") == "v1.1.0", "更新包裝了卻沒有變成 pending"
    assert (tree / "apps" / r2.app_id / "versions" / "v1.1.0" / ".complete").exists(), \
        "sentinel 必須是目標機自己驗過後才寫的"

    step(9, "目標機:改變主意 —— 取消還沒套用的更新(--clear-pending)")
    out = run_bootstrap(tree, "--clear-pending")
    print("    " + "\n    ".join((out.stdout + out.stderr).strip().splitlines()[-3:]))
    state = json.loads((tree / "apps" / r2.app_id / "state" / "state.json").read_text("utf-8"))
    print(f"    state: current={state.get('current')}  pending={state.get('pending')}")
    assert out.returncode == 0 and state.get("pending") is None

    step(10, "目標機:重新武裝那一版(--set-pending)—— 取消不等於判它死刑")
    out = run_bootstrap(tree, "--set-pending", "v1.1.0")
    state = json.loads((tree / "apps" / r2.app_id / "state" / "state.json").read_text("utf-8"))
    print(f"    exit={out.returncode}  pending={state.get('pending')}")
    assert out.returncode == 0 and state.get("pending") == "v1.1.0"

    step(11, "目標機:沒有可退回的版本時,--rollback 要「明講並失敗」,不能假裝成功")
    out = run_bootstrap(tree, "--rollback")
    message = (out.stdout + out.stderr).strip().splitlines()
    print(f"    exit={out.returncode}(要非 0)")
    print("    " + "\n    ".join(message[-3:]))
    assert out.returncode != 0, "沒東西可退卻回報成功 —— 這正是 R2 扣分的『假裝成功』"

    step(12, "目標機:GC 在繁中主控台(cp950)不能崩潰")
    env = dict(os.environ, PYTHONIOENCODING="cp950", PYTHONUTF8="0")
    out = subprocess.run([sys.executable, str(tree / "bootstrap" / "gc.py")],
                         capture_output=True, text=True, encoding="cp950",
                         errors="replace", env=env)
    print(f"    exit={out.returncode}  輸出 {len(out.stdout.splitlines())} 行"
          f"  UnicodeEncodeError={'UnicodeEncodeError' in out.stderr}")
    assert out.returncode == 0 and "UnicodeEncodeError" not in out.stderr

    print("\n全部通過。交付樹:", tree)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
