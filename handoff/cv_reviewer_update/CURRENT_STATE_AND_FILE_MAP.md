# 現況與檔案地圖

> ⚠️ **本文件為早期草稿。** Slice 1–8 已全部實作並測試(2026-07-11,
> 全 repo 276 passed / 18 skipped)。最新實作狀態以
> `../cv_reviewer_update_final/IMPLEMENTATION_STATUS.md` 為準。

## 1. 已完成程式

| 原 repo 路徑 | 用途 |
|--------------|------|
| `src/provision_builder/package_services.py` | Registry/ObjectStore contracts、SQLite、filesystem、PackageService |
| `local_services.py` | publish/promote/resolve/download/list Lab CLI |
| `tests/test_package_services.py` | package domain 聚焦測試 |
| `deploy/local/compose.yml` | PostgreSQL 16 + MinIO 服務骨架 |
| `deploy/local/init-registry.sql` | 正式 Registry 基礎 schema |
| `deploy/local/.env.example` | Lab 設定範例，不可用於 production |

## 2. 原 repo 必读文件

| 路徑 | 重點 |
|------|------|
| `SPEC.md` | 現有離線 dep-pack 的權威規格與 as-built 記錄 |
| `docs/TOOL_DEVELOPMENT_AND_DISTRIBUTION.md` | 多工具開發／發布長期架構 |
| `docs/CV_REVIEWER_GUI_UPDATE_PLAN.md` | CV Reviewer GUI 與更新規畫 |
| `docs/LOCAL_CONTROL_PLANE_LAB.md` | 本機 Lab 操作、正式服務骨架與安全事項 |
| `docs/OFFLINE_DEPLOY.md` | 現有工廠離線部署操作 |

## 3. 現有能力不可重造

- `core.deppack` 原生 manifest 與 fail-closed 驗證。
- `native_Provision` scan/build/verify/report。
- big-deps 隔離與去重。
- source package 與逐檔 SHA-256。
- stdlib-only `apply.py` 原子套用。
- `warmup.py` 預建 per-tool venv。
- 真實 Tauri／Portal E2E 與 engine log 證據。

新系統應編排這些能力，不自行組另一套 pip、wheel manifest 或離線安裝格式。

## 4. 現況限制

- 本機目前找不到 `docker` 指令。
- SQLite／filesystem Lab 已實際執行並測試。
- PostgreSQL／MinIO Compose 尚未在此機啟動驗證。
- `PostgreSQLRegistry`、`MinioObjectStore`、HTTP Control Plane、Web Console、Native_App Agent 尚未實作。
- 正式 `.napp` assembly、schema 與 package signature 尚未實作。
- 使用者口中的 `cv_reviewer` repo 實際路徑／名稱仍需確認；不可猜成工作區中的其它 CV 專案。

## 5. 驗證命令

```powershell
py -3.11 -m pytest tests\test_package_services.py
py -3.11 -m pytest tests
```

最後一次結果：

```text
5 passed
166 passed, 6 skipped
```

## 6. 本機 Lab 命令

```powershell
Set-Content -Encoding UTF8 demo.napp "CV Reviewer demo"
py -3.11 local_services.py publish cv-reviewer 1.0.0 demo.napp
py -3.11 local_services.py promote cv-reviewer 1.0.0 --channel production
py -3.11 local_services.py resolve cv-reviewer --channel production
py -3.11 local_services.py download cv-reviewer downloaded.napp --channel production
```

Lab 狀態預設存於 `.local-services/`，已被 `.gitignore` 排除。

