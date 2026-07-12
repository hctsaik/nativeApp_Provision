"""Build a self-contained step-by-step HTML guide from real Playwright shots.

Run after ``e2e/capture_screenshots.py``. Every image is embedded as base64 so
the resulting HTML can be copied and opened offline without a companion folder.
"""

from __future__ import annotations

import base64
import html
import sys
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Step:
    number: str
    audience: str
    title: str
    action: str
    expected: str
    image: str
    note: str = ""


STEPS = (
    Step("01", "Fleet 管理員（目前 Lab）", "開啟應用列表", "目前 Lab 打開 Fleet Web Console，確認 Applications 中出現 cv-reviewer。",
         "首頁會列出 cv-reviewer；點選名稱進入應用詳情。", "01_console_home.png",
         "正式桌面入口是 Native App → Management Center → Fleet；獨立 :8090 只保留遠端管理與 Lab。"),
    Step("02", "Fleet 管理員（目前 Lab）", "進入 CV Reviewer 詳情", "點選 cv-reviewer。",
         "頁面會顯示 Channels、Releases、Builds、Rollout 與 Actions。", "02_app_overview.png"),
    Step("03", "Fleet 管理員（目前 Lab）", "填寫 Build & publish", "在 Build & publish 輸入版本 2.0.0，先不選 promote channel。",
         "按 Build 前確認版本正確；正式版本發布後不可覆蓋。", "03_build_form.png"),
    Step("04", "Fleet 管理員（目前 Lab）", "建置並發布", "按下 Build。",
         "頁面重新載入，Releases／Builds 出現 2.0.0 與建置結果。", "04_after_build.png"),
    Step("05", "Fleet 管理員（目前 Lab）", "準備 Promote", "在 Promote 選擇版本 2.0.0 與 production。",
         "確認目標 channel 後才送出；rollback 也是把 channel 指回舊版。", "05_promote_form.png"),
    Step("06", "Fleet 管理員（目前 Lab）", "上架到 Production", "按下 Promote。",
         "Channels 的 production 應指向 2.0.0。", "06_after_promote.png"),
    Step("07", "Fleet 管理員（目前 Lab）", "開始分批推送", "在 Start rollout 選擇 2.0.0，stage percent 輸入 10。",
         "按下後只讓確定性分桶中的 10% 裝置取得新版 desired state。", "07_rollout_start.png"),
    Step("08", "Fleet 管理員（目前 Lab）", "推進 Rollout", "將 rollout 從 10% 推進到 50%。",
         "Rollout 區會顯示目前百分比與 Advance／Approve／Pause／Resume 控制。", "08_rollout_controls.png"),
    Step("09", "Fleet 管理員（目前 Lab）", "註冊測試裝置", "在 Register device 輸入 device-42，群組保留 canary。",
         "按 Register 後，該裝置可由 rollout desired-state 規則管理。", "09_register_device.png"),
    Step("10", "本機管理員（Lab）", "查看裝置可用更新", "開啟 Diagnostics Device Portal。",
         "畫面顯示目前版本、LKG 與 production 可用版本，並提供立即更新。", "10_portal_available.png",
         "正式產品中這一步應從 Native App Management Center 進入，不要求使用者輸入 :8091。"),
    Step("11", "本機管理員（Lab）", "執行裝置更新", "按下立即更新。",
         "Agent 下載、驗證並原子啟用 2.0.0；畫面顯示目前版本已更新。", "11_portal_updated.png",
         "Device Portal 是 Diagnostics Only；正式 UI 將透過相同 /management API 顯示非同步進度。"),
)


# 正式桌面路徑：所有 Fleet 動作都由 Native App Management Center 開始。
NATIVE_APP_STEPS = (
    Step("01", "Fleet 管理員", "開啟 Native App", "啟動 Native App，確認目前角色為 admin。", "看到 CIM Platform 首頁與工作流程選單。", "01-native-app-home.png"),
    Step("02", "Fleet 管理員", "進入 Management Center", "在工作流程選擇「管理中心」並按 Start。", "Native App 內顯示 Management Center，不需另外開瀏覽器。", "02-management-center.png"),
    Step("03", "本機管理員", "查看 Applications", "點選 Applications，查看這台電腦的應用狀態。", "同一畫面列出 AI4BI、Large Vision、Annotation 與已安裝版本。", "03-management-center-applications.png"),
    Step("04", "Fleet 管理員", "開啟 Fleet", "點選 Fleet。", "同一個 Native App 視窗內出現中央 Applications。", "04-management-center-fleet.png"),
    Step("05", "Fleet 管理員", "進入 CV Reviewer", "點選 cv-reviewer。", "看到 Channels、Releases、Builds、Rollout 與管理動作。", "05-fleet-cv-reviewer.png"),
    Step("06", "Fleet 管理員", "填寫 Build & publish", "輸入版本 2.0.0，promote channel 先留空。", "中央建置表單已準備完成。", "06-fleet-build-form.png"),
    Step("07", "Fleet 管理員", "建置並發布", "按下 Build。", "Releases 與 Builds 出現 2.0.0 成功紀錄。", "07-fleet-after-build.png"),
    Step("08", "Fleet 管理員", "準備 Promote", "選擇版本 2.0.0 與 production。", "升版目標已設定。", "08-fleet-promote-form.png"),
    Step("09", "Fleet 管理員", "上架 Production", "按下 Promote。", "production channel 指向 2.0.0。", "09-fleet-after-promote.png"),
    Step("10", "Fleet 管理員", "準備分批推送", "選擇 2.0.0，stage percent 輸入 10。", "只先讓 10% 目標裝置收到 desired state。", "10-fleet-rollout-form.png"),
    Step("11", "Fleet 管理員", "啟動 Rollout", "按下 Start rollout。", "Management Center 顯示 rollout 狀態及後續控制。", "11-fleet-rollout-active.png"),
)


def _data_uri(path: Path) -> str:
    if not path.is_file():
        raise FileNotFoundError(f"missing real Playwright screenshot: {path}")
    return "data:image/png;base64," + base64.b64encode(path.read_bytes()).decode("ascii")


def _step_html(step: Step, shots: Path) -> str:
    note = f'<div class="note"><b>產品邊界：</b>{html.escape(step.note)}</div>' if step.note else ""
    return f"""
    <article class="step" id="step-{step.number}">
      <div class="step-head"><span class="num">{step.number}</span><div>
        <span class="audience">{html.escape(step.audience)}</span>
        <h2>{html.escape(step.title)}</h2>
      </div></div>
      <div class="instruction"><b>你要做：</b>{html.escape(step.action)}</div>
      <div class="expected"><b>你會看到：</b>{html.escape(step.expected)}</div>
      {note}
      <figure><img src="{_data_uri(shots / step.image)}" alt="{html.escape(step.title)} 真實操作截圖">
        <figcaption>{html.escape(step.image)} — 由 Playwright 實際操作後擷取</figcaption></figure>
    </article>"""


def build(shots: Path, destination: Path) -> None:
    cards = "\n".join(_step_html(step, shots) for step in NATIVE_APP_STEPS)
    page = f"""<!doctype html>
<html lang="zh-Hant"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Native App 離線更新 — 真實 GUI Step-by-Step</title>
<style>
:root{{--ink:#17202a;--muted:#667085;--line:#d9dee7;--brand:#315a88;--ok:#17663a;--warn:#8a5700;--bg:#f4f6f9}}
*{{box-sizing:border-box}} body{{margin:0;background:var(--bg);color:var(--ink);font:15px/1.65 "Microsoft JhengHei UI",sans-serif}}
header{{background:linear-gradient(135deg,#17324f,#315a88);color:white;padding:44px max(24px,calc((100% - 1080px)/2))}}
header h1{{margin:0 0 8px;font-size:32px}} header p{{margin:4px 0;max-width:850px}}
main{{max-width:1080px;margin:28px auto;padding:0 20px 60px}} .summary,.boundary,.step{{background:white;border:1px solid var(--line);border-radius:14px;box-shadow:0 4px 18px #10203010}}
.summary,.boundary{{padding:22px;margin-bottom:22px}} .boundary{{border-left:6px solid var(--warn)}}
code{{background:#eef2f6;padding:2px 6px;border-radius:5px}} pre{{background:#111d2a;color:#e8eef5;padding:16px;border-radius:10px;overflow:auto}}
.step{{padding:24px;margin:24px 0}} .step-head{{display:flex;gap:14px;align-items:center;border-bottom:1px solid var(--line);padding-bottom:14px;margin-bottom:16px}}
.num{{display:grid;place-items:center;width:54px;height:54px;border-radius:50%;background:var(--brand);color:white;font-weight:800;font-size:20px}}
h2{{margin:2px 0 0;font-size:23px}} .audience{{color:var(--brand);font-weight:700;font-size:13px;text-transform:uppercase}}
.instruction,.expected,.note{{padding:12px 14px;border-radius:9px;margin:10px 0}} .instruction{{background:#edf4fc}} .expected{{background:#edf8f1;color:var(--ok)}} .note{{background:#fff4db;color:#6e4500}}
figure{{margin:18px 0 0}} img{{display:block;width:100%;height:auto;border:1px solid #b8c2cf;border-radius:10px}} figcaption{{color:var(--muted);font-size:12px;margin-top:7px}}
.tag{{display:inline-block;padding:3px 9px;border:1px solid #ffffff55;border-radius:999px;margin-right:5px;font-size:12px}}
@media print{{body{{background:white}} header{{padding:24px;color:black;background:white;border-bottom:2px solid #333}} .step{{break-inside:avoid;box-shadow:none}}}}
</style></head><body>
<header><span class="tag">真實 Playwright 點擊</span><span class="tag">11 張內嵌截圖</span><span class="tag">可離線開啟</span>
<h1>Native App 管理中心／離線更新 GUI 操作教學</h1>
<p>本文件的畫面由隔離 Lab 啟動真實服務後，使用 Playwright 實際點擊 Build、Promote、Rollout 與 Update 所產生；不是示意圖。</p></header>
<main>
<section class="summary"><h2>開始前</h2><p>在 repo 根目錄執行：</p><pre>py -3.11 demo\\lab_serve.py</pre>
<p><b>唯一正式入口：</b>Native App → Management Center → Fleet。管理員不需另開 Web Console，也不需記住 localhost port。</p></section>
<section class="boundary"><h2>先理解正式產品邊界</h2>
<p><b>步驟 01–11</b> 全部是在 Native App 視窗內真實操作的截圖。Applications 是裝置端管理；Fleet 是中央治理，但兩者都由 Management Center 進入。</p></section>
{cards}
<section class="summary"><h2>完成檢查</h2><ul>
<li>Production channel 指向 2.0.0。</li><li>Rollout 已建立並可分階段推進。</li><li>裝置 active version 更新為 2.0.0。</li><li>更新失敗時應保留 last-known-good。</li></ul>
<p>重建本文件：<code>py -3.11 e2e\\capture_screenshots.py</code>，再執行 <code>py -3.11 e2e\\build_gui_step_guide.py</code>。</p></section>
</main></body></html>"""
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(page, encoding="utf-8")
    print(f"wrote {destination} ({len(page) / 1024:.0f} KB, {len(NATIVE_APP_STEPS)} embedded real screenshots)")


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    shots = Path(args[0]) if args else ROOT / "e2e" / "native-app-fleet"
    destination = Path(args[1]) if len(args) > 1 else ROOT / "docs" / "native-update-gui-step-by-step.html"
    build(shots, destination)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
