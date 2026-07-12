# Phase 0 驗證結果與設計提案 — 簡易版 Streamlit + Tauri 可交付資料夾產生器

> 本文是 [`SIMPLE_STREAMLIT_TAURI_FOLDER_BUILDER.md`](SIMPLE_STREAMLIT_TAURI_FOLDER_BUILDER.md) 的**前置調查回覆**。
> 該 spec §8 Phase 0 要求「先確認預建 Tauri 殼的動態 URL 契約,不支援就停下來回報,不要做假的整合」。
> 本文回報 Phase 0 結果、提出調整後的設計、並列出待決事項。
>
> 狀態:**已實作並通過真實 E2E(見 §14 完成報告)。**
> 日期:2026-07-12
>
> ⚠️ **閱讀順序**:§10–§13 是正式決議,**優先於** §0–§9 的初版敘述。
> §0–§9 已依決議修訂完畢(§13.1)。

---

## 0. 一頁摘要(先看這段)

| 項目 | 結論 |
|------|------|
| spec §3.2 的三種動態 URL 契約 | **一種都不支援**(已讀 Rust 原始碼逐條確認) |
| 是否因此阻塞? | **否**。找到第四條 spec 未設想的通道:**殼的 engine 契約**本身就能傳動態 URL,且零重編 |
| 代價 | 使用者要**多按一次「啟動」**,並看得到 portal 外框。這是與原 spec §10 DoD 的**唯一**落差(§12 已正式接受為 MVP 行為) |
| 動態 port | **有兩種,不可混談**(見 §10.3):control port 由殼配置;**Streamlit port 仍須 launcher 自己選**(含 bind race 重試) |
| 可攜 Python | **已經存在**,不需自己造(`fetch-standalone-python.ps1`,產出 relocatable CPython 3.11 含 pip/venv) |
| 建置機網路 | **可用**(已實測 `pip download streamlit` 成功) |
| 已拍板 | 走 **engine-shim 路(零重編)**,接受多按一次「啟動」;六項待決事項見 §11 |

---

## 1. Phase 0:動態 URL 契約驗證結果

spec §3.2 列了三種可接受的契約,依優先序驗證如下。**三種都不支援。**

### 1.1 選項 1 — 命令列參數(`host.exe --url http://127.0.0.1:49152`)❌

殼的視窗建立處寫死載入烤進 exe 的前端,**完全沒有 argv 解析**:

```rust
// apps/host-tauri/src-tauri/src/lib.rs:47
WebviewWindowBuilder::new(&h2, "main", WebviewUrl::App("index.html".into()))
```

`WebviewUrl::App` = 從 exe 內嵌的 frontendDist 取資產(`tauri.localhost/index.html`),
不是任意 URL。要改成 `WebviewUrl::External(url)` 必須改 Rust 並重編。

### 1.2 選項 2 — 環境變數(`STREAMLIT_APP_URL=...`)❌

殼只讀這些 env,沒有任何 URL 類:

| env | 用途 | 出處 |
|-----|------|------|
| `CIM_ENGINE_EXE` | engine 路徑(可為 `.py`) | `sidecar.rs:289` |
| `CIM_ENGINE_PYTHON` | 跑 `.py` engine 的直譯器 | `sidecar.rs:118` |
| `CIM_LOG_DIR` | log 目錄 | `sidecar.rs:303`(⚠️ 見 §5.3,預建 exe 可能不含此支援) |
| `CIM_DEV_MODE` | 傳給 portal 的 devMode 旗標 | `bridge.rs:114` |

### 1.3 選項 3 — 殼讀取的 runtime config 檔 ❌

不存在這條路徑。殼的設定全部來自上表的 env 與內嵌前端。

### 1.4 結論

依 spec §11.7,這是「必須立即回報的阻塞點」。**但阻塞可以繞過** —— 見下一節。

---

## 2. 第四條路:殼的 engine 契約就是動態 URL 通道

### 2.1 殼實際的啟動流程(讀碼還原)

```
cim-light.exe 啟動
  → find_free_port()                       sidecar.rs:39   ← 動態 port(殼自己找)
  → spawn(CIM_ENGINE_EXE, --control-port <port> --log-dir <dir>)   sidecar.rs:45,113
      · engine 路徑是 .py → 用 CIM_ENGINE_PYTHON 跑            sidecar.rs:116-121
      · 注入 PYTHONUTF8=1;cwd = engine 檔案所在目錄            sidecar.rs:126-129
  → 輪詢 GET /health 直到 {"status":"ok"}(packaged 逾時 120s)  sidecar.rs:60,189
  → 開視窗,載入內嵌 portal(index.html)                        lib.rs:47
  → portal 呼叫 listTools() → GET /tools                        bridge.rs:47
  → 使用者按「啟動」→ POST /tools/{id}/start                    bridge.rs:52
  → ★ 殼把回應中的 input_url 當 iframe 的 src ★
  → 監看 engine 崩潰,崩了自動重啟                              sidecar.rs:198
```

**關鍵**:第 ★ 行。`input_url` 是 engine 自己決定的**任意 URL**。
所以只要我提供一個假裝成 engine 的小程式(shim),讓它回報「你的 Streamlit 在
`http://127.0.0.1:<本次動態 port>`」,殼就會把那個 URL 開起來。

這完全滿足 spec §5.1 的真需求:
- ✅ 傳給殼的 URL 是「實際成功 health check 的那一個」
- ✅ 不是固定 port
- ✅ 不是退化成系統瀏覽器
- ❌ 但 portal 外框仍在,且需按一次「啟動」

### 2.2 為什麼會多一次點擊(不可迴避,除非重編)

portal 前端開機時會自動選好工具,**但不會自動按啟動**:

```jsx
// apps/portal-react/src/main.jsx:592-597
nativeApi.listTools().then((items) => {
  setTools(items);
  const first = items.find(t => t.category === "sheet") ?? items[0];
  if (first?.tool_id) setSelectedToolId(first.tool_id);   // 只選,不啟動
});
```

portal 是 React 前端,**與 exe 綁在一起**(frontendDist 內嵌)。要讓它自動啟動唯一工具
只需約 5 行 JS,但那需要重編 exe → 本機 WDAC 擋。

### 2.3 好消息:tool_id 用 `app-` 開頭就是單一全高 iframe

engine 依 tool_id 前綴決定版面([`engine.py:769`](../../nativeApp/sidecar/python-engine/engine.py)):
`app-*` → category `app` → portal 走 `AppPanel`,**一個全高 iframe、無 Input/Output 分頁、
不套 cv_framework 外框**([`main.jsx:338`](../../nativeApp/apps/portal-react/src/main.jsx))。
正是我們要的樣子。所以 shim 回報的 tool_id 一律用 `app-<slug>`。

---

## 3. Engine Shim 契約(shim 必須實作的全部端點)

### 3.0 命令列契約(§10.1)

殼**一定**用這個形式起 shim(`sidecar.rs:45,113-121`):

```text
runtime\python.exe launcher\engine_shim.py --control-port <殼配置> --log-dir <殼配置>
```

- 用 `argparse` + `parse_known_args()`:**忽略並記錄**未知參數(容忍未來殼新增旗標),但**缺少必要參數必須立即以非零 exit code 失敗**。
- HTTP server **必須** bind 殼傳入的 `--control-port`,**不可**自己找 control port;**只能** bind `127.0.0.1`。
- `/health` 只有在 server 完成 bind、能正常服務後才回 `{"status":"ok"}`。

### 3.1 HTTP 端點

殼 + portal 只會打這些。JSON 形狀必須逐欄位對齊
([`engine.py:235-257`](../../nativeApp/sidecar/python-engine/engine.py) 的 pydantic model)。

| 方法 | 路徑 | 誰打 | 回應(最小可用) |
|------|------|------|------------------|
| GET | `/health` | 殼(就緒判定) | `{"status":"ok"}` |
| GET | `/tools` | portal 開機 | `[{"tool_id":"app-<slug>","name":"<顯示名>","version":"1.0.0","category":"app"}]` |
| POST | `/tools/{id}/start` | 按「啟動」 | `{"tool_id":"app-<slug>","input_url":"http://127.0.0.1:<port>","output_url":"<同左>","input_port":<port>,"output_port":<port>,"category":"app","mode":"iframe","ready":true,"run_id":"<id>"}` |
| GET | `/tools/active/status` | portal 輪詢 | 執行中 `{"active":true,"tool_id":"app-<slug>","category":"app","input_alive":true,"output_alive":true,"input_url":"...","output_url":"...","result_mtime":-1}`;已停止 `{"active":false}` |
| POST | `/tools/stop` | 按「停止」 | **不可假回成功**,見 §3.2 |
| POST | `/shutdown` | **殼關閉時**(`sidecar.rs` stop:先 POST,5 秒後才 kill) | `{"ok":true}`,然後結束行程 |
| GET | `/runtime` | portal 開機 | 任意 dict(portal 失敗只記 warning) |
| GET | `/diagnostics` | portal | 任意 dict |
| POST | `/selected-paths` | 檔案對話框 | `{"paths":[...]}` |

**shim 用 stdlib `http.server` 即可,零第三方相依**(不需要 fastapi/uvicorn)。
這一點很重要:它讓交付包的 runtime 只需要裝 **streamlit + 使用者專案的相依**,不必背整個 engine 的相依樹。

### 3.2 `/tools/stop` 與 `/tools/{id}/start` 的真實語意(§10.2)

Streamlit 的 owner 是 `launch.py`(§11 決議 1),但 portal 的停止/啟動只會打到 shim。
因此 shim **不得**自行殺程序、也**不得**假回成功,而是透過**受 token 保護的本機控制通道**請求 launcher:

```text
portal「停止」→ POST /tools/stop → shim → launcher:終止本次 Streamlit process tree
                                        → 等 port 真的釋放 → 才回 200
                                        → 之後 /tools/active/status 回 {"active":false}
portal「啟動」→ POST /tools/{id}/start → shim → launcher:重選 port → 重起 Streamlit
                                              → health check 通過 → 回傳「新的」URL
```

逾時或失敗一律回**非 2xx + 可理解錯誤**,不可告訴 portal 假成功。

### 3.3 ⚠️ 30 秒硬預算(實作時最容易踩的雷)

殼轉發給 engine 的**每一個** HTTP 呼叫都有 **30 秒逾時**([`bridge.rs:27,32,37`](../../nativeApp/apps/host-tauri/src-tauri/src/bridge.rs)):

```rust
ureq::post(&url).timeout(Duration::from_secs(30)).send_json(body)
```

所以 §3.2 的「Stop 後再 Start」整條鏈(殺掉 → 選 port → 起 Streamlit → health ready)
**必須在 30 秒內完成**,否則 portal 會收到殼的 timeout 錯誤。
- P1 必須**實測**冷啟與重啟耗時並記錄在測試裡(Streamlit 重啟一般 3–8 秒,預算充裕但不可假設)。
- 首次 Start 不受影響:launch.py 已在殼啟動前把 Streamlit 起好並 health check 過,shim 只是回報 URL,近乎瞬間。

### 3.4 ⚠️ shim 必須無狀態(崩潰監看會重啟它,而且換 port)

殼會監看 engine,掛掉就 `sleep(3s)` 後重新 `start()`([`sidecar.rs:198-240`](../../nativeApp/apps/host-tauri/src-tauri/src/sidecar.rs))
—— 而 `start()` 會**重新 `find_free_port()`**,也就是 shim 會在**不同的 control port** 上被重新拉起。
因此:
- shim 不可把 control port 或任何執行期狀態寫死/快取;一切從 argv 與 env 重新讀。
- Streamlit 的 URL 由 `CIM_APP_URL`(或向 launcher 查詢)取得,**shim 不自己選 port、不自己生 URL**。
- 重啟後 portal 會重新 `listTools()`([`main.jsx:632`](../../nativeApp/apps/portal-react/src/main.jsx)),shim 必須能正確回答。

---

## 4. 建議的交付資料夾形狀

與 spec §4 的**唯一結構差異**是多了 `launcher/engine_shim.py`(因為殼「一定要有 engine 才肯開窗」)。

```text
<output>/<app-name>/
├─ start.bat                  # User 唯一入口。只做:cd /d %~dp0 → runtime\python.exe launcher\launch.py
├─ app-package.json           # 建置資訊,全部相對路徑
├─ application/               # 使用者的 Streamlit 專案(排除 .git/.venv/__pycache__/node_modules…)
│  ├─ app.py                  #   入口(實際檔名由 manifest 指定)
│  └─ requirements.txt
├─ runtime/
│  ├─ python.exe              # 可攜 CPython 3.11(relocatable,自帶 pip/venv)
│  └─ Lib/site-packages/      #   已離線裝好 streamlit + 專案相依
├─ launcher/
│  ├─ launch.py               # ★ 主邏輯:選 port → 起 streamlit → health check → 起殼 → 清理
│  └─ engine_shim.py          # ★ 殼要的「engine」,只回報 streamlit 的 URL(stdlib only)
├─ shell/
│  └─ cim-light.exe           # 預建 Tauri 殼(從範本來源複製,不重編)
└─ data/
   └─ logs/                   # 執行時建立(streamlit-*.log / launcher-*.log / host.log)
```

### 4.1 兩個程序、兩個 port、一條控制通道

```text
              ┌──────────────── launch.py(本次啟動的唯一 owner)────────────────┐
              │  · 擁有 Streamlit process tree                                  │
              │  · 開一個 loopback 控制伺服器(127.0.0.1:<OS 配發>,隨機 token) │
              └───────▲──────────────────────────────────┬─────────────────────┘
                      │ ③ stop / restart 請求(帶 token)  │ ① spawn(env 繼承)
                      │                                   ▼
                      │                        ┌──────────────────────┐
                      │                        │  cim-light.exe(殼)  │
                      │                        │  · find_free_port()  │ ← control port
                      │                        └──────────┬───────────┘
                      │                                   │ ② spawn(env 再次繼承)
                      │                                   ▼
              ┌───────┴────────────────────────────────────────────┐
              │  engine_shim.py — 只回答殼的 engine API,不擁有程序 │
              └────────────────────────────────────────────────────┘
```

**地基事實(已驗證)**:殼 spawn engine 時**沒有** `env_clear()`,只額外加 `PYTHONUTF8=1`
([`sidecar.rs:126-127`](../../nativeApp/apps/host-tauri/src-tauri/src/sidecar.rs))。
因此 launch.py 設的 env 會**穿過殼、再穿到 shim**(兩層繼承)。控制通道就靠這個傳遞:

| env | 由誰設 | 給誰用 |
|-----|--------|--------|
| `CIM_ENGINE_EXE` | launch.py | 殼:要起哪個 engine → `<pkg>\launcher\engine_shim.py` |
| `CIM_ENGINE_PYTHON` | launch.py | 殼:用哪個直譯器跑 `.py` engine → `<pkg>\runtime\python.exe` |
| `CIM_APP_ID` / `CIM_APP_NAME` | launch.py | shim:`/tools` 要回報的 tool_id(`app-<slug>`)與顯示名 |
| `CIM_LAUNCHER_URL` | launch.py | shim:控制通道位址(`http://127.0.0.1:<port>`) |
| `CIM_LAUNCHER_TOKEN` | launch.py | shim:**每次啟動隨機產生**的 token,不寫死在 package |

> Streamlit 的 URL **不從 env 直接讀**,而是 shim 每次向 launcher 查詢當前 URL —— 因為
> Stop/Restart 後 port 會變(§3.2),env 會過期。`CIM_APP_URL` 僅作為首次啟動的快取/除錯用。

### 4.2 啟動時序

```
User 雙擊 start.bat
  → runtime\python.exe launcher\launch.py
  → 讀 app-package.json(所有路徑 resolve 後必須仍在交付根目錄內,擋 .. 逃逸)
  → 起 launcher 控制伺服器:bind 127.0.0.1:0(OS 配發)+ 產生隨機 token
  → 選 Streamlit port:先試 preferred_port(8501);被占用 → OS 配發 ephemeral
       · bind race 最多重試 5 次(§10.3)
  → 起 streamlit(--server.address=127.0.0.1 --server.headless=true --browser.gatherUsageStats=false)
  → 輪詢 http://127.0.0.1:<port>/_stcore/health,總逾時 + 單次 request 逾時
       · 若 Streamlit 在 ready 前退出 → 立即失敗、印 log 路徑、【不開空白 Tauri】
  → ready 後才起殼:shell\cim-light.exe
        env: 見 §4.1 表格
        cwd: <pkg>\data        ← 讓殼的 log 落在 data\logs(見 §5.3)
  → 殼 find_free_port() → 起 shim → 等 /health ok → 開視窗
  → portal listTools() → 下拉自動選好唯一的 app(但不自動啟動)
  → 使用者按一次「啟動」→ POST /tools/{id}/start → shim 向 launcher 查 URL → iframe 顯示 Streamlit
  → (可選)按「停止」→ shim → launcher 真的殺掉 Streamlit → 等 port 釋放 → /status 回 active:false
  → (可選)再按「啟動」→ launcher 重選 port、重起、health check → shim 回新 URL(30 秒預算,§3.3)
  → 關閉 Tauri → 殼先 POST /shutdown 給 shim → launch.py 等殼結束
                → 殺掉「本次自己建立的」Streamlit process tree → 關控制伺服器 → 寫結束狀態
```

---

## 5. 已知地雷與注意事項

### 5.1 shim 必須無狀態(詳見 §3.4)
殼的崩潰監看會重啟 engine,**而且換一個新的 control port**。shim 不自己選 port、不自己起 Streamlit、不快取狀態;
當前 URL 一律向 launcher 查詢(§4.1)。

### 5.2 不可用名稱掃描殺 python
spec §5.2 明令。只能殺 launch.py 自己 spawn 的 process tree(保 PID → terminate → 逾時 kill)。
**shim 絕不可殺程序**(§10.2):它沒有 ownership,只能請 launcher 動手。
多開時兩份 package 各自有獨立 port / token / process tree,**關掉其中一份不得誤殺另一份**(§11 決議 5)。

### 5.3 預建殼可能不認 `CIM_LOG_DIR`
可攜 runtime 文件的實作紀錄寫明:`resolve_log_dir()` 優先讀 `CIM_LOG_DIR` 的改動**需在非 WDAC 機器 rebuild 殼才生效**。
→ 所以 launch.py **不能依賴這個 env**,改用既有 `start.bat` 的同一招:**把殼的 cwd 設成 `<pkg>\data`**,
殼就會把 log 寫到 `data\logs`。(重編後兩條路徑會一致,不衝突。)

### 5.4 中文/空白路徑
殼與 engine 之間走 argv 傳 `--log-dir`,launch.py 用 `subprocess` list 形式傳參,不經 shell,可安全處理空白與中文。需納入測試。

---

## 6. 待決事項(⚠️ 已全部拍板 — 決議見 §11)

> 本節保留當初的**選項與取捨理由**作為背景。**六項的正式決議一律以 §11 為準**;
> 若本節文字與 §11 衝突,以 §11 為準。摘要:
> 1) launcher 擁有 Streamlit(+ 受 token 保護的控制通道) 2) 第一版與 Fleet 解耦
> 3) 建置時可連網、產出物離線 4) 接受 250–350MB 5) 不做單實例鎖 6) 本輪不改 nativeApp 程式碼

### 待決 1 — 誰擁有 Streamlit 程序?

| 方案 | 做法 | 優點 | 缺點 |
|------|------|------|------|
| **A(建議)** | `launch.py` 起 streamlit,health check 過才起殼;shim 只是「回報 URL 的傳聲筒」 | 清理責任單一(殼關 → launch.py 殺自己的 tree);符合 spec §5 流程;使用者按「啟動」時 streamlit 早已 ready,幾乎瞬間顯示 | streamlit 在使用者按啟動前就已在跑(其實是優點:快) |
| B | 殼起 shim → 使用者按「啟動」時 shim 才起 streamlit | 比較像真 engine 的行為 | 啟動慢(按下去才開始起 streamlit,要等數秒~數十秒);清理責任分散到 shim;shim 被崩潰監看重啟時狀態複雜 |

**我建議 A。**

### 待決 2 — 這個交付包要不要跟 Fleet / 模組系統解耦?

shim 不碰 catalog、不碰 SQLite、不碰 plugin.yaml。因此產出的資料夾與 nativeApp 的
模組匯入 / `.napp` / Fleet rollout **完全無關**,是一個獨立的東西。

- **解耦(建議,符合「簡易版」)**:交付包就是死的,更新 = 重發一包新資料夾。
- **不解耦**:未來要讓這些交付包也能被 Fleet 推更新 → 設計現在就要留鉤子(至少 app_id / version / 更新來源欄位)。

**我建議先解耦**,但 `app-package.json` 保留 `app_id` 與 `version` 欄位,未來要接 Fleet 不用改結構。

### 待決 3 — 建置時可以連網嗎?

第一版想直接用 pip 從 PyPI 把 streamlit + 專案相依裝進 runtime(**GUI 上明示「建置時會連網」**),
**但產出物在 User 端 100% 離線**。之後再接既有的 deppack / wheel cache 做到「建置也離線」。

- 這符合 spec §7「若第一個增量暫時允許建置機連網下載 dependencies,GUI 必須清楚標示」。
- 已實測本機 pip 可連 PyPI。

### 待決 4 — 大小可接受嗎?

> ⚠️ **以下是當初的估計。實測是 474 MB —— 見 §14.4 的實際組成與瘦身選項。**

| 項目 | 約略大小 |
|------|----------|
| 可攜 CPython 3.11 | ~80 MB |
| streamlit + 其相依(pandas/numpy/pyarrow…) | ~150–250 MB |
| Tauri 殼 cim-light.exe | ~10 MB |
| **單一交付資料夾合計** | **~250–350 MB**(視專案相依而定) |

若嫌大,可討論:多個 App 共用一份 runtime(但就違反「複製資料夾=部署」的單一性)。

### 待決 5 — 多開行為?

雙擊兩次會產生兩個獨立 port、兩個視窗、互不干擾(spec §5.2 的底線要求)。
要不要加**單實例鎖**(第二次雙擊時把既有視窗帶到前景)?spec 說「可選」。

**我建議第一版不加**,保持簡單,但確保兩份互不誤殺。

### 待決 6 — 那一次點擊,現在要不要順手準備好升級路徑?

我可以同時寫好(但不編譯)這兩個小改動,等你哪天有無 WDAC 的建置機:
1. `portal-react/src/main.jsx`:當工具清單只有一個且 category 是 `app` → 自動啟動(~5 行)。
2. (可選)`lib.rs`:支援 `--url` / `CIM_APP_URL` 直接開 `WebviewUrl::External` → 連 portal 外框都不見了,變成純粹的單一 App 視窗。

重編後,**既有的交付資料夾只要換掉 `shell/cim-light.exe` 就自動升級**,不必重新打包。

---

## 7. 測試策略(對齊 spec §9)

### 7.1 單元測試(builder / launcher,不碰 GUI)
- project 不存在 / entrypoint 不存在 / entrypoint 在 project 外 / `..` 逃逸
- requirements 缺少、或未宣告 streamlit
- shell executable 缺少 → fail closed
- preferred port 空閒 → 用 8501;被占用 → 取得其他 port
- health check:成功 / 逾時 / streamlit 提前退出
- manifest 路徑全為相對,resolve 後仍在 package root 內
- cleanup 只處理自己的 subprocess
- staging 失敗不覆蓋既有成功輸出(原子換位)
- **shim CLI**:確實 bind 殼傳入的 `--control-port`(不可只呼叫 handler 測 JSON — §10.1);缺參數即失敗;未知參數忽略但記錄
- **控制通道**:無 token / 錯 token 的請求一律拒絕;token 每次啟動不同;launcher 結束時通道關閉
- **`/tools/stop`**:確實終止 Streamlit 且**等到 port 真的釋放**才回 200;之後 `/tools/active/status` 回 `{"active":false}`
- **Stop 後再 Start**:重選 port、重起、health ready 後才回**新的** URL;整條鏈 < 30 秒(§3.3)
- **停止/啟動逾時**:回非 2xx 與可理解錯誤,不得假成功

### 7.2 真實 E2E(不可只 mock)
用 fixture Streamlit app(印 `READY`),跑**真實** streamlit + **真實**預建 Tauri 殼,
以 Playwright over CDP 連上 WebView2(沿用 [`e2e-test` skill](../../nativeApp/apps/host-tauri/e2e/lib.mjs) 的既有 harness):

1. 產出物不含建置機的絕對路徑
2. **先占住 8501**,launcher 仍能用其他 port ready
3. `/_stcore/health` 回 200
4. **Tauri 視窗裡真的算繪出 `READY`**(不是只檢查 process 活著)
5. **portal 按「停止」→ 該 port 真的不再接受連線**(不是只看 portal 顯示)
6. **再按「啟動」→ 換到新 port、視窗重新算繪出 `READY`**
7. 關閉 Tauri 後該 port 不再接受連線、無殘留 python
8. 把產出物搬到**含空白與中文的路徑**仍能啟動
9. 斷網仍能啟動
10. **同時開兩份**:各自獨立 port,關掉一份不影響另一份(§11 決議 5)

### 7.3 交付文件
E2E 過程逐步截圖 → 產出一份可離線開啟的 HTML step-by-step(沿用
[`e2e/build_gui_step_guide.py`](../e2e/build_gui_step_guide.py) 的內嵌 base64 做法),讓你照著做。

---

## 8. 實作順序(定案後)

| 階段 | 內容 | 驗收 |
|------|------|------|
| P0 | ✅ **已完成** — 動態 URL 契約驗證(本文 §1、§2) | 三種契約皆不支援;engine 契約可代替 |
| P1 | `launcher/`:`launch.py` + `engine_shim.py` + **受 token 保護的本機控制通道**,以 fixture app + **真實 cim-light.exe** 驗證,不接 GUI | 8501 空閒/被占用都能起;**Start → Stop → 再 Start → 關殼** 全程無假成功、無殘留;shim 確實監聽殼給的 control port;重啟耗時 < 30 秒(§3.3) |
| P2 | `src/provision_builder/streamlit_desktop/`:validate / runtime / builder + 範本 | 產出物複製到含空白中文路徑仍可離線啟動 |
| P3 | 接進 `provision_gui.py`(獨立區塊,背景 worker,不卡 UI thread) | 全程從 GUI 選 fixture 專案並建立成功 |
| P4 | 測試補齊 + README + 真實 E2E 截圖 HTML | spec §10 DoD 逐條檢查(除「一次點擊」外全綠) |

---

## 9. 與 DoD 的逐條對照(誠實版,已依 §12 更新)

| DoD 條目 | 本設計 |
|----------|--------|
| 管理員可從 GUI 選任意 Streamlit 專案及入口 | ✅ |
| GUI 能產生結構完整的 User 資料夾 | ✅ |
| User 電腦不需預裝 Python/Streamlit/Node/Rust | ✅ |
| ~~User 只需雙擊 `start.bat`~~ → **雙擊 + 按一次「啟動」** | ⚠️ **已依 §12 正式改寫為 MVP 操作契約**,不再視為待消除的缺陷(§2.2 說明原因;換用可重編的殼後才恢復原 DoD) |
| 8501 被占用時自動改用可用 port | ✅(launcher 負責,含 bind race 重試 — §10.3) |
| Tauri 顯示的是本次啟動的正確 URL | ✅(只交付真正通過 health check 的那一個) |
| **portal Stop / 再 Start / 關閉 Tauri 三條路徑都無殘留** | ✅(§3.2 真實停止,不假回成功 — §10.2) |
| 可在離線、含空白/中文的搬移路徑執行 | ✅(納入測試) |
| 錯誤訊息可理解、log 可定位 | ✅ |
| 新增單元測試通過、既有測試無 regression | ✅ |
| 真實 Streamlit + 真實預建 Tauri 的 E2E 通過 | ✅(P1 就要做,不等到最後) |

---

## 10. 設計審查決議（開工前必讀）

> 審查結論：**同意採用 engine-shim 路線**。這不是退化成瀏覽器或固定 port 的替代方案；動態 port、Streamlit health check 通過後才交付 URL、Tauri 內顯示內容及離線交付等核心需求均保留。
>
> 以下四項為開工前必須納入設計與測試的修正。完成後依 P1 → P2 → P3 → P4 實作。

### 10.1 `engine_shim.py` 必須實作殼的命令列契約

預建殼實際會用下列形式啟動 shim：

```text
runtime/python.exe launcher/engine_shim.py \
  --control-port <由殼配置的 port> \
  --log-dir <由殼配置的 log path>
```

因此 shim 必須：

1. 使用 `argparse` 接收 `--control-port` 與 `--log-dir`。
2. HTTP server 必須監聽殼傳入的 `--control-port`，不可自行找 control port。
3. 只能 bind `127.0.0.1`，不可監聽 `0.0.0.0`。
4. 使用 `--log-dir` 寫入 shim log；建立目錄失敗時要以非零 exit code 結束並輸出可理解的錯誤。
5. 對未知參數採取明確策略：建議使用 `parse_known_args()` 忽略並記錄未知參數，以容忍未來殼增加非關鍵參數；但缺少必要參數必須立即失敗。
6. `/health` 只有在 HTTP server 已完成 bind 且能正常服務時才回 `{"status":"ok"}`。

P1 必須先加入 focused test，驗證 shim 確實監聽指定的 control port；不要只直接呼叫 handler 測試 JSON。

### 10.2 `/tools/stop` 不可假回成功

目前設計選擇 `launch.py` 擁有 Streamlit 程序，但 portal 的「停止」只會呼叫 shim：

```text
POST /tools/stop
```

若 shim 只回成功、Streamlit 卻繼續執行，portal 狀態與真實程序會不一致。因此正式契約如下：

- `launch.py` 是 Streamlit process tree 的唯一 owner。
- shim 不可直接按名稱掃描或自行殺掉任意 `python.exe`。
- shim 與 launcher 之間建立一個只限本機、只供本 package 使用的控制通道。
- `/tools/stop` 必須通知 launcher 終止本次 Streamlit process tree，等待 port 釋放後才回成功。
- 停止後 `/tools/active/status` 回 `{"active":false}`。
- 再按「啟動」時，launcher 必須重新啟動 Streamlit、重新選擇可用 port、完成 health check；shim 只能在 ready 後回傳新的 URL。
- 啟動或停止逾時時回非 2xx 與清楚錯誤，portal 不可被告知假成功。

控制通道可以使用 loopback HTTP 或其他可測試的本機 IPC，但必須：

- 使用每次啟動產生的隨機 token 驗證請求；
- 不監聽 LAN；
- 不把固定 token 寫死在 package；
- launcher 結束時一併關閉；
- 不讓 shim 變成第二個程序 owner。

P1 的真實流程驗收至少包含：

```text
啟動 → health ready → portal Stop → port 釋放
     → portal 再次 Start → 新 URL ready → 關閉殼 → 無殘留程序
```

如果第一個增量確實無法支援 Stop/Restart，則必須在 portal 中真正隱藏或停用「停止」操作；不得保留可按按鈕並由 shim 假回成功。但修改內嵌 portal 需要重編殼，因此目前設計應以完成上述控制通道為準。

### 10.3 分清楚兩種動態 port

本文 §0 的「動態 port 已經存在，不需自己造」需要改成更精確的說法。系統內有兩個不同用途的 port：

| Port | 擁有者 | 用途 | 選擇方式 |
|------|--------|------|----------|
| Engine-shim control port | 預建 Tauri 殼 | 殼與 shim 的控制 API | 由殼的 `find_free_port()` 配置，透過 `--control-port` 傳給 shim |
| Streamlit application port | `launch.py` | 實際 Streamlit UI | 先試 preferred port；占用時由 OS 配置，並處理 bind race |

殼既有的 `find_free_port()` **不負責** Streamlit application port。`launch.py` 仍須完成 spec §5.1 的選 port、最多 5 次 bind race 重試及 health check。兩個 port 不可混用，也不能假設兩者固定相鄰。

### 10.4 本輪不要順手修改 Native App/Tauri 原始碼

待決事項 6 本輪只保留為後續工作單，不修改 `nativeApp` repo：

- 「只有一個 app 時自動啟動」的 portal 補丁方向合理，但需在可重編殼的環境另案實作及驗證。
- `WebviewUrl::External` 不只是替換 URL，還涉及 CSP、navigation allowlist、外部頁面權限、sidecar 是否仍啟動及關閉清理；不可先寫一個未驗證補丁宣稱未來可直接套用。
- 本輪驗收以目前預建殼及「雙擊後按一次啟動」為正式行為。

## 11. 六項待決事項的正式拍板

| 待決事項 | 決議 | 實作要求 |
|----------|------|----------|
| 1. Streamlit 程序 owner | 採方案 A：`launch.py` 擁有 | shim 只提供 engine API；Stop/Restart 經過受保護的本機控制通道請求 launcher |
| 2. Fleet／模組系統 | 第一版解耦 | `app-package.json` 保留 `app_id`、`version`，但不實作更新或 rollout |
| 3. 建置時連網 | 第一版允許 | GUI 明示可能連網；User 交付包執行時必須完全離線 |
| 4. 交付大小 | 接受約 250–350 MB → ⚠️ **實測 474 MB**(§14.4) | GUI 建置前顯示估計，完成後顯示實際資料夾大小；不可把估計值當硬上限 |
| 5. 多開 | 第一版不做單實例鎖 | 每次啟動使用獨立 port／token／process tree，關閉其中一份不得誤殺另一份 |
| 6. 未來殼升級 | 本輪不改程式 | 只記錄後續工作單，待可重編環境再設計、實作、E2E |

## 12. 更新後的 MVP 操作契約與 DoD

原 spec 的「User 只需雙擊 `start.bat`」在目前不可重編的預建殼下無法成立。第一版正式操作契約改為：

> User 雙擊 `start.bat`，等待 Tauri Portal 出現，再按一次「啟動」，即可使用 Streamlit App。

這是已知且接受的 MVP 行為，不再標記為預期於本輪消除的缺陷。未來換用支援自動啟動的重編殼後，才恢復單次雙擊的原始 DoD。

除上述操作差異外，原 spec §10 其餘 DoD 全部維持；特別是：

- 傳給 Tauri 的 URL 必須是本次真正通過 health check 的 URL；
- Streamlit port 被占用時必須自動改用可用 port；
- User 端完全離線且不需預裝 Python、Node、Rust 或 Streamlit；
- 搬移到含空白／中文的路徑後仍可啟動；
- portal Stop、再次 Start、關閉 Tauri 三條路徑都不得留下本次 Streamlit process tree；
- P1 必須先以真實預建殼驗證 shim，再開始 GUI 整合。

## 13. 核准後的實作入口

後續 AI 可依下列順序直接開工，不需要重新決定架構：

1. 更新本文 §0、§3、§4、§6、§8、§9，使其與 §10–§12 的正式決議一致。
2. P1 先實作 launcher、shim、受 token 保護的本機控制通道及 focused tests。
3. 使用 fixture Streamlit App 與真實 `cim-light.exe` 驗證 Start → Stop → Restart → Close。
4. P1 通過後才實作資料夾 builder。
5. P2 通過搬移與斷網測試後才接 `provision_gui.py`。
6. 最後跑相關 regression 與真實 WebView2 E2E，並產生逐步操作 HTML。

---

## 14. 完成報告(2026-07-12)

**狀態:P1–P4 全部完成,真實 E2E 通過。**

### 14.1 落點

| 位置 | 內容 |
|------|------|
| `src/provision_builder/streamlit_desktop/` | `models.py` / `validate.py` / `runtime.py` / `builder.py` / `discover.py`(核心,不 import Tkinter) |
| `src/provision_builder/streamlit_desktop/templates/` | `launch.py`(launcher)、`engine_shim.py`(殼要的 engine)、`start.bat` |
| `provision_gui.py` | 新增「Streamlit 桌面資料夾」**分頁**(既有 dep-pack 流程原封不動搬進第一頁) |
| `tests/test_streamlit_desktop_launcher.py` | 32 個測試(manifest 逃逸、選埠、健康檢查、停止、token 通道、shim 契約) |
| `tests/test_streamlit_desktop_builder.py` | 25 個測試(驗證、組裝、原子換位、自檢) |
| `tests/test_streamlit_desktop_discover.py` | 13 個測試(自動偵測殼/runtime/入口;含「拒絕在多個候選之間亂猜」) |
| `e2e/streamlit_desktop_build.py` | 真實建置(真 runtime、真 pip) |
| `e2e/streamlit-desktop-drive.mjs` | 真實 E2E(真 Streamlit + 真 cim-light.exe + 真 WebView2 CDP) |
| `e2e/capture_provision_gui.py` | 管理端 GUI 真實截圖 |
| `docs/streamlit-desktop-step-by-step.html` | **逐步操作教學**(8 張真實截圖內嵌,可離線開啟) |

### 14.2 真實 E2E 結果(`e2e/streamlit-desktop/result.json`)

| 驗證項目 | 實測 |
|----------|------|
| 交付包只曝一個 app 工具 | `["app-portable-streamlit-smoke"]` |
| **8501 被刻意占用 → 自動改用其他埠** | ✅(最後一次跑是 62864;每次 OS 配發的埠不同) |
| **Tauri 視窗真的算繪出應用** | 從 iframe 讀到 `READY` ✅(非「process 活著」的代理指標) |
| **按「停止」→ 埠真的關閉** | ✅(§10.2 要求的「不可假回成功」) |
| **再按「啟動」→ 換到新埠並重新算繪** | ✅(最後一次跑是 55280) |
| **關閉視窗 → launcher 收尾、無殘留** | ✅ |

> 埠號每次執行都不同(OS 配發),`result.json` 是最後一次執行的實測值;
> 教學 HTML 的內文與驗證表都從同一份 `result.json` 產生,不會出現兩個對不起來的數字。

**§3.3 的 30 秒預算實測**:Stop → Start → health ready 全程 **1.4 秒**(18:09:11.267 → 18:09:12.705),
距離殼的 30 秒硬逾時非常寬裕。冷啟(spawn → healthy)約 **1.5 秒**。

回歸:native_Provision 全套測試通過,既有測試零 regression。

### 14.2b 搬移驗證(`e2e/streamlit_desktop_relocate_check.py`)

把交付包整包複製到 `dist\relocate check 中文 資料夾\`(**同時含空白與中文**)後啟動:
manifest 解析、選埠、spawn Streamlit、health check 全部正常,`/_stcore/health` 回 **200**。
該次沒有其他程式占用 8501,launcher 就用了 8501 —— 順帶證明「偏好埠可用時會用它」。

### 14.3 實作中發現並修掉的三個真實缺陷

1. **`ControlServer.shutdown()` 會永久阻塞** —— CPython 的 `BaseServer.shutdown()` 會等 `serve_forever()`
   迴圈發出的事件,若 server 從未 `start()` 過,那個事件永遠不會被設定。任何「還沒啟動就要收尾」的
   路徑都會讓 launcher 掛死。已加 `_serving` 旗標。(由測試抓到,不是推測。)
2. **原子換位在 Windows 撞 `WinError 5`** —— 剛寫完 450 MB 的 runtime,Defender 仍持有 handle,
   `os.rename` 被拒。這是**真實使用者也會遇到**的失敗。已改為退避重試(0.5s→8s,6 次),並補回歸測試。
   (由第一次真實建置抓到 —— 單元測試的小目錄不會觸發。)
3. **launcher 的 `print()` 被區塊緩衝吃掉** —— stdout 一旦被導向檔案或父程序就不是行緩衝,
   「ready at …」會卡在緩衝區裡。使用者若把 console 導出來除錯,會看不到啟動訊息(看起來像卡死)。
   已在使用者可見的訊息加 `flush=True`。(由搬移驗證抓到 —— 它從管線讀 launcher 的輸出時掛住了。)

### 14.4 ⚠️ 交付大小:實測 474 MB(估計是 250–350 MB)

實際組成:

| 項目 | 大小 | 說明 |
|------|------|------|
| `runtime/` | **457.4 MB** | 其中 site-packages 佔絕大多數 |
| ├ pyarrow | 85.5 MB | Streamlit **硬相依**(DataFrame 序列化) |
| ├ pandas | 66.4 MB | Streamlit 硬相依 |
| ├ numpy (+numpy.libs) | 53.1 MB | pandas/pyarrow 的相依 |
| ├ streamlit | 25.4 MB | |
| ├ pydeck / PIL / altair / pygments | ~55 MB | Streamlit 硬相依 |
| └ CPython 本體 + stdlib | ~150 MB | |
| `shell/cim-light.exe` | 16.6 MB | |
| `application/`(使用者專案) | ~0 MB | **成本完全不是來自使用者的 app** |

**結論:成本的地板是「Streamlit 生態系」,不是我們的打包方式。** 任何 Streamlit 桌面化方案都要背這些。

瘦身選項(尚未實作,待決):

| 可移除 | 省下 | 風險 |
|--------|------|------|
| `__pycache__`(可重建) | 81.5 MB | 首次啟動要重新編譯,慢幾秒 |
| 各套件的 `tests/` 目錄 | 55.7 MB | 低(執行期不 import) |
| `pip`(裝完就沒用) | 9.3 MB | 低(但 `setuptools`/`pkg_resources` 不可動,有些套件會 import) |
| `tcl` / tkinter | 6.7 MB | 中(若使用者 app 用 matplotlib TkAgg 會掛) |

保守做法(只拿前兩項)可降到 **約 337 MB**。實作時必須重跑 E2E 確認 `import streamlit` 與畫面算繪仍正常。

### 14.5b GUI 只問「非問不可」的東西(2026-07-12 追加)

初版表單有六個欄位,其中四個是噪音——它們的答案每次都一樣,或可從專案推導。
改成:**唯一必填是「專案資料夾」**,其餘由 `discover.py` 偵測:

| 欄位 | 怎麼來的 | 猜不到時 |
|------|----------|----------|
| Tauri 殼 | 掃 `../nativeApp/apps/host-tauri/prebuilt/` 等已知位置(可用 `CIM_TAURI_EXE` 覆寫) | 畫面顯示 ✗ 與取得方式;不會靜靜用錯的殼 |
| 可攜 Python runtime | 掃 `.runtime-cache/python311` 等(可用 `CIM_PORTABLE_PYTHON` 覆寫) | 出現「下載可攜 Python」按鈕,一鍵跑 nativeApp 既有的 fetch 腳本 |
| 輸出位置 | 預設 `dist/streamlit-apps` | — |
| 應用名稱 | 專案資料夾名 | — |
| 入口檔案 | 先找 `app.py`/`main.py`/`streamlit_app.py`;否則找**唯一**一個 import streamlit 的檔案 | **有多個候選就明講要人選**,不擲骰子 |
| 偏好連接埠 | 預設 8501 | — |

殼與 runtime 移進預設收合的「進階設定」。**偵測結果一律顯示在畫面上**——
靜靜地用一個使用者不知道的路徑,比問他還糟。
(13 個 `test_streamlit_desktop_discover.py` 測試釘住這些行為,包含「拒絕在兩個候選之間亂猜」。)

### 14.5 已知限制(不是缺陷,是已接受的決議)

- **User 雙擊 `start.bat` 後仍要按一次「啟動」**(§12 正式接受的 MVP 操作契約)。
  換用可重編的殼後,**交付資料夾不必重做**,只要換掉 `shell/cim-light.exe`。
- **斷網環境尚未在真機驗過**(§7.2 第 9 項)。間接證據:launcher 與 shim 只打 127.0.0.1,
  runtime 的相依在建置時就裝好了,執行期不呼叫 pip;但「拔網路線再跑一次」還沒做。
- 建置時需連網(pip);產出物在 User 端完全離線。
- **多開**尚未做真機驗證(§11 決議 5 只要求「互不誤殺」;單元測試已證明 supervisor 只殺自己的程序)。
