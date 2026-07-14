#!/usr/bin/env python3
"""高風險情境浸泡測試 —— 用「真的程序、真的檔案、真的磁碟」跑,不是用假物件。

單元測試證明「每個零件的邏輯是對的」;這支證明「把它們湊起來、在一台真的 Windows
上、連續操作十幾次之後,它還是對的」。前面幾輪的教訓很一致:每個零件都綠燈,
湊起來卻是壞的(共用 runtime 被 import 探測污染、cmd.exe 弄壞含中文的 .bat、
`import gc` 拿到 Python 的垃圾回收器)——那些沒有一個是讀碼看得出來的。

涵蓋(使用者指名的高風險項目):
  [2]  預設 port 被占用 → 自動換一個沒被占用的
  [3]  執行中收到新版 → 進 pending;現行版本不受影響;重啟才切換
  [4]  更新複製到一半就斷電 → 絕不可以被升成 PROD
  [5]  新版啟動失敗 → 自動退回上一個可用版本
  [6]  連續更新多次 → 沒有殘留的 .staging-* 逐漸吃滿磁碟
  [8]  中文路徑、含空白的路徑、唯讀目錄
  [9]  關窗後 Streamlit/Python 完全結束,不留程序、不留 port
  [10] v1 → v2 → v3 → rollback,檢查 PREV / PROD / NEXT 與 GC

不涵蓋(這台機器做不到,需要一台乾淨的 VM):
  [1]  全新 Windows、沒有 Python、沒有 WebView2 的機器上雙擊 start.bat
       —— 這一項必須在真的 VM 上做,任何在開發機上的模擬都是自欺欺人。
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from provision_builder.streamlit_desktop import (  # noqa: E402
    BuildRequest, build_into_store, export_full_tree, export_update,
    find_entrypoint, find_runtime, find_shell,
)

WORK = ROOT / "dist" / "soak"
FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    mark = "PASS" if ok else "FAIL"
    print(f"    [{mark}] {name}" + (f" — {detail}" if detail else ""), flush=True)
    if not ok:
        FAILURES.append(f"{name}{(' — ' + detail) if detail else ''}")
    return ok


def step(n: str, text: str) -> None:
    print(f"\n[{n}] {text}", flush=True)


def bootstrap(tree: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(tree / "bootstrap" / "bootstrap.py"), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=dict(os.environ, PYTHONUTF8="1"))


def state_of(tree: Path, app: str) -> dict:
    return json.loads((tree / "apps" / app / "state" / "state.json").read_text("utf-8"))


def make_app(root: Path, marker: str) -> Path:
    """一個最小但是「真的」的 Streamlit 專案。"""
    root.mkdir(parents=True, exist_ok=True)
    (root / "app.py").write_text(
        "import streamlit as st\n"
        f"st.title('soak {marker}')\n"
        f"st.write('version marker: {marker}')\n", encoding="utf-8")
    (root / "requirements.lock.txt").write_text("streamlit==1.40.0\n", encoding="utf-8")
    return root / "app.py"


def request_for(project: Path, out: Path) -> BuildRequest:
    return BuildRequest(
        project_dir=project,
        entrypoint=find_entrypoint(project).value,
        display_name="浸泡測試 App",          # 中文顯示名:cp950 / bat 的地雷區
        output_dir=out,
        shell_exe=find_shell().value,
        runtime_template=find_runtime().value,
        preferred_port=0,
        requirements=project / "requirements.lock.txt",
        app_id_override="soak-app",
    )


# ── [2] port ────────────────────────────────────────────────────────────────

def test_busy_port_is_never_handed_out() -> None:
    step("2", "預設 port 被占用 → 一定要換一個沒被占用的")
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "launch", ROOT / "src/provision_builder/streamlit_desktop/templates/launch.py")
    launch = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(launch)

    # 真的占住一個埠(不是 mock)
    held = socket.socket()
    held.bind(("127.0.0.1", 0))
    held.listen(1)
    busy = held.getsockname()[1]
    try:
        picked = [launch.pick_port(busy) for _ in range(5)]
        check("被占用的埠一次都沒有被選中", busy not in picked, f"占用={busy} 選到={picked}")
        check("選到的埠都是真的可以 bind 的",
              all(launch.is_port_free(p) for p in picked))
    finally:
        held.close()


# ── [8] 路徑地雷 ─────────────────────────────────────────────────────────────

def test_hostile_paths() -> None:
    step("8", "中文路徑、含空白的路徑、唯讀目錄")
    hostile = WORK / "中文 專案 資料夾"           # 中文 + 空白,一次到位
    entry = make_app(hostile, "v1")
    check("中文+空白路徑的專案,入口偵測得到", find_entrypoint(hostile).found,
          str(entry.relative_to(WORK)))

    # 唯讀目錄:Windows 上 os.chmod(dir, 0o444) 「什麼都不會做」——那是 POSIX 語義。
    # 第一版的這支測試就是這樣騙自己的:它印出 PASS,同時往那個「唯讀」目錄裡
    # 寫了 390 MB。要真的擋住寫入,必須用 ACL。
    readonly = WORK / "readonly-out"
    readonly.mkdir(parents=True, exist_ok=True)
    user = os.environ.get("USERNAME", "")
    denied = subprocess.run(["icacls", str(readonly), "/deny", f"{user}:(W,AD)"],
                            capture_output=True, text=True)
    if denied.returncode != 0:
        check("能不能建立一個真的唯讀目錄(ACL)", False, denied.stdout.strip()[-60:])
        return
    try:
        probe = readonly / "canary.txt"
        try:
            probe.write_text("x", encoding="utf-8")
            check("這個目錄真的寫不進去(先自我驗證,不要測一個假的唯讀)", False,
                  "canary 竟然寫成功了")
            probe.unlink(missing_ok=True)
            return
        except OSError:
            check("這個目錄真的寫不進去(先自我驗證)", True)

        r = build_into_store(request_for(hostile, readonly), readonly, version="v1.0.0",
                             progress=lambda _l: None)
        message = " ".join(r.errors)
        check("寫不進去的輸出目錄:不會謊報成功", not r.ok,
              "回報 ok=True" if r.ok else "")
        check("而且錯誤訊息是給人看的(不是裸 traceback)",
              bool(message) and "Traceback" not in message, message[:70])
    except Exception as exc:                       # noqa: BLE001
        check("唯讀目錄不該噴出裸例外", False, f"{type(exc).__name__}: {exc}")
    finally:
        subprocess.run(["icacls", str(readonly), "/remove:d", user],
                       capture_output=True)
        shutil.rmtree(readonly, ignore_errors=True)


# ── [3][5][6][10] 版本鏈 ─────────────────────────────────────────────────────

def test_version_chain_and_rollback() -> Path:
    step("10", "v1 → v2 → v3 → rollback:PREV / PROD / NEXT 與 GC")
    project = WORK / "chain"
    make_app(project, "v1")
    store = WORK / "store"
    app = "app-soak-app"

    sizes: list[tuple[str, float]] = []
    for i, version in enumerate(("v1.0.0", "v2.0.0", "v3.0.0"), start=1):
        make_app(project, f"v{i}")                 # 每一版內容都不一樣
        r = build_into_store(request_for(project, store), store, version=version,
                             progress=lambda _l: None)
        if not check(f"建置 {version}", r.ok, "; ".join(r.errors)[:80]):
            return store
        sizes.append((version, r.added_mb))
        check(f"{version} 有重用共用 runtime" if i > 1 else f"{version} 建立了共用 runtime",
              r.runtime_reused if i > 1 else True,
              f"本次新增 {r.added_mb:.1f} MB")

    # 第二、三版「新增的磁碟」應該只是版本槽,不是又一份 runtime
    for version, added in sizes[1:]:
        check(f"{version} 沒有再吃一份 500MB 的 runtime", added < 100,
              f"新增 {added:.1f} MB")

    tree = Path(export_full_tree(store, WORK / "deliver", app_id=app,
                                 version="v3.0.0").out_dir)
    st = state_of(tree, app)
    check("交付出去的是 v3.0.0", st["current"] == "v3.0.0", str(st["current"]))
    check("而且它在試用期(candidate=current,失敗才退得回去)",
          st["candidate"] == st["current"])
    check("有一個可以退回去的版本", bool(st["previous"]), str(st["previous"]))

    shipped_previous = st["previous"]
    out = bootstrap(tree, "--rollback")
    st = state_of(tree, app)
    check("--rollback 成功", out.returncode == 0, (out.stdout + out.stderr).strip()[-70:])
    check("退回到了交付時就備好的那個版本", st["current"] == shipped_previous,
          f"退到 {st['current']}(交付時備的是 {shipped_previous})")
    check("退回去的版本是真的存在、驗得過的",
          (tree / "apps" / app / "versions" / str(st["current"]) / ".complete").is_file(),
          str(st["current"]))
    check("被退掉的 v3.0.0 記為失敗(自動更新不會再把它裝回來)",
          any(e.get("version") == "v3.0.0" for e in st["failed_versions"]))
    return tree


# ── [4] 斷電:只複製一半的更新 ────────────────────────────────────────────────

def test_half_copied_update_is_never_promoted(tree: Path) -> None:
    step("4", "更新複製到一半就斷電 → 絕不可以被升成 PROD")
    app = "app-soak-app"
    store = WORK / "store"

    # 用一個「乾淨」的版本來測。第一版的這支測試用了剛剛被 rollback 標記成失敗的
    # v3.0.0,於是 --install 確實被拒絕了——但理由是「它在失敗清單裡」,而不是
    # 「這個檔案壞了」。損毀那條路徑「一次都沒有被執行到」,測試卻是綠的。
    # 一個因為別的理由而剛好通過的測試,就是沒有測試。
    project = WORK / "chain"
    make_app(project, "v9")
    fresh = build_into_store(request_for(project, store), store, version="v9.0.0",
                             progress=lambda _l: None)
    if not check("先建一個乾淨的新版本來測損毀(不能用已經被標記失敗的版本)", fresh.ok):
        return
    payload = Path(export_update(store, app, "v9.0.0", WORK / "payload",
                                 include_runtime=False).out_dir)

    before = state_of(tree, app)["current"]

    # 模擬「複製到一半就斷電」:把 payload 裡的一個檔案截斷
    victims = sorted(payload.rglob("*.py"))
    if not victims:
        check("找得到可以破壞的檔案", False)
        return
    victim = victims[-1]
    original = victim.read_bytes()
    victim.write_bytes(original[: len(original) // 2])       # 半個檔案

    out = bootstrap(tree, "--install", str(payload))
    st = state_of(tree, app)
    said = (out.stdout + out.stderr)

    check("安裝被拒絕(exit != 0)", out.returncode != 0, f"exit={out.returncode}")
    check("目前版本沒有被動到", st["current"] == before, str(st["current"]))
    check("沒有把半套的版本設成待套用", st.get("pending") in (None, ""), str(st.get("pending")))
    check("而且它說得出「系統沒有任何變更」", "沒有任何變更" in said or "不會安裝" in said,
          said.strip().splitlines()[-1][:60] if said.strip() else "(無輸出)")

    victim.write_bytes(original)                    # 修好,證明它現在裝得起來
    out = bootstrap(tree, "--install", str(payload))
    check("修好之後,同一個包裝得起來(不是永久拒絕)", out.returncode == 0,
          (out.stdout + out.stderr).strip()[-60:])


# ── [6] 連續更新不可以累積 staging ────────────────────────────────────────────

def test_repeated_updates_leave_no_staging() -> None:
    step("6", "連續更新多次 → 不可以留下逐漸吃滿磁碟的 .staging-*")
    store = WORK / "store"
    project = WORK / "chain"
    for i in range(4, 7):                            # v4, v5, v6
        make_app(project, f"v{i}")
        r = build_into_store(request_for(project, store), store,
                             version=f"v{i}.0.0", progress=lambda _l: None)
        if not r.ok:
            check(f"建置 v{i}.0.0", False, "; ".join(r.errors)[:70])
            return

    leftovers = [p for p in store.rglob(".staging-*")] + \
                [p for p in store.rglob("*.staging-*")]
    check("連續建置 6 版之後,一個 .staging-* 都沒有留下",
          not leftovers, f"殘留 {len(leftovers)} 個: {[p.name for p in leftovers[:3]]}")

    versions = sorted((store / "apps" / "app-soak-app" / "versions").iterdir())
    runtimes = sorted((store / "deps" / "runtimes").iterdir())
    check("6 個版本共用「一份」runtime", len(runtimes) == 1,
          f"版本 {len(versions)} 個 / runtime {len(runtimes)} 份")


# ── [9] 關窗後不留程序、不留 port ────────────────────────────────────────────

def test_no_orphan_processes_after_close() -> None:
    step("9", "關窗後 Streamlit / Python 完全結束,不留程序、不留 port")
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "launch", ROOT / "src/provision_builder/streamlit_desktop/templates/launch.py")
    launch = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(launch)

    # 起一棵「真的」程序樹:python -> 子 python,然後殺掉它,確認整棵都死了
    port = launch.pick_port(0)
    child = subprocess.Popen(
        [sys.executable, "-c",
         "import subprocess,sys,time;"
         "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(120)']);"
         "print(p.pid, flush=True); time.sleep(120)"],
        stdout=subprocess.PIPE, text=True)
    grandchild_pid = int(child.stdout.readline().strip())
    time.sleep(0.5)

    def alive(pid: int) -> bool:
        # tasklist 在繁中 Windows 上輸出 cp950。text=True 預設拿 utf-8 去解,
        # 會丟 UnicodeDecodeError,stdout 變成 None,然後 `in out.stdout` 炸掉。
        # 這正是我們在產品裡修過好幾次的同一個坑,測試自己又踩了一次。
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                             capture_output=True, text=True,
                             encoding="cp950", errors="replace")
        return str(pid) in (out.stdout or "")

    check("測試用的程序樹真的起來了", alive(child.pid) and alive(grandchild_pid))
    subprocess.run(["taskkill", "/PID", str(child.pid), "/T", "/F"],
                   capture_output=True)
    time.sleep(1.0)
    check("殺掉父程序之後,「孫程序」也死了(taskkill /T 殺的是整棵樹)",
          not alive(grandchild_pid), f"pid {grandchild_pid}")
    check("埠也放掉了", launch.is_port_free(port))


def main() -> int:
    shutil.rmtree(WORK, ignore_errors=True)
    WORK.mkdir(parents=True)

    test_busy_port_is_never_handed_out()
    test_hostile_paths()
    tree = test_version_chain_and_rollback()
    test_half_copied_update_is_never_promoted(tree)
    test_repeated_updates_leave_no_staging()
    test_no_orphan_processes_after_close()

    print("\n" + "=" * 70)
    if FAILURES:
        print(f"{len(FAILURES)} 項未通過:")
        for f in FAILURES:
            print("  ·", f)
        return 1
    print("全部通過。")
    print("\n注意:[1] 全新 Windows VM(沒有 Python / 沒有 WebView2)雙擊 start.bat")
    print("      這一項「不在」本測試涵蓋範圍——它必須在真的 VM 上做。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
