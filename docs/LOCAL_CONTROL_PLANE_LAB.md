# Native App 本機 Registry／Object Storage 實驗環境

> 狀態：可執行的第一條垂直切片  
> 目的：實際體驗 application release、channel、artifact、下載驗證與失敗安全。  
> 長期架構請搭配 [CV Reviewer GUI 化發布與自動更新規畫](CV_REVIEWER_GUI_UPDATE_PLAN.md) 閱讀。

## 1. 已交付內容

| 模式 | Registry | Object Store | 現在是否可用 |
|------|----------|--------------|----------------|
| 自足 Lab | SQLite | 本機 immutable object 目錄 | 是，不需安裝服務 |
| 正式服務骨架 | PostgreSQL | MinIO | 需先安裝 Docker／Podman |

目前這台開發機沒有 `docker` 指令，因此本輪完整測試自足 Lab；Compose 檔已提供，但尚未在這台機器啟動。這不影響 interface 與 application service 的開發。

## 2. Adapter 架構

```text
PackageService
├─ Registry interface
│  ├─ SQLiteRegistry（現在可跑）
│  ├─ PostgreSQLRegistry（下一階段）
│  └─ OracleRegistry（公司要求時）
└─ ObjectStore interface
   ├─ FileObjectStore（現在可跑）
   └─ MinioObjectStore（下一階段）
```

GUI、CLI、Control Plane API 與 Native_App updater 只呼叫 `PackageService`，不直接寫 SQL 或 MinIO SDK。SQLite 提供關聯表、外鍵、交易與 SQL，可作為 Oracle-like 開發替身；正式 adapter 仍需要自己的 integration tests。

## 3. 立即體驗

```powershell
Set-Content -Encoding UTF8 demo-cv-reviewer.napp "CV Reviewer 1.0.0 demo"
py -3.11 local_services.py publish cv-reviewer 1.0.0 demo-cv-reviewer.napp
py -3.11 local_services.py promote cv-reviewer 1.0.0 --channel production
py -3.11 local_services.py resolve cv-reviewer --channel production
py -3.11 local_services.py download cv-reviewer .local-client\cv-reviewer.napp --channel production
py -3.11 local_services.py list cv-reviewer
```

資料會落在：

```text
.local-services/
├─ registry.db
└─ objects/applications/cv-reviewer/1.0.0/cv-reviewer-1.0.0.napp
```

同版本不能覆蓋。下載先寫暫存檔，SHA-256 正確後才原子換位；object 被修改時下載失敗且不留下目的檔。

## 4. 已實作契約

Registry：`create_release`、`get_release`、`list_releases`、`promote`、`resolve`。

Object Store：`put`、`open`、`exists`。

Package Service：`publish`、`download`。

這是最小閉環。後續 Control Plane API、Web GUI 與 Native_App Agent 都應建立在同一組 use cases 上。

## 5. PostgreSQL／MinIO Lab

部署檔：

```text
deploy/local/
├─ compose.yml
├─ init-registry.sql
└─ .env.example
```

安裝 Docker Desktop 或相容 Compose runtime 後：

```powershell
Copy-Item deploy\local\.env.example deploy\local\.env
# 編輯 .env，勿沿用範例密碼到正式環境
docker compose --env-file deploy\local\.env -f deploy\local\compose.yml up -d
docker compose --env-file deploy\local\.env -f deploy\local\compose.yml ps
```

| 服務 | 位址 | 用途 |
|------|------|------|
| PostgreSQL | `localhost:5432` | Application Registry |
| MinIO S3 API | `http://localhost:9000` | 程式存取 artifact |
| MinIO Console | `http://localhost:9001` | 人工查看 bucket/object |

MinIO 初始化會建立 private bucket `native-apps`；PostgreSQL 會建立 applications、releases、channels 與 audit events。

停止但保留資料：

```powershell
docker compose --env-file deploy\local\.env -f deploy\local\compose.yml down
```

刪除 volume 會永久刪除 Lab 資料，本文件不提供自動刪除 volume 的指令。

## 6. 長期正式方案

- Control Plane：FastAPI + PostgreSQL + MinIO，管理 release、channel、desired state、RBAC、audit 與短效 URL。
- Build Worker：重用 `native_Provision`，執行 checkout、package、dep-pack、offline selfcheck、warmup、Tauri E2E、簽章與上傳。
- Native_App Agent：resolve、download、verify、compatibility、venv、healthcheck、atomic activation 與 rollback。
- 中央 Web Console：發布、promotion、rollout 與裝置治理。
- Native_App Portal：使用者更新進度與本機 rollback。
- Tkinter：保留離線包、bootstrap 與診斷，不作中央治理介面。

## 7. 下一批實作

1. FastAPI Control Plane，以 HTTP 包裝目前 `PackageService`。
2. `PostgreSQLRegistry` 與相同 contract tests。
3. `MinioObjectStore` 與 Compose integration tests。
4. `app.yaml`、`package.json` JSON Schema 與 `.napp` assembly。
5. Web Console release 頁與 build live log。
6. Native_App Local Agent 與 SQLite device state。
7. 以 `cv_reviewer` 跑 publish → production → download → activate → rollback E2E。

## 8. 測試與故障注入

目前覆蓋完整閉環、不可變 object、不存在 release 禁止 promotion、竄改拒絕與 object-key traversal。

```powershell
py -3.11 -m pytest tests\test_package_services.py
py -3.11 -m pytest tests
```

正式 adapter 尚需驗證 PostgreSQL concurrent promotion、MinIO multipart 中斷、pre-signed URL 過期，以及 Local Agent 在下載／啟用中被終止。

## 9. 安全

- `.env.example` 只供 Lab。
- Production MinIO bucket 不公開，只使用短效 pre-signed URL。
- Production release 應驗簽；SHA-256 只能證明完整性，不能證明發布者身分。
- DB／MinIO credential 不下發到 Web frontend。
- 同一 `app_id + version` 永遠不可改寫。
- GUI 必須呼叫共用 service，不自行組 SQL、S3 key 或 checksum。

