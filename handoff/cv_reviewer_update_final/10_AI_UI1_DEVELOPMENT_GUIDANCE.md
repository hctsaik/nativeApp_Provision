# 10 — UI-1 開發 AI 實作指引

> 狀態：UI-1 開工前必讀  
> 日期：2026-07-11  
> 目的：避免下一個 AI 再建立一套獨立 GUI；將現有 Native Agent 整理成 Native App Management Center 可消費的 application management service。

## 1. 一句話任務

> 不要再建立 GUI；先把現有 Native Agent 轉成可被 Native App Management Center 消費的非同步 Application Management Service。Device Portal 只是暫時的測試 client，Native App 才是正式入口。

## 2. UI-1 是跨 repo 工作

### `native_Provision`

- Agent operation service。
- 非同步 operation runner。
- 結構化 progress events。
- Device-local `/management` API。
- `ApplicationManagementView` 聚合模型。
- Device Portal 改用同一 service／API，保留為 diagnostics regression client。

### `nativeApp`

- Management Center 的 application list／detail 入口。
- 呼叫 device-local `/management` API。
- 顯示 update progress、error、retry、rollback 與 diagnostics。
- 使用既有 Management Store、tool catalog、enabled_dev／enabled_prod 與 RBAC。
- 透過現有 iframe／HTTP／bridge 擴充點接入，不以重編 Rust 殼為前置。
- 在 Management Center 內預留 Fleet 區;有 Fleet/Release 權限時顯示中央 Builds、
  Releases、Channels、Rollouts、Devices,無權限時整區不顯示。

只完成 `native_Provision` 的新頁面，不算 Native App GUI 整合完成。

## 3. 開工前先校正文件與測試基線

final handoff 有少量歷史資訊未同步：

- `00_START_HERE.md`、`07_CHECKLISTS.md` 仍出現 `304 passed / 18 skipped`。
- `IMPLEMENTATION_STATUS.md` 與 `04_CODE_MAP.md` 是 `311 passed / 18 skipped`。
- `01_CONSTRAINTS.md` 部分「`.napp` 尚未實作」敘述已過期。
- `05_TASK_SLICE_1_5.md` 的「下一步 Slice 2」是歷史任務卡。
- `06_ROADMAP_SLICES.md` 的 FastAPI 是原始規畫；實際 Control Plane 使用 stdlib `http.server`。

開發前：

1. 重新執行 `py -3.11 -m pytest tests`。
2. 以實際結果統一文件基線。
3. 清楚標記 Slice 1–8 已完成、下一步是 UI-1。
4. 不因文件過期而重做 Slice 2–8。

## 4. 先定義 ApplicationManagementView

GUI 不得自行拼接 Agent SQLite、Control Plane、plugin catalog、Management Store 與 `active.json`。由 backend 聚合成穩定 view model。

建議第一版：

```json
{
  "app_id": "cv-reviewer",
  "display_name": "CV Reviewer",
  "category": "app",
  "installed": true,
  "active_version": "1.2.0",
  "last_known_good": "1.2.0",
  "desired_version": "1.3.0",
  "latest_version": "1.3.0",
  "update_state": "UPDATE_AVAILABLE",
  "enabled": true,
  "can_launch": true,
  "can_install": false,
  "can_update": true,
  "can_rollback": true,
  "health": "HEALTHY",
  "current_operation": null
}
```

規則：

- View model 不得包含 `cv_reviewer` 特判。
- `app_id`、`tool_id`、`plugin_id` 若不一致，使用明確 alias mapping，不用名稱猜。
- Remote unavailable 時仍回傳本機 installed／active／LKG 資訊。
- 權限計算後輸出 `can_*`，但 backend 仍必須在 mutation API 再做 RBAC enforcement。

## 5. 更新必須是非同步 operation

目前 `NativeAgent.update()` 同步阻塞。不可讓 HTTP request 等待下載、venv、warmup 或 healthcheck 完成。

正確流程：

```text
POST /management/applications/{app_id}/update
→ 建立 operation journal
→ 回 202 Accepted + operation_id
→ background worker 執行
→ 寫入 operation state/events
→ GUI polling 或 SSE 讀取進度
```

回應範例：

```json
{
  "operation_id": "op-...",
  "state": "QUEUED",
  "status_url": "/management/operations/op-..."
}
```

Operation 必須持久化於 SQLite；Agent 或 Native App 重啟後仍可查看、reconcile，不可只放記憶體。

## 6. 結構化進度事件

不得解析 stdout 判斷進度。Domain／operation runner 主動發出事件：

```json
{
  "sequence": 12,
  "operation_id": "op-123",
  "app_id": "cv-reviewer",
  "operation": "UPDATE",
  "stage": "VERIFYING",
  "state": "RUNNING",
  "percent": 35,
  "message_key": "application.update.verifying",
  "detail": {},
  "can_cancel": true,
  "created_at": "2026-07-11T00:00:00Z"
}
```

必要 stage：

```text
QUEUED
RESOLVING
DOWNLOADING
VERIFYING
EXTRACTING
PREPARING_DEPENDENCIES
HEALTHCHECK
ACTIVATING
OBSERVING
COMPLETED
FAILED
ROLLED_BACK
```

`message_key` 供 GUI 在地化；domain error code 與 log reference 放詳細資料，不直接當一般使用者訊息。

## 7. 第一版傳輸使用 polling 即可

優先實作：

```text
GET /management/operations/{operation_id}
GET /management/operations/{operation_id}/events?after=<sequence>
```

GUI 每 500–1000 ms polling。若現有 stdlib server 能穩定支援 SSE，可再增加 SSE；不要為 WebSocket 引入另一套 server framework或破壞 stdlib-only 約束。

## 8. 每個 application 同時只允許一個 mutation

必須拒絕：

- 同時兩次 update。
- update 與 rollback 並行。
- update 時執行 GC。
- activation 時執行 reconcile。

建議 SQLite 保證每個 `app_id` 同時只有一個 active mutation operation。衝突回：

```text
HTTP 409
error.code = operation_in_progress
```

Status／list 等唯讀操作可並行。

## 9. 取消能力由 backend 決定

| Stage | 第一版是否可取消 |
|-------|------------------|
| QUEUED | 是 |
| DOWNLOADING | 是 |
| VERIFYING | 是 |
| EXTRACTING | 僅 staging 可安全清理時 |
| PREPARING_DEPENDENCIES | 視 subprocess 中止與清理能力 |
| MIGRATING | 預設否 |
| ACTIVATING | 否 |
| OBSERVING | 否 |

GUI 只遵守事件的 `can_cancel`，不可自行依 stage 猜測。

## 10. Device-local API

建議 contract：

```text
GET  /management/applications
GET  /management/applications/{app_id}
POST /management/applications/{app_id}/install
POST /management/applications/{app_id}/update
POST /management/applications/{app_id}/rollback
POST /management/applications/{app_id}/reconcile
POST /management/applications/{app_id}/gc
GET  /management/operations/{operation_id}
GET  /management/operations/{operation_id}/events
POST /management/operations/{operation_id}/cancel
```

Native App frontend 不直接連 Control Plane、Registry DB 或 MinIO。資料路徑：

```text
Native App UI
→ device-local management API
→ ApplicationManagementService
   ├─ Agent state
   ├─ Control Plane client
   └─ Native App plugin/Management catalog adapter
```

## 11. 不建立第二套 application catalog

Native App 已有：

- `tools`
- `tool_versions`
- plugin catalog
- enabled_dev／enabled_prod
- RBAC／Management Store
- Oracle adapter

新系統建立 projection／mapping，不另建互不相干的 User-facing catalog。需明定 `app_id`、`tool_id`、`plugin_id` 的 identity mapping。

建議 UI view 的來源優先順序與衝突規則另寫 ADR；不得由 frontend 用 display name 合併。

## 12. Install、Update、Launch 分開

| 狀態 | 主要操作 |
|------|----------|
| 未安裝 | Install |
| 已安裝、有新版 | Update／Open current |
| 已準備、無更新 | Open |
| 更新中 | 顯示 operation，不重複送 mutation |
| 更新失敗、有 LKG | Open current／Retry |
| 無可用版本 | 禁止 Open，顯示可行動原因 |

不要讓「Open」背後同步執行數分鐘的安裝且沒有明確進度。

## 13. RBAC 必須由 backend 強制

一般使用者：查看、啟動、依 policy 更新。  
本機管理員：rollback、retry、reconcile、GC、journal/log。  
Fleet 管理員：build、publish、promote、yank、rollout、device policy。

隱藏按鈕不是安全控制；每個 mutation endpoint 都要檢查權限並有 403 測試。

## 14. UI-1 建議實作順序

1. 重跑測試、校正文檔基線。
2. 盤點 Native App Management Center 現有 route／iframe／bridge 擴充點。
3. 定義 `ApplicationManagementView` schema。
4. 定義 operation、event、error schema。
5. 抽出 `ApplicationManagementService`。
6. 將 update／rollback／reconcile／GC 包裝成非同步 operation。
7. 加入 per-app mutation lock 與 restart reconciliation。
8. 實作 `/management` API。
9. 將 Device Portal 改為呼叫同一 service／API。
10. 接入 Native App Management Center。
11. 使用第二個 fixture app 證明沒有 `cv_reviewer` 特判。
12. 執行跨 repo GUI E2E。

## 15. UI-1 必測場景

1. 未安裝 app → install → active。
2. 已安裝舊版 → update → active/LKG 更新。
3. Control Plane 離線 → 顯示 remote unavailable，但可開 LKG。
4. Hash／簽章錯誤 → 新版拒絕，active 不變。
5. Healthcheck 失敗 → rollback，UI 顯示友善原因。
6. Agent 下載中重啟 → operation reconcile。
7. Agent activation 中重啟 → active/LKG 一致。
8. Update 執行中再按 update → 409。
9. Update 執行中按 GC → 409。
10. 一般使用者 rollback → 403。
11. 新增第二個 app → 同一列表與詳細頁可操作。
12. User 全程不輸入 `:8090`、`:8091` 或 CLI。
13. Device Portal 舊測試持續通過，作為 diagnostic regression。
14. 現有 USB provision／apply／warmup 回歸不受影響。
15. Fleet 管理員可從 Native App Management Center 進入 Fleet 功能;
    獨立 `:8090` 只作遠端管理/Lab,兩者共用 Control Plane API。

## 16. Definition of Done

- Native App 是裝置端唯一正式入口。
- Device Portal 被標記為 Diagnostics Only。
- Management Center 使用通用 view model 顯示多個 application。
- 長時間操作全部非同步且可觀測。
- Agent／Native App 重啟後 operation 可恢復或安全 reconcile。
- 同一 app 不會發生互相衝突的 mutation。
- RBAC 在 backend 生效。
- Remote unavailable 不影響已安裝 LKG 啟動。
- 沒有 `cv_reviewer` 專屬 updater 或 UI 特判。
- Fleet Console 與裝置 Management Center 的產品邊界符合 `09_GUI_INTEGRATION.md`。
