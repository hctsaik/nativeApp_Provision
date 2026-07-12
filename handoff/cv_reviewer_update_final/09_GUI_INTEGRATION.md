# 09 — GUI 產品邊界與遷移規格(Native App 整合)

> 狀態:已採納的產品資訊架構修正(2026-07-11)
> 來源:自 `../cv_reviewer_update/NATIVE_APP_GUI_INTEGRATION.md` 收錄;本檔為權威版。
> 核心判斷:**Native App 是平台,`cv_reviewer` 只是其中一個 application;
> 更新系統是平台能力,不是另一套面向 User 的產品。**
> 任何 GUI 相關工作(畫面、路由、入口)動手前必讀本檔;
> 與現有程式的對照與真正缺口見文末【附錄 A】。

## 1. 現況問題

目前 Lab 暴露多個入口:

```text
Native App Portal
Native App Management Center
Web Console :8090
Device Portal :8091
Control Plane API :8080
```

這種設計方便分開測試元件,但不適合作為正式產品:

- 一般使用者不知道應從哪個入口管理應用。
- Device Portal 與 Native App Management Center 職責重疊。
- `cv_reviewer` 看起來像獨立產品,而不是 Native App 中的一個應用。
- Registry、MinIO、channel、blob、venv 等技術概念被暴露給一般使用者。
- 每增加一個 application,容易誤以為要再做一套更新 GUI。

離線更新導覽 HTML(操作教學)的多入口畫面是教學/實驗產物,
不應直接當成最終產品資訊架構。

## 2. 正式產品只保留兩個管理層級

### 2.1 Native App Management Center(裝置端)

管理單一裝置上的所有 application,供一般使用者與本機管理員使用。

負責:查看可用/已安裝/可更新的 application;安裝、更新、啟動與停止;
顯示 active / latest / last-known-good;rollback、retry、reconcile 與本機 cache 清理;
顯示更新進度與可行動錯誤;管理 application 權限、啟用狀態與本機資料。

### 2.2 Native App Management Center／Fleet(中央治理功能)

中央發布與多裝置治理功能,正式 UI 入口位於 **Native App Management Center → Fleet**,
只供 Developer、Release Manager、IT/Device Admin 使用。Control Plane、Registry、MinIO
仍是中央獨立服務;「UI 在 Native App 內」不等於把中央資料層搬到每台裝置。

負責:Application registry;Build、release 與簽章結果;dev/staging/production
promotion;Yank;Device groups 與 desired state;分批 rollout、approve、pause、
resume;成功率、失敗門檻、audit 與 RBAC。

獨立瀏覽器版 Fleet Console 只保留為遠端管理、Lab 與救援入口;
**一般桌面操作從 Native App 進入,且 Fleet 不是一般使用者啟動 `cv_reviewer` 的入口。**

## 3. 正確產品結構

```text
Native App
├─ Portal:瀏覽與啟動應用
└─ Management Center
   ├─ Applications / Updates / Diagnostics(裝置端)
   └─ Fleet(依權限顯示)
      └─ Applications / Builds / Releases / Channels / Rollouts / Devices
          │
          ▼
Control Plane + Registry + MinIO
          │
          ▼
Native App Python Update Agent:實際執行裝置更新交易
          │
          ▼
Applications:cv_reviewer / VisualLatent / AI4BI / …
```

## 4. cv_reviewer 的定位

`cv_reviewer` repo 只提供:`app.yaml`、`plugin.yaml`、Python source、assets、
requirements、healthcheck、migrations、tests。

它**不**負責:顯示自己的更新管理 GUI;直接連線 Registry/MinIO;操作 Native App
安裝目錄;實作自己的 updater、rollback 或 venv 管理。

Native App 統一管理所有 application;未來加入其它工具時只要遵守相同
package contract,不新增另一套管理 GUI。

## 5. 一般使用者 GUI

### 5.1 Portal 應用卡片

```text
┌────────────────────────────────────┐
│ CV Reviewer                        │
│ 已安裝 1.2.0 · 有新版 1.3.0        │
│ [開啟] [更新]                      │
└────────────────────────────────────┘
```

一般 UI **不顯示**:`.napp`、Registry/MinIO、channel pointer、SHA-256/簽章細節、
blob/wheelhouse/venv fingerprint、desired/observed 內部欄位。
必要時放「技術詳細資料」摺疊區供管理員查看。

### 5.2 啟動行為

使用者按「開啟」:

```text
快速檢查 desired identity
├─ remote unavailable → 啟動 last-known-good
├─ 無更新             → 啟動 active
└─ 有更新
   ├─ policy=自動 → 顯示進度並更新
   └─ policy=詢問 → 顯示「更新並開啟／使用目前版本」
```

更新失敗顯示人類訊息(例:「新版更新失敗,已使用上一個可用版本 1.2.0。
[查看原因][稍後重試]」),不得只顯示 `HashMismatch` 等內部 error code;
詳細頁再附 error code 與 log reference。

## 6. Management Center 畫面

**應用列表**:應用/已安裝/最新版/狀態/操作;支援搜尋、category、
installed/update/error 篩選;一般操作不要求輸入 app ID 或版本字串。

**Application 詳細頁**分區:Overview(版本、狀態、主要動作)、Versions
(本機/遠端版本、rollback)、Storage(source/blob/venv/data/cache 用量)、
Diagnostics(journal、最近錯誤、reconcile、healthcheck)、Permissions
(沿用 Native App RBAC/啟用狀態)。

**更新進度**:

```text
✓ 取得版本資訊 → ✓ 下載 → ✓ 驗證 → ● 準備執行環境 → ○ 健康檢查 → ○ 啟用新版
```

GUI 訂閱 Agent 的**結構化進度事件**,不解析 stdout 文字判斷階段。

## 7. Device Portal 的處置

`native_agent/portal.py` 的 status/update/rollback/reconcile/GC 全部整合進
Native App Management Center。獨立 Device Portal 降級為:

- Agent 開發測試介面
- Native App 尚未啟動時的診斷/救援入口
- 自動化與故障注入 fixture
- 無頭或維修模式的 localhost-only 工具

**正式 User 文件不得要求一般使用者開 `http://127.0.0.1:8091/` 管理 application。**

## 8. Web Console 的處置

功能拆分歸屬(注意:「本機」列目前實作位置見附錄 A 勘誤):

| 功能 | 正式歸屬 |
|------|----------|
| Build/publish | Fleet Console |
| Promote/yank | Fleet Console |
| Rollout/device groups | Fleet Console |
| 本機 update/rollback | Native App Management Center |
| 本機 reconcile/GC | Native App Management Center |
| 一般 application launch | Native App Portal |

正式桌面入口為 **Native App Management Center → Fleet**。`:8090` 保留為相同中央功能的
獨立 Web／Lab／遠端管理入口,不得在一般 User SOP 中要求先離開 Native App 再開它。

## 9. 不重編 Tauri 殼的過渡方案

WDAC 限制 Cargo,不能把重編 Rust 殼設為前置。第一階段:

```text
Native App 現有 Management Center/Portal
        │ existing HTTP/bridge/iframe route
        ▼
Python engine management endpoint
        │
        ▼
native_agent application service
```

可行方式:Python sidecar 提供 Management UI HTML,由現有 Native App 頁面或
iframe 載入;在現有 Portal catalog 加「應用管理」入口指向 sidecar route;
用既有 engine API 回傳 status 與 progress events;保留原生 Tauri adapter,
待可重編殼後只換 UI 容器、不改 Agent domain。

**過渡方案仍必須讓使用者從 Native App 進入,不要求自行輸入 localhost port。**

## 10. Device-local management API 與進度事件

Management Center 用 device-local API,不直接用中央治理 API:

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

進度事件範例:

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

GUI 用 `message_key` 顯示在地化文字;domain error code 保留於詳細資料。

## 11. 權限模型

| 動作 | 一般使用者 | 本機管理員 | Fleet 管理員 |
|------|------------|------------|----------------|
| 查看/啟動已允許 app | 是 | 是 | — |
| 更新到 production desired version | 依 policy | 是 | 設定 policy |
| rollback | 否 | 是 | 可下 desired state |
| reconcile/GC | 否 | 是 | 可查看結果 |
| build/publish/promote/yank | 否 | 否 | 是 |
| rollout/device groups | 否 | 否 | 是 |

沿用 Native App 現有 RBAC 與 Management Store,不另建角色系統;
中央 Fleet identity 與裝置本機角色的映射需另立 ADR。

## 12. GUI 遷移順序

### Phase UI-1:整合裝置管理 ← **下一步**

- 為 Agent 建立 device-local management API(含進度事件)。
- 把 Device Portal 的 status/update/rollback/reconcile/GC 接入 Management Center。
- 應用卡片顯示 installed/latest/update state。
- 獨立 Device Portal 標記為 Diagnostics Only。

驗收:一般使用者只從 Native App 即可更新並啟動 `cv_reviewer`,不用開 `:8091`。

### Phase UI-2:統一應用 catalog

- Agent installed state + Control Plane available release + 既有 plugin catalog
  合併成一個 UI view model;不為 `cv_reviewer` 寫專屬頁面;
  串接既有 enabled_dev/enabled_prod/RBAC。

驗收:新增第二個 application 不修改 Management Center 頁面結構。

### Phase UI-3:Fleet Console 定位

- 在 Native App Management Center 增加依角色顯示的 Fleet 區;
  透過 Control Plane API 提供 Applications/Builds/Releases/Channels/Rollouts/Devices。
- `:8090` 正名 Fleet Web Console,保留遠端管理/Lab 用途,與 Native App 內 Fleet
  使用相同 API 與權限模型;移除/隱藏本機裝置維護功能。

### Phase UI-4:原生殼整合

- 取得可重編 Tauri 環境後,再決定是否把 server-rendered Management UI 換成
  原生 frontend route。Agent API、state machine 與 view model contract 不變。

## 13. GUI 驗收條件(12 項)

1. Native App Portal 可列出 `cv_reviewer` 與其它 application。
2. 使用者可從 Native App 查看已安裝/最新版本並更新。
3. 更新過程顯示結構化階段與進度。
4. 更新失敗時 UI 告知已保留/啟動 LKG。
5. 本機管理員可 rollback、reconcile、GC 及查看 log。
6. 一般使用者與桌面 Fleet 管理員都從 Native App 進入;不需開啟 `:8090`、`:8091`
   或輸入 CLI。獨立 `:8090` 只供明確選擇的遠端管理/Lab 情境。
7. `cv_reviewer` 不含專屬 updater GUI。
8. 新增 application 不新增另一套管理頁或另一個 localhost port。
9. Control Plane/MinIO 離線時,Native App 仍能顯示並啟動已安裝版本。
10. 現有 Device Portal 測試保留為 Agent diagnostic regression tests。
11. Fleet Console 與 Management Center 的 RBAC 邊界明確。
12. 不需重編 Rust/Tauri 殼即可完成第一階段整合。

## 14. 不做事項

- 不把 Device Portal 包裝成另一個面向 User 的桌面應用。
- 不替每個 application 建立自己的更新頁。
- 不讓 Native App frontend 直接連線 MinIO 或 Registry DB。
- 不把 build/publish/rollout 混入一般使用者 Portal。
- 不刪除 CLI;CLI 留給 CI、自動化與救援。
- 不因 UI 整合而重寫 Agent、dep-pack 或 package domain。

## 15. 建議下一個實作工作(contract-first,不急著改畫面)

1. 定義 `ApplicationManagementView`(UI view model)。
2. 定義 device-local management API 與 progress event schema。
3. 建立 Agent service adapter,讓現有 Device Portal 與未來 Management Center 共用。
4. 用現有 Device Portal regression tests 保護行為。
5. 再把相同 view model 接進 Native App 的 Management Center 擴充點。

GUI 整併是**更換 presentation layer**,不是再造一套更新系統。

---

## 附錄 A — 與現有程式的對照(給下一個 AI;2026-07-11 核對)

### A1. 已成立、不用重做

| 規格要求 | 現況 |
|----------|------|
| cv_reviewer 只是一般 app(§4) | 程式無任何 `cv-reviewer` 特判;napp/build/agent 全以 `app_id` 泛化,只有 lab seed 用到該名稱 |
| :8090 = Fleet 範圍(§8) | `web_console/` 現有功能(build/promote/yank/rollout/device)全是 Fleet 級;改名即可 |
| Device Portal 保留為診斷(§7) | `native_agent/portal.py` + `tests/test_device_portal.py` 正好可轉為 diagnostics + regression |
| Python sidecar、不重編殼(§9) | Portal 本來就是 stdlib `http.server` 出 HTML |
| 離線可啟動(驗收 #9) | agent:remote unavailable → START_CACHED;本機 `active.json` |
| CLI 保留(§14) | `python -m native_agent` 的 update/status/rollback/reconcile/gc 已存在 |

### A2. 真正要新做的(UI-1 的實際工程,依重要性)

1. **非同步操作 + 結構化進度事件 —— 最大缺口。**
   現在 `NativeAgent.update()`(`native_agent/agent.py`)是**同步阻塞**,
   只回最終 `UpdateOutcome`;`state.db` 的 operations 有 `current_step`
   但沒有對外事件流、沒有 percent / `message_key` / `can_cancel`。
   §6 進度畫面、§10 `/operations/{id}/events`、驗收 #3 都建立在這之上。
2. **`/management/...` device-local API contract 不存在**——Portal 目前是自訂
   路由(`/update`、`/rollback`);需照 §15.3 抽 Agent service adapter,
   Portal 與 Management Center 共用同一份。
3. **`ApplicationManagementView` view model 不存在**(§15.1)——把 agent
   installed state + Control Plane release + plugin catalog 併成一個 view。
4. 次要:目前只有 update/activate,沒有「未安裝→install」的區分;
   start/stop 屬 Native App 殼,不在 agent。

### A3. 勘誤與範圍註記

- **§8 表格的「目前位置」**:本機 update/rollback/reconcile/GC 目前在
  **:8091 Device Portal**,不在 :8090;表格寫的是「正式歸屬」,判讀時別混淆。
- **驗收 #2/#6/#12 跨 repo**:sidecar 側(API、事件、view model)在本 repo
  (`native_Provision`)可完成;「從 Native App 進入、不輸入 localhost port」
  需動 `C:\code\claude\nativeApp` 的殼(iframe/bridge route)。
  規劃 UI-1 時把兩邊工作分開列。
- 現有 `demo/lab_serve.py` 的三入口與導覽 HTML 是 **lab/教學工具**,
  依 §1 不代表最終產品 IA;保留作開發驗證用。
