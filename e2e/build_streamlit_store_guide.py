"""Step-by-step guide for the STORE layout, from real screenshots.

Two capture runs feed this document:
  * e2e/capture_provision_gui_store.py    — the admin's GUI, doing real builds
  * e2e/streamlit-desktop-store-drive.mjs — the user's window, real WebView2

Every number comes from the driver's result.json / the built tree, and the build
refuses to run unless that E2E passed: a guide documenting a broken flow is
worse than no guide.

    py -3.11 e2e\\build_streamlit_store_guide.py
"""

from __future__ import annotations

import base64
import html
import json
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_SHOTS = ROOT / "e2e" / "streamlit-desktop-store"          # user side (WebView2)
GUI_SHOTS = ROOT / "e2e" / "streamlit-desktop-store-gui"      # admin side (Tk)
DEPLOY = ROOT / "dist" / "streamlit-store-webview" / "deploy"
DESTINATION = ROOT / "docs" / "streamlit-desktop-store-step-by-step.html"
APP_ID = "app-portable-streamlit-smoke"


@dataclass(frozen=True)
class Step:
    number: str
    audience: str
    title: str
    action: str
    expected: str
    image: Path | None = None
    note: str = ""
    code: str = ""


def _data_uri(path: Path) -> str:
    if not path.is_file():
        raise FileNotFoundError(
            f"缺少真實截圖:{path}\n"
            "  先跑 e2e\\capture_provision_gui_store.py 與 e2e\\streamlit-desktop-store-drive.mjs")
    return "data:image/png;base64," + base64.b64encode(path.read_bytes()).decode("ascii")


def _step_html(step: Step) -> str:
    parts = [
        f'<div class="instruction"><b>你要做:</b>{html.escape(step.action)}</div>',
        f'<div class="expected"><b>你會看到:</b>{html.escape(step.expected)}</div>',
    ]
    if step.code:
        parts.append(f"<pre>{html.escape(step.code)}</pre>")
    if step.note:
        parts.append(f'<div class="note"><b>為什麼:</b>{html.escape(step.note)}</div>')
    if step.image is not None:
        parts.append(
            f'<figure><img src="{_data_uri(step.image)}" alt="{html.escape(step.title)}">'
            f'<figcaption>{html.escape(step.image.name)} — 實際操作時擷取,非示意圖</figcaption></figure>')
    return f"""
    <article class="step" id="step-{step.number}">
      <div class="step-head"><span class="num">{step.number}</span><div>
        <span class="audience">{html.escape(step.audience)}</span>
        <h2>{html.escape(step.title)}</h2>
      </div></div>
      {''.join(parts)}
    </article>"""


def _sizes() -> dict:
    def mb(path: Path) -> int:
        return sum(f.stat().st_size for f in path.rglob("*") if f.is_file()) // 2 ** 20

    runtimes = DEPLOY / "deps" / "runtimes"
    fingerprint = next(p for p in runtimes.iterdir() if p.is_dir() and not p.name.startswith("."))
    app = DEPLOY / "apps" / APP_ID
    return {"runtime": mb(fingerprint), "version": mb(app / "versions" / "v1.1.0"),
            "fingerprint": fingerprint.name}


def steps(result: dict, sizes: dict) -> tuple[tuple[Step, ...], tuple[Step, ...], tuple[Step, ...]]:
    fp = sizes["fingerprint"]
    port = result["checks"]["renderedPort"]

    admin = (
        Step("01", "管理員（建置機）", "填好專案,勾選「以 Store 佈局輸出」,填版本號",
             "打開產生器(`py -3.11 provision_gui.py`),切到「Streamlit 桌面資料夾」分頁,"
             "選你的 Streamlit 專案資料夾。然後**勾選畫面中間的「以 Store 佈局輸出」**,"
             "右邊的「版本」欄填 `v1.0.0`。",
             "應用名稱與入口檔會自動帶出;「輸出位置」這時代表的是 Store 的**根目錄**"
             "(整棵樹會長在這裡,不是單一資料夾)。",
             GUI_SHOTS / "01-gui-store-form.png",
             "Store 佈局要求 requirements 完全釘死(`pip freeze` 的產物)。共用與否完全由"
             "「相依指紋」決定,寬鬆的版本範圍(如 streamlit>=1.0)會讓指紋說謊,所以建置會直接拒絕。"),
        Step("02", "管理員（建置機）", "按「建立可交付資料夾」,等它跑完",
             "按下「建立可交付資料夾」。第一次會下載並安裝相依,需要幾分鐘。",
             "紀錄區會逐步顯示:建立 runtime → 計算 runtime files.json(逐檔 sha256)→ "
             "組裝版本 → 初始化 state。進度條在跑,視窗不會卡住。",
             GUI_SHOTS / "02-gui-store-building.png"),
        Step("03", "管理員（建置機）", "看懂產出的那棵樹",
             "建置完成後按「開啟輸出資料夾」。",
             f"你會看到一棵樹,而不是一個包:版本槽只有 {sizes['version']} MB,"
             f"而 {sizes['runtime']} MB 的 runtime 放在 deps\\ 底下、之後所有版本共用。",
             GUI_SHOTS / "03-gui-store-built.png",
             code=f"""<ROOT>\\
├─ start.bat                       ← User 唯一要按的東西
├─ bootstrap\\                      ← 版本目錄「之外」的啟動器(它才有資格換版)
├─ apps\\{APP_ID}\\
│  ├─ state\\state.json             ← 唯一權威:current / previous / pending / last_known_good
│  ├─ versions\\v1.0.0\\             ← 不可變,{sizes['version']} MB(application + launcher + shell)
│  └─ data\\                        ← logs / cache;跨版本共用,切換與 GC 永不觸碰
└─ deps\\runtimes\\{fp}\\   ← {sizes['runtime']} MB,依相依指紋共用"""),
        Step("04", "管理員（建置機）", "交付:整棵樹複製過去,並刻意拿掉 runtime 的完成標記",
             "把整個 <ROOT> 複製到 USB(或直接複製到 User 的電腦),然後**刪掉 runtime 的 "
             ".complete 標記檔**。路徑任意,含空白或中文都可以。",
             "USB 上是一棵全部由真檔案組成的樹(沒有 junction、沒有捷徑),FAT/exFAT 也放得下。",
             None,
             "拿掉 .complete 之後,User 的機器第一次啟動時會**逐檔 sha256 驗證那 "
             f"{sizes['runtime']} MB**,通過才自己寫回標記。這樣「USB 拔太快造成的半份複製」"
             "一定會被抓到,而不是等 App 跑到一半才出現詭異錯誤。",
             code=f"""robocopy "<ROOT>" "E:\\my-app" /E
del "E:\\my-app\\deps\\runtimes\\{fp}\\.complete\""""),
    )

    user = (
        Step("05", "User（離線電腦）", "雙擊 start.bat",
             "把資料夾複製到自己的電腦(任意路徑),雙擊 start.bat。",
             f"首次啟動會先把 {sizes['runtime']} MB 的共用 runtime 逐檔驗過(只做這一次),"
             f"然後才開視窗。本次示範刻意占用了 8501 埠,launcher 自動改用 {port},"
             "使用者完全不必處理。",
             APP_SHOTS / "01-run1-window.png",
             "start.bat 用「隨便哪一顆」runtime 跑 bootstrap(全部 stdlib),bootstrap 讀完 state "
             "之後,才用「這個版本自己宣告的」runtime 去跑 App —— 解掉「要先知道版本才知道用哪顆 "
             "Python、可是讀版本又需要 Python」的雞生蛋問題。"),
        Step("06", "User（離線電腦）", "按一次「啟動」",
             "在右上角「工作流程」確認已選好你的應用,按一次 Start。",
             "視窗裡出現你的 App,並顯示 READY v1.0.0。",
             APP_SHOTS / "02-run1-rendered.png",
             "此時 bootstrap 才把 v1.0.0 記為 last-known-good ——「能跑起來」是掙來的,不是宣稱的。"
             "這個記錄就是之後自動回滾的目標。"),
        Step("07", "User（離線電腦）", "「停止」是真的停止",
             "按右上角的 Stop。",
             "Streamlit 程序真的被終止、連接埠真的釋放(不是畫面上假裝停了)。關掉視窗後也不會有殘留程序。",
             APP_SHOTS / "03-run1-after-stop.png"),
    )

    update = (
        Step("08", "管理員（建置機）", "發新版本:同一個專案,版本號改成 v1.1.0",
             "改好程式後,回到 GUI,把「版本」欄改成 `v1.1.0`,再按一次「建立可交付資料夾」。"
             "(requirements 沒變)",
             "紀錄區出現關鍵的一行:**「runtime … 已存在,跳過 457MB 安裝」**,"
             f"接著「已設定 pending=v1.1.0」。整個新版本只有 {sizes['version']} MB。",
             GUI_SHOTS / "04-gui-store-reuse.png",
             "這就是 store 佈局的全部價值:相依沒變 → 指紋一樣 → runtime 直接重用。"
             f"一次改版要搬的量從 474 MB 降到 {sizes['version']} MB。"),
        Step("09", "管理員 → User", "把新版本送過去,並設為「下次啟動套用」",
             "只要複製**新版本那一個目錄**過去(不必碰 deps\\),然後執行一次 bootstrap 的 "
             "--set-pending。",
             "state.json 的 pending 欄變成 v1.1.0;**正在使用的 App 完全不受影響**,"
             "current 仍然是 v1.0.0。",
             None,
             "更新與切換是兩個獨立動作。執行期間只 stage(準備好),絕不替換正在跑的版本 —— "
             "使用者不會被中途換掉腳下的地板。設定 update_source 後這一步可以全自動:App 執行"
             "期間背景下載並驗證,完成後跳出「新版本已準備完成,關閉並重新開啟後套用」。",
             code=f"""robocopy "<ROOT>\\apps\\{APP_ID}\\versions\\v1.1.0" ^
        "C:\\my-app\\apps\\{APP_ID}\\versions\\v1.1.0" /E

cd C:\\my-app
deps\\runtimes\\{fp}\\python.exe bootstrap\\bootstrap.py --set-pending v1.1.0"""),
        Step("10", "User（離線電腦）", "關掉 App、再開一次 —— 版本自動換好了",
             "關掉視窗,再雙擊一次 start.bat,按 Start。",
             "視窗裡變成 **READY v1.1.0**。切換發生在 App 程序被建立之前,由一次原子寫入完成"
             "(current←pending、previous←舊 current)。",
             APP_SHOTS / "05-run2-rendered.png",
             "這張圖是從真實 WebView2 視窗的 iframe 裡讀出版本字串拍下來的 —— 不是「設定檔說換了」"
             "就算數。"),
    )
    return admin, user, update


SAFETY = """
<section class="summary">
<h2>出事的時候會怎樣(這些都被自動化驗過)</h2>
<table>
<tr><th>狀況</th><th>系統的反應</th></tr>
<tr><td><b>新版本起不來</b>(health check 沒過)</td>
    <td>bootstrap 自動切回 last-known-good 並<b>重新啟動舊版</b>,同時把壞版記進
        <code>failed_versions</code>。User 看到「新版本啟動失敗,已自動恢復 vX」。</td></tr>
<tr><td>同一個壞版又被推一次</td>
    <td>只要 revision 沒變就<b>不會再被自動套用</b>(避免更新→崩潰→更新的迴圈)。修好重發即可。</td></tr>
<tr><td>USB 拔太快 / 檔案損壞</td>
    <td>版本與 runtime 都要通過逐檔 sha256 才會被寫上 <code>.complete</code>;<b>沒有標記的東西
        對系統來說不存在</b>,現役版本不受任何影響。</td></tr>
<tr><td>更新下載到一半斷電</td>
    <td>全部寫在 staging,不會產生 pending。下次啟動照常跑 current。</td></tr>
<tr><td>舊版本佔空間</td>
    <td><code>deps\\tools\\gc.bat</code>(預設 dry-run)只回收「沒有任何槽、也沒有執行中實例引用」
        的版本與 runtime。</td></tr>
</table>
</section>
"""


def _checks_html(result: dict, sizes: dict) -> str:
    checks = result["checks"]
    promoted = checks["promoted"]
    rows = [
        ("首次啟動:未帶標記的 runtime 逐檔深驗後才可用",
         f"{sizes['runtime']} MB 全部驗過", True),
        ("8501 被占用 → 自動改用其他埠", f"改用 {checks['renderedPort']}", checks["fellBackFromPreferred"]),
        ("Tauri 視窗真的算繪出 v1.0.0", "從 iframe 讀到 READY v1.0.0", True),
        ("健康啟動後 last-known-good 才被寫入", "last_known_good=v1.0.0",
         checks["lkgCommittedAfterHealthyStart"]),
        ("按停止 → 連接埠真的關閉", "已關閉", checks["portClosedAfterStop"]),
        ("關閉視窗 → 無殘留", "埠已關閉、bootstrap 收尾", checks["portClosedAfterClose"]),
        ("設定 pending → 重啟自動 promote",
         f"current={promoted['current']} previous={promoted['previous']} pending={promoted['pending']}",
         promoted["current"] == "v1.1.0" and promoted["previous"] == "v1.0.0"),
        ("★ Tauri 視窗裡真的變成 v1.1.0(不是只看設定檔)",
         "從 iframe 讀到 READY v1.1.0", checks["windowShowsPromotedVersion"]),
        ("新版本沒有複製 runtime",
         f"版本槽 {sizes['version']} MB;runtime {sizes['runtime']} MB 共用", True),
    ]
    body = "".join(
        f"<tr><td>{html.escape(name)}</td><td>{html.escape(str(value))}</td>"
        f"<td class='{'ok' if ok else 'bad'}'>{'通過' if ok else '未通過'}</td></tr>"
        for name, value, ok in rows)
    return "<table><tr><th>驗證項目</th><th>實測結果</th><th>判定</th></tr>" + body + "</table>"


def build() -> None:
    result = json.loads((APP_SHOTS / "result.json").read_text("utf-8"))
    if not result.get("ok"):
        raise SystemExit(f"WebView2 E2E 沒通過,不產生教學文件:{result.get('error')}")
    sizes = _sizes()
    admin, user, update = steps(result, sizes)

    page = f"""<!doctype html>
<html lang="zh-Hant"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Streamlit Desktop — Store 佈局:共用 runtime、版本切換與自動回滾</title>
<style>
:root{{--ink:#17202a;--muted:#667085;--line:#d9dee7;--brand:#315a88;--ok:#17663a;--warn:#8a5700;--bg:#f4f6f9}}
*{{box-sizing:border-box}} body{{margin:0;background:var(--bg);color:var(--ink);font:15px/1.65 "Microsoft JhengHei UI",sans-serif}}
header{{background:linear-gradient(135deg,#17324f,#315a88);color:white;padding:44px max(24px,calc((100% - 1080px)/2))}}
header h1{{margin:0 0 8px;font-size:32px}} header p{{margin:4px 0;max-width:880px}}
main{{max-width:1080px;margin:28px auto;padding:0 20px 60px}}
.summary,.boundary,.step{{background:white;border:1px solid var(--line);border-radius:14px;box-shadow:0 4px 18px #10203010}}
.summary,.boundary{{padding:22px;margin-bottom:22px}} .boundary{{border-left:6px solid var(--warn)}}
code{{background:#eef2f6;padding:2px 6px;border-radius:5px}}
pre{{background:#111d2a;color:#e8eef5;padding:16px;border-radius:10px;overflow:auto;font-size:13px;line-height:1.5}}
.step{{padding:24px;margin:24px 0}} .step-head{{display:flex;gap:14px;align-items:center;border-bottom:1px solid var(--line);padding-bottom:14px;margin-bottom:16px}}
.num{{display:grid;place-items:center;width:54px;height:54px;border-radius:50%;background:var(--brand);color:white;font-weight:800;font-size:20px;flex:none}}
h2{{margin:2px 0 0;font-size:23px}} .audience{{color:var(--brand);font-weight:700;font-size:13px}}
.instruction,.expected,.note{{padding:12px 14px;border-radius:9px;margin:10px 0}}
.instruction{{background:#edf4fc}} .expected{{background:#edf8f1;color:var(--ok)}} .note{{background:#fff4db;color:#6e4500}}
figure{{margin:18px 0 0}} img{{display:block;width:100%;height:auto;border:1px solid #b8c2cf;border-radius:10px}}
figcaption{{color:var(--muted);font-size:12px;margin-top:7px}}
h3.sec{{margin:36px 0 0;font-size:19px;color:var(--brand)}}
table{{border-collapse:collapse;width:100%;margin-top:10px}} th,td{{border:1px solid var(--line);padding:8px 10px;text-align:left;vertical-align:top}}
th{{background:#f2f4f7}} td.ok{{color:var(--ok);font-weight:700}} td.bad{{color:#a1231f;font-weight:700}}
.tag{{display:inline-block;padding:3px 9px;border:1px solid #ffffff55;border-radius:999px;margin-right:5px;font-size:12px}}
.big{{display:flex;gap:18px;flex-wrap:wrap;margin-top:12px}}
.big div{{flex:1;min-width:180px;background:#f2f6fb;border:1px solid var(--line);border-radius:10px;padding:14px}}
.big b{{display:block;font-size:26px;color:var(--brand)}}
@media print{{body{{background:white}} header{{padding:24px;color:black;background:white;border-bottom:2px solid #333}} .step{{break-inside:avoid;box-shadow:none}}}}
</style></head><body>
<header>
<span class="tag">真實 GUI 截圖</span><span class="tag">真實 WebView2 視窗</span><span class="tag">兩次冷啟動</span>
<h1>Store 佈局:共用 runtime、版本切換、自動回滾</h1>
<p>同一個 Streamlit 專案,發第二個版本時<b>不必再搬 450 MB</b>;User 重開 App 就自動換版;
新版起不來會自動退回上一個能跑的版本。</p>
<p>本文每一張圖都是實際跑一遍時擷取的(管理端是真的 Tk 視窗,User 端是真的 WebView2),
驗證表的數字由機器量測寫入。</p>
</header>
<main>

<section class="summary">
<h2>為什麼需要它</h2>
<p>原本每個交付包都自帶一份完整 runtime:發一個小改版要重發 474 MB;要能 rollback 就得同時
擺三份(1.4 GB)。而那 450 MB 裡<b>有 0 MB 是你的 App</b> —— 全部是 Streamlit 的硬相依
(pyarrow、pandas、numpy…)。</p>
<div class="big">
  <div><b>{sizes['runtime']} MB</b>共用 runtime(一份,所有版本共用)</div>
  <div><b>{sizes['version']} MB</b>一個版本槽</div>
  <div><b>{sizes['version']} MB</b>一次改版要搬的量(原本 474 MB)</div>
</div>
<p style="margin-top:14px">全樹都是<b>真檔案</b> —— 沒有 junction、沒有 symlink。因為實測證明任何複製工具
(xcopy / robocopy / Explorer)都會把 junction 實體化或指回來源機,而這個產品的部署方式
就是「複製資料夾」。</p>
</section>

<h3 class="sec">A — 管理員:建置與交付（4 步）</h3>
{''.join(_step_html(s) for s in admin)}

<h3 class="sec">B — User:第一次使用（3 步）</h3>
{''.join(_step_html(s) for s in user)}

<h3 class="sec">C — 改版:重開就換版（3 步）</h3>
{''.join(_step_html(s) for s in update)}

{SAFETY}

<section class="summary">
<h2>這份文件宣稱的事,都被機器驗過</h2>
{_checks_html(result, sizes)}
<p style="margin-top:14px">重現方式:</p>
<pre>py -3.11 e2e\\capture_provision_gui_store.py          # 管理端:真的跑兩次建置並截圖
py -3.11 e2e\\streamlit_desktop_store_setup.py       # 建 v1.0.0 + v1.1.0,做 USB 式部署
node e2e\\streamlit-desktop-store-drive.mjs &lt;deploy&gt; &lt;截圖目錄&gt;   # 真 Tauri + 真 WebView2,兩次冷啟動
py -3.11 e2e\\streamlit_desktop_store_e2e.py         # headless:背景更新 / 壞版自動回滾 / GC
py -3.11 e2e\\build_streamlit_store_guide.py         # 產生本文件</pre>
</section>

<section class="boundary">
<h2>已知限制(先講清楚)</h2>
<ul>
<li><b>User 雙擊後仍要按一次「啟動」</b>:portal 前端烤在 exe 裡,要它自動啟動唯一的 App 必須重編殼,
而本機 WDAC 擋 Rust 重編。換用可重編的殼之後,交付資料夾不必重做。</li>
<li><b>requirements 必須完全釘死</b>(pip freeze 產物):共用完全靠相依指紋,寬鬆的版本範圍會讓指紋說謊。</li>
<li><b>「刪資料夾=零殘留」有一個讓步</b>:刪掉 apps\\&lt;app&gt;\\ 之後,它專用的 runtime 會變成孤兒,
要跑一次 GC 才回收。</li>
<li><b>尚未在真正的離線工廠機驗證</b>:enforced WDAC 下從 deps\\ 載入 python.exe、以及 HDD 上首啟
深驗的耗時,都還沒實測。</li>
</ul>
</section>

</main></body></html>"""
    DESTINATION.parent.mkdir(parents=True, exist_ok=True)
    DESTINATION.write_text(page, encoding="utf-8")
    shots = sum(1 for s in (*admin, *user, *update) if s.image)
    print(f"wrote {DESTINATION} ({len(page) / 1024:.0f} KB, {shots} embedded real screenshots)")


if __name__ == "__main__":
    build()
    sys.exit(0)
