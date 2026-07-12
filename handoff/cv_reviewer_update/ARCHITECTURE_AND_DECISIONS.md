# 架構與已拍板決策

## 1. 目標架構

```text
Web Management Console
          │ HTTPS
          ▼
Application Control Plane ───── PostgreSQL／Oracle-like Registry
          │
          ├──── Build Job ───── Build Worker（重用 native_Provision）
          │                              │
          │                              ▼
          └──────────────────────────── MinIO immutable artifacts
                                         │
                          ┌──────────────┼──────────────┐
                          ▼              ▼              ▼
                    Native_App A   Native_App B   Native_App C
                    Local Agent    Local Agent    Local Agent
```

## 2. 專案責任

| 專案／元件 | 責任 |
|------------|------|
| `cv_reviewer` | `app.yaml`、程式碼、資源、requirements、healthcheck、migration、測試 |
| `native_Provision` | package builder、dep-pack、offline selfcheck、apply、warmup、E2E、Build Worker 核心 |
| Control Plane | application/release/channel、desired state、rollout、RBAC、audit、pre-signed URL |
| MinIO | immutable package、dependency blob、build log、E2E artifact |
| Native_App Local Agent | resolve、download、verify、venv、healthcheck、activate、rollback、observed state |
| Web Console | build、release、promotion、rollout、device 管理 |
| Native_App Portal | 使用者更新進度、本機版本、重試與 rollback |

## 3. 不可違反的決策

1. DB 只保存 metadata，不保存 Python source 或 package binary。
2. MinIO artifact 使用不可變 object；同一 `app_id + version` 不可覆蓋。
3. Runtime 不直接執行 DB、MinIO 或共享磁碟上的 Python。
4. 下載到 staging，驗證成功後才進入本機版本 cache。
5. active version 只在 verify、dependencies、healthcheck 全部成功後切換。
6. Control Plane／MinIO 中斷時，已安裝的 last-known-good 必須仍可啟動。
7. 程式版本、venv 與使用者資料分離；更新不能覆蓋 config/project data。
8. Python 相依依 fingerprint 建 venv；只改 source 時應重用既有 venv。
9. 正式 rollout 使用 desired state，不直接對裝置送一次性「立即安裝」命令。
10. GUI、CLI、API 共用 application service，不複製建置／發布邏輯。
11. 現有 `core.deppack` 是 dependency manifest 權威，不另創 wheel manifest。
12. 正式自動下載執行的 production package 最終必須驗簽；SHA-256 不等於發布者身分。

## 4. GUI 決策

### 中央 Web Console

長期發布與治理入口，包含：

- Applications
- Builds 與 live log
- Releases
- dev／staging／production channels
- Promotion
- Rollouts
- Devices／Device Groups
- Audit Logs

### Native_App UI

一般使用者只看到：檢查更新、下載、驗證、準備環境、更新完成，或「更新失敗，已使用上一版」。管理頁顯示 desired／active／last-known-good、retry、rollback 與 log。

### Tkinter

不再擴充為中央管理台。保留現有離線 provision、開發者本機建置、bootstrap 與緊急診斷。

## 5. Package contract

Application repo 維護 `app.yaml`，Build Worker 產生 `package.json`。宣告與實際建置證明不可混為同一份文件。

> ⚠️ 本節的 `.napp` 內容清單已被取代:定稿版 **不內嵌 `dependency/wheels/` 與
> `big-deps/` 本體**,改為 `blob-references.json`(content-addressed 參照)。
> 以 `DEVELOPMENT_KICKOFF.md` §5 與 `../cv_reviewer_update_final/02_ARCHITECTURE.md` §3 為準。

`.napp` 至少包含（**原始草稿,已被上述取代**）：

```text
package.json
application/
dependency/deppack.json
dependency/wheels/
big-deps/
migrations/
checksums.json
signature.json（正式環境）
```

`package.json` 記錄 app/version、source commit、platform API、OS/arch、Python/ABI、dependency fingerprint、data schema、entrypoint、healthcheck、build provenance 與 artifact hashes。

## 6. 本機狀態

```text
NativeApp data/
├─ agent/state.db                 # 權威 SQLite 狀態
├─ applications/cv-reviewer/
│  ├─ active.json                 # runtime 快速讀取入口
│  ├─ versions/<version>/
│  ├─ venvs/<dependency-hash>/
│  └─ data/{config,projects,cache,logs}/
└─ blobs/sha256/
```

## 7. 更新狀態機

```text
CHECKING
├─ remote unavailable → START_CACHED
├─ same version       → START_ACTIVE
└─ update found
   → DOWNLOADING
   → VERIFYING
   → EXTRACTING
   → DEPS_READY
   → MIGRATION_READY
   → HEALTHCHECK
   ├─ failure → FAILED／ROLLBACK／START_CACHED
   └─ success → ACTIVATING／START_NEW／OBSERVING／LAST_KNOWN_GOOD
```

同一壞版本需記錄 failed 狀態，避免每次啟動無限重試。

