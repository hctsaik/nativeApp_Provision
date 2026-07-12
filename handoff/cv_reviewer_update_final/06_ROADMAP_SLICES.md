# 06 — Slice 2–8 任務卡

> ✅ **Slice 2–8 已全部實作並測試(完成當時 276 passed;現行基線見 `07_CHECKLISTS.md`)。**
> 本卡片保留作為各 slice 的原始需求與驗收準則對照。實際交付、程式位置與「外部才能
> 收尾」的缺口見 [`IMPLEMENTATION_STATUS.md`](IMPLEMENTATION_STATUS.md)。
> 註:Slice 2 卡片原寫 FastAPI 是初始規畫;**實作採 stdlib `http.server`**(見 IMPLEMENTATION_STATUS)。

> 通則:每個 Slice 都遵守 `07_CHECKLISTS.md` §1 的開發門檻。
> 每張卡片:目標 → 前置 → 工作項 → 驗收 → 地雷。

---

## Slice 2:HTTP Control Plane

**目標**:用 HTTP 完成與本機 CLI 相同的閉環(publish → promote → resolve → download),
錯誤碼穩定。

**前置**:Slice 1.5 完成(error taxonomy 是 HTTP 映射的基礎)。

**工作項**

1. 新開子專案資料夾 `control_plane/`(repo 內,**有自己的 requirements.txt**;
   FastAPI 允許在這裡,`src/provision_builder/` 仍零第三方)。
2. 路由(全部用 `(app_id, version)`,**沒有 release_id**):

```text
POST /api/v1/applications/{app_id}/releases/{version}          # publish(multipart 上傳 .napp)
GET  /api/v1/applications/{app_id}/releases                    # list
POST /api/v1/applications/{app_id}/releases/{version}/promote  # body: {"channel": "..."}
POST /api/v1/applications/{app_id}/releases/{version}/yank
GET  /api/v1/applications/{app_id}/channels/{channel}          # resolve → release JSON(含 status)
POST /api/v1/artifacts/{app_id}/{version}/download-url         # 回下載 URL(lab 版可回本服務的直接串流路由)
GET  /api/v1/artifacts/{app_id}/{version}                      # lab 直接下載串流
```

3. 所有 route handler **只呼叫 `PackageService`**;不寫 SQL、object key、hash 邏輯。
4. exception handler:`PackageDomainError` → `03_DOMAIN_SPEC.md` §4 的 HTTP 映射表,
   body 固定 `{"error": {"code", "message"}}`。
5. 測試用 FastAPI TestClient,不需真的開 port。

**驗收**

- HTTP 閉環等價 CLI 閉環;下載檔 SHA-256 與 publish 時一致。
- 重複版本、缺 artifact、錯 hash、未知 channel、yanked:HTTP status 與
  error code 與映射表逐一相符(每種至少一個測試)。
- `py -3.11 -m pytest tests` 基線不退。

**地雷**

- ❌ 在 handler 裡 try/except 個別 domain error 再自行決定 status——
  只允許一個集中 exception handler 查表。
- ❌ 把 control_plane 的相依裝進全域 Python;用獨立 venv。

---

## Slice 3:PostgreSQL / MinIO adapters

**目標**:正式資料層可插拔,與 SQLite / filesystem 通過**同一組 contract tests**。

**前置**:P3(外部 PG / MinIO endpoint,環境變數指定;本機無 docker)。

**工作項**

1. 依 `docs/REGISTRY_LOGICAL_SCHEMA.md` 寫 migration(修正 `init-registry.sql`
   與 logical schema 的差異,含 status 值域)。
2. `PostgreSQLRegistry`(保留未來 `OracleRegistry` 邊界)、`MinioObjectStore`。
3. **MinioObjectStore.put 必須 client 端串流計算 SHA-256 與 size**
   (multipart ETag 不是 SHA-256)。
4. contract tests 參數化:同一組測試跑 SQLite+filesystem 與 PG+MinIO
   (後者以環境變數 gate,無 endpoint 時 skip 並明確標示)。
5. Unreferenced object 的安全 GC(只刪無 release 引用且超時限者)。
6. Compose image 釘版本或 digest,不用 `:latest`。

**驗收**

- 兩組 adapter 契約測試全綠;大檔 multipart 上傳後 sha256 正確。
- Concurrent promotion、上傳中斷、竄改、pre-signed URL 過期各有測試。
- 本機(無 docker)跑測試:PG/MinIO 測試 skip 但不 fail。

---

## Slice 4:cv_reviewer application package

**目標**:對真實 cv_reviewer 產出 `.napp`,完全斷網 apply / warmup / Tauri E2E 通過。

**前置**:**P1(使用者確認 repo 資訊)+ P2(簽章 ADR 核准)**。兩者缺一不可。

**工作項**

1. `app.yaml` / `package.json` 的 JSON Schema(identifier 規則引用 §2 同一 regex)。
2. 驗 `app.yaml` / `plugin.yaml` 的 id、version、entrypoint、requires。
3. **Source install / assemble**(`apply.py` 目前不套 source-packages,這裡補):
   驗 source manifest → 安裝到 `versions/<version>.staging` → 不切 active。
4. Blob reference manifest:big-deps 不內嵌,產 `blob-references.json`
   (sha256 清單),blob 本體進 `blobs/sha256/`。
5. 重用 dep-pack 產 `dependency-manifest.json`(core.deppack 格式,不另創)。
6. `.napp` 組包 + `checksums.json` + `signature.json`(依 ADR)。

**驗收**

- 真實 cv_reviewer 產包後,在乾淨目錄、`PIP_INDEX_URL` 指死位址下:
  apply → warmup → Tauri E2E 全通過(沿用 `SPEC.md` §17–18 的驗證手法)。
- 只改 source 重新產包:dependency fingerprint 不變、blob 不重下、venv 重用。

**地雷**

- ❌ 只組 `.napp` 就宣稱「完整離線安裝完成」——source install 是本 Slice 的核心。
- ❌ 把 torch 等大 wheel 塞進 `.napp` 本體。

---

## Slice 5:Build Worker

**目標**:從 commit 到 staging artifact 的全自動建置。

**流程**:checkout commit → validate contract → build source/deps →
offline selfcheck → 隔離 apply/warmup → healthcheck / Tauri E2E →
manifest / 簽章 → upload staging → register build result。

**要求**:job workspace 隔離、可取消完整 subprocess tree、structured log、
保留測試報告與截圖(進 MinIO)。

---

## Slice 6:Web Console

**前置**:W2 — 先實測 WDAC 下 Node/Vite/esbuild 能否執行;
不行就 server-rendered(FastAPI + Jinja)或 CI 產靜態檔。

**第一版頁面**:Applications、Builds、Releases、Channels。
GUI 只呼叫 Control Plane API,**不持有 DB / MinIO credential**。

**驗收**:發布人員只用 Web GUI 即可選 commit、建置、看 log、驗證、
promote staging / production。

---

## Slice 7:Native_App Python Agent

**約束**:W1 — Python sidecar,不碰 Rust 殼。

**工作項**:本機 SQLite state(journal 欄位見 `02_ARCHITECTURE.md` §7)、
desired/observed state、下載續傳、hash/簽章/compatibility 驗證、
blob cache、source install、venv fingerprint、warmup、healthcheck、
atomic activation、LKG 晉升(§8 條件)、rollback、GC、開機 reconcile。

**驗收故障注入**(任何情況不得破壞 active version 與使用者資料):

- Registry 斷線、MinIO 斷線 → START_CACHED
- 下載中斷 → 續傳或乾淨重來
- object 損毀 → HashMismatch,拒絕啟用
- healthcheck 失敗 → rollback + failed 記錄(不無限重試)
- 啟用中斷電 → 開機 reconcile 修復

---

## Slice 8:Rollout 與治理

Device identity / groups、10% → 50% → 100% 分批 rollout、失敗率門檻與自動
pause、OIDC / RBAC / approval / audit、trusted publisher 與簽章輪替。
(細節屆時再展開;先不設計。)
