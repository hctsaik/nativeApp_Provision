# CV Reviewer 更新平台 — 開發啟動規格

> 狀態：Ready for development  
> 日期：2026-07-11  
> 用途：接下來實作的單一入口；若其它 handoff 摘要與本文衝突，先停止該工作並以 ADR 定案，不可由開發者或 AI 靜默選擇。

GUI 產品邊界與遷移規格請同時遵守 [NATIVE_APP_GUI_INTEGRATION.md](NATIVE_APP_GUI_INTEGRATION.md)：裝置端管理整合進 Native App Management Center，獨立 Device Portal 僅作診斷，中央 Web Console 定位為 Fleet Console。

## 1. 開發目標

第一個成功情境：

```text
cv_reviewer 指定 commit
→ 建置不可變 application package
→ 上傳 artifact store
→ 建立 release
→ promote 到 production
→ Native_App 查詢 desired version
→ 只下載本機缺少的 source/blob
→ 驗證、準備 venv、healthcheck
→ 原子啟用新版
→ 啟動成功並晉升 last-known-good
```

任一步驟失敗時，目前 active version 與使用者資料不得被破壞；Registry 或 MinIO 無法使用時，已安裝版本仍須正常啟動。

## 2. 阻斷性前置

以下未確認前，不進入對應 Slice：

### P1：確認 cv_reviewer 真實 repo

開始 Slice 4 前直接向使用者確認：

- repo 絕對路徑與正式名稱。
- `plugin.yaml` 路徑。
- entrypoint 與 category。
- source root、assets、必須排除的本機資料。
- config、project、cache、logs 的實際資料目錄。
- requirements 與目前 data schema。

不可把 `CV_Viewer` 或工作區其它 CV 專案猜成 `cv_reviewer`。

### P2：WDAC 與建置能力

目前硬限制：WDAC 阻擋 Cargo，且尚無可重編 Native_App Rust／Tauri 殼的建置機。

因此：

- Local Agent 第一版必須是 Python sidecar／engine service，走既有殼可使用的 IPC 或啟動路徑。
- 不得把修改或重編 Rust 殼設為任何前期 Slice 的前置條件。
- Web frontend 開工前實測 Node/Vite/esbuild 是否可執行。
- 若本機 frontend build 受阻，選擇 server-rendered UI，或在可建置的 CI／Worker 產生靜態檔。
- Rust 殼解凍是外部里程碑，不阻塞 Control Plane、Worker、package contract 與 Python Agent。

### P3：正式服務測試環境

目前本機沒有 `docker` 指令。PostgreSQL／MinIO integration tests 必須支援環境變數指定外部 endpoint，可在 CI 或另一台機器執行；本機持續跑 SQLite／filesystem contract tests。

## 3. 文件權威

按範圍判定：

| 範圍 | 權威來源 |
|------|----------|
| 現有 dep-pack、big-deps、apply | `native_Provision/SPEC.md` 與實際程式 |
| WDAC、Tauri、Native_App 擴充限制 | Native_App 上游 handoff 與實際程式 |
| CV Reviewer 實際入口與資料模型 | 經使用者確認的 cv_reviewer repo |
| Application package／Registry／Agent | 本 handoff + 後續核准 ADR |

上游整體文件目前位於：

```text
C:\code\claude\CV_Viewer\PLATFORM_ARCHITECTURE_AND_DEPLOYMENT.md
```

開工前核對其文件鏈；如與本文衝突，新增 ADR 記錄取捨。

## 4. 已拍板架構

```text
Web Console
    │
Control Plane ───── PostgreSQL／Oracle-like Registry
    │                         │
    ├── Build Job ── Build Worker（重用 native_Provision）
    │                         │
    └────────────────────── MinIO / S3 immutable artifacts
                              │
                        Native_App Python Agent
                              │
                        local cache / venv / data
```

### 責任邊界

- DB 保存 application、release、channel、desired/observed state、audit；不保存 Python binary。
- MinIO 保存不可變 package 與 content-addressed blob；不決定 production 指向。
- Build Worker 執行 package、dep-pack、offline selfcheck、warmup、E2E、簽章。
- Native_App Python Agent 執行下載、驗證、安裝、healthcheck、activation、rollback。
- Web Console 負責中央發布與治理。
- Native_App Portal 顯示使用者更新進度與本機版本管理。
- Tkinter 只保留離線打包、bootstrap 與診斷。

## 5. Package 與 big-deps 定案

`.napp` 不內嵌大型 wheel 本體，只保存 source、小型 metadata 與 blob references：

```text
cv-reviewer-1.4.2.napp
├─ package.json
├─ application/
├─ dependency-manifest.json
├─ blob-references.json
├─ migrations/
├─ checksums.json
└─ signature.json
```

大型 wheel／模型：

```text
MinIO:    blobs/sha256/<hash>
裝置本機: blobs/sha256/<hash>
```

Agent 只下載本機缺少的 hash，然後以 hardlink 或 copy 組裝每工具 wheelhouse，再交給現有 `core.deppack` 驗證。只修改 source、dependency fingerprint 未變時，不重新下載 torch，也不重建 venv。

## 6. Source install 與 dependency apply

現有 `apply.py` 只處理 dependency cache，不會套用 `source-packages`。不可把它描述成完整 application installer。

長期拆分：

```text
Dependency Apply
└─ 重用 apply.py / core.deppack

Application Install
├─ 驗 source manifest
├─ 安裝到 versions/<version>.staging
└─ 不直接切換 active

Application Activate
├─ dependency/import check
├─ migration checkpoint
├─ healthcheck
├─ atomic active pointer
└─ observation → last-known-good
```

Slice 4 必須實作 source install/assemble；不得只組 `.napp` 後宣稱完整離線安裝已完成。

## 7. Release、channel 與版本語意

- Release identity 第一版使用複合鍵 `(app_id, version)`，API 不使用尚不存在的 `release_id`。
- Download URL：`POST /api/v1/artifacts/{app_id}/{version}/download-url`。
- 是否更新只比較 desired channel pointer 與本機 active identity 是否相等，不做 semver 大小排序。
- Rollback 可將 channel 指回舊版；Agent 不因版本字串變小而拒絕。
- Release 可進入 `yanked`，但不刪 immutable object。
- Yank 流程：標記 yanked、channel 指回已知良好版本、裝置停止新安裝。

## 8. Error taxonomy

Slice 1.5 先完成穩定錯誤分類：

```text
DuplicateVersion
ArtifactAlreadyExists
ArtifactMissing
ArtifactCorrupted
InvalidIdentifier
UnknownApplication
UnknownChannel
ReleaseNotPublished
ReleaseYanked
HashMismatch
RegistryUnavailable
ObjectStoreUnavailable
```

規則：

- Service 層產生 domain error。
- HTTP 層只映射 status code/error code，不重新判斷業務狀態。
- 發布前查 Registry 只改善錯誤體驗，真正唯一性仍由 DB constraint 與 immutable object 保證。
- Upload 成功、Registry 失敗可留下 unreferenced object；由安全 GC 清理，不發布半套 metadata。
- MinIO multipart ETag 不是 SHA-256；adapter 必須在 client 串流時計算 SHA-256 與 size。

## 9. Identifier 規則

`app_id`、`version`、`channel` 第一版統一限制：

```regex
^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$
```

同一規則必須由：

- Python domain validation
- `app.yaml`／`package.json` JSON Schema
- HTTP request validation
- Registry migration

共同遵守，不得各自發明。

## 10. Schema 單一權威

目前 SQLite 與 PostgreSQL schema 已有欄位差異。開始 Slice 3 前建立 logical schema／migration 規格，至少包含：

```text
applications
application_releases
application_channels
audit_events
```

SQLite、PostgreSQL、未來 Oracle adapter 都要通過相同 contract tests；各 DB 可以有不同 DDL，但領域欄位與約束語意必須一致。

Compose image 進 integration 階段時釘選明確版本或 digest，不使用 `minio/minio:latest`／`minio/mc:latest` 作為可重現測試基準。

## 11. Agent journal、斷電恢復與 LKG

Agent 的 `state.db` 是本機權威狀態，至少記錄：

```text
operation_id
app_id
from_version
to_version
current_step
previous_active
desired_identity
started_at
updated_at
last_error
```

啟動時執行 reconcile：

- 未驗證 staging：刪除或安全續作。
- verified 但未 activation：可從 journal 繼續。
- active 已切換、observation 未完成：重做 healthcheck；失敗切回 previous LKG。
- migration 狀態不明：fail closed，不猜測資料狀態。
- active pointer 不完整：由 SQLite journal 和 LKG 修復。

LKG 晉升第一版條件：

1. install/verify/warmup 成功。
2. pre-start healthcheck 成功。
3. 應用真實啟動成功。
4. observation window 內至少完成指定次數 post-start healthcheck，或穩定運行達設定時間。
5. 才更新 last-known-good。

同一失敗 release 記錄 failed 狀態，除非使用者明確重試、metadata 變更或出現新版本，否則每次啟動不重跑相同失敗流程。

## 12. 簽章 ADR 必須先於 package schema 定稿

現有內部 USB provision 仍遵守 `SPEC.md` D1，不強制簽章；透過 Control Plane／MinIO 自動下載執行的 production package 必須驗簽。

Slice 4 前先完成 ADR，至少決定：

- 演算法（優先評估 Ed25519）。
- canonical payload／manifest digest。
- `signature.json` schema。
- key ID、trusted public key 分發。
- key rotation、revoke 與離線 trust-store 更新。
- dev/staging 測試金鑰政策。
- Build Worker 如何存取 signing key。

ADR 定案前可以實作 hash 與 package prototype，但不得凍結 production package format。

## 13. 開發切片

### Slice 1：Local package domain（已完成）

SQLite Registry、filesystem object store、publish/promote/resolve/download、5 個聚焦測試。

### Slice 1.5：Domain hardening（下一步）

- Error taxonomy。
- Identifier validation。
- Duplicate publish 穩定行為。
- Yank 狀態模型。
- Logical schema 初稿。
- 補 orphan object 與 retry 測試。

驗收：任何相同輸入在不同失敗點都回傳穩定 domain error；原有測試全綠。

### Slice 2：HTTP Control Plane

- 用 `(app_id, version)` API。
- Release/channel endpoints。
- Structured error response。
- Artifact download URL abstraction。
- 不在 route handler 寫 SQL、object key 或 hash 邏輯。

驗收：HTTP 完成 publish → promote → resolve → download；錯誤碼穩定。

### Slice 3：PostgreSQL／MinIO adapters

- Logical schema migrations。
- PostgreSQLRegistry。
- MinioObjectStore。
- 外部 endpoint 環境變數。
- Compose/CI integration tests。
- Multipart SHA-256、concurrent promotion、upload interruption。

### Slice 4：cv_reviewer package

前置：P1 repo 資訊已由使用者確認，簽章 ADR 已完成。

- `app.yaml`／`package.json` schemas。
- Source install／assemble。
- Blob reference manifest。
- dep-pack reuse。
- `.napp` build。
- 完全斷網 apply/warmup/Tauri E2E。

### Slice 5：Build Worker

隔離 workspace、checkout、build、selfcheck、E2E、簽章、staging upload、register、cancel、structured log。

### Slice 6：Web Console

Applications、Builds、Releases、Channels；先驗證 WDAC 下的 frontend build 路徑。

### Slice 7：Native_App Python Agent

Desired/observed state、本機 SQLite、blob cache、download、verify、source install、venv、healthcheck、journal/reconcile、activate/rollback。

### Slice 8：Rollout 與治理

Device identity/groups、分批 rollout、自動 pause、OIDC/RBAC/approval/audit、key rotation。

## 14. 每一 Slice 的開發門檻

每個 Slice 都必須：

1. 先更新 contract／ADR／acceptance criteria。
2. 實作 domain/service，再做 adapter，最後接 GUI。
3. 增加聚焦單元測試。
4. 正式 adapter 增加 integration tests。
5. 跑既有不連網回歸測試。
6. 涉及真實應用時跑斷網與 Tauri E2E。
7. 更新 handoff/current-state。
8. 重建 handoff ZIP；ZIP 是 generated artifact，以資料夾內容為權威。

## 15. 開發開始命令

目前基線：

```powershell
py -3.11 -m pytest tests\test_package_services.py
py -3.11 -m pytest tests
```

最後已知結果：

```text
5 passed
166 passed, 6 skipped
```

第一個開發工作應是 Slice 1.5，而不是直接建立 Web route 或 Native_App Agent。

## 16. Definition of Done

長期第一階段完成的判準：

- 發布人員能從中央 UI 對 `cv_reviewer` 建置、驗證、發布與 promote。
- Native_App Python Agent 能依 production pointer 安全更新。
- 只改 source 不重下大 wheel、不重建相同 fingerprint venv。
- Registry／MinIO 斷線時啟動 LKG。
- Hash／簽章／healthcheck 失敗時拒絕新版。
- 更新中斷或斷電後能 reconcile。
- 管理者能 rollback，且 data/config/projects 不受破壞。
- 現有 USB provision／apply／warmup 流程保持相容。
