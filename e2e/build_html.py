"""把 GUI E2E 的實測結果 + 截圖注入 HTML 模板，產出可發布的單檔說明書。

為什麼要有這一步：說明書裡的每個數字（安裝秒數、對照組結論、截圖）都應該來自
**真的跑過的那一次**，而不是手抄。截圖或結果變了，重跑一次就同步。

    py -3.11 e2e/build_html.py                       # 用預設路徑
    py -3.11 e2e/build_html.py e2e/out docs/offline-deploy.html
"""

from __future__ import annotations

import html
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
TEMPLATE = REPO / "docs" / "offline-deploy.template.html"

SCENARIO_LABEL = {
    "A-no-provision": "A",
    "B-cold-no-warmup": "B",
    "C-warmup-first": "C",
}


def esc(text: object) -> str:
    return html.escape(str(text), quote=True)


def chip(ok: bool, label_ok: str = "PASS", label_bad: str = "FAIL") -> str:
    cls = "chip--pass" if ok else "chip--fail"
    return f'<span class="chip {cls}">{label_ok if ok else label_bad}</span>'


def yes_no(value: bool, good: bool | None = None) -> str:
    """布林值 → 帶語意色的短標。good 指明「哪個值才是好的」。"""
    if good is None:
        return "是" if value else "否"
    cls = "chip--pass" if value == good else "chip--fail"
    return f'<span class="chip {cls}">{"是" if value else "否"}</span>'


def scenario_rows(results: list[dict]) -> str:
    rows: list[str] = []
    for entry in results:
        actual = entry.get("actual", {})
        secs = round((entry.get("elapsedMs") or 0) / 1000)
        label = SCENARIO_LABEL.get(entry["key"], entry["key"])

        if actual.get("errorPage"):
            screen = '<span class="chip chip--fail">ModuleNotFoundError</span>'
        elif actual.get("working"):
            screen = '<span class="chip chip--pass">LV 介面</span>'
        else:
            screen = '<span class="chip chip--warn">空白</span>'

        deps = ('<span class="chip chip--pass">ready</span>' if actual.get("depsReady")
                else '<span class="chip chip--fail">unavailable</span>')
        torch = yes_no(bool(actual.get("torch")), good=True)

        cond = esc(entry["title"])
        if entry.get("firstStartFailed"):
            first = round((entry.get("firstFailureMs") or 0) / 1000)
            cond += f'<br><span style="color:var(--caution)">首次 Start 於 {first}s 逾時失敗</span>'

        rows.append(
            "<tr>"
            f'<td class="num"><b>{label}</b></td>'
            f"<td>{cond}</td>"
            f"<td>{screen}</td>"
            f"<td>{deps}</td>"
            f"<td>{torch}</td>"
            f'<td class="num">{secs}</td>'
            f"<td>{chip(bool(entry.get('pass')))}</td>"
            "</tr>"
        )
    return "\n".join(rows)


def pick(results: list[dict], key: str) -> dict:
    for entry in results:
        if entry["key"] == key:
            return entry
    return {}


def main() -> int:
    out_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else REPO / "e2e" / "out"
    dest = Path(sys.argv[2]) if len(sys.argv) > 2 else REPO / "docs" / "offline-deploy.html"

    report = json.loads((out_dir / "result.json").read_text(encoding="utf-8"))
    figures = json.loads((out_dir / "figures.json").read_text(encoding="utf-8"))
    provision = json.loads((Path(report["provisionDir"]) / "provision.json").read_text(encoding="utf-8"))

    results = report["results"]
    cold = pick(results, "B-cold-no-warmup")
    warm = pick(results, "C-warmup-first")

    tool = provision["tools"][0]
    target = provision["target"]
    total_mb = tool["total_bytes"] / 1024 / 1024
    big_mb = sum(b["size"] for b in provision["big_deps"]) / 1024 / 1024
    passed = sum(1 for r in results if r.get("pass"))

    values = {
        "TOOL_ID": f"{tool['tool_id']}（{len(tool['requires'])} 個 requires）",
        "COMMIT": (provision["git"]["platform_commit"] or "—")[:12],
        "TARGET": f"{target['platform_tag']} / py{target['python_version']} / {target['abi']}",
        "PACK_SIZE": f"{total_mb:.1f} MB（其中 {big_mb:.1f} MB 是 torch）",
        "RUN_DATE": provision["created_at"][:10],
        "E2E_SUMMARY": f"{passed}/{len(results)} 對照組符合預期",
        "SCENARIO_ROWS": scenario_rows(results),
        "WARMUP_SECONDS": str(round((warm.get("warmupMs") or 0) / 1000)),
        "COLD_FAIL_SECONDS": str(round((cold.get("firstFailureMs") or 0) / 1000)),
        "COLD_TOTAL_SECONDS": str(round((cold.get("elapsedMs") or 0) / 1000)),
        "WARM_START_SECONDS": str(round((warm.get("elapsedMs") or 0) / 1000)),
        "FOOTER": (
            f"由 <code>e2e/gui_offline_e2e.mjs</code> 於 {provision['created_at'][:10]} 實測後產生"
            f"（Playwright over CDP → 真實 WebView2；pip 索引指向死位址 "
            f"<code>{esc(report['deadIndex'])}</code>）。"
            f"<br>重跑：<code>node e2e/gui_offline_e2e.mjs dist/provision e2e/out</code> → "
            f"<code>py -3.11 e2e/make_figures.py</code> → <code>py -3.11 e2e/build_html.py</code>"
        ),
    }

    page = TEMPLATE.read_text(encoding="utf-8")

    def sub_fig(match: re.Match) -> str:
        name = match.group(1)
        if name not in figures:
            raise SystemExit(f"[錯誤] 模板要 {name}，但 figures.json 沒有它")
        return figures[name]

    page = re.sub(r"\{\{FIG:([a-z_]+)\}\}", sub_fig, page)
    for key, value in values.items():
        page = page.replace("{{" + key + "}}", value)

    leftover = re.findall(r"\{\{[^}]+\}\}", page)
    if leftover:
        raise SystemExit(f"[錯誤] 模板還有沒填的欄位：{sorted(set(leftover))}")

    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(page, encoding="utf-8")
    print(f"寫出 {dest}（{len(page) / 1024:.0f} KB，含 {len(figures)} 張內嵌截圖）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
