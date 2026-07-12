# Native App 應用管理 GUI 整合建議

> ⚠️ 本文件已收錄為
> [`../cv_reviewer_update_final/09_GUI_INTEGRATION.md`](../cv_reviewer_update_final/09_GUI_INTEGRATION.md)
> (權威版,含「與現有程式的對照」附錄與缺口分析)。開發請讀該版;本檔保留原稿。

> 狀態：產品資訊架構修正建議  
> 日期：2026-07-11  
> 核心判斷：Native App 是平台，`cv_reviewer` 只是其中一個 application；更新系統是平台能力，不是另一套面向 User 的產品。

## 1. 現況問題

目前 Lab 暴露多個入口：

```text
Native App Portal
Native App Management Center
Web Console :8090
Device Portal :8091
Control Plane API :8080
```

這種設計方便分開測試元件，但不適合作為正式產品：

- 一般使用者不知道應從哪個入口管理應用。
- Device Portal 與 Native App Management Center 職責重疊。
- `cv_reviewer` 看起來像獨立產品，而不是 Native App 中的一個應用。
- Registry、MinIO、channel、blob、venv 等技術概念被暴露給一般使用者。
- 每增加一個 application，容易誤以為要再做一套更新 GUI。

`C:\Users\hctsa\Downloads\native` 目前保存的是離線更新導覽 HTML，不是 Native App 正式管理介面，不應把該操作導覽的多入口畫面直接當成最終產品資訊架構。

## 2. 正式產品只保留兩個管理層級

### 2.1 Native App Management Center

管理單一裝置上的所有 application，供一般使用者與本機管理員使用。

負責：

- 查看可用、已安裝與可更新的 application。
- 安裝、更新、啟動與停止。
- 顯示 active、latest、last-known-good。
- rollback、retry、reconcile 與本機 cache 清理。
- 顯示更新進度與可行動錯誤。
- 管理 application 權限、啟用狀態與本機資料。

### 2.2 Native App Fleet Console

中央發布與多裝置治理後台，只供 Developer、Release Manager、IT／Device Admin 使用。

負責：

- Application registry。
- Build、release 與簽章結果。
- dev／staging／production promotion。
- Yank。
- Device groups 與 desired state。
- 分批 rollout、approve、pause、resume。
- 成功率、失敗門檻、audit 與 RBAC。

Fleet Console 不應成為一般使用者啟動 `cv_reviewer` 的入口。

## 3. 正確產品結構

```text
中央管理
Native App Fleet Console
├─ Applications
├─ Builds
├─ Releases
├─ Channels
├─ Rollouts
└─ Devices
          │
          ▼
Control Plane + Registry + MinIO
          │
          ▼
裝置端
Native App
├─ Portal：瀏覽與啟動應用
├─ Management Center：安裝、更新、rollback、診斷
└─ Python Update Agent：實際執行更新交易
          │
          ▼
Applications
├─ cv_reviewer
├─ VisualLatent
├─ AI4BI
└─ 其它 application
```

## 4. cv_reviewer 的定位

`cv_reviewer` repo 只應提供：

```text
app.yaml
plugin.yaml
Python source
assets
requirements
healthcheck
migrations
tests
```

它不負責：

- 顯示自己的更新管理 GUI。
- 直接連線 Registry／MinIO。
- 操作 Native App 安裝目錄。
- 實作自己的 updater、rollback 或 venv 管理。

Native App 統一管理所有 application。未來加入 `defect_inspector`、`report_viewer` 或其它工具時，只要遵守相同 package contract，不新增另一套管理 GUI。

## 5. 一般使用者 GUI

### 5.1 Native App Portal 應用卡片

```text
┌────────────────────────────────────┐
│ CV Reviewer                        │
│ 已安裝 1.2.0 · 有新版 1.3.0       │
│ [開啟] [更新]                      │
└────────────────────────────────────┘

┌────────────────────────────────────┐
│ VisualLatent                       │
│ 已是最新版 1.0.0                   │
│ [開啟]                             │
└────────────────────────────────────┘
```

一般使用者只需要理解 application 名稱、目前狀態與可執行動作。下列概念不顯示在一般 UI：

- `.napp`
- Registry／MinIO
- channel pointer
- SHA-256／signature 細節
- blob／wheelhouse／venv fingerprint
- desired／observed state 內部欄位

必要時可放在「技術詳細資料」的摺疊區供管理員查看。

### 5.2 啟動行為

使用者按「開啟」：

```text
快速檢查 desired identity
├─ remote unavailable → 啟動 last-known-good
├─ 無更新             → 啟動 active
└─ 有更新
   ├─ policy=自動 → 顯示進度並更新
   └─ policy=詢問 → 顯示「更新並開啟／使用目前版本」
```

更新失敗時顯示：

```text
新版更新失敗，已使用上一個可用版本 1.2.0。
[查看原因] [稍後重試]
```

不得只顯示 `HashMismatch`、`MINIO_TIMEOUT` 等內部 error code；UI 顯示人類訊息，詳細頁再附 error code 與 log reference。

## 6. Native App Management Center

### 6.1 應用列表

| 應用 | 已安裝 | 最新版 | 狀態 | 操作 |
|------|--------|--------|------|------|
| CV Reviewer | 1.2.0 | 1.3.0 | 可更新 | 更新 |
| VisualLatent | 1.0.0 | 1.0.0 | 正常 | 開啟 |
| AI4BI | — | 2.1.0 | 未安裝 | 安裝 |

支援搜尋、category、installed/update/error 篩選。一般操作不要求使用者輸入 app ID 或版本字串。

### 6.2 Application 詳細頁

```text
CV Reviewer

狀態
  Active             1.2.0
  Last known good    1.2.0
  Production         1.3.0
  Update Agent       正常
  Dependencies       已準備

[更新到 1.3.0] [回復上一版] [修復] [開啟記錄]

版本歷史
  1.3.0  可安裝
  1.2.0  使用中 / LKG
  1.1.0  本機保留
```

詳細頁分區：

- Overview：版本、狀態與主要動作。
- Versions：本機與遠端版本、rollback。
- Storage：source、blob、venv、data、cache 用量。
- Diagnostics：journal、最近錯誤、reconcile、healthcheck。
- Permissions：既有 Native App RBAC／啟用狀態。

### 6.3 更新進度

```text
正在更新 CV Reviewer

✓ 取得版本資訊
✓ 下載應用程式
✓ 驗證套件
● 準備執行環境
○ 健康檢查
○ 啟用新版

[在背景執行]
```

GUI 訂閱 Agent 的結構化進度事件，不解析 stdout 文字判斷階段。

## 7. Device Portal 的處置

目前 `native_agent/portal.py` 的功能：

- status
- update
- rollback
- reconcile
- GC

正式產品中全部整合進 Native App Management Center。

獨立 Device Portal 降級為：

- Agent 開發測試介面。
- Native App 尚未啟動時的診斷／救援入口。
- 自動化與故障注入 fixture。
- 無頭或維修模式的 localhost-only 工具。

正式 User 文件不得要求一般使用者開啟 `http://127.0.0.1:8091/` 才能管理 application。

## 8. Web Console 的處置

目前 `:8090` 的 Web Console 拆分如下：

| 功能 | 正式歸屬 |
|------|----------|
| Build／publish | Fleet Console |
| Promote／yank | Fleet Console |
| Rollout／device groups | Fleet Console |
| 本機 update／rollback | Native App Management Center |
| 本機 reconcile／GC | Native App Management Center |
| 一般 application launch | Native App Portal |

Web Console 正式命名為 **Native App Fleet Console**，避免與裝置端 Management Center 混淆。

## 9. 不重編 Tauri 殼的過渡方案

目前 WDAC 限制 Cargo，不能把重編 Rust 殼設為前置。

第一階段：

```text
Native App 現有 Management Center／Portal
        │ existing HTTP/bridge/iframe route
        ▼
Python engine management endpoint
        │
        ▼
native_agent application service
```

可行方式：

1. 由 Python sidecar 提供 Management UI HTML，透過現有 Native App 頁面或 iframe 載入。
2. 在現有 Portal application catalog 增加「應用管理」入口，指向 sidecar route。
3. 使用既有 engine API 回傳 application status 與 progress events。
4. 保留原生 Tauri 整合 adapter，待取得可重編殼的機器後再換 UI 容器，不改 Agent domain。

過渡方案仍必須讓使用者從 Native App 進入，不要求自行輸入 localhost port。

## 10. API 與進度事件

Native App Management Center 需要的是 device-local API，不應直接使用中央治理 API。

建議：

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
```

進度事件範例：

```json
{
  "operation_id": "...",
  "app_id": "cv-reviewer",
  "stage": "PREPARING_ENVIRONMENT",
  "percent": 65,
  "message_key": "application.update.preparing_environment",
  "can_cancel": false
}
```

GUI 使用 `message_key` 顯示在地化文字；domain error code 保留於詳細資料。

## 11. 權限模型

| 動作 | 一般使用者 | 本機管理員 | Fleet 管理員 |
|------|------------|------------|----------------|
| 查看／啟動已允許 app | 是 | 是 | — |
| 更新到 production desired version | 依 policy | 是 | 設定 policy |
| rollback | 否 | 是 | 可下 desired state |
| reconcile／GC | 否 | 是 | 可查看結果 |
| build／publish／promote／yank | 否 | 否 | 是 |
| rollout／device groups | 否 | 否 | 是 |

沿用 Native App 現有 RBAC 與 Management Store，不另建一套互不相干的角色系統；中央 Fleet identity 與裝置本機角色如何映射需另立 ADR。

## 12. GUI 遷移順序

### Phase UI-1：整合裝置管理

- 為 Agent 建立 device-local management API。
- 把 Device Portal status/update/rollback/reconcile/GC 接入 Native App Management Center。
- Native App 應用卡片顯示 installed/latest/update state。
- 獨立 Device Portal 標記為 Diagnostics Only。

驗收：一般使用者只從 Native App 即可更新並啟動 `cv_reviewer`，不用開 `:8091`。

### Phase UI-2：統一應用 catalog

- 將 Agent installed state、Control Plane available release 與既有 plugin catalog 合併成 UI view model。
- 顯示所有 application，不為 `cv_reviewer` 寫專屬頁面。
- 串接既有 enabled_dev／enabled_prod／RBAC。

驗收：新增第二個 application 不修改 Management Center 頁面結構。

### Phase UI-3：Fleet Console 定位

- 將 `:8090` 明確命名為 Fleet Console。
- 移除／隱藏本機裝置維護功能。
- 加入中央 Applications、Builds、Releases、Channels、Rollouts、Devices 導覽。

### Phase UI-4：原生殼整合

- 取得可重編 Tauri 的環境後，再決定是否把 server-rendered Management UI 換為原生 frontend route。
- Agent API、state machine 與 view model contract 保持不變。

## 13. GUI 驗收條件

1. Native App Portal 可列出 `cv_reviewer` 與其它 application。
2. 使用者可從 Native App 查看已安裝／最新版本並更新。
3. 更新過程顯示結構化階段與進度。
4. 更新失敗時 UI 告知已保留／啟動 LKG。
5. 本機管理員可 rollback、reconcile、GC 及查看 log。
6. 一般使用者不需開啟 `:8090`、`:8091` 或輸入 CLI。
7. `cv_reviewer` 不含專屬 updater GUI。
8. 新增 application 不新增另一套管理頁或另一個 localhost port。
9. Control Plane／MinIO 離線時，Native App 仍能顯示並啟動已安裝版本。
10. 現有 Device Portal 測試保留為 Agent diagnostic regression tests。
11. Fleet Console 與 Management Center 的 RBAC 邊界明確。
12. 不需重編 Rust／Tauri 殼即可完成第一階段整合。

## 14. 不做事項

- 不把 Device Portal 包裝成另一個面向 User 的桌面應用。
- 不替每個 application 建立自己的更新頁。
- 不讓 Native App frontend 直接連線 MinIO 或 Registry DB。
- 不把 build／publish／rollout 混入一般使用者 Portal。
- 不刪除 CLI；CLI 留給 CI、自動化與救援。
- 不因 UI 整合而重寫 Agent、dep-pack 或 package domain。

## 15. 建議下一個實作工作

先完成 UI-1 的 contract，不急著改畫面：

1. 定義 `ApplicationManagementView`。
2. 定義 device-local management API 與 progress event schema。
3. 建立 Agent service adapter，讓現有 Device Portal 與未來 Management Center 共用。
4. 用現有 Device Portal regression tests 保護行為。
5. 再把相同 view model 接進 Native App 的 Management Center 擴充點。

這能確保 GUI 整併是更換 presentation layer，而不是再造一套更新系統。

