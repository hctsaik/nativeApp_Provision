# CV Reviewer GUI 化發布與自動更新規畫

> 狀態：第一版架構與 MVP 規格  
> 適用範圍：`cv_reviewer` 先行導入，驗證後推廣到其它 Native_App 應用。  
> 相關文件：[平台工具的開發、離線相依與多機部署架構](TOOL_DEVELOPMENT_AND_DISTRIBUTION.md)、[離線部署操作](OFFLINE_DEPLOY.md)。  
> 可執行的本機 Registry／Object Storage Lab 請參考 [LOCAL_CONTROL_PLANE_LAB.md](LOCAL_CONTROL_PLANE_LAB.md)。

## 1. 目的

讓發布人員與一般使用者不需要操作 Python CLI，即可完成 `cv_reviewer` 的建置、驗證、發布、更新與回復：

- 發布人員在 `native_Provision` GUI 選擇應用、建立版本、驗證並發布到 MinIO。
- Native_App 每次啟動應用前檢查 Registry；只有版本改變才下載。
- 新版下載到本機暫存區，通過 SHA-256、相容性、warmup 與 healthcheck 後才原子切換。
- DB 或 MinIO 無法連線、下載損毀或新版啟動失敗時，繼續使用最後成功版本。
- 管理者可從 GUI 查看版本、重試更新及回復上一版。
- 完全離線環境仍可使用現有 provision 資料夾與 `apply.py`，不依賴 MinIO。

核心原則：

> DB 管理發布資訊，MinIO 保存不可變套件，本機 cache 才是執行來源。

## 2. 專案責任

| 專案 | 責任 | 不負責 |
|------|------|--------|
| `Native_App` | 檢查更新、顯示進度、本機 cache、啟用版本、啟動與 rollback | 建置 wheel、解析 pip dependency graph |
| `native_Provision` | 掃描、建置 source/dep-pack、驗證、發布 MinIO、登記 Registry | 執行應用、保存使用者資料 |
| `cv_reviewer` | 程式碼、資源、版本、requirements、healthcheck、migration | MinIO 存取、平台安裝目錄操作 |

## 3. 整體流程

```text
cv_reviewer repo
      │
      ▼
native_Provision GUI
  掃描 → 檢查版本 → 建置 → 離線驗證 → 發布
      │                                  │
      │                                  ├─ package artifact → MinIO
      │                                  └─ release metadata → Registry DB
      ▼
Native_App
  啟動前查 Registry → 有新版才下載 → verify → warmup → healthcheck
      │
      ├─ 成功：原子切換 active version → 啟動新版
      └─ 失敗：保留／回復 last-known-good → 啟動舊版
```

## 4. 應用宣告

`cv_reviewer` repo 新增 `app.yaml`，描述發布與生命週期；既有 `plugin.yaml` 繼續描述 Native_App runtime 載入契約。

```yaml
schema_version: 1

app:
  id: cv-reviewer
  name: CV Reviewer
  version: 1.0.0
  category: app

compatibility:
  platform_api: ">=1.0,<2.0"
  os: windows
  arch: amd64
  python: "3.11"
  abi: cp311

source:
  root: src
  entrypoint: cv_reviewer.main:run

dependencies:
  python:
    - opencv-python==4.11.0.86
    - numpy>=1.26,<2

lifecycle:
  healthcheck: cv_reviewer.health:check
  startup_timeout_seconds: 60

data:
  schema_version: 1
  directories: [config, projects, cache, logs]
```

建置前必須檢查 `app.yaml` 與 `plugin.yaml` 的 `id`、`version`、entrypoint 與 requirements。一致性失敗時 GUI 禁止發布，並指出欄位與兩邊的值。

## 5. 發布套件

每個 `app_id + version` 是不可變發布單位；內容有任何變動都必須增加版本。

```text
cv-reviewer-1.0.0.napp
├─ package.json
├─ application/
├─ dependency/
│  ├─ deppack.json
│  └─ wheels/
├─ big-deps/
├─ migrations/
└─ checksums.json
```

第一版 `.napp` 可使用 ZIP 格式。`package.json` 至少記錄：

- app ID、名稱、版本與 category。
- Git commit、建置時間與 builder version。
- Native_App API、Windows、CPU、Python 與 ABI 相容性。
- entrypoint、healthcheck 與 data schema version。
- source SHA-256 與 dependency fingerprint。
- dep-pack、big-deps 及所有檔案的 SHA-256。

現有 `core.deppack`、big-deps、source package、`apply.py` 與 `warmup.py` 繼續重用；新格式只把它們綁成一個可版本管理的 application release。

## 6. MinIO 與 Registry

### 6.1 MinIO

```text
native-apps/
└─ cv-reviewer/
   ├─ 1.0.0/
   │  ├─ cv-reviewer-1.0.0.napp
   │  └─ package.json
   └─ 1.1.0/
      ├─ cv-reviewer-1.1.0.napp
      └─ package.json
```

規則：

- 禁止覆蓋已發布版本。
- 先上傳 staging object，遠端驗證完成後才建立 release record。
- Registry channel 更新是最後一步；未完成的上傳不會被 Native_App 看見。
- DB 不保存 Python 原始碼或 package binary。

### 6.2 Registry DB

MVP 使用三個概念表：

| 表 | 主要欄位 |
|----|----------|
| `applications` | app_id、display_name、enabled、default_channel |
| `application_releases` | app_id、version、object_key、sha256、size、相容性、dependency fingerprint、status |
| `application_channels` | app_id、channel、version、updated_at、updated_by |

支援 `development`、`staging`、`production` 三個 channel。Promotion 只改 channel 指標，不複製或修改 artifact。

## 7. 發布端 GUI：native_Provision

沿用目前 `provision_gui.py` 與 `gui_backend`，改為分頁或步驟式流程。

### 7.1 「建置」頁

1. 選擇應用 repo。
2. 選擇 Native_App 專案。
3. 自動讀取 `app.yaml`／`plugin.yaml`。
4. 顯示 app ID、目前版本、Git commit、requires、目標 ABI。
5. 顯示錯誤與警告；有 blocking error 時停用「開始建置」。
6. 按「開始建置」後顯示每階段進度：
   - Source Package
   - Dependency Pack
   - big-deps
   - SHA-256
   - offline selfcheck
   - package assembly

### 7.2 「驗證」頁

沿用目前 Apply → Warmup → Tauri E2E：

- 使用隔離的 cache、venv、logs 與 WebView2 profile。
- 要求 healthcheck 成功。
- app 類要求 iframe 有實質內容且無 traceback。
- 要求 engine log 出現 `Per-tool deps ready`。
- 顯示 PASS／FAIL、耗時、log 與截圖位置。
- 未通過驗證的 package 預設禁止發布；管理員 override 留待後續版本。

### 7.3 「發布」頁

顯示：

- App／版本／Git commit。
- Package 大小與 SHA-256。
- Source 與 dependency 是否改變。
- 目標 channel。
- MinIO bucket/object key。
- 最近一次驗證結果。

使用者按「發布」後，GUI 依序顯示：

```text
本機驗證 → 上傳暫存物件 → 遠端校驗 → 建立 release → 更新 channel
```

任一步驟失敗都應保留可行動訊息；不得留下 Registry 指向不完整 artifact。

### 7.4 「版本」頁

- 列出 development／staging／production 目前版本。
- 列出 release 歷史、發布人與時間。
- 支援 staging → production promotion。
- 支援 production 指回上一版。
- rollback 前顯示影響範圍並二次確認。

CLI 仍保留供 CI 使用，GUI 必須呼叫相同 application service，不複製 build／publish 邏輯。

## 8. 使用端 GUI：Native_App

### 8.1 一般使用者流程

使用者點擊 CV Reviewer 時：

1. 顯示「正在檢查更新」。
2. 無新版時直接啟動本機 active version。
3. 有新版時顯示下載、驗證、準備環境及啟動進度。
4. 成功時顯示新版本並開啟應用。
5. 更新失敗時顯示「新版更新失敗，已使用上一個可用版本」。

長時間下載時提供「使用目前版本」；選擇後可取消本次前景等待，但不得破壞背景暫存資料或 active version。

### 8.2 管理頁

每個應用顯示：

- Active version、last-known-good version。
- production 最新版本。
- 上次檢查、下載、驗證與啟動結果。
- 本機 source、venv 與 cache 大小。
- 「立即檢查」、「更新」、「重試」、「回復上一版」、「開啟 log」。

第一版不向一般使用者提供 channel 切換；channel 由裝置或管理員設定。

### 8.3 本機狀態

```text
data/apps/cv-reviewer/
├─ active.json
├─ package-state.json
├─ versions/
│  ├─ 1.0.0/
│  └─ 1.1.0/
├─ venvs/
│  └─ <dependency-fingerprint>/
├─ downloads/
└─ data/
   ├─ config/
   ├─ projects/
   ├─ cache/
   └─ logs/
```

程式版本與使用者資料分離。更新、rollback 或移除舊版不得刪除 `data/config` 與 `data/projects`。

## 9. 啟動前更新狀態機

```text
IDLE
  → CHECKING
      ├─ Registry unavailable → START_CACHED
      ├─ same version         → START_ACTIVE
      └─ update found         → DOWNLOADING
                                  → VERIFYING
                                  → INSTALLING_DEPS（需要時）
                                  → WARMING_UP（需要時）
                                  → HEALTHCHECK
                                      ├─ success → ACTIVATING → START_NEW
                                      └─ failure → ROLLBACK → START_CACHED
```

重要規則：

- 每次啟動都可檢查，但版本相同不重新下載。
- 所有下載先進 staging，不直接覆蓋 active version。
- source 未變且 dependency fingerprint 相同時直接沿用本機 venv。
- 新版只有在 verify、warmup、healthcheck 全部成功後才可成為 active。
- 同一個壞版本在本機標為 failed，避免每次啟動無限重試。
- DB／MinIO 中斷不影響已安裝版本啟動。
- 本機從未成功安裝且遠端不可用時才阻止啟動。

## 10. MVP 分期

### Phase 1：CV Reviewer 程式碼更新

- `app.yaml` 與 package manifest。
- GUI 建置、驗證與發布。
- MinIO immutable artifact。
- Registry release 與 production pointer。
- Native_App 啟動前檢查、下載、SHA-256、原子切換。
- 離線 fallback、保留上一版、管理頁手動 rollback。
- 暫時禁止 requirements 與 data schema 改變。

### Phase 2：依賴更新

- dep-pack 隨 release 發布。
- dependency fingerprint venv cache。
- GUI 顯示 apply／warmup 進度。
- import check 與 healthcheck。

### Phase 3：多應用

- application scaffold。
- 通用建置／發布 GUI。
- 多應用 package DB、磁碟用量與垃圾回收。
- staging／production promotion 與裝置 channel policy。

### Phase 4：治理

- 發布者簽章與 trusted publisher。
- RBAC、發布審批與 audit log。
- 分批 rollout、裝置群組、成功率與自動停止 rollout。

## 11. Phase 1 驗收條件

1. 發布人員只透過 GUI 即可掃描、建置、E2E 驗證並發布 `cv_reviewer`。
2. 相同版本重啟不下載 package。
3. 發布新版本後，Native_App 下一次啟動 CV Reviewer 能下載並切換。
4. 更新過程中 active version 始終完整可用。
5. Package 改一個 byte 時 SHA-256 驗證失敗，舊版正常啟動。
6. Registry 或 MinIO 關閉時，已安裝版本正常啟動。
7. 新版 healthcheck 失敗時自動 rollback，且不在下次啟動無限重試。
8. GUI 能查看 active、last-known-good、latest 與失敗原因。
9. 管理員能從 GUI 手動回復上一版。
10. 更新與 rollback 不改動 CV Reviewer 的 config 與 project data。
11. CLI 與 GUI 使用相同後端服務，CI 可無 GUI 完成相同 build／verify／publish。
12. 現有離線 provision build／apply／warmup 流程保持相容。

## 12. 本版不做

- 從 DB 逐檔讀取或直接執行 Python 原始碼。
- 直接在 MinIO／共享磁碟上執行應用。
- 每次啟動都重新下載相同版本。
- Phase 1 自動執行資料 schema migration。
- 一開始就做跨應用共用 venv。
- 在未驗證完成前覆蓋目前版本。

## 13. 實作順序

1. 確定 `cv_reviewer` 實際 repo 路徑、入口點、資料目錄及目前 `plugin.yaml`。
2. 定義並驗證 `app.yaml`、`package.json` schema。
3. 抽出 GUI／CLI 共用的 application build service。
4. 擴充 native_Provision GUI 的建置、驗證、發布與版本頁。
5. 建立 MinIO staging/promotion 與 Registry repository。
6. 在 Native_App 實作本機 package state 與原子 activation。
7. 加入啟動前 update state machine 與 GUI 進度。
8. 完成 Phase 1 故障注入與 E2E 驗收，再開放 production channel。
