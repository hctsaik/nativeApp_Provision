# native_Provision — 離線補給包產生器

在**有網路的開發機**上掃描一個 CIM 平台專案，把所有 plugin 宣告的 Python 相依
（`plugin.yaml` 的 `requires:`）預先下載成「離線補給包」；複製到**沒有網路的電腦**後
執行一步 `apply.py`，平台引擎即可全程離線安裝所有工具相依。

> 完整規格見 [SPEC.md](SPEC.md)。
> 給工廠端使用者的操作說明書見 [docs/OFFLINE_DEPLOY.md](docs/OFFLINE_DEPLOY.md)
> Application Registry／MinIO 長期架構與本機 Lab 見 [docs/LOCAL_CONTROL_PLANE_LAB.md](docs/LOCAL_CONTROL_PLANE_LAB.md)。
> 真實 Playwright 點擊與逐步截圖教學見 [docs/native-update-gui-step-by-step.html](docs/native-update-gui-step-by-step.html)。
> （含 GUI 截圖的圖文版：[docs/offline-deploy.html](docs/offline-deploy.html)）。

## Streamlit 桌面資料夾（簡易版交付）

GUI 的第二個分頁做的是另一件事：**把一個 Streamlit 專案變成可以直接交給 User 的資料夾**
（可攜 Python + 你的專案 + 既有的預建 Tauri 殼 + launcher）。User 雙擊 `start.bat` 就能用，
不需安裝 Python、Streamlit、Node 或 Rust，也不需自己選連接埠（8501 被占用時會自動改用可用埠）。

- 逐步操作教學（真實截圖，可離線開啟）：[`docs/streamlit-desktop-step-by-step.html`](docs/streamlit-desktop-step-by-step.html)
- 設計與預建殼的 Phase 0 調查：[`docs/SIMPLE_STREAMLIT_TAURI_FOLDER_BUILDER_PHASE0_AND_DESIGN.md`](docs/SIMPLE_STREAMLIT_TAURI_FOLDER_BUILDER_PHASE0_AND_DESIGN.md)

兩個已知限制，先講清楚：

1. **User 雙擊後還要按一次「啟動」**。Tauri 殼的工具選單是烤進 exe 的前端，要讓它自動啟動唯一的
   應用必須重編殼，而本機 WDAC 擋 Rust 重編。等有可重編的機器，換掉 `shell/cim-light.exe`
   就能升級成真正的「雙擊即用」，**交付資料夾不必重做**。
2. **交付包約 470 MB**。成本幾乎全來自 Streamlit 的硬相依（pyarrow 85 MB、pandas 66 MB、
   numpy 53 MB…），與你的 app 大小無關。

這個流程與 dep-pack／`.napp` 發布**完全分開**：產出物不進 registry、不做 rollout，
複製資料夾＝部署，刪掉資料夾＝完全移除。

同分頁另有 **Store 佈局輸出**（勾選「以 Store 佈局輸出」+ 版本號）：版本化目錄 +
共用 runtime（同 requirements 的新版本**零複製**，一次改版只搬 ~16MB 而非 474MB）+
背景更新 + 啟動時自動套用 pending + 壞版自動回滾（PROD/PREV/NEXT 語意）。
需要**完全釘死**的 requirements（`pip freeze` 產物）。

- 逐步操作教學（真實 WebView2 截圖，兩次冷啟動證明視窗真的換版）：
  [`docs/streamlit-desktop-store-step-by-step.html`](docs/streamlit-desktop-store-step-by-step.html)
- 實作規格與缺陷紀錄：[`docs/STREAMLIT_DESKTOP_ATOMIC_UPDATE_IMPLEMENTATION_SPEC.md`](docs/STREAMLIT_DESKTOP_ATOMIC_UPDATE_IMPLEMENTATION_SPEC.md)
- 為什麼不用 junction／不用實體資料夾輪轉（含實測）：[`docs/STREAMLIT_DESKTOP_STORE_AND_SLOTS_DESIGN.md`](docs/STREAMLIT_DESKTOP_STORE_AND_SLOTS_DESIGN.md)

操作上**只需要選一個東西：Streamlit 專案資料夾**。應用名稱與入口檔案會自動帶出，
Tauri 殼與可攜 Python runtime 會自動偵測（偵測結果會顯示在畫面上；要換來源時打開「進階設定」）。

第一次使用若還沒有可攜 Python runtime，GUI 會出現「下載可攜 Python」按鈕（需連網，只需做一次）。
也可以自己先跑：

```powershell
powershell -File ..\nativeApp\scripts\win\fetch-standalone-python.ps1 `
  -DestRoot .runtime-cache\python311 -Flatten
```

## 打包 GUI（建議用法）

在可連網的 Windows 建置機雙擊：

```text
start-gui.bat
```

或使用 Python 3.11 啟動：

```powershell
py -3.11 provision_gui.py
```

GUI 依序引導發布人員選擇 Module 資料夾、CIM 平台專案與輸出位置。Module 資料夾可以是
單一 Module（直接含 `plugin.yaml`），也可以是 Modules 根目錄（子資料夾含 `plugin.yaml`）。
所有 `enabled: true` 的 Module 都能勾選，不要求一定要有 `requires:`。目標固定為
`Windows x64 / Python 3.11 / cp311`，避免誤產其它 ABI 的 wheel。

「掃描 Module」與「開始打包」固定在三個路徑欄位正下方，不會因視窗高度或 Windows DPI
而被擠出畫面。開始打包會在掃描成功並列出可選 Module 後啟用。

建置仍由既有 `provision.py build` 執行，因此 GUI 與 CLI 具有完全相同的增量快取、
大型相依隔離、SHA-256 manifest 與完全離線安裝自檢。按「取消」會終止整個建置程序樹；
下次重跑時，核心會重建未完成的工具包。完成後可直接開啟輸出資料夾及查看 `REPORT.md`。

原始碼與 Python 安裝元件分開輸出：

```text
<輸出>/
├─ source-packages/<module-id>/
│  ├─ source/                    Module 原始碼
│  └─ source-manifest.json       版本與逐檔 SHA-256
├─ packs/<module-id>/             只有 requires: Module 才有
└─ big-deps/                      大型 wheel 去重區
```

因此沒有 Python 相依的 Module 仍能打包原始碼；只修改原始碼時，也不需要重建既有 wheel。

Source Package 使用暫存目錄原子換位。若輸出位於 OneDrive，OneDrive／防毒軟體可能暫時
鎖住換下來的隱藏舊版；新包已成功換位後，舊版清理失敗不會讓整次打包誤判失敗。
被鎖住的 `.module-id.old-*` 只是不再使用的回收候選，不影響目前 `module-id` 正式內容。

### 在選定資料夾實際驗證

打包完成後，GUI 的「實際套用與 Tauri 驗證」區可以選擇一個**隔離驗證資料夾**及工具，
再按「套用、暖機並啟動 Tauri 驗證」。它會自動完成：

1. 把剛產出的 dep-pack 實際套用到驗證資料夾。
2. 在禁止 PyPI 的環境中建立全新的 per-tool venv。
3. 啟動所選平台專案的 `prebuilt/cim-light.exe`、engine 與獨立 WebView2 profile。
4. 在 Portal 選擇工具並按 Start。
5. 同時檢查 iframe 不是 Python traceback，以及 engine log 有 `Per-tool deps ready`。
6. 保存 `validation-result.json`、Tauri logs 與前後畫面截圖。

驗證依工具類型處理：`app`／`sheet` 會在 Portal 中選取並按 Start；一般
`category: module` 通常是 Sheet 內部元件，不會出現在 Portal 工具下拉選單，因此改驗證
平台 `PluginLoader` 能載入其 process 原始碼，再確認 Tauri engine 與 Portal 都能啟動。
GUI 驗證成功後會保留 Tauri 視窗開啟，讓發布人員繼續手動操作；關閉 Tauri 視窗即可結束。

每次驗證會重建該驗證資料夾內的 `deppack-cache`、`tool-venvs`、`logs` 與 `wv2`；
不會修改平台正式資料。Tauri 驗證需要工具出現在 Portal 的工具選單中，且平台專案已有
`apps/host-tauri/prebuilt/cim-light.exe` 與 Node `playwright-core`。

---

## 它解決什麼問題

工具在自己的 `plugin.yaml` 宣告相依，平台在工具首次啟動時建 per-tool venv 安裝。
但**離線機沒有 PyPI**。平台已有「單一工具」的離線包機制（dep-pack），缺的是
「掃整個專案、一次產齊、大東西分開處理」的批次工具——這就是本專案。

```
連網開發機                                     沒有網路的電腦
──────────────────────                        ──────────────────────
provision.py build <平台專案>                  複製整個補給包資料夾
   ↓ 掃描所有 plugin.yaml requires:               ↓
   ↓ pip download（鎖 win_amd64/cp311）        python apply.py --deppack-cache <目標>
   ↓ 大 wheel 隔離到 big-deps\                    ↓ 驗 sha256 + 原子換位（不跑 pip、不連網）
   ↓ 離線可裝自檢（--no-index 重解一次）           ↓
provision\                                     啟動平台 → 第一次點開工具
  packs\<工具>\{wheels\, deppack.json}            → engine 驗章 → pip --no-index 離線安裝
  big-deps\*.whl                                 → 裝進該工具專屬 venv
  provision.json / REPORT.md / apply.py
```

**產出的形狀就是平台 `CIM_DEPPACK_CACHE` 的形狀**，所以平台端零改動。

## 三條指令

```powershell
# 1) 連網機：產包（--dest 就是補給包根目錄）
py -3.11 provision.py build C:\code\claude\nativeApp --dest D:\provision

# 先看計畫、不下載：
py -3.11 provision.py build C:\code\claude\nativeApp --dry-run

# 2) 搬運後：驗證完整性（逐檔 sha256；不需要平台專案，隨身碟插上就能驗）
py -3.11 provision.py verify D:\provision

# 3) 離線機：套用（直接跑包內那支 apply.py，不需要本 repo）
python apply.py --deppack-cache <APP_ROOT>\data\<project-key>\deppack-cache

# 4) 離線機：暖機（把相依真的裝進 per-tool venv，讓第一次按 Start 就成功）
python warmup.py --project <平台專案> --deppack-cache <同上> --tool-venvs <APP_ROOT>\data\<key>\tool-venvs
```

常用旗標：`--tools a,b` 只包指定工具、`--force` 忽略增量快取全部重產、
`--big-threshold-mb N` 調整大相依門檻（預設 100 MB，`0` = 關閉隔離）。

## 大型相依會被分出來

單檔超過門檻的 wheel（torch、CUDA 那種）會被搬到補給包頂層的 `big-deps\`，
跨工具**只存一份**。使用者因此能一眼看到大東西在哪，並把它與其餘部分**分開搬運**
（例如另用一顆隨身硬碟）。

各 pack 的 `deppack.json` **保持完整**——它描述的是「apply 之後」的形狀；
大 wheel 缺席只是搬運期的暫態。`apply.py` 把它們放回各工具的 `wheels\` 後，
用平台自己的 sha256 定義驗證重組正確。`big-deps\` 沒放回去就執行 apply，
用到它的工具會被**跳過並明確告知**，其餘工具照常套用，且不會留下半套的 dep-pack。

## apply 與 warmup 的分工

|              | 需要平台專案 | 跑 pip | 連網 | 只用 stdlib |
|--------------|:---:|:---:|:---:|:---:|
| `apply.py`   | 否 | 否 | 否 | 是 |
| `warmup.py`  | **是** | **是**（`--no-index`） | 否 | 否 |

`warmup.py` 存在的理由是一個 GUI E2E 實測到的行為：Tauri 殼的 HTTP bridge
（`bridge.rs::api_post`）對 engine 有 **30 秒逾時**，而 engine 是在
`POST /tools/<id>/start` 裡**同步**安裝相依的。torch 級相依要 76 秒 → 殼先放棄，
畫面顯示「Failed to start tool」（相依其實裝完了）。先跑 warmup，第一次按 Start
只要 12 秒就算繪完成。詳見 [docs/OFFLINE_DEPLOY.md](docs/OFFLINE_DEPLOY.md) 的對照組 B。

## 設計上的三個硬性質

- **目標標籤永遠明示**（`win_amd64` / `3.11` / `cp311`）。曾發生過用開發機的
  Python 3.14 下載 wheel、到鎖 3.11 的平台上 51 個 wheel 全數不可裝的事故。
- **產包/驗章格式來自被掃描的專案自己**（`import` 它的 `core.deppack`），
  不在本專案複製一份實作——平台改格式時本工具立刻知道，而不是到工廠現場才爆。
- **`apply.py` 自足**：只用標準函式庫，不 import 本專案任何模組、不連網、不呼叫 pip。
  它會被逐字複製進每個補給包，在離線機獨立執行。安裝是平台 engine 的事。

## 開發

```powershell
py -3.11 -m pip install -r requirements-dev.txt
py -3.11 -m pytest tests                     # 單元測試（不連網、不需要平台）
py -3.11 -m pytest tests --network --project-root C:\code\claude\nativeApp   # 含真實整合測試
```

`--network` 的測試會真的 `pip download`；`--project-root` 的測試會 import 真平台的
`core.deppack` 驗證耦合契約。端到端測試 (`tests/test_e2e_offline.py`) 會把
`PIP_INDEX_URL` 指向死位址，藉此證明離線安裝那一步真的沒有連網。

**GUI E2E**（需要平台的 Tauri 殼與預建 portal dist）：

```powershell
# Source Package + GUI 內建 Tauri 驗證的端到端測試（走真實 GUI 後端；app 與 module 兩條路徑）
py -3.11 e2e/gui_flow_e2e.py                          # 預設 --tools app-lv,module_001 → exit 0

node e2e/gui_offline_e2e.mjs dist/provision e2e/out   # 三個對照組，Playwright over CDP
py -3.11 e2e/make_figures.py                          # 截圖 → 內嵌 data URI
py -3.11 e2e/build_html.py                            # 注入模板 → docs/offline-deploy.html
```

`gui_flow_e2e.py` 對每個工具走 GUI「開始打包」與「Tauri 驗證」兩顆按鈕背後的同一批後端：
先建原始碼包（`app-lv` 額外增量命中既有 dep-pack，不連網），再由 `validate_package.mjs`
以死 PyPI index 實測離線安裝、啟動真 Tauri 殼、按 Start 並要求 iframe 畫出真 UI。

三個對照組（沒有補給包 / 有包但跳過 warmup / 先跑 warmup）在**真的斷網**下驗證：
工具的 iframe 畫出真正的 UI（而不是 traceback）、engine.log 說相依 ready、
以及該工具 venv 的 `python.exe` 真的 `import torch` 成功。

## 專案結構

| 路徑 | 職責 |
|------|------|
| `provision.py` | CLI 入口（build / verify / apply） |
| `provision_gui.py` | 獨立打包 GUI（專案／工具選擇、進度、取消、完成摘要） |
| `start-gui.bat` | Windows 雙擊啟動 GUI |
| `src/provision_builder/gui_backend.py` | GUI 可測試後端（掃描、建置／驗證命令、可取消子程序） |
| `src/provision_builder/source_pack.py` | Module 掃描與獨立、原子性的 Source Package |
| `e2e/validate_package.mjs` | 單一工具 Apply → Warmup → Tauri 的實機驗證 driver |
| `apply.py` | 離線機執行的自足腳本（會被複製進產出） |
| `src/provision_builder/gateway.py` | 與平台 `core.deppack` 的唯一耦合點 |
| `src/provision_builder/scan.py` | 掃 `plugin.yaml`（glob 鏡射 engine） |
| `src/provision_builder/build.py` | 主流程：增量、產包、失敗續行 |
| `src/provision_builder/bigdeps.py` | 大相依隔離、去重、引用計數 |
| `src/provision_builder/selfcheck.py` | 離線可裝自檢（`pip --no-index` 重解） |
| `src/provision_builder/verify.py` | 完整性驗證（不依賴平台） |
| `src/provision_builder/manifest.py` `report.py` | `provision.json` / `REPORT.md` |
| `warmup.py` | 離線機：借平台 `core.tool_deps` 把相依裝進 per-tool venv |
| `e2e/gui_offline_e2e.mjs` | GUI E2E：真 Tauri 殼 + 斷網 + 三個對照組 |
| `e2e/gui_flow_e2e.py` | Source Package + GUI Tauri 驗證的 E2E 入口（走真實後端，app/module 兩路徑） |
