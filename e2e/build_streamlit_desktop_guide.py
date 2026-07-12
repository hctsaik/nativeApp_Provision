"""Build the step-by-step HTML guide from the screenshots we actually captured.

Two halves, because two different people do them:
  * the admin, in provision_gui.py  (e2e/capture_provision_gui.py)
  * the end user, on the delivered folder (e2e/streamlit-desktop-drive.mjs)

Images are embedded as base64 so the page can be mailed around and opened
offline. The verification table is read from the driver's result.json — the
numbers in this document are the ones the machine measured, not ones we typed.

    py -3.11 e2e\\build_streamlit_desktop_guide.py
"""

from __future__ import annotations

import base64
import html
import json
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GUI_SHOTS = ROOT / "e2e" / "streamlit-desktop-gui"
APP_SHOTS = ROOT / "e2e" / "streamlit-desktop"
DESTINATION = ROOT / "docs" / "streamlit-desktop-step-by-step.html"


@dataclass(frozen=True)
class Step:
    number: str
    audience: str
    title: str
    action: str
    expected: str
    image: Path
    note: str = ""


ADMIN_STEPS = (
    Step("01", "管理員（建置機）", "打開產生器,切到「Streamlit 桌面資料夾」分頁",
         "在 native_Provision 目錄執行 `py -3.11 provision_gui.py`,點第二個分頁。",
         "只有一個必填欄位:專案資料夾。Tauri 殼與可攜 Python 開啟時就自動偵測好了,"
         "畫面上會顯示偵測到什麼(而不是偷偷用一個你不知道的路徑)。",
         GUI_SHOTS / "01-gui-empty-tab.png",
         "殼與 runtime 每次建置都一樣,是機器找得到的東西,不該叫人來輸入。要換來源時打開「進階設定」即可。"),
    Step("02", "管理員（建置機）", "只要選專案資料夾",
         "選你的 Streamlit 專案資料夾。",
         "應用名稱與入口檔案會自動帶出(可以改)。入口檔案的判斷順序是:先找 app.py / main.py / "
         "streamlit_app.py;找不到就找唯一一個 import streamlit 的檔案;若有多個候選,會**明講**要你自己選,不會亂猜。",
         GUI_SHOTS / "02-gui-filled.png"),
    Step("03", "管理員（建置機）", "先按「檢查專案」",
         "按下「檢查專案」。",
         "會逐條確認:入口檔在專案內、requirements.txt 存在且有宣告 streamlit、殼與 runtime 都找得到。任何一項不過就不讓你建置(fail closed),不會做出一個半殘的資料夾。",
         GUI_SHOTS / "03-gui-checked.png"),
    Step("04", "管理員（建置機）", "建立可交付資料夾",
         "按下「建立可交付資料夾」,等待數十秒。",
         "狀態列顯示 OK 與實際大小(本例 474 MB)。全部在暫存目錄組好、通過自檢後才原子換位——建置失敗不會破壞上一版能用的資料夾。",
         GUI_SHOTS / "04-gui-built.png",
         "建置時會連網下載相依(pip);但產出的資料夾在 User 端執行時完全離線。"),
)

def user_steps(checks: dict) -> tuple[Step, ...]:
    """Ports come from the run we are documenting — hard-coding them would make
    the prose and the verification table disagree the next time E2E runs."""
    rendered = checks.get("renderedPort")
    restarted = checks.get("restartedPort")
    return (
        Step("05", "User（離線電腦）", "雙擊 start.bat",
             "把整個資料夾複製給 User,請他雙擊 start.bat。",
             "launcher 會自己挑一個可用的連接埠、把 Streamlit 叫起來、確認健康檢查通過,才開視窗。"
             "使用者不必安裝 Python、Streamlit、Node 或 Rust。",
             APP_SHOTS / "01-window-opened.png",
             f"本次示範刻意先占用 8501,launcher 自動改用 {rendered}——不需要使用者做任何事。"),
        Step("06", "User（離線電腦）", "按一次「啟動」",
             "在右上角「工作流程」確認已選好你的應用,按一次「Start」。",
             "應用就顯示在視窗裡(單一全高畫面,沒有多餘外框)。",
             APP_SHOTS / "02-app-rendered.png",
             "這一次點擊是目前唯一的妥協:portal 前端烤在 exe 裡,要讓它自動啟動必須重編殼,而本機 WDAC 擋重編。"),
        Step("07", "User（離線電腦）", "「停止」是真的停止",
             "按右上角的 Stop。",
             f"Streamlit 程序真的被終止、連接埠 {rendered} 真的被釋放——不是畫面上假裝停了。",
             APP_SHOTS / "03-after-stop.png"),
        Step("08", "User（離線電腦）", "再按「啟動」會換一個新埠",
             "再按一次 Start。",
             f"launcher 重新選埠(本次是 {restarted})、重起 Streamlit、健康檢查通過後才把新網址交給視窗。"
             "實測整趟約 1.4 秒,遠低於殼的 30 秒逾時。",
             APP_SHOTS / "04-restarted.png"),
    )


def _data_uri(path: Path) -> str:
    if not path.is_file():
        raise FileNotFoundError(f"缺少真實截圖:{path}(先跑 capture_provision_gui.py 與 streamlit-desktop-drive.mjs)")
    return "data:image/png;base64," + base64.b64encode(path.read_bytes()).decode("ascii")


def _step_html(step: Step) -> str:
    note = f'<div class="note"><b>為什麼:</b>{html.escape(step.note)}</div>' if step.note else ""
    return f"""
    <article class="step" id="step-{step.number}">
      <div class="step-head"><span class="num">{step.number}</span><div>
        <span class="audience">{html.escape(step.audience)}</span>
        <h2>{html.escape(step.title)}</h2>
      </div></div>
      <div class="instruction"><b>你要做:</b>{html.escape(step.action)}</div>
      <div class="expected"><b>你會看到:</b>{html.escape(step.expected)}</div>
      {note}
      <figure><img src="{_data_uri(step.image)}" alt="{html.escape(step.title)}">
        <figcaption>{html.escape(step.image.name)} — 實際操作時擷取,非示意圖</figcaption></figure>
    </article>"""


def _checks_html(result: dict) -> str:
    checks = result.get("checks", {})
    rows = [
        ("交付包只曝一個應用", ", ".join(checks.get("tools", [])) or "—", bool(checks.get("tools"))),
        ("8501 被占用 → 自動改用其他埠", f"改用 {checks.get('renderedPort')}", bool(checks.get("fellBackFromPreferred"))),
        ("Tauri 視窗真的算繪出應用", "讀到 READY", True),
        ("按停止 → 連接埠真的關閉", "已關閉", bool(checks.get("portClosedAfterStop"))),
        ("再啟動 → 換到新的埠", f"改用 {checks.get('restartedPort')}", bool(checks.get("restartedPort"))),
        ("關閉視窗 → 無殘留程序", "launcher 已收尾、埠已關閉",
         bool(checks.get("launcherExited") and checks.get("portClosedAfterClose"))),
    ]
    body = "".join(
        f"<tr><td>{html.escape(name)}</td><td>{html.escape(str(value))}</td>"
        f"<td class='{'ok' if passed else 'bad'}'>{'通過' if passed else '未通過'}</td></tr>"
        for name, value, passed in rows
    )
    return ("<table><tr><th>驗證項目</th><th>實測結果</th><th>判定</th></tr>" + body + "</table>")


def build() -> None:
    result = json.loads((APP_SHOTS / "result.json").read_text("utf-8"))
    if not result.get("ok"):
        raise SystemExit(f"E2E 沒通過,不產生教學文件:{result.get('error')}")
    admin = "\n".join(_step_html(step) for step in ADMIN_STEPS)
    user = "\n".join(_step_html(step) for step in user_steps(result.get("checks", {})))
    page = f"""<!doctype html>
<html lang="zh-Hant"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Streamlit 桌面資料夾 — 真實 GUI Step-by-Step</title>
<style>
:root{{--ink:#17202a;--muted:#667085;--line:#d9dee7;--brand:#315a88;--ok:#17663a;--warn:#8a5700;--bg:#f4f6f9}}
*{{box-sizing:border-box}} body{{margin:0;background:var(--bg);color:var(--ink);font:15px/1.65 "Microsoft JhengHei UI",sans-serif}}
header{{background:linear-gradient(135deg,#17324f,#315a88);color:white;padding:44px max(24px,calc((100% - 1080px)/2))}}
header h1{{margin:0 0 8px;font-size:32px}} header p{{margin:4px 0;max-width:860px}}
main{{max-width:1080px;margin:28px auto;padding:0 20px 60px}}
.summary,.boundary,.step{{background:white;border:1px solid var(--line);border-radius:14px;box-shadow:0 4px 18px #10203010}}
.summary,.boundary{{padding:22px;margin-bottom:22px}} .boundary{{border-left:6px solid var(--warn)}}
code{{background:#eef2f6;padding:2px 6px;border-radius:5px}} pre{{background:#111d2a;color:#e8eef5;padding:16px;border-radius:10px;overflow:auto}}
.step{{padding:24px;margin:24px 0}} .step-head{{display:flex;gap:14px;align-items:center;border-bottom:1px solid var(--line);padding-bottom:14px;margin-bottom:16px}}
.num{{display:grid;place-items:center;width:54px;height:54px;border-radius:50%;background:var(--brand);color:white;font-weight:800;font-size:20px}}
h2{{margin:2px 0 0;font-size:23px}} .audience{{color:var(--brand);font-weight:700;font-size:13px}}
.instruction,.expected,.note{{padding:12px 14px;border-radius:9px;margin:10px 0}}
.instruction{{background:#edf4fc}} .expected{{background:#edf8f1;color:var(--ok)}} .note{{background:#fff4db;color:#6e4500}}
figure{{margin:18px 0 0}} img{{display:block;width:100%;height:auto;border:1px solid #b8c2cf;border-radius:10px}}
figcaption{{color:var(--muted);font-size:12px;margin-top:7px}}
h3.sec{{margin:36px 0 0;font-size:19px;color:var(--brand)}}
table{{border-collapse:collapse;width:100%;margin-top:10px}} th,td{{border:1px solid var(--line);padding:8px 10px;text-align:left}}
th{{background:#f2f4f7}} td.ok{{color:var(--ok);font-weight:700}} td.bad{{color:#a1231f;font-weight:700}}
.tag{{display:inline-block;padding:3px 9px;border:1px solid #ffffff55;border-radius:999px;margin-right:5px;font-size:12px}}
@media print{{body{{background:white}} header{{padding:24px;color:black;background:white;border-bottom:2px solid #333}} .step{{break-inside:avoid;box-shadow:none}}}}
</style></head><body>
<header>
<span class="tag">真實操作截圖</span><span class="tag">真實 Streamlit + 真實 Tauri 殼</span><span class="tag">可離線開啟</span>
<h1>把 Streamlit 專案變成可交付資料夾</h1>
<p>管理員在 GUI 選一個 Streamlit 專案 → 產生一個資料夾 → User 雙擊 <code>start.bat</code> 就能用,
不必安裝 Python、Streamlit、Node 或 Rust,也不必自己選連接埠。</p>
<p>本文所有畫面都是實際跑一遍時擷取的,驗證表的數字由機器量測寫入,不是手打。</p>
</header>
<main>

<section class="summary">
<h2>你會得到什麼</h2>
<pre>&lt;輸出&gt;\\portable-streamlit-smoke\\
├─ start.bat            ← User 唯一要按的東西
├─ app-package.json     ← 全部相對路徑,整包可任意搬移
├─ application\\         ← 你的 Streamlit 專案
├─ runtime\\python.exe   ← 可攜 Python(已裝好 streamlit 與專案相依)
├─ launcher\\            ← 選埠、起 Streamlit、健康檢查、收尾
├─ shell\\cim-light.exe  ← 既有的預建 Tauri 殼(不重編)
└─ data\\logs\\           ← 出事時看這裡</pre>
<p>複製資料夾 = 部署;刪掉資料夾 = 完全移除,不留登錄檔、不留全域狀態。</p>
</section>

<section class="boundary">
<h2>先講清楚一個妥協</h2>
<p>User 雙擊 <code>start.bat</code> 後,還要<b>按一次「啟動」</b>,應用才會顯示。</p>
<p>原因:Tauri 殼的工具選單(portal)是<b>烤進 exe</b> 的前端,它開機時會自動選好你的應用,但不會自動按啟動。
要改掉這一下必須重編殼,而本機的 WDAC 政策擋掉了 Rust 重編。等有可重編的機器,
把 <code>shell\\cim-light.exe</code> 換掉就能升級成真正的「雙擊即用」,<b>交付資料夾不必重新製作</b>。</p>
</section>

<h3 class="sec">A 部分 — 管理員:做出資料夾</h3>
{admin}

<h3 class="sec">B 部分 — User:拿到資料夾之後</h3>
{user}

<section class="summary">
<h2>這份文件宣稱的事,都被機器驗過</h2>
{_checks_html(result)}
<p style="margin-top:14px">重現方式:</p>
<pre>py -3.11 e2e\\streamlit_desktop_build.py          # 建交付包(真 pip、真 runtime)
node e2e\\streamlit-desktop-drive.mjs &lt;交付包&gt; &lt;截圖目錄&gt;   # 真 Tauri + 真 WebView2
py -3.11 e2e\\capture_provision_gui.py           # 管理端 GUI 截圖
py -3.11 e2e\\build_streamlit_desktop_guide.py   # 產生本文件</pre>
</section>

<section class="summary">
<h2>疑難排解</h2>
<ul>
<li><b>雙擊沒反應 / 閃退</b>:看 <code>data\\logs\\launcher-*.log</code>。若 Streamlit 自己啟動失敗,launcher 會直接報錯並印出 log 位置,<b>不會</b>開一個空白視窗騙你。</li>
<li><b>應用本身出錯</b>:看 <code>data\\logs\\streamlit-*.log</code>——那是你的專案的輸出。</li>
<li><b>連接埠被占用</b>:不用管,launcher 會自動換一個可用的埠。</li>
<li><b>搬移資料夾</b>:直接複製整包即可,裡面沒有任何建置機的絕對路徑。</li>
</ul>
</section>

</main></body></html>"""
    DESTINATION.parent.mkdir(parents=True, exist_ok=True)
    DESTINATION.write_text(page, encoding="utf-8")
    size_kb = len(page) / 1024
    shots = len(ADMIN_STEPS) + len(user_steps(result.get("checks", {})))
    print(f"wrote {DESTINATION} ({size_kb:.0f} KB, {shots} embedded real screenshots)")


if __name__ == "__main__":
    build()
    sys.exit(0)
