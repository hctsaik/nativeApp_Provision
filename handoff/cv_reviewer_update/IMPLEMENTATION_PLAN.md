# 實作計畫與驗收

## Slice 1：本機 package domain（已完成）

- `Registry`／`ObjectStore` protocols。
- `SQLiteRegistry`／`FileObjectStore`。
- `PackageService.publish()`／`download()`。
- Immutable object、channel promotion、SHA-256 download。
- CLI 可執行 publish → promote → resolve → download。

驗收現況：5 個聚焦測試、全 repo 166 passed／6 skipped。

## Slice 2：HTTP Control Plane

建議 FastAPI，但 framework 必須在獨立 Control Plane 專案，不可破壞 `native_Provision` runtime 零第三方相依。

最小 API：

```text
POST /api/v1/releases
GET  /api/v1/applications/{app_id}/releases
POST /api/v1/releases/{app_id}/{version}/promote
GET  /api/v1/applications/{app_id}/channels/{channel}
POST /api/v1/artifacts/{release_id}/download-url
```

驗收：透過 HTTP 完成與本機 CLI 相同閉環；重複版本、缺 artifact、錯 hash、未知 channel 都有穩定 error code。

## Slice 3：正式 adapters

- `PostgreSQLRegistry`，保留未來 `OracleRegistry` 邊界。
- `MinioObjectStore`。
- 以相同 contract tests 驗 SQLite/PostgreSQL、filesystem/MinIO。
- Upload staging → remote verify → release record → channel promotion。

驗收：Compose integration test、concurrent promotion、上傳中斷、竄改、pre-signed URL 過期。

## Slice 4：Application package

- `app.yaml` JSON Schema。
- `package.json` JSON Schema。
- 檢查 `app.yaml`／`plugin.yaml` 的 id、version、entrypoint、requires。
- 組合現有 source package、dep-pack、big-deps 成 `.napp`。
- 精確 dependency lock、SBOM、build provenance。

驗收：以真實 `cv_reviewer` 產生 package，完全斷網 apply／warmup／Tauri E2E 通過。

## Slice 5：Build Worker

```text
checkout commit
→ validate contract
→ build source/deps
→ offline selfcheck
→ isolated apply/warmup
→ healthcheck/Tauri E2E
→ manifest/sign
→ upload staging
→ register build result
```

要求：job workspace 隔離、可取消完整 subprocess tree、structured log、保留測試報告與截圖。

## Slice 6：Web Console

第一版頁面：Applications、Builds、Releases、Channels。GUI 呼叫 Control Plane API，不持有 DB／MinIO credential。

驗收：發布人員只用 Web GUI 即可選 commit、建置、看 log、驗證結果、promote staging／production。

## Slice 7：Native_App Local Agent

- 本機 SQLite state。
- Desired／observed state。
- 下載續傳、hash／signature、compatibility。
- Version staging、venv fingerprint、warmup、healthcheck。
- Atomic activation、last-known-good、rollback、GC。

驗收故障：Registry 斷線、MinIO 斷線、下載中斷、object 損毀、healthcheck 失敗、啟用中斷電。任何情況不得破壞 active version。

## Slice 8：Rollout 與治理

- Device identity、groups。
- 10% → 50% → 100% rollout。
- 失敗率門檻、自動 pause。
- OIDC、RBAC、approval、audit。
- Trusted publisher 與簽章輪替。

## 開發規則

1. 每個 slice 先形成 API/schema，再做 adapter 與 GUI。
2. 每個正式 adapter 必須通過共用 contract tests。
3. 每個狀態轉移可重入；程序重啟後能從 SQLite 恢復。
4. 不把 production secret 寫入 repo、frontend 或 package。
5. 不覆蓋 immutable release；修正內容必須增加版本。
6. 不以 mock E2E 取代真實 Native_App/Tauri 驗收。
7. 不因長期架構而破壞現有 USB 離線 provision 流程。

