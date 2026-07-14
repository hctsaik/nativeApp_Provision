# 簡易版 Streamlit + Tauri 可交付資料夾產生器

> 文件用途：交給後續 AI 依序實作。第一版只做 Windows x64、單一 Streamlit 專案、資料夾式交付，不做安裝程式與中央發布。

## 1. 我對需求的理解

管理人員要能在目前的管理工具（`provision_gui.py`）中選擇一個既有 Streamlit 專案資料夾，並指定該專案的入口 `.py`。管理工具把下列內容組成一個可直接交付給 User 的資料夾：

- Streamlit 專案原始碼與靜態資源；
- 可攜式 Python runtime 與已安裝的專案相依套件；
- 既有的預建 Tauri 殼；
- 負責選 port、啟動服務、等待 ready、啟動 Tauri 及清理程序的 launcher；
- User 唯一需要雙擊的 `start.bat`。

User 不必安裝 Python、Node、Rust 或 Streamlit，也不必自行選 port。若預設 port 已被占用，launcher 必須自動改用另一個可用 port，並讓 Tauri 開啟正確的本機 URL。

第一版的核心體驗是：**管理員選專案並按「建立」，User 收到資料夾後雙擊 `start.bat` 即可使用。**

## 2. 第一版範圍

### 2.1 必須完成

1. 在 `provision_gui.py` 新增獨立的「Streamlit 桌面資料夾」區塊。
2. 可選擇：
   - Streamlit 專案資料夾；
   - 入口 `.py`（預設嘗試 `app.py`，但不可只靠猜測）；
   - 輸出資料夾；
   - 應用顯示名稱；
   - 預建 Tauri 殼或殼範本資料夾來源。
3. 建立前檢查入口檔及 `requirements.txt` 是否存在，並檢查 requirements 中是否包含 `streamlit`。
4. 產生完整可搬移的資料夾，所有執行路徑都以資料夾自身位置為基準，不可寫死建置電腦的絕對路徑。
5. `start.bat` 啟動時自動取得可用 port。
6. Streamlit 只能監聽 `127.0.0.1`，不可預設暴露到區域網路。
7. 等待 Streamlit health endpoint 成功後才顯示 Tauri 主視窗；逾時需顯示清楚錯誤並寫 log。
8. Tauri 關閉後，launcher 應終止自己建立的 Streamlit 子程序。
9. 同一份交付資料夾可位於含空白或中文字的路徑。
10. 建置完成後，GUI 顯示輸出位置與成功／失敗摘要。

### 2.2 第一版不做

- 不產生 MSI、NSIS 或單一 exe 安裝檔；
- 不做自動更新、Registry、MinIO、簽章或 Fleet rollout；
- 不支援 macOS、Linux、ARM 或多個 Streamlit 入口；
- 不安裝系統服務，不要求系統管理員權限；
- 不在 User 電腦執行線上 `pip install`；
- 不修改任意 Streamlit 專案的業務程式碼；
- 不要求在目前受 WDAC 限制的環境重編 Rust/Tauri。

## 3. 重要限制與決策

### 3.1 Tauri 殼必須採用既有預建版本

目前專案文件已記錄 Rust/Tauri 殼凍結且本機不可重編。因此 MVP 應複製已知可執行的預建殼及其必要資源。殼範本來源必須可設定，建置器不得假設所有電腦都有固定的 `C:\...` 路徑。

如果預建殼不存在、與 launcher 的動態 URL 契約不相容，建置必須 fail closed，告知管理員缺少什麼；不可悄悄改成只開系統瀏覽器並宣稱成功。

### 3.2 動態 URL 契約要先驗證

後續 AI 動手前，先確認預建 Tauri 殼支援下列其中一種方式，依優先順序選用：

1. 命令列參數，例如 `host.exe --url http://127.0.0.1:49152`；
2. 環境變數，例如 `STREAMLIT_APP_URL=http://127.0.0.1:49152`；
3. launcher 在每次啟動前產生殼所讀取的 runtime config。

若三種方式都不支援，這不是 Python launcher 可以掩蓋的問題，必須取得可重編 Tauri 的環境，加入動態 URL 支援後再繼續。不要用「先綁定一個固定 port」代替需求。

### 3.3 不要讓 `start.bat` 承擔主要邏輯

`start.bat` 只負責定位交付根目錄並呼叫 bundled Python：

```bat
@echo off
setlocal
cd /d "%~dp0"
"runtime\python.exe" "launcher\launch.py"
exit /b %errorlevel%
```

找 port、建立 subprocess、health check、log 與 cleanup 全放在可測試的 Python 模組，避免複雜 batch quoting 與程序管理問題。

## 4. 預期輸出結構

```text
<output>/<app-name>/
├─ start.bat                         # User 唯一入口
├─ app-package.json                  # 建置資訊與相對路徑設定
├─ application/
│  ├─ app.py                         # 範例；實際入口可為其他檔名
│  ├─ requirements.txt
│  └─ ...                            # 專案其餘檔案
├─ runtime/
│  ├─ python.exe
│  ├─ python311.dll
│  └─ Lib/site-packages/             # 已離線安裝 streamlit 與專案相依
├─ launcher/
│  └─ launch.py
├─ shell/
│  ├─ host.exe                       # 預建 Tauri 殼，實際名稱由 manifest 指定
│  └─ ...                            # 殼所需 DLL、resources、WebView 設定等
└─ data/
   └─ logs/                          # 執行時建立；可寫資料不要放 application/
```

`app-package.json` 建議最小格式：

```json
{
  "schema_version": 1,
  "display_name": "My Streamlit App",
  "entrypoint": "application/app.py",
  "python": "runtime/python.exe",
  "shell_executable": "shell/host.exe",
  "host": "127.0.0.1",
  "preferred_port": 8501,
  "startup_timeout_seconds": 60,
  "health_path": "/_stcore/health"
}
```

所有欄位使用相對於交付根目錄的相對路徑。讀入後必須 resolve 並驗證仍位於交付根目錄內，避免 `..` 路徑逃逸。

## 5. 啟動流程

```text
User 雙擊 start.bat
        ↓
runtime/python.exe 執行 launcher/launch.py
        ↓
讀取並驗證 app-package.json
        ↓
由 OS 配置 loopback 可用 port
        ↓
啟動 python -m streamlit run <entrypoint>
  --server.address=127.0.0.1
  --server.port=<port>
  --server.headless=true
  --browser.gatherUsageStats=false
        ↓
輪詢 http://127.0.0.1:<port>/_stcore/health
        ↓ ready
用已確認的契約把 URL 傳給 Tauri 殼並啟動
        ↓
等待 Tauri 結束
        ↓
終止本次建立的 Streamlit process tree，寫入結束狀態
```

### 5.1 Port 處理細節

- 先嘗試 `preferred_port`；已占用時才改用 OS 配置的 ephemeral port。
- 可用性檢查與 Streamlit 真正 bind 之間存在 race condition。launcher 若看到「address already in use」，應重新選 port 並重試，建議最多 5 次。
- 不可用「從 8501 一直加一」作為唯一策略，以免撞到其他固定服務。
- health check 必須有總逾時、短輪詢間隔，且每次 HTTP request 自身也要有短逾時。
- 傳給殼的 URL 只能是實際成功 health check 的那一個 URL。

### 5.2 程序與 log

- stdout/stderr 寫到 `data/logs/streamlit-YYYYMMDD-HHMMSS.log`。
- launcher 自己的狀態與例外寫到 `data/logs/launcher-YYYYMMDD-HHMMSS.log`。
- 不可用名稱掃描並殺掉所有 `python.exe` 或 `streamlit.exe`；只清理由本 launcher 建立的 process tree。
- Windows 上應使用新的 process group 或 Job Object；MVP 若先用 stdlib subprocess，至少保存 PID、正常 terminate、逾時後再 kill，並補測試。
- 若 Streamlit 在 ready 前退出，立即失敗並顯示 log 路徑，不可仍啟動空白 Tauri。
- 可選擇加上單實例鎖；若未做，至少確保雙擊兩次會各自使用不同 port，且互不誤殺。

## 6. 管理工具畫面

在既有 GUI 中加入一個明確獨立區塊，不要把它混入現有 dep-pack 或 `.napp` 發布流程：

```text
Streamlit 桌面資料夾（簡易版）

專案資料夾： [____________________] [瀏覽]
入口檔案：   [application/app.py___] [瀏覽]
應用名稱：   [My Streamlit App______]
Tauri 範本：  [____________________] [瀏覽]
輸出位置：   [____________________] [瀏覽]

[檢查專案] [建立可交付資料夾]

狀態：建置中／成功／失敗
輸出：D:\delivery\MyStreamlitApp
```

入口檔案選取後必須確認它位於專案資料夾內。建置工作沿用目前 GUI 的背景工作模式，禁止在 Tk UI thread 中執行複製或 pip，避免畫面無回應。

## 7. 建置器的建議模組切分

不要把所有邏輯直接塞進 `provision_gui.py`。建議新增：

```text
src/provision_builder/streamlit_desktop/
├─ __init__.py
├─ models.py       # BuildRequest、BuildResult、manifest 資料結構
├─ validate.py     # 專案、entrypoint、requirements、shell 契約檢查
├─ runtime.py      # 複製 runtime、離線安裝相依
├─ builder.py      # staging、組裝、原子換位、結果摘要
└─ templates/
   ├─ launch.py
   └─ start.bat
```

GUI 只收集輸入、呼叫 builder、轉發進度事件與顯示結果。核心 builder 不可 import Tkinter，才能由單元測試與未來 CLI 重用。

### 7.1 建置流程

1. 將所有輸入轉為絕對路徑並驗證。
2. 檢查專案、入口、requirements、可攜 runtime 與 Tauri 範本。
3. 在輸出目錄旁建立唯一 staging 目錄。
4. 複製專案，套用**分深度**的預設排除（下方 7.1.1），並讓專案能用根目錄的
   `.provisionignore` 覆寫其中任何一條。
5. 複製可攜 runtime。不得直接打包管理員當前的任意 venv。
6. 使用 staging 中的 Python，把 requirements 安裝到 staging runtime；正式產物建議從本專案既有 wheel/dep-pack cache 離線安裝。
7. 明確驗證 `runtime/python.exe -c "import streamlit"` 及入口的路徑存在。
8. 複製 launcher、batch、manifest 與 Tauri shell。
9. 執行 package smoke test。
10. 全部成功後才把 staging 原子換位成最終資料夾；失敗時移除 staging，不破壞既有成功輸出。

若第一個增量暫時允許建置機連網下載 dependencies，GUI 必須清楚標示「建置時可能連網」；但產出的 User 資料夾在執行時仍必須完全離線。後續應接回本 repo 現有 dep-pack/big-deps 能力。

### 7.1.1 排除規則：深度會改變一個名字的意思

實作於 `builder.ignore_reason()`（唯一決策點；估算與複製共用它）。完整操作說明見
[README 的 `.provisionignore` 章節](../README.md#哪些東西不會被交付以及怎麼把它要回來provisionignore)。

- **任何深度排除**：`.git/` `.hg/` `.svn/` `.venv/` `__pycache__/` `.pytest_cache/`
  `.mypy_cache/` `.ruff_cache/` `.streamlit_cache/` `node_modules/` `site-packages/`、
  `*.pyc` `*.pyo` `*.whl` `*.egg-info`。
  這些名字不可能是 App 本身：`.whl` 執行時永遠不會被打開（相依已在 `runtime/`），
  `node_modules/` 是前端建置相依，編譯結果在 `dist/`。
- **只在專案根目錄排除**：`venv/` `env/` `wheels/` `wheelhouse/` `vendor/` `dist/` `build/`、
  `*.zip` `*.tar.gz` `*.7z`。
  這些名字在根目錄是建置垃圾，往下一層意思完全不同：
  `<component>/frontend/dist/` **就是**那個 Streamlit 元件（`declare_component(path=...)` 指向它）；
  `assets/data.zip`、`models/weights.tar.gz` 是 App **執行時要讀的資料**。
  用名字砍到底 = 建置成功、smoke test 通過、在建置機上跑得起來（原檔還在專案裡），
  到工廠現場才 `FileNotFoundError`。

- **逃生門**：專案根目錄的 `.provisionignore`，gitignore 語意，**最後一條符合的規則說了算**，
  且 `!樣式` 可以把「內建規則排掉的東西」再包含回來（內建排除只是起始立場）。
  任何「已自動排除 X」的訊息都必須同時告訴使用者這件事。

### 7.1.2 Store 模式的版本槽成本

Store 版本之間共用 **runtime**，不共用 `application\`（無硬連結、無檔案級去重），
因此每個版本目錄都是專案的完整副本。`scan_project(request, versioned=True)` 會在
**檢查時**（專案結構還改得動時）算出每版成本並警告，訊息由
`builder.version_slot_warning()` 產生，也存在 `ProjectScan.version_slot_warning`。

## 8. 分階段實作工作單

### Phase 0：確認 Tauri 動態 URL 契約（先做，阻塞後續）

- 找出預建殼完整資料夾及 executable。
- 用一個最小本機 HTTP server 手動驗證殼能透過參數、環境變數或 config 開啟動態 port。
- 把契約與必要檔案寫成 shell template manifest。
- 若不支援，停止並回報需要修改 Tauri；不要繼續做假的整合。

驗收：在兩個不同 port 啟動測試頁，殼都能顯示正確內容。

### Phase 1：純 Python launcher

- 實作 manifest 載入、port 選擇、Streamlit subprocess、health check、Tauri subprocess 與 cleanup。
- 先以 fixture Streamlit app 驗證，不接 GUI。

驗收：8501 空閒與被占用兩種情況都能開啟；Tauri 關閉後 Streamlit port 釋放。

### Phase 2：資料夾 builder

- 實作輸入驗證、staging 組裝、runtime/專案/shell 複製、requirements 安裝及原子換位。
- 產生 `start.bat` 與 `app-package.json`。

驗收：把輸出複製到另一個含空白與中文字的路徑，離線雙擊仍可啟動。

### Phase 3：接入管理 GUI

- 新增畫面欄位、瀏覽按鈕、檢查與建立按鈕。
- 使用背景 worker，串接進度、錯誤與完成結果。
- 不影響既有 build、verify、Tauri validation 功能。

驗收：完全從 GUI 選 fixture 專案並建立成功，GUI 建置期間仍可重繪與移動。

### Phase 4：測試與文件收尾

- 補齊下節測試。
- 在 `README.md` 增加入口與限制，連回本文件。
- 寫一份交付給 User 的短版 README（可由 builder 一併產生）。

## 9. 最低測試要求

### 9.1 單元測試

- project directory 不存在；
- entrypoint 不存在、在 project 外、或用 `..` 逃逸；
- requirements 缺少或未宣告 Streamlit；
- shell executable 缺少；
- preferred port 空閒；
- preferred port 被占用後取得其他 port；
- health check 成功、逾時、Streamlit 提前退出；
- manifest 所有路徑皆為相對且 resolve 後位於 package root；
- cleanup 只處理自己的 subprocess；
- staging 失敗不覆蓋既有成功輸出。

### 9.2 整合測試

建立最小 fixture：

```python
import streamlit as st

st.title("Portable Streamlit smoke test")
st.write("READY")
```

至少驗證：

1. 產物不引用原始專案或建置機的絕對路徑；
2. 阻塞 8501 後，launcher 能用其他 port ready；
3. health endpoint 回應成功；
4. Tauri 實際顯示 `READY`，不是只檢查 process 存活；
5. 關閉 Tauri 後該 port 不再接受連線；
6. 將產物搬移後仍能啟動；
7. 斷網環境仍能啟動。

不要只 mock socket 與 subprocess 就宣稱端到端完成；至少保留一條使用真實 Streamlit 與真實預建 Tauri 殼的 Windows E2E。

## 10. 完成定義（Definition of Done）

只有同時符合以下條件才算完成：

- 管理員可從 GUI 選擇任意符合規範的 Streamlit 專案及入口；
- GUI 能產生結構完整的 User 資料夾；
- User 電腦不需要預裝 Python、Streamlit、Node 或 Rust；
- User 只需雙擊 `start.bat`；
- 8501 被占用時會自動改用可用 port；
- Tauri 顯示的是本次啟動的正確 URL；
- 專案可在離線環境、含空白／中文的搬移路徑中執行；
- 關閉 Tauri 後沒有本次 Streamlit 背景程序殘留；
- 錯誤訊息可理解，且 log 能定位失敗原因；
- 新增單元測試通過，既有測試沒有 regression；
- 真實 Streamlit + 真實預建 Tauri 的 E2E 通過。

## 11. 後續 AI 的實作原則

1. 先完成 Phase 0，確認殼的 URL 契約後才寫 builder。
2. 先寫可測試的 launcher 與 builder，再接 GUI。
3. 每個 Phase 都先補 focused tests，再跑相關 regression。
4. 不重寫本 repo 已有的 dependency cache、原子輸出與 GUI background worker；能重用就重用。
5. 不把建置機絕對路徑、User-specific 路徑或 secret 寫進產物。
6. 不以固定 port、系統瀏覽器或要求 User 安裝 Python 當作需求的替代方案。
7. 發現預建 Tauri 不支援動態 URL 時立即回報阻塞點，不擅自縮減產品行為。

