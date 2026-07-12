"""REPORT.md 產生（SPEC §5.2）—— 補給包給人看的介面。

工廠端拿到隨身碟時，第一個（也可能是唯一）會打開的檔案就是它。所以：
大型相依放**前面且醒目**（決定「要不要另外搬運」），離線機操作步驟放最後（照抄即可）。
"""

from __future__ import annotations

from . import BIG_DEPS_DIRNAME, PACKS_DIRNAME
from ._util import human_size


def _tools_table(tools: list[dict], plan_by_id: dict[str, str]) -> list[str]:
    if not tools:
        return ["（本次沒有任何工具產包。）"]
    lines = [
        "| 工具 | requires | wheel 數 | 大小 | 本次 |",
        "|------|----------|---------:|-----:|------|",
    ]
    for tool in tools:
        requires = ", ".join(tool["requires"])
        if len(requires) > 60:
            requires = requires[:57] + "…"
        action = {"rebuild": "重建", "reuse": "沿用快取"}.get(plan_by_id.get(tool["tool_id"], ""), "—")
        lines.append(
            f"| `{tool['tool_id']}` | {requires} | {tool['wheel_count']} |"
            f" {human_size(tool['total_bytes'])} | {action} |"
        )
    return lines


def _big_deps_section(big_deps: list[dict], threshold_mb: int) -> list[str]:
    lines = [f"## 大型相依（單檔 > {threshold_mb} MB）", ""]
    if not big_deps:
        lines += ["本次沒有超過門檻的大型相依，整包可以直接搬運。", ""]
        return lines

    total = sum(int(b["size"]) for b in big_deps)
    lines += [
        f"下列 {len(big_deps)} 個 wheel 共 **{human_size(total)}**，"
        f"已集中隔離在 `{BIG_DEPS_DIRNAME}\\` 資料夾（跨工具只存一份）。",
        "",
        "| wheel | 大小 | 被哪些工具使用 |",
        "|-------|-----:|----------------|",
    ]
    for entry in big_deps:
        used = "、".join(f"`{t}`" for t in entry["used_by"])
        lines.append(f"| `{entry['name']}` | {human_size(int(entry['size']))} | {used} |")
    lines += [
        "",
        f"> **這個資料夾很大，可以與補給包的其餘部分分開搬運**（例如另用一顆隨身硬碟）。",
        f"> 到離線機之後，把這些檔案放回 `{BIG_DEPS_DIRNAME}\\` 再執行 apply 即可；",
        f"> apply 會把它們放進各工具的 wheelhouse 並用 sha256 驗證重組正確。",
        f"> 若 `{BIG_DEPS_DIRNAME}\\` 缺檔就執行 apply，用到它的工具會被**跳過**"
        f"（不會留下半套），其餘工具照常套用。",
        "",
    ]
    return lines


def render_report(provision: dict, plan_by_id: dict[str, str], *, pruned: list[str] | None = None) -> str:
    tools = list(provision.get("tools", []))
    big_deps = list(provision.get("big_deps", []))
    target = provision.get("target", {})
    git = provision.get("git", {})
    threshold_mb = int(provision.get("big_threshold_mb", 0))
    total_bytes = sum(int(t["total_bytes"]) for t in tools)

    lines: list[str] = [
        "# 離線補給包 — 內容與操作說明",
        "",
        "> 本檔由 native_Provision 自動產生。它描述這一包裡有什麼，以及**在沒有網路的電腦上要怎麼用**。",
        "",
        "## 總覽",
        "",
        f"- **來源專案**：`{provision.get('source_project', '')}`",
        f"- **平台 commit**：`{git.get('platform_commit') or '（非 git 專案）'}`",
        f"- **產生時間**：{provision.get('created_at', '')}（UTC）",
        f"- **目標機器**：{target.get('platform_tag')} / Python {target.get('python_version')}"
        f" / ABI {target.get('abi')}",
        f"- **工具數**：{len(tools)}　**總大小**：{human_size(total_bytes)}"
        f"　**大型相依**：{len(big_deps)} 個",
        "",
    ]

    submodules = git.get("submodules") or {}
    if submodules:
        lines += ["<details><summary>submodule 指標</summary>", ""]
        for path, sha in sorted(submodules.items()):
            lines.append(f"- `{path}` @ `{sha}`")
        lines += ["", "</details>", ""]

    lines += _big_deps_section(big_deps, threshold_mb)

    lines += ["## 工具清單", ""] + _tools_table(tools, plan_by_id) + [""]

    skipped = list(provision.get("skipped_tools", []))
    if skipped:
        lines += ["## 未納入的工具（不需要補給包）", ""]
        for skip in skipped:
            lines.append(f"- `{skip['tool_id']}` — {skip['reason']}")
        lines.append("")

    failed = list(provision.get("failed_tools", []))
    if failed:
        lines += [
            "## ⚠ 產包失敗的工具",
            "",
            "這些工具**不在**本補給包裡；到離線機後它們的相依會裝不起來。請修正後重跑 build。",
            "",
        ]
        for fail in failed:
            reason = str(fail["reason"]).strip().replace("\n", "  \n    ")
            lines += [f"- `{fail['tool_id']}`", f"    {reason}", ""]

    if pruned:
        lines += ["## 本次清理的孤兒大相依", ""]
        for name in pruned:
            lines.append(f"- `{name}`（已無工具引用）")
        lines.append("")

    lines += _offline_steps(big_deps)
    return "\n".join(lines) + "\n"


def _offline_steps(big_deps: list[dict]) -> list[str]:
    big_note = (
        f"確認 `{BIG_DEPS_DIRNAME}\\` 內的大型相依都在"
        f"（若先前分開搬運，現在把它們放回去）。"
        if big_deps else
        f"本包沒有大型相依，`{BIG_DEPS_DIRNAME}\\` 可能不存在，這是正常的。"
    )
    return [
        "## 在沒有網路的電腦上怎麼用",
        "",
        "### 步驟 1 — 複製",
        "",
        "把**整個補給包資料夾**複製到離線機的任意位置（例如 `D:\\provision`）。",
        "",
        "### 步驟 2 — 確認大型相依就位",
        "",
        big_note,
        "",
        "先驗一次完整性（可選，但建議；它會逐檔比對 sha256）：",
        "",
        "```",
        "<可攜Python> apply.py --deppack-cache <目標> --dry-run",
        "```",
        "",
        "### 步驟 3 — 套用",
        "",
        "把補給包裡的 dep-pack 放到平台會去找的位置。`<目標>` 依啟動方式而定：",
        "",
        "| 啟動方式 | `--deppack-cache` 要指到 |",
        "|----------|--------------------------|",
        "| 可攜模式（`start.bat`） | `<APP_ROOT>\\data\\<project-key>\\deppack-cache` |",
        "| 開發模式（`start-dev.bat`） | `<平台專案>\\sidecar\\python-engine\\.deppack-cache` |",
        "| 自訂（有設 `CIM_DEPPACK_CACHE`） | 該環境變數的值 |",
        "",
        "> 可攜模式的 `<project-key>` 由 `start.bat` 產生（`<專案資料夾名>-<路徑sha256前8碼>`）。",
        "> **先跑一次 `start.bat` 讓 `data\\<project-key>\\` 長出來**，再照抄那個路徑。",
        "",
        "```",
        "runtime\\python311\\python.exe apply.py --deppack-cache <目標>",
        "```",
        "",
        "（開發模式若機器上有 `py -3.11`，也可以 `py -3.11 apply.py --deppack-cache <目標>`。）",
        "",
        "### 步驟 4 — 啟動平台，第一次開啟工具",
        "",
        "照平常方式啟動（`start.bat` 或 `start-dev.bat`）。第一次點開有相依的工具時，",
        "engine 會自動：驗證 dep-pack 的 sha256 → 用 `pip --no-index` 從補給包離線安裝",
        "→ 裝進該工具專屬的 venv。**全程不連網、不需要 admin。**",
        "",
        "同一個工具之後再啟動會走指紋快取，直接秒開。",
        "",
        "### 這一步會做什麼／不會做什麼",
        "",
        f"- apply **只搬檔案**：把 `{PACKS_DIRNAME}\\<工具>\\` 放到目標位置，"
        f"並把 `{BIG_DEPS_DIRNAME}\\` 的大 wheel 補回各工具的 `wheels\\`。",
        "- apply **不執行 pip、不連網**。真正的安裝是平台 engine 在工具首次啟動時做的。",
        "- apply 採「暫存組裝 + 原子換位」：中途失敗或斷電時，目標維持原樣，",
        "  **不會留下 wheels 不完整的 dep-pack**（那會讓 engine 的 fail-closed 驗章擋下且訊息難懂）。",
        "",
        "### 疑難排解",
        "",
        "| 症狀 | 原因 | 解法 |",
        "|------|------|------|",
        "| apply 說「大型相依未就位」 | `big-deps\\` 分開搬運後沒放回去 | 把檔案放回 `big-deps\\` 再跑一次 |",
        "| apply 說「sha256 不符」 | 搬運中檔案損毀 | 重新複製該檔案；仍失敗則在連網機重產補給包 |",
        "| 工具啟動時說 dep-pack 驗證失敗 | 目標位置的包不完整或被改過 | 重跑 apply（它會原子性覆蓋） |",
        "| 工具啟動時仍試圖連網 | `--deppack-cache` 指錯位置 | 確認它等於 engine 的 `CIM_DEPPACK_CACHE` |",
        "",
    ]
