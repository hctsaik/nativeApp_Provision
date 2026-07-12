#!/usr/bin/env python3
"""warmup.py — 在離線機**開 GUI 之前**先把相依裝進 per-tool venv。

為什麼需要它（GUI E2E 實測發現）：
    Tauri 殼的 HTTP bridge（`bridge.rs::api_post`）對 engine 的請求有 **30 秒逾時**。
    而工具首次啟動時，engine 會在 `POST /tools/<id>/start` 的處理過程中同步安裝相依
    （`_prewarm_deps_and_timeout`）。torch 這種等級的相依實測要 76 秒 → 殼在 30 秒
    就放棄，portal 顯示「Failed to start tool: undefined」。
    （engine 其實有把相依裝完、Streamlit 也起來了，只是沒人收到回應。）

    先跑一次 warmup，把安裝成本移出「按下 Start」那一刻，第一次點開就會成功。

它跟 apply.py 的分工：
    apply.py   只搬檔案。stdlib-only、不連網、不跑 pip。**不需要平台專案。**
    warmup.py  真的裝。借用平台自己的 `core.tool_deps`（同一套驗章 + `pip --no-index`）。
               **需要平台專案，而且必須用平台會用的那顆 Python 跑。**

用法（在離線機，補給包資料夾內）：
    <平台用的python> warmup.py --project <平台專案> --deppack-cache <目標> --tool-venvs <venv根>

    可攜模式：
      runtime\\python311\\python.exe warmup.py ^
          --project      D:\\CIM\\engine ^
          --deppack-cache D:\\CIM\\data\\engine-a1b2c3d4\\deppack-cache ^
          --tool-venvs    D:\\CIM\\data\\engine-a1b2c3d4\\tool-venvs

全程不連網：相依只從 apply 過的 dep-pack 以 `pip --no-index --find-links` 安裝；
dep-pack 驗章失敗時平台會 fail-closed（拒裝，且不退回連 PyPI）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

PROVISION_MANIFEST = "provision.json"
ENGINE_SUBPATH = ("sidecar", "python-engine")


def guard_console_encoding() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # type: ignore[union-attr]
        except Exception:
            pass


def human_secs(seconds: float) -> str:
    return f"{seconds:.1f}s" if seconds < 60 else f"{int(seconds // 60)}m{int(seconds % 60):02d}s"


def load_tools(root: Path, wanted: set[str] | None) -> list[tuple[str, list[str]]]:
    """從補給包的 provision.json 讀「哪些工具、各要什麼相依」。

    刻意不去掃 plugin.yaml：補給包裡有什麼，就只暖機什麼。掃不到的工具本來也沒 wheel。
    """
    manifest_path = root / PROVISION_MANIFEST
    if not manifest_path.is_file():
        raise SystemExit(f"[錯誤] 找不到 {manifest_path}——這個資料夾不是補給包。")
    try:
        tools = json.loads(manifest_path.read_text(encoding="utf-8")).get("tools", [])
    except (OSError, ValueError) as exc:
        raise SystemExit(f"[錯誤] {PROVISION_MANIFEST} 無法解析：{exc}") from exc

    known = {str(t["tool_id"]) for t in tools}
    if wanted is not None:
        unknown = wanted - known
        if unknown:
            raise SystemExit(
                f"[錯誤] --tools 指定了補給包裡沒有的工具：{'、'.join(sorted(unknown))}\n"
                f"       這包裡有：{'、'.join(sorted(known)) or '（無）'}"
            )
        tools = [t for t in tools if str(t["tool_id"]) in wanted]

    return [(str(t["tool_id"]), list(t.get("requires", []))) for t in tools]


def import_tool_deps(project_root: Path):
    """借平台自己的 core.tool_deps（同一套驗章與離線安裝邏輯，不重造）。"""
    engine_root = project_root.joinpath(*ENGINE_SUBPATH)
    if not (engine_root / "engine.py").is_file():
        raise SystemExit(
            f"[錯誤] {project_root} 不是 CIM 平台專案（找不到 {engine_root / 'engine.py'}）"
        )
    sys.path.insert(0, str(engine_root))
    try:
        from core.tool_deps import ensure_tool_deps  # type: ignore[import-not-found]
    except ImportError as exc:
        raise SystemExit(f"[錯誤] 無法 import 平台的 core.tool_deps：{exc}") from exc
    return ensure_tool_deps


def check_interpreter() -> None:
    """venv 的 ABI 綁在建立它的直譯器上。用錯 Python 暖機 = engine 之後會重建（白做）。"""
    if sys.version_info[:2] != (3, 11):
        print(f"[警告] 這支直譯器是 Python {sys.version_info.major}.{sys.version_info.minor}，"
              f"平台鎖 3.11。", file=sys.stderr)
        print("       請改用平台會用的那顆 Python（可攜模式 = runtime\\python311\\python.exe），"
              "否則 engine 啟動工具時會再建一次 venv。", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    guard_console_encoding()
    parser = argparse.ArgumentParser(
        description="離線機：先把 per-tool 相依裝好，讓第一次按 Start 就成功",
    )
    parser.add_argument("--project", required=True, help="CIM 平台專案根")
    parser.add_argument("--deppack-cache", required=True,
                        help="apply.py 套用到的位置（= 平台的 CIM_DEPPACK_CACHE）")
    parser.add_argument("--tool-venvs", required=True,
                        help="per-tool venv 的家（= 平台的 CIM_TOOL_VENVS_DIR）")
    parser.add_argument("--tools", default=None, help="只暖機這些工具（逗號分隔）")
    parser.add_argument("--dry-run", action="store_true", help="只列出會做什麼")
    args = parser.parse_args(argv)

    root = Path(__file__).resolve().parent
    cache = Path(args.deppack_cache).resolve()
    venvs = Path(args.tool_venvs).resolve()
    wanted = {t.strip() for t in args.tools.split(",") if t.strip()} if args.tools else None

    tools = load_tools(root, wanted)
    if not tools:
        print("補給包裡沒有需要暖機的工具。")
        return 0

    if not (cache.is_dir() and any(cache.iterdir())):
        raise SystemExit(
            f"[錯誤] {cache} 是空的——請先跑一次：\n"
            f"        python apply.py --deppack-cache {cache}"
        )

    check_interpreter()
    print(f"補給包  ：{root}")
    print(f"平台專案：{args.project}")
    print(f"dep-pack：{cache}")
    print(f"venv 根 ：{venvs}")
    print(f"直譯器  ：{sys.executable}")
    print("")

    if args.dry_run:
        for tool_id, requires in tools:
            print(f"  [計畫] {tool_id}：{len(requires)} 個 requires → {venvs / tool_id}")
        return 0

    # 這兩個必須在 import 之前設好也行、之後設也行（tool_deps 是執行期讀 env），
    # 但設在呼叫之前最不容易出錯。
    os.environ["CIM_DEPPACK_CACHE"] = str(cache)
    os.environ["CIM_TOOL_VENVS_DIR"] = str(venvs)
    venvs.mkdir(parents=True, exist_ok=True)

    ensure_tool_deps = import_tool_deps(Path(args.project).resolve())

    failures: list[tuple[str, str]] = []
    for tool_id, requires in tools:
        if not requires:
            continue
        print(f"  [暖機] {tool_id}（{len(requires)} 個 requires）… ", end="", flush=True)
        started = time.monotonic()
        result = ensure_tool_deps(tool_id, requires)
        elapsed = time.monotonic() - started
        if result.ok:
            how = "指紋命中，跳過 pip" if not result.installed else "離線安裝完成"
            print(f"{how}（{human_secs(elapsed)}）")
        else:
            print("失敗")
            print(f"         {result.message.strip()[:400]}")
            failures.append((tool_id, result.message))

    print("")
    if failures:
        print(f"失敗 {len(failures)} 個：{'、'.join(t for t, _ in failures)}")
        print("常見原因：apply.py 沒跑過 / dep-pack 驗章失敗（檔案被改過）/ "
              "用了與平台不同的 Python。")
        return 1

    print(f"全部就緒（{len(tools)} 個工具）。現在啟動平台，第一次點開工具就會直接算繪。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
