# 04 — 現有程式地圖與現況

> 每完成一個 Slice,回來更新 §5「進度」。

## 1. 新系統已完成的程式(Slice 1 + 1.5)

| 檔案(repo 相對路徑) | 內容 |
|------------------------|------|
| `src/provision_builder/package_errors.py` | **(1.5 新增)** 12 個穩定 domain error(`PackageDomainError` 家族,各帶 `code`)+ `IDENTIFIER_RE` + `validate_identifier` |
| `src/provision_builder/package_services.py` | `Release`、`Registry`/`ObjectStore` Protocol(含 `yank`)、`SQLiteRegistry`、`FileObjectStore`、`PackageService`(publish/promote/yank/resolve/list/download,全走 domain error) |
| `local_services.py`(repo 根目錄) | Lab CLI:publish / promote / **yank** / resolve / download / list;錯誤輸出 `ERROR[code]:`;狀態存 `.local-services/`(已 gitignore) |
| `tests/test_package_services.py` | 服務層行為測試(閉環、不可變、identifier、duplicate 穩定、orphan、yank、download 失敗分類) |
| `tests/test_package_errors.py` | **(1.5 新增)** error code 契約 + `validate_identifier` 邊界 |
| `docs/REGISTRY_LOGICAL_SCHEMA.md` | **(1.5 新增)** Registry logical schema 單一權威(SQLite/PG/Oracle 對齊基準) |
| `deploy/local/compose.yml` | PostgreSQL 16 + MinIO 服務骨架(**本機無 docker,尚未啟動驗證**) |
| `deploy/local/init-registry.sql` | 正式 Registry 基礎 schema(與 logical schema 仍有差異,Slice 3 收斂) |
| `deploy/local/.env.example` | Lab 設定範例,**不可用於 production** |

Slice 1.5 已修掉的原始問題(全部有測試守住):

1. ✅ `publish()` 重排(先驗 identifier → 查 registry → 查 object → put → create),
   重複版本**任何失敗點都回 `DuplicateVersion`**;上傳成功而寫 DB 失敗只留
   unreferenced object 給 GC(不發布半套 metadata)。
2. ✅ `_safe_part()` 由 `validate_identifier`(regex)取代,冒號/空白/前導符全擋。
3. ✅ 全面改用帶 `code` 的 `PackageDomainError`;HTTP 層(Slice 2)只查表映射。
4. ✅ 加入 yank 狀態模型(`published`→`yanked`,冪等,object 不刪)。
5. ⚠️ SQLite 已補 `status` CHECK 值域;其餘欄位差異(display_name/enabled/
   fingerprint/audit_events)依計畫留待 Slice 3 migration 收斂,已記於
   `docs/REGISTRY_LOGICAL_SCHEMA.md` §4。

> 設計取捨(依決策 #10「GUI/CLI/API 共用同一 service 層」):promote/yank/
> resolve/list 已加為 `PackageService` 的薄委派方法,CLI 全部改走 service。
> Registry adapter 仍擁有 SQL 與 DB 例外→domain error 的轉譯,無邏輯重複。
> Slice 2 的 HTTP handler 因此只需呼叫 `PackageService`,不碰 registry。

## 2. 既有平台能力(不可重造,只可編排)

| 能力 | 權威文件 |
|------|----------|
| dep-pack 格式與 fail-closed 驗證(`core.deppack`) | `SPEC.md` §2 |
| provision build / verify / apply CLI | `SPEC.md` §4 |
| 產出佈局、`provision.json`、`REPORT.md` | `SPEC.md` §5 |
| big-deps 隔離與去重 | `SPEC.md` §6 |
| 掃描規則、YAML 讀取 | `SPEC.md` §7 |
| 增量與離線自檢 | `SPEC.md` §8 |
| `apply.py` 行為(**只管 dependency,不套 source-packages**) | `SPEC.md` §9 |
| 地雷與否決表 | `SPEC.md` §13 |
| 平台耦合契約 | `SPEC.md` §14 |
| as-built 紀錄、warmup、GUI E2E、source package E2E | `SPEC.md` §16–18 |

其它必讀:

| 文件 | 重點 |
|------|------|
| `docs/TOOL_DEVELOPMENT_AND_DISTRIBUTION.md` | 多工具開發/發布長期架構 |
| `docs/CV_REVIEWER_GUI_UPDATE_PLAN.md` | CV Reviewer GUI 與更新規畫 |
| `docs/LOCAL_CONTROL_PLANE_LAB.md` | 本機 Lab 操作、正式服務骨架與安全事項 |
| `docs/OFFLINE_DEPLOY.md` | 現有工廠離線部署操作 |

## 3. 驗證命令與基線

```powershell
# 聚焦測試(新系統 domain)
py -3.11 -m pytest tests\test_package_services.py
# 全 repo 回歸
py -3.11 -m pytest tests
```

基線(Slice 1–8 + UI-1 native_Provision 側,2026-07-12 實測):全 repo `335 passed, 18 skipped`
(18 skipped = 原 6 + PG/MinIO contract 12,本機無 docker)。
**任何 Slice 結束時,全 repo 回歸不得低於此基線**(新增測試當然要全綠)。
各 slice 產物與外部缺口見 `IMPLEMENTATION_STATUS.md`。

## 4. Lab 快速煙霧測試

```powershell
Set-Content -Encoding UTF8 demo.napp "CV Reviewer demo"
py -3.11 local_services.py publish cv-reviewer 1.0.0 demo.napp
py -3.11 local_services.py promote cv-reviewer 1.0.0 --channel production
py -3.11 local_services.py resolve cv-reviewer --channel production
py -3.11 local_services.py download cv-reviewer downloaded.napp --channel production
```

## 5. 進度(每個 Slice 完成後更新這裡)

| Slice | 狀態 | 備註 |
|-------|------|------|
| 1:Local package domain | ✅ 完成 | |
| 1.5:Domain hardening | ✅ 完成 | error taxonomy + identifier + yank + logical schema |
| 2:HTTP Control Plane | ✅ 完成 | `control_plane/`,stdlib http.server + live smoke |
| 3:PostgreSQL / MinIO adapters | ✅ 完成 | blob store/GC/contract 本機綠;PG/MinIO adapters 已寫,整合測試 CI 才跑(P3) |
| 4:cv_reviewer package | ✅ 程式完成 | `.napp`/schema/簽章/source install 以 fixture 驗;真實 repo E2E 待 P1 |
| 5:Build Worker | ✅ 完成 | `build_worker/`,可取消 + JSONL log |
| 6:Web Console | ✅ 完成 | `web_console/`,server-rendered(避開 W2 前端建置) |
| 7:Native_App Python Agent | ✅ 完成 | `native_agent/`,Python sidecar(遵 W1);12 故障注入測試 |
| 8:Rollout 與治理 | ✅ 完成 | `control_plane/rollout.py`;OIDC 待外部身分提供者 |

| UI-1:裝置管理整合(management API + 進度事件) | ✅ native_Provision 側完成 | `native_agent/{operations,management,management_api}.py`;24 測試;**nativeApp 殼接入未動**(任務卡 `11_TASK_UI1.md`) |
| UI-2:統一應用 catalog | ⬜ | 依賴 UI-1 view model |
| UI-3:Fleet Console 正名與瘦身 | ⬜ | `:8090` 只留 Fleet 級功能 |
| UI-4:原生殼整合 | ⬜ | 依賴 Rust 殼解凍;只換 UI 容器 |

> 全 8 slice 已實作並測試;外部才能驗收的缺口(P1 真實 repo、P3 實體 PG/MinIO、
> P2 Ed25519、OIDC、Rust 殼)集中列於 `IMPLEMENTATION_STATUS.md` §3。
> GUI 產品 IA 已由 `09_GUI_INTEGRATION.md` 收斂:現有 Console/Portal 是 lab 形態,
> 正式產品為 Management Center(裝置端)+ Fleet Console(中央)兩層。
