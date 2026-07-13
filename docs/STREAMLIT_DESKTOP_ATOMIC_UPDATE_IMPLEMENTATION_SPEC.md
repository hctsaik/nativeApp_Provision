# Streamlit Desktop 原子更新與共用 Runtime 實作規格

> 用途：交給後續 AI 直接實作。
>
> 狀態：**已實作**（2026-07-12）。落點：`src/provision_builder/streamlit_desktop/device/`
> （隨包出貨到 `<ROOT>/bootstrap/`，stdlib-only）+ `store_builder.py`（建置端）。
> 測試：`tests/test_streamlit_desktop_state.py`（Phase 1 基礎）、
> `tests/test_streamlit_desktop_store_flow.py`（Phase 2–5 流程）。
> 真實 E2E（兩支，都通過）：
> - `e2e/streamlit_desktop_store_e2e.py`（headless）：真 pip/真 Streamlit/真 bootstrap 鏈，
>   含首啟深驗、背景更新、cold-start promote、**壞版自動回滾**、failed_versions 不重試。
> - `e2e/streamlit-desktop-store-drive.mjs`（真機 WebView2）：真 `cim-light.exe` + CDP，
>   兩次冷啟動，**從 Tauri 視窗的 iframe 裡讀出版本字串**證明重啟真的換版
>   （v1.0.0 → 設定 pending → 重啟 → 視窗顯示 v1.1.0）。
>
> 逐步操作教學（真實截圖）：`docs/streamlit-desktop-store-step-by-step.html`。
> 實作過程中由測試/E2E 抓到並修正的缺陷清單見本文件 §17。
>
> 平台：第一版僅支援 Windows x64、可攜式 CPython 3.11、預建 Tauri 殼。
>
> 相關文件：
>
> - [`SIMPLE_STREAMLIT_TAURI_FOLDER_BUILDER.md`](SIMPLE_STREAMLIT_TAURI_FOLDER_BUILDER.md)
> - [`SIMPLE_STREAMLIT_TAURI_FOLDER_BUILDER_PHASE0_AND_DESIGN.md`](SIMPLE_STREAMLIT_TAURI_FOLDER_BUILDER_PHASE0_AND_DESIGN.md)
> - [`STREAMLIT_DESKTOP_STORE_AND_SLOTS_DESIGN.md`](STREAMLIT_DESKTOP_STORE_AND_SLOTS_DESIGN.md)

## 1. 目標

Streamlit Desktop App 每次啟動時先檢查本機是否已有完整的待更新版本。若有，先完成版本切換，再啟動 App。App 執行期間在背景檢查遠端是否有新版本；若有，下載並驗證完成後放入邏輯 `NEXT`，通知 User：

> 新版本已準備完成。關閉並重新開啟 App 後將自動套用。

下一次啟動時：

```text
PREVIOUS ← 舊 CURRENT
CURRENT  ← 舊 PENDING
PENDING  ← null
```

然後以新 CURRENT 啟動。如果新版無法通過 Streamlit health check，必須自動切回 last-known-good／PREVIOUS 並啟動舊版。

系統必須能在 requirements 沒有改變時只下載 application code，不重複下載或複製 Python、Streamlit、NumPy、Pandas 等 libraries。

## 2. 已拍板的架構決策

1. 不使用實體 `PROD/PREV/NEXT` 目錄 rename。
2. 不使用 junction、symlink 或 per-library `PYTHONPATH` 拼裝。
3. Application 版本保存在不可變的 `versions/<version>/`。
4. `PROD/PREV/NEXT` 是管理畫面上的邏輯名稱，權威狀態集中在一份 `state/state.json`。
5. `state.json` 使用「完整 tmp 檔 + flush + `os.replace()`」原子替換。
6. Python 與全部 site-packages 以完整 runtime fingerprint 為單位共用。
7. Runtime 不可原地修改；requirements 改變就建立新 fingerprint。
8. 更新下載與版本切換是兩個獨立動作：執行期間只 stage，下一次 cold start 才 promote。
9. 不強制關閉正在使用的 App。
10. 第一版允許建置端／裝置端下載更新時使用網路，但完成後的版本必須能離線啟動。

## 3. 名詞

| 顯示名稱 | 狀態欄位 | 意義 |
|----------|----------|------|
| PROD | `current` | 下一次正常啟動使用的正式版本 |
| PREV | `previous` | 前一個可快速 rollback 的完整版本 |
| NEXT | `pending` | 已下載、驗證並準備於下次啟動套用的版本 |
| LKG | `last_known_good` | 最近一次真正通過啟動 health check 的版本 |
| Candidate | `candidate` | 已 promote、正在等待首次啟動驗證的新版本 |

文件與程式欄位統一使用 `PREV`／`previous`，不要再使用容易和 pre-release 混淆的 `PRE`。

## 4. 部署目錄

```text
<ROOT>/
├─ bootstrap/
│  ├─ start.bat
│  └─ bootstrap.py                 # 位於版本目錄外，負責 promote/rollback/啟動
├─ apps/
│  └─ <app-id>/
│     ├─ state/
│     │  ├─ state.json             # 唯一權威狀態
│     │  ├─ update.lock            # 跨程序鎖；內容含 owner metadata
│     │  └─ history/               # 有界的操作結果，不是權威狀態
│     ├─ versions/
│     │  ├─ v1.2.0/
│     │  │  ├─ application/
│     │  │  ├─ launcher/
│     │  │  ├─ shell/
│     │  │  ├─ app-package.json
│     │  │  ├─ files.json
│     │  │  └─ .complete
│     │  └─ v1.3.0/
│     ├─ staging/                  # 未完成下載；永遠不可被 state.json 引用
│     └─ data/
│        ├─ logs/
│        ├─ cache/
│        ├─ home/
│        ├─ tmp/
│        └─ leases/                # 執行中版本/runtime lease
└─ deps/
   └─ runtimes/
      ├─ cp311-<fingerprint>/
      │  ├─ python.exe
      │  ├─ Lib/site-packages/
      │  ├─ runtime.json
      │  ├─ files.json
      │  └─ .complete
      └─ staging/
```

`bootstrap.py` 不可放在 `versions/<version>` 內，否則它無法安全地管理目前與下一版本。User 的 `start.bat` 永遠只呼叫這個版本外 bootstrap。

### 4.1 自舉策略（解決雞生蛋問題）

`bootstrap.py` 需要 Python 直譯器，但「正確的」直譯器位置要讀完 `state.json` → current 版本 manifest → runtime fingerprint 才知道。解法分兩段：

1. **start.bat 用「任一」runtime 自舉**：以 `for /d` 掃 `deps\runtimes\*\python.exe`，挑到第一顆就拿來跑 `bootstrap\bootstrap.py`（bootstrap 與其模組全部 stdlib-only，任何 3.11 runtime 都能執行）。`deps\runtimes\` 全空 → 印明確錯誤（「交付包不完整，缺共用 runtime」）並 pause。
2. **bootstrap 用「正確的」runtime 跑 App**：讀 state → current 版本 → `runtime_fingerprint` → 以 `deps\runtimes\<fp>\python.exe` spawn `launcher/launch.py`。自舉直譯器與執行直譯器可以不同顆，無害。

`bootstrap/` 內含 bootstrap.py 與它的兄弟模組（state/locks/integrity/…，皆 stdlib-only），由 builder 從 `provision_builder/streamlit_desktop/device/` 複製；模組使用「套件內相對 import、失敗時退回同目錄 import」的雙模式，使其既可被測試 import、也可作為散檔執行。

多 app 入口：builder 為每個 app 產 `start-<app-id>.bat`；整根只有一個 app 時另附 `start.bat`。bootstrap 未帶 `--app` 時，若 `apps\` 下只有一個 app 則自動選定，否則要求參數。

## 5. `state.json` 契約

最小 schema：

```json
{
  "schema_version": 1,
  "generation": 18,
  "app_id": "my-streamlit-app",
  "current": "v1.3.0",
  "previous": "v1.2.0",
  "pending": null,
  "candidate": "v1.3.0",
  "last_known_good": "v1.2.0",
  "failed_versions": [],
  "last_operation": {
    "id": "uuid",
    "kind": "promote",
    "status": "completed",
    "timestamp_utc": "2026-07-12T10:00:00Z"
  }
}
```

規則：

- `generation` 每次成功寫入加一。
- 版本值只能是安全 identifier，不得包含路徑分隔符、`..`、磁碟代號或控制字元。
- `current` 必填，而且必須指向完整版本。
- `previous`、`pending`、`candidate` 可為 `null`。
- `candidate` 必須等於剛 promote 的 `current`，直到 health check 成功或 rollback。
- `last_known_good` 只能在真實 Streamlit health check 成功後更新。
- 所有路徑由可信 root 加上已驗證 identifier 組成；不可接受 manifest 提供任意絕對路徑。

### 5.1 原子寫入

集中實作單一 `StateStore`，其他模組不得自行寫 JSON：

1. 取得 app update lock。
2. 讀取並驗證目前 state。
3. 產生完整的新 state，`generation + 1`。
4. 在同一個 `state/` 目錄建立唯一 tmp 檔。
5. UTF-8 寫入完整 JSON，flush 並 `os.fsync()`。
6. 關閉檔案 handle。
7. 使用 `os.replace(tmp, state.json)`。
8. 重新讀回並驗證 generation 與內容。
9. 釋放 lock。

不要分別寫 `PROD.txt`、`PREV.txt`、`NEXT.txt`。不要讓讀取端長期保持 `state.json` handle 開啟。

## 6. Application version 契約

每個 `versions/<version>/app-package.json` 至少包含：

```json
{
  "schema_version": 2,
  "app_id": "my-streamlit-app",
  "version": "v1.3.0",
  "entrypoint": "application/app.py",
  "runtime_fingerprint": "cp311-a1b2c3d4e5f6",
  "shell_executable": "shell/cim-light.exe",
  "health_path": "/_stcore/health",
  "startup_timeout_seconds": 60
}
```

完整版本必須同時符合：

- 目錄位於正確 app 的 `versions/` 下。
- `app_id`、`version` 與目錄名稱一致。
- 所有相對路徑 resolve 後仍位於該版本目錄。
- `files.json` 中每個檔案的 size 與 SHA-256 正確。
- 所需 runtime 存在且完整。
- `.complete` 是最後才建立的 sentinel。

版本目錄一旦 `.complete` 存在就視為不可變。更新、修復或補檔必須產生新版本或重新 stage，不可直接改內容。

## 7. Runtime Store 與只更新程式碼

Runtime fingerprint 必須涵蓋：

- Python 完整版本；
- OS、architecture 與 ABI；
- 正規化後的完整 dependency lock；
- 會影響 runtime 內容的 builder format version。

概念公式：

```text
fingerprint = sha256(
  python_version + platform + architecture + abi
  + normalized_dependency_lock
  + runtime_builder_version
)
```

如果新舊版本的 `runtime_fingerprint` 相同：

```text
只下載 versions/<new-version>/，不下載 deps/runtimes/。
```

若不同，才另外下載缺少的新 runtime。舊 runtime 必須保留，直到不再被 current、previous、pending、candidate、LKG 或 active lease 引用。

### 7.1 Dependency lock 硬性規則

第一版建置必須有完整 lock，不接受只有寬鬆 `requirements.txt` 的可更新 package。至少要求：

- 所有 PyPI dependency 都固定為 `name==version`；
- 正規化名稱大小寫與 `-`／`_`；
- 排序後計算 fingerprint；
- 拒絕 editable install；
- local path、VCS URL、直接 URL 必須另有明確 hash 與可攜性規則，第一版可直接拒絕；
- 環境 marker 必須在建置目標平台解析後凍結；
- lock 與建置後 `pip freeze` 必須能對帳。

### 7.2 Runtime 必須保持唯讀

Launcher 啟動 App 時至少設定：

```text
PYTHONDONTWRITEBYTECODE=1
PYTHONPYCACHEPREFIX=<app-data>/cache/pycache
HOME=<app-data>/home
USERPROFILE=<app-data>/home
TMP=<app-data>/tmp
TEMP=<app-data>/tmp
```

Streamlit 與第三方套件的 writable cache/config 也要導向 `apps/<app-id>/data/`。不得讓任何 App 寫入共用 runtime。

## 8. 啟動、Promote 與 Rollback 狀態機

### 8.1 每次啟動

```text
User 雙擊 start.bat
  ↓
bootstrap 取得 app update lock
  ↓
讀取並驗證 state.json
  ↓
若 pending != null：驗證 pending version + runtime
  ├─ 驗證失敗：清除/隔離 pending，記錄失敗，仍啟動 current
  └─ 驗證成功：一次原子寫入新 state
       previous       = current
       current        = pending
       pending        = null
       candidate      = 新 current
       last_known_good 保持不變
  ↓
釋放 update lock
  ↓
為 current version/runtime 建立 active lease
  ↓
啟動 current
  ↓
Streamlit health check
  ├─ 成功：candidate 清空，last_known_good=current
  └─ 失敗且 current==candidate：rollback 後啟動 LKG/PREV
```

Promote 不搬動、不 rename 版本目錄，只原子替換 state。該次啟動解析 current 成固定絕對路徑後，不再重新讀 state。

Health check 失敗但 `current != candidate`（穩定版因環境因素起不來，如防毒鎖檔）：**照現行行為 fail loud**（印錯誤與 log 路徑、非零 exit code），不觸發 rollback —— 回滾只服務「新版本壞了」，不掩蓋環境問題。

### 8.2 Rollback

候選版本首次啟動失敗時：

1. 終止本次啟動的 candidate process tree。
2. 取得 update lock。
3. 確認 `last_known_good` 或 `previous` 仍完整。
4. 原子更新：`current=LKG`、失敗 candidate 加入 `failed_versions`、`candidate=null`。
5. 釋放 lock。
6. 建立 LKG lease 並啟動 LKG。
7. 通知 User 新版啟動失敗，已恢復上一版本，並顯示 log 路徑。

同一版本若已在 `failed_versions`，背景 updater 不可自動再次設為 pending，除非 release manifest 的修訂識別碼不同或管理員明確重試。

## 9. 執行期間背景更新

App 成功啟動後，launcher/updater 在背景做一次更新檢查。第一版不需要持續 polling；每次啟動檢查一次即可。

```text
GET latest release metadata
  ↓
沒有新版 → 結束背景工作
  ↓ 有新版
驗證 app_id、版本政策、release manifest
  ↓
判斷 runtime fingerprint 本機是否已存在
  ├─ 已存在 → 只下載 application version artifact
  └─ 不存在 → 下載 application + runtime artifact
  ↓
下載到唯一 staging 目錄
  ↓
逐檔 SHA-256／大小／schema／路徑逃逸驗證
  ↓
完成 runtime（若需要），最後建立 runtime .complete
  ↓
完成 app version，最後建立 version .complete
  ↓
原子更新 state.pending
  ↓
通知 User 下次啟動套用
```

### 9.1 Update Provider 邊界

第一版不要把 core updater 綁死到 Fleet、HTTP、共享資料夾或 USB。定義介面：

```python
class UpdateProvider(Protocol):
    def get_latest_release(self, app_id: str, current_version: str) -> ReleaseMetadata | None: ...
    def download_app(self, release: ReleaseMetadata, destination: Path) -> None: ...
    def download_runtime(self, release: ReleaseMetadata, destination: Path) -> None: ...
```

可先實作一個最簡單、可測試的 provider；但 artifact 必須由 manifest、size、SHA-256 驗證，不能只信檔名或 HTTP status。未來再由 Fleet push 或遠端 provider 實作相同介面。

**第一個 provider 必須是 `FolderUpdateProvider`（本地資料夾／USB）** —— 這條產品線的 User 機是離線的，
「網路下載」是選配而非預設。update source 目錄佈局：

```text
<update-source>/<app-id>/
├─ release.json                    # {schema, app_id, version, revision, runtime_fingerprint}
├─ versions/<version>/             # 版本 payload（含 files.json，不含 .complete）
└─ runtimes/<fingerprint>/         # 只在目標機缺該指紋時需要（含 files.json + runtime.json，不含 .complete）
```

update source 的位置由 `apps/<app-id>/config.json` 的 `update_source` 指定（管理員可編輯）；
未設定 → 背景更新整段跳過，系統退化為純手動部署（仍完全可用）。§2.10 修正：**裝置端預設不需要網路**。

### 9.2 User 通知

下載及驗證全部完成、`pending` 已成功寫入後才能通知：

> 新版本 {version} 已準備完成。關閉並重新開啟 App 後將自動套用。

第一版按鈕只需要「知道了」，不得擅自關閉 App。通知失敗不能讓更新交易失敗，但必須寫 log；下次啟動仍照 state.pending promote。

## 9.3 首次部署與匯出（自 STORE_AND_SLOTS_DESIGN §4 併入）

- **建置**：builder 以 lock 檔算指紋；store 命中（`.complete` 在）即跳過 457MB 安裝；未命中在 `deps/runtimes/.staging-*` 組裝後原子換位。版本目錄 ~17MB。
- **首次部署**：匯出整個 `<ROOT>` 到 USB，**排除 runtime 的 `.complete`**（版本目錄的 `.complete` 保留——17MB 的損壞會在啟動時大聲失敗且可回滾，457MB 的損壞必須在首啟被逐檔驗出）→ 目標機整根複製到任意路徑 → 雙擊 `start-<app>.bat` → 首啟深度驗證 runtime（逐檔 sha256，印進度）→ 寫 sentinel → 起 App。零安裝、全樹真檔案、FAT/exFAT USB 皆可。
- **手動更新（無 update source 時）**：把 `versions/<ver>/` 與（若指紋變了）`runtimes/<fp>/` 複製進對應位置 → 管理員以 `bootstrap.py --set-pending <ver>` 或編輯工具寫入 pending → 下次啟動自動 promote。
- **移除**：刪 `apps/<app>/` = App 連 data 一起消失；store 孤兒由 GC 事後回收（「刪資料夾=零殘留」的唯一讓步，寫進 README）。

## 10. Lock、Lease 與並行

### 10.1 Update lock

下列操作必須持有同一個 per-app update lock：

- 修改 `state.json`；
- 將 staging version 安裝進 `versions/`；
- promote、rollback；
- GC 掃描該 app 狀態。

Lock metadata 至少包含 PID、process start time、operation ID、建立時間。不可只因 lock 檔存在就永遠拒絕；要能判斷 owner 是否仍活著並安全處理 stale lock。

### 10.2 Runtime verification lock

每個 runtime fingerprint 需要獨立 lock。多個 App 同時首次使用相同 runtime 時，只允許一個程序深度驗證並建立 `.complete`；其他程序等待後重新檢查結果。

### 10.3 Active lease

App 啟動前建立 lease，內容至少包含：

```json
{
  "lease_id": "uuid",
  "app_id": "my-streamlit-app",
  "version": "v1.3.0",
  "runtime_fingerprint": "cp311-...",
  "pid": 1234,
  "process_start_time": "...",
  "created_at_utc": "..."
}
```

正常關閉後刪除。GC 必須把有效 lease 引用的 version/runtime 加入 keep-set。Stale lease 只能在確認 PID 與 process start time 不符合後清理，不能僅用 PID，因為 Windows 會重用 PID。

## 11. GC 規則

第一版 GC 手動執行且預設 dry-run。Keep-set 包含所有 App 的：

- `current`；
- `previous`；
- `pending`；
- `candidate`；
- `last_known_good`；
- 有效 active leases；
- 管理員設定的保留版本。

刪除 runtime 前需再次取得 store GC lock 並重新掃描 keep-set，避免掃描後到刪除前有新版本開始使用。刪除順序先移除／改名 `.complete` 使其 fail closed，再刪內容。GC 不可碰 app data。

## 12. 建議模組切分

```text
src/provision_builder/streamlit_desktop/
├─ state.py             # State schema、StateStore、原子替換
├─ identifiers.py       # app/version/fingerprint 驗證
├─ locks.py             # app lock、runtime lock、stale owner 判定
├─ leases.py            # active lease
├─ integrity.py         # files.json、SHA-256、.complete
├─ runtime_store.py     # fingerprint、resolve、verify、install
├─ updater.py           # check/stage/pending
├─ provider.py          # UpdateProvider protocol
├─ bootstrap.py         # promote、launch、health、rollback
├─ gc.py
└─ notifications.py
```

GUI、batch 與 provider 只能呼叫 service 層，不可各自重寫 state、hash、lock 或路徑規則。

## 13. 分階段實作工作單

### Phase 1：State 與不可變版本基礎

- 實作 identifier、state schema、StateStore 原子寫入。
- 實作 version manifest、files.json 與 `.complete` 驗證。
- 實作 per-app lock 與中斷／stale lock 測試。

驗收：在每個可注入的寫入中斷點後，舊或新 `state.json` 至少有一份完整有效，不出現半份 JSON。

### Phase 2：Runtime Store

- 定義 lock contract 與正規化 fingerprint。
- 實作 runtime staging、驗證、不可變安裝及 verification lock。
- Launcher 將所有 writable cache 導向 app data。

驗收：兩個版本相同 fingerprint 只保留一份 runtime；兩個程序並行首啟不會重複安裝或接受半份 runtime。

### Phase 3：Bootstrap、Promote 與 Rollback

- `start.bat` 只定位 root 並啟動版本外 bootstrap。
- 實作 pending promote、lease、current 啟動、health check、candidate commit 與失敗 rollback。
- 沿用既有 Streamlit launcher、engine shim、動態 port 與 Tauri 流程。

驗收：v1 → pending v2 → 重啟切 v2；v2 health 失敗時自動回 v1，沒有殘留 process。

### Phase 4：背景更新與通知

- 實作 UpdateProvider protocol 與第一個 provider。
- App ready 後背景檢查一次。
- 只在 fingerprint 缺少時下載 runtime。
- 完整 stage 後更新 pending 並通知 User。

驗收：requirements 不變時網路傳輸與磁碟新增內容不包含 runtime；中斷下載不會產生 pending。

### Phase 5：GC、GUI 與 E2E

- 實作 dry-run GC、keep-set 與 active lease。
- 管理 GUI 顯示 PROD/PREV/NEXT、下載進度、runtime 命中與更新結果。
- 執行真實 Streamlit + 真實 Tauri + 斷網／占 port／中文路徑 E2E。

## 14. 最低測試要求

### 14.1 State 與安全

- state 缺少、JSON 損壞、schema 不符、generation 不合法；
- version identifier 路徑逃逸；
- tmp 寫入、flush、replace 前後各中斷點；
- 兩個 updater 同時修改 state；
- stale lock、PID reuse 防護；
- manifest 宣告檔、額外檔、缺檔、hash 不符；
- artifact 解壓 traversal、絕對路徑、reparse point 拒絕。

### 14.2 Runtime

- 相同 lock 得到相同 fingerprint；
- lock 不同得到不同 fingerprint；
- 本機已有 fingerprint 時完全不下載 runtime；
- runtime 半份、hash 錯、缺 `.complete` 必須 fail closed；
- 並行 verification 只有一個 writer；
- 執行後 runtime 內容與 hash 不變。

### 14.3 更新生命週期

- 無 pending 時啟動 current；
- pending 完整時 promote；
- pending 損壞時不影響 current；
- candidate health 成功後更新 LKG；
- candidate health 失敗自動 rollback；
- App 執行期間完成 stage，不替換本次正在執行的固定版本；
- 通知失敗不回滾已完成 pending；
- 下載中斷後重啟可安全清理或續傳，不會誤 promote。

### 14.4 真實 Windows E2E

1. 安裝 v1，啟動並顯示版本 v1。
2. App 執行期間提供 v2（相同 runtime），只下載 code，通知 User。
3. 關閉重啟，顯示 v2；PREV/LKG 狀態正確。
4. 提供會 health check 失敗的 v3，重啟後自動回 v2。
5. 提供依賴改變的 v4，只新增一份新 runtime。
6. PROD 與 PREV 分別引用不同 runtime 時，GC 兩份皆保留。
7. 有效 lease 引用但 state 不再引用的 runtime 不被 GC。
8. 路徑包含空白與中文字仍可更新與啟動。
9. 8501 被占用仍能啟動。
10. 更新完成後斷網，pending promote 與啟動仍成功。
11. **目標機 spike**（上線前，非開發機）：enforced WDAC 下自 `deps\runtimes\<fp>\` 載入 python.exe/DLL 可執行；`os.replace` 檔案原子替換行為一致；首啟 457MB 深度驗證的實際耗時（HDD 情境）可接受。任一失敗 → 降級回現行全量 fat 包（schema v2 launcher 向下相容）。

## 15. 完成定義

- User 執行期間只背景下載及驗證，不替換正在執行的版本。
- 下載完成後顯示「下次啟動套用」通知。
- 下次啟動在 App process 建立前完成 pending promote。
- 任一時間只透過完整 `state.json` 決定 CURRENT/PREV/PENDING。
- 版本目錄與 runtime 不做原地修改或 rename 輪轉。
- requirements 不變時只更新程式碼，不下載或複製 runtime。
- requirements 改變時建立新 runtime，舊版仍可立即 rollback。
- 新版啟動失敗能自動回到 LKG。
- Crash、斷電或中斷下載不會讓 current 指向半份版本。
- 多程序首次驗證與更新不會互相破壞。
- GC 不刪除任何 state 或 active lease 仍引用的版本/runtime。
- focused tests、相關 regression 與真實 Windows E2E 全部通過。

## 16. 後續 AI 禁止自行簡化的事項

1. 不得把 `state.json` 改回三個分離指標檔。
2. 不得用三個實體資料夾 rename 模擬交易。
3. 不得在完整驗證前寫入 pending 或 `.complete`。
4. 不得在 requirements 相同時仍為每個版本複製完整 runtime。
5. 不得修改既有 fingerprint runtime 的內容。
6. 不得只用 PID 判斷 lease owner。
7. 不得讓 GC 忽略執行中的 lease。
8. 不得在 User 使用期間強制關閉 App 或 promote。
9. 不得只 mock subprocess/socket 就宣稱 E2E 完成。
10. 遇到現有 launcher、shim 或 Tauri 契約衝突時，先記錄證據並回報，不可偷偷退化成固定 port 或系統瀏覽器。

## 17. 實作紀錄：測試與真實 E2E 抓到並修正的缺陷（2026-07-12）

寫下來的原因：每一個都是「單元測試層面看似正確、真實環境才會炸」的類型，
後續改動時不要把這些修復當成可簡化的贅肉。

1. **鎖檔的「空檔案窗口」**（並行測試抓到）：`O_EXCL` 建檔與寫入 owner metadata 之間，
   另一個等待者讀到空 body → 誤判 stale → 偷走活鎖。修法：鎖的宣告改為
   「tmp 檔先寫滿 → `os.link` 硬連結到鎖路徑」——宣告是原子的，body 永遠完整
   （hardlink 免特權已實測）。`locks.py`。
2. **release() 吃 sharing violation 就放棄**（並行測試抓到）：Windows 上等待者
   正 open 著鎖檔讀 metadata 時，持有者的 `os.remove` 會拿到 WinError 5；
   靜默放棄 = 留下「owner 還活著」的孤兒鎖，永遠解不開。修法：retry 迴圈。
3. **bootstrap 自己污染不可變 runtime**（真實 E2E 首啟深驗抓到）：bootstrap 在
   共用 runtime 下執行,import stdlib 時 Python 把 `.pyc` 寫進 `Lib/__pycache__`
   → 逐檔驗證發現未宣告檔案 → fail closed。修法：`sys.dont_write_bytecode = True`
   於 bootstrap/gc 首行 + start.bat 設 `PYTHONDONTWRITEBYTECODE=1`。
   （驗證機制行為完全正確——抓到的是我們自己。）
4. **revision 在回滾路徑上遺失**（真實 E2E 抓到）：壞版 promote → 崩潰 → rollback
   時，`failed_versions` 記到 `revision: None`，背景 updater 下一輪比對不上
   release.json 的 revision → **把同一個壞版再 stage 回來**，形成更新-崩潰迴圈。
   修法：revision 隨 `pending_revision → candidate_revision → failed_versions` 全程攜帶；
   `is_failed` 對 revision 未知的舊記錄採保守解釋（擋下所有 revision，管理員可用
   `--set-pending` 強制重試）。
5. **8501 埠上的孤兒程序讓負向測試假失敗**（全套回歸抓到）：測試 fixture 不可
   假設 8501 可用——任何真的在 8501 上的 Streamlit 會回應健康檢查,把
   「永不健康」測試變成 DID NOT RAISE。fixture 改用 OS 配發的埠。

### 17.1 打包三個真實專案(CV_Viewer / AI4BI / ANnoTation)後又修掉的九個缺陷

「拿真的專案來打」暴露的問題,比任何 fixture 都多。全部有回歸測試釘住:

1. **入口偵測只看根目錄** — CV_Viewer 的入口在 `5_PG_Develop/app.py`、AI4BI 在 `ai4bi/ui/app.py`,
   兩個都被回報成「這個資料夾裡沒有 Streamlit 檔案」。改為遞迴(≤3 層)+ 排除
   `spike`/`tests`/`.venv` + 只認「會算繪畫面」的檔案;多個候選就明講要人選,不擲骰子。
2. **launcher 從錯誤的目錄啟動 App** — cwd 原本設在「入口檔所在資料夾」,但 Streamlit 專案的
   慣例是**從專案根目錄跑**(`streamlit run ai4bi/ui/app.py`)——那正是把根目錄放進 sys.path 的
   原因。AI4BI 的 `from ai4bi.analysis...` 因此必炸。改為 cwd = `application/` 並顯式設
   `PYTHONPATH`。**這是架構 bug,不是專案的錯。**
3. **pip 讀不了含中文註解的 requirements.txt** — pip 用系統 locale(cp950)解 UTF-8 檔,
   `UnicodeDecodeError`,一個套件都還沒下載就死。**任何中文註解的專案都會中招。**
   修法:pip 跑在 `PYTHONUTF8=1`,不改使用者的檔案。
4. **專案的 `wheels/` 被打包進去** — CV_Viewer 的離線 wheel 快取 124 MB 全進了交付包,
   而 User 端一個 `.whl` 都不會讀。排除 `*.whl`/`*.pyc`/`*.egg-info`。
   (fat builder 先修、store_builder 漏掉 → 版本槽 152MB,**量出來才發現**。)
5. **`pip freeze --all` 產的 lock 會炸掉 pip** — 可攜 runtime 的 pip 是本地 wheel 裝的,freeze
   吐出 `pip @ file:///D:/a/python-build-standalone/...`(建置機的路徑,沒人的磁碟上有)。
   store 模式先修(normalize_lock 忽略),**fat 模式漏掉** → pip OSError。
   現在 builder 一律先產一份「去掉 pip/setuptools/wheel」的乾淨副本給 pip(不動使用者的檔案)。
6. **只支援 requirements.txt** — AI4BI 只有 `pyproject.toml`。新增 `requirements.py`:
   解析順序 `requirements.lock.txt` → `requirements.txt` → `pyproject.toml [project].dependencies`。
7. **固定的 8501 預設埠** — 跟機器上每一個 Streamlit 搶(開發時真的被一個孤兒 Streamlit 咬到)。
   改為 `preferred_port = 0` = 在 **8000–9000 隨機挑,每個候選都先 bind 測試**,最多 20 次。
8. **Tauri 殼被複製進每一個版本** — 16.6 MB × N,而且位元組完全相同,佔 CV_Viewer 版本槽的 60%。
   移進 `deps/shells/<內容雜湊>/`,跟 runtime 一樣共用;GC 保護仍被引用的殼;`export_update` 會
   在目標機缺該殼時一併帶過去。CV_Viewer 版本槽 28 → 11.2 MB;AI4BI 104 → 3.0 MB。
9. **「輸出位置」在兩種模式下意思不同卻沒講** — fat=放包的資料夾;store=整棵樹的根。
   使用者把 store 樹建到裝著 fat 包的資料夾裡,再建同版本號就撞上「版本目錄不可變」。
   現在切換模式會換預設輸出並即時說明,「版本已存在」的錯誤也直接給出下一步(建議的新版本號)。

另外,builder 現在**主動報告大小組成**(runtime/application/shell + 最大的相依 + 專案裡 >10MB 的
大檔警告)。起因:AI4BI 的專案根目錄有一個 **84.8 MB 的螢幕錄影**,而它的程式碼只有 3 MB——
使用者不該需要來問「為什麼這麼大」。

### 17.2 情境評分(multi-agent)抓到的十二個缺陷 — 2026-07-13

10 個真實使用情境、每個一位獨立評分者(全部實際讀碼),再由一位 critic 逐條複驗。
初評分數:S1 36 / S2 45 / S3 28 / S4 32 / S5 58 / S6 30 / S7 50 / S8 48 / S9 25 / S10 40。
**critic 複驗後,沒有任何一條扣分是誤報。** 修正如下:

**會做出壞交付物的(blocker)**
1. **壞版會被晉升成 last-known-good** — `/_stcore/health` 是 Streamlit **伺服器**回的,
   App 腳本 `import cv2` 失敗照樣回 200。healthy marker 又寫在**殼啟動之前**,所以
   「缺 WebView2 → 殼一秒就死」也算健康。**連退路都被污染。**
   修:(a) 建置端 AST **可達性分析**(從入口檔 transitive closure,不是掃全 repo)比對
   runtime 實際裝了什麼,缺套件直接**讓建置失敗**;(b) launcher 先 render 一次頁面、掃 App
   自己的 log(ModuleNotFoundError/Traceback),再等殼活過視窗建立期,**才**寫 marker。
2. **`export_update` 匯出的 runtime 在目標機必定驗證失敗** — 它濾掉 `__pycache__`/`*.pyc`,
   但 files.json 宣告了那 4,039 個(AI4BI 7,221 個).pyc。錯誤訊息還叫人「重新複製」——複製一百次也一樣。
   修:pip 加 `--no-compile`(runtime 本來就以 `PYTHONDONTWRITEBYTECODE` 執行,.pyc 是純負擔),
   匯出只剝 `.complete`,不再過濾任何 files.json 宣告過的東西。
3. **「檢查專案」會把檔案寫進使用者的 repo** — `resolve()` 沒有 staging 時把
   `requirements.from-pyproject.txt` 寫進專案根目錄(AI4BI 裡真的長出來了)。唯讀的動作不該有副作用。

**會讓使用者做錯事的(major)**
4. **完成對話框說謊** — 說「User 下次啟動自動套用」(那個 pending 只寫在建置機這棵樹上)、
   說「雙擊 start.bat」(第二個 App 進來時那個檔案已被刪除)。
   修:`StoreBuildResult` 帶回真相(pending_set / entry_bats / removed_start_bat / added_mb),
   對話框據實陳述,並在 start.bat 被移除時改用 showwarning。
5. **GUI 沒有交付與維護的入口** — `export_update()` 只有 API、`update_source` 從未被寫過、
   GC 只能手打含 sha 指紋的長路徑。修:加「匯出交付／更新包…」(首次部署 / 增量更新)、
   更新來源欄位、「回收未使用的版本／runtime(先試算)」。
6. **教學叫人跑一個不存在的檔案** — `deps\tools\gc.bat` builder 從來沒產生過。
   修:產生 `tools\gc.bat` 與 `tools\admin.bat`(選單式:狀態 / 退版 / 回收)。
7. **現場沒有自救的牌** — bootstrap 只有 `--set-pending`。修:加 `--status`(一頁可念給電話另一頭)、
   `--rollback`(不必等它啟動失敗)、`--clear-failed`(修好的版本可以再試)。
8. **fail-fast 閘門是裝飾品** — 「檢查專案」不看 Store 規則,所以「必須釘死的 lock」要等建置炸了才知道。
   修:`validate_store_request()`;預估大小改成**真的掃專案**並回答「runtime 會重用嗎」(+0 MB vs +700 MB)。
9. **警告來得太晚** — 85MB 錄影的警告在複製 600MB **之後**才出現。修:`scan_project()` 在「檢查專案」階段就報。

**會讓人困惑或無助的(minor)**
10. **pip 失敗時 log 被自己刪掉** — 錯誤訊息指向一個不存在的路徑。修:先搶救 log 到輸出目錄,
    GUI 提供「要現在打開嗎」。同時 pip 輸出**串流**到紀錄區(原本 5–10 分鐘毫無動靜,像當機),
    並加上會誠實說明自己能做什麼的「取消」鈕。
11. **`.title()` 把 CV_Viewer 變成「Cv Viewer」** — 這個字串會進 manifest、README、視窗標題、
    產線看到的下拉。修:保留已含大寫的 token。
12. **交付根目錄沒有一個字告訴 User 要按 Start** — 修:產生「讀我-使用說明.txt」;start.bat 加
    中文標題、`chcp 65001`、改用 `pushd`(`cd /d` 對 UNC 路徑會靜默失敗然後印出**錯的**診斷)。
    另外:FAT/exFAT(USB)上 `os.link` 失敗時改印「請先複製到本機 NTFS」而不是 errno 1。

---

## §17.3 第二輪情境評分:當「功能存在」開始騙人

第二輪由 10 個獨立 agent 對修好的程式重跑同樣 10 個情境,外加一個 critic **逐條讀碼複驗**。

| 情境 | R1 | R2 | 為什麼分數更低 |
|---|---|---|---|
| S1 首次打包(happy path) | 36 | **55** | 自動偵測、pip 串流、搶救 log 都真的修好了 |
| S2 發新版給 12 台機器 | 45 | **28** | R1 是「沒有匯出按鈕」;R2 是「有按鈕,照著做必定失敗」 |
| S3 壞版的擋/判/退 | 28 | **15** | 閘門在它唯一該生效的情境下 100% 被跳過 |
| S4 離線工廠機首次部署 | 32 | **25** | 「首次部署」匯出物根本不可執行 |
| S5 站錯分頁的救援 | 58 | **38** | 對站**對**分頁的人斷言「這不是 CIM 模組」 |
| S6 AI4BI 先 fat 再 Store | 30 | **23** | import 閘門誤判 lazy import → fat 建置在 pip 之後硬失敗 |
| S7 209MB 專案的肥肉 | 50 | **43** | `StoreBuildResult.warnings` 是死碼 |
| S8 第二個 App 共用 runtime | 48 | **30** | 中文應用名 → `UnicodeEncodeError`;admin.bat 退錯 App |
| S9 現場 IT 回收磁碟 | 25 | **22** | `gc.py` 的 `⚠` 在 cp950 崩潰,且印在刪除**之前** → 零回收 |
| S10 產線 User 雙擊 | 40 | **38** | 環境失敗(缺 WebView2)被誤判成版本失敗 + 謊報回退 |

**critic 的 `hallucinated_defects` = 空陣列**:沒有任何一條扣分是程式其實已經處理好的。

分數下降不是退步,是**扣分的性質變了**。R1 扣的是「功能不存在」——管理員知道自己卡住。
R2 扣的是「功能存在、對話框言之鑿鑿、照做必定失敗」——把「知道自己做不到」變成「以為自己做到了」。
對散在產線上的 12 台機器,後者傷害更大。

### 這一輪修掉的根因

1. **「首次部署」匯出的包不可執行** — 匯出只複製 `apps/<app>/versions/<ver>/` + `deps/`,
   沒有 `bootstrap/`、沒有 `start.bat`、沒有 `state/`。修:`export_full_tree()` 匯出整棵樹
   (排除 `apps/*/data/` 這種建置機的 log 與 lease),`export_update()` 退回它真正的職責。
   兩個按鈕、兩件事——上一版把它們塞進同一個「是/否」對話框,而「否」那條路做出來的東西裝不起來。

2. **增量包在目標機無法套用,而且產品裡沒有指令能解** — 匯出剝掉 `.complete`(正確:sentinel 必須在
   目標機**賺**到,不能從 USB 信任過來),但 `--set-pending` 的 deep verify 又硬性要求它。
   runtime 與 shell 早就會自己 `write_complete()`,只有版本目錄沒學會。修:`bootstrap --install <包>`
   ——複製 → 逐檔驗證 `files.json` → **驗過才自己寫 sentinel** → set_pending,一個指令做完。

3. **healthy marker 建立在一個不存在的前提上** — `first_render_error()` 用 `GET /` 想「觸發第一次
   render」,但 `/` 只回靜態 index.html;Streamlit 要等 websocket session 建立才會執行腳本,
   而那要等 User 在 portal 按 Start。所以那 20 秒等的是一份**永遠不會有錯誤的 log**。
   修:launcher 在 spawn 任何東西**之前**,用 App 自己的 runtime 做一次 in-process 的
   import 閉包檢查(AST 可達性 + `find_spec`);殼活過視窗建立期才寫 marker;殼結束時再掃一次
   App 的 log,若 session 中真的炸了就**撤銷** marker——否則壞版會被晉升成 last-known-good,
   污染的正是我們要退回去的那一版。

4. **import 閘門同時漏判與誤判** — 漏判:它寫在 `ensure_runtime()` 的 early-return **之後**,
   所以「lock 沒變」時 100% 跳過(而那正是它唯一該生效的情境)。誤判:`ast.walk` 把
   **函式內的 lazy import** 當成必需,AI4BI 的 `import anthropic`(選用 LLM 後端)於是讓
   fat 建置在 6 分鐘的 pip 之後硬失敗。修:模組層級 import 才是必需,函式內與
   `try/except ImportError` 內一律視為選用;閘門移到 `validate` 階段(0 秒就擋)。

5. **exit code 說謊** — 任何非零 exit 都被當成「這個版本壞了」→ 寫進 `failed_versions` → 通知
   「已恢復前一版本」。但殼與 WebView2 是**所有版本共用**的:缺 WebView2 時退回舊版一樣開不起來。
   修:3 = App 壞了、4 = 版本樹壞了、5 = **這台機器**壞了(不碰 state、不謊報回退、直接告訴他去裝 WebView2)。

6. **取消鈕是假的** — `_sd_cancelled = True` 是整個 repo 對這個旗標的**唯一一次出現**,沒有讀取端;
   pip 照跑到底,最後跳「建立完成」。關視窗時的「會取消目前工作」取消的是另一條沒在跑的流程,
   pip 變成孤兒繼續往 600MB 的 `.staging-*` 寫檔,而且沒有任何機制清理它。
   修:`should_cancel` 貫穿 build/store build,`taskkill /T /F` 殺整棵 pip 樹,清 staging,
   回傳 `cancelled=True`,GUI 走「已取消」分支。

7. **cp950** — `gc.py` 的 `⚠`(U+26A0)在繁中主控台 `print()` 直接崩潰,而 `log(plan.summary())`
   排在刪除迴圈**之前** → 唯一被文件指名的回收入口,永遠回收 0 MB。`admin.bat` 的回收項
   從不帶 `--apply`。中文應用名 + `encoding="ascii"` → `UnicodeEncodeError`(不是 `OSError`,
   躲過攔截清單)→ 版本已經建好了卻回報失敗。

---

## §17.4 第四輪:當「修好的東西」自己變成新的謊

R3 → R4 分數:S1 97→72、S2 50→32、S3 —→35、S4 —→38、S5 47→42、S6 30→23、S7 55→62、
S8 30→26、S9 54→50、S10 52→65。**連續 >90 = 0**;critic 逐條讀碼複驗 58 個扣分點,**零幻覺**。

分數不是單調上升的。原因很清楚:**這一輪扣的分,有一半是前幾輪的修正自己製造出來的**。

### 我修出來的三個新缺陷

1. **`_revoke_marker()` 是安慰劑**(S3 blocker)
   我在 R2 發現「壞版會被晉升成 last-known-good」,於是讓 launcher 在 App 中途炸掉時
   **撤銷** healthy marker。但 `bootstrap.py` 在 **marker 一出現**就呼叫 `commit_candidate()`
   —— LKG 早就 latch 進去了,我刪的是一個沒有人會再讀的檔案。
   更糟:`commit_candidate()` 把 `candidate` 清成 None,於是結束後的
   `if refreshed.candidate != version` 恆真,**整段回滾邏輯被跳過**。壞版既沒被標記失敗、
   也沒被退掉,而且現在是 last-known-good。明天早上再演一次。
   **教訓:修在症狀上(撤銷 marker)而不是修在 latch 上,等於沒修。**

2. **正常關窗被誣告成「這台電腦壞了」**(S10 major)
   我寫的 12 秒存活窗只看 `proc.poll() is not None`,**`proc.returncode` 抓了卻從來不讀**。
   User 開啟 App、看一眼、12 秒內關掉 → 判定 exit 5「應用視窗一開就關閉了,請安裝 WebView2」。

3. **一條排除樣式排掉整個專案**(R3 抓到)+ **`dist` 按名字不分深度**(S6 blocker)
   `EXCLUDED_DIRS` 含 `"dist"`,比對用的是 bare name。AI4BI 的自訂 Streamlit 元件
   `ai4bi/ui/components/field_well/frontend/dist/` 就這樣被靜靜砍掉 —— 那個目錄**就是元件本身**。
   建置成功,交付出去的 App 在該顯示元件的地方顯示一個空白框。

### 三條跨情境的根因(修一次、多處得分)

| 根因 | 涉及情境 | 帳面點數 |
|---|---|---|
| **A. WebView2 供應鏈**:全 repo **沒有任何程式碼會產生 `prereq/`**,但 start.bat 與讀我都叫人去執行需要它的 `tools\安裝WebView2.bat` | S4 blocker、S1 major、S10 major | 66 |
| **B. healthy marker 的 commit 時機** | S3 blocker、S10 major | 59 |
| **C. 匯出的「真相層」**:送錯版本(`current` 是產線已經在跑的那一版)、送出建置機的 `state.json`(含 pending!)、送出所有歷史版本、對話框叫人雙擊一個多 App 樹裡已被刪除的 `start.bat` | S2 blocker、S4 major、S8 blocker | 107 |

「離線工廠機」是這個產品存在的理由,而**唯一需要預裝的東西(WebView2)的離線安裝檔,
從來沒有任何一行程式碼會把它放進交付包**。自救路徑是一條死路,而我們還在讀我裡指著它。
