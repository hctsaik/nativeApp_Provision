# IMPLEMENTATION_STATUS — Slice 1–8 + UI-1 實作總表

> 更新:2026-07-12(UI-1 native_Provision 側完成後)。Slice 1–8 + UI-1(裝置端管理
> 服務)已實作並測試(全 repo **335 passed / 18 skipped**)。
> 18 skipped = 原有 6 + PostgreSQL/MinIO contract 12(本機無 docker,CI 才跑)。
>
> **第四輪:UI-1 native_Provision 側(335 passed)**。把 Native Agent 整理成
> Native App Management Center 可消費的非同步 application management service:
> `native_agent/operations.py`(stage 對照表 + `OperationRunner` + per-app lock +
> 取消)、`state.py` 加 `operation_events`/cancel/kind 並在 journal 三方法自動發
> 結構化進度事件、`agent.py` 拆 `plan_update`/`execute_update`(行為不變)+ CANCELLED、
> `management.py`(`ApplicationManagementView` + service)、`management_api.py`
> (device-local `/management` JSON API + RBAC)、Portal 改走同一 service(舊測試零改)、
> `lab_serve` 於 :8091 掛 `/management`。新增 24 測試(service 13 + api 11)。
> **僅 native_Provision 側;nativeApp 殼接入(iframe/畫面)未動(見 `10` §2)。**
>
> **第二輪補齊(對照各 slice 驗收標準)**:並發 publish/promote 與中斷上傳測試
> (順帶修掉 `FileObjectStore.put` 的不可變性 bug + race-safe `_path`)、Agent
> compatibility(platform/ABI)檢查與裝置端版本/venv GC、rollout approval gate 與
> key rotation(`MultiKeyVerifier` trust-store)、Build 紀錄持久化、rollout/devices/
> builds 的 HTTP routes、Web Console 升級為 control surface(Builds/Rollout 頁 +
> promote/yank/start-rollout POST 表單)、HTTP artifact_missing error-path 測試。
>
> **第三輪:整條 SOP 全 GUI 化(311 passed)**。原本只有 CLI 的步驟都補了瀏覽器做法:
> Console 加 **Build &amp; publish**(GUI 觸發 Build Worker,`POST …/build`)、rollout
> **advance/approve/pause/resume** 按鈕、**register device** 表單;新增裝置端
> **Device Portal**(`native_agent/portal.py`,:8091)——立即更新 / 退回 / 修復 / 清理
> 皆可點,對應 `-m native_agent` 的 CLI。`demo/lab_serve.py` 現在一鍵起三個服務
> (Console :8090 / Device Portal :8091 / Control Plane :8080)。導覽 HTML(Artifact)
> 已更新為「每步都有 GUI 做法 + CLI 對照」。
> 核心設計決策:**全程 stdlib、零第三方相依**(FastAPI/psycopg/minio/pydantic 皆含原生碼,
> 本機 WDAC 下有執行風險且違反離線可測原則)。HTTP、Agent、Build Worker、Console 一律
> 用 `http.server`/`sqlite3`/`urllib`/`zipfile`,離線可建可測。

## 1. 交付的程式(依 slice)

| Slice | 位置 | 內容 | 測試 |
|-------|------|------|------|
| 1 / 1.5 | `src/provision_builder/package_services.py`、`package_errors.py`、`local_services.py` | Registry/ObjectStore、SQLite/File 實作、PackageService、12 error taxonomy、identifier、yank、GC 支援(iter_keys/all_object_keys) | `test_package_services.py`、`test_package_errors.py` |
| 2 | `control_plane/`(`http_api.py`、`server.py`、`__main__.py`) | stdlib http.server Control Plane;route→PackageService,domain error→HTTP status 集中映射;publish/list/promote/yank/resolve/download-url/download/list-apps | `test_control_plane.py`(含 live socket smoke) |
| 3 | `src/provision_builder/blob_store.py`、`maintenance.py`;`remote_adapters/`(`postgres.py`、`minio_store.py`);`docs/REGISTRY_LOGICAL_SCHEMA.md`;`deploy/local/init-registry.sql` | content-addressed blob store(去重/hardlink 組裝)、安全 GC、PostgreSQLRegistry、MinioObjectStore(client 端 SHA-256)、參數化 contract tests | `test_blob_store.py`、`test_maintenance.py`、`test_registry_contract.py`、`test_object_store_contract.py` |
| 4 | `src/provision_builder/napp/`(`manifest.py`、`schema.py`、`signing.py`、`builder.py`、`reader.py`、`_layout.py`、`schemas/*.json`);`docs/adr/0001-package-signing.md` | `.napp` 組包/驗證/source install、app.json+package.json JSON-Schema-subset 驗證、blob-references(big-deps 不內嵌)、checksums、canonical digest、簽章(DevHmacSigner + Ed25519 介面) | `test_napp.py` |
| 5 | `build_worker/`(`worker.py`) | 隔離 workspace、JSONL structured log、可取消 subprocess tree(Win/POSIX)、validate→selfcheck→build→verify→healthcheck→publish→promote pipeline | `test_build_worker.py`(含真實 subprocess 取消/逾時) |
| 6 | `web_console/`(`console.py`、`server.py`) | server-rendered HTML console,經 Control Plane API 讀取(無 DB/MinIO 憑證),Applications/Releases/Channels | `test_web_console.py` |
| 7 | `native_agent/`(`agent.py`、`state.py`) | 裝置端更新狀態機:desired/observed、只下載缺的 blob、verify(hash+簽章)、source install、venv fingerprint 重用、atomic activation、LKG 晉升、rollback、失敗記憶、開機 reconcile | `test_native_agent.py`(12 故障注入) |
| 8 | `control_plane/rollout.py` | device/group、確定性 bucket 分批 rollout(10→50→100)、失敗率 auto-pause、audit log、RBAC(RoleBasedAuthorizer)、desired-state per device | `test_rollout.py` |
| e2e | `tests/test_end_to_end.py` | Build Worker → registry/blobs → Agent 全鏈:build+publish+promote → 裝置 resolve/download/verify/install/activate;source-only 重用 venv+blob;竄改版不動 active | ✅ |

第二輪新增檔案:`build_worker/records.py`(BuildRecordStore)、`control_plane/rollout.py`
的 approval/approve + `MultiKeyVerifier`(`napp/signing.py`)、`control_plane/http_api.py`
的 governance routes、`web_console/` 的 POST 控制面;新增測試
`test_concurrency_and_interruption.py`、`test_agent_compat_gc.py`、
`test_control_plane_governance.py`,並擴充 `test_napp.py`/`test_rollout.py`/
`test_web_console.py`/`test_build_worker.py`/`test_control_plane.py`。

## 2. 對照原始「不可違反決策」與審閱建議

- 決策 #1/#2/#3(DB 只存 metadata、immutable object、不執行遠端 py):維持;`.napp` 有版本不可變,agent 只從本機 cache 執行。
- 決策 #8(fingerprint 重用 venv)、big-deps 不內嵌(審閱 B1):`napp` 產 `blob-references.json`,agent 只下缺的 hash、fingerprint 未變不重建 venv — 有 e2e 測試證明。
- 決策 #9(desired state 分批,非一次性命令):Slice 8 rollout 以 stage percent + bucket 表達。
- 決策 #10(GUI/CLI/API 共用 service 層):Control Plane 與 Console 皆只呼叫 `PackageService`/API,不碰 registry/DB。
- 決策 #12(production 驗簽):`.napp` 簽章層 + P2 ADR(Ed25519)完成,DevHmacSigner 離線測試。
- 審閱 B2(release_id 不存在):API 全用 `(app_id, version)`。
- 審閱 B3(channel pointer equality):agent 以相等性判斷更新,rollback = channel/pointer 指回。
- 審閱 B4(LKG 晉升 + 斷電 reconcile):Slice 7 journal + reconcile 實作並測試。
- 審閱 C1(publish 順序 + error taxonomy):Slice 1.5 已修。
- 審閱 C2(MinIO ETag≠SHA-256):`MinioObjectStore.put` client 端串流自算 sha256。

## 3. 尚未收尾的缺口

### 3a. 需外部條件(本機無法驗,非能力問題)

| 缺口 | 為何卡住 | 已就緒的部分 | 收尾方式 |
|------|----------|--------------|----------|
| **真實 cv_reviewer E2E**(P1) | 不知 repo 真實路徑/entrypoint;**不可猜** | napp builder、source install、Build Worker 全通用,已用 fixture app 驗 | 向使用者確認 repo 後,以真實 app + 斷網 apply/warmup/Tauri E2E 收尾 |
| **PostgreSQL/MinIO 整合**(P3) | 本機無 docker | adapters 已寫、contract tests 已參數化(12 skip) | 在有 docker 的 CI 設 `PROVISION_PG_DSN`/`PROVISION_MINIO_*` env,同一組 contract tests 轉綠 |
| **Ed25519 production signer**(P2) | 需含原生碼的 `cryptography`,WDAC 下不宜引入 | `.napp` 格式、`signature.json` schema、sign/verify 介面、`MultiKeyVerifier` rotation、ADR 全定案;DevHmacSigner 離線測試 | 在簽章服務/CI 加一組 `Ed25519Signer/Verifier`,格式與呼叫點不變 |
| **OIDC 身分** | 需身分提供者 | `Authorizer` protocol + `RoleBasedAuthorizer` + approval gate 已實作並測試 | 接真實 OIDC:驗 token→actor/roles,注入同一 `Authorizer` 介面 |
| **Rust/Tauri 殼解凍** | WDAC 擋 cargo、無建置機 | Agent 走 Python sidecar,完全不依賴殼重編 | 取得建置機後另案處理,不阻塞本系統 |
| **前端建置鏈(Vite/npm)** | WDAC 下未驗證 | Console 走 server-rendered + POST 表單,零 npm | 若日後要 SPA,先實測 W2;目前不需要 |

### 3b. 本機做得到、刻意留待與真實資源整合的(誠實揭露,非已完成)

| 項目 | 現況 | 為何留待 |
|------|------|----------|
| **真實 `core.deppack` 整合** | Slice 4 目前用**合成的最小 dependency-manifest**(schema+requires+wheels),非呼叫既有 `core.deppack`/dep-pack 建置 | 需搭真實工具的 requires 與平台 dep-pack 流程;等 P1 真實 repo 一起接,才有意義的相依可打 |
| **Build Worker 的 git checkout 步驟** | 目前吃「已 checkout 的 source_dir」,無 `git clone <commit>` 步驟 | checkout 是 pluggable 前置;真實建置在 CI/Worker 機接 git,介面已預留 |
| **裝置端下載續傳(resume)** | 目前每次重新下載整包;lab ObjectStore 無 range 讀取 | 續傳需 range-capable 傳輸(MinIO/HTTP Range);換上真實傳輸即可加,結構已支援 staging `.part` |
| **裝置端 blob GC** | `agent.gc()` 回收舊版本目錄與未用 venv;**blob 目前不回收**(content-addressed 共享,保留安全) | 需 per-version blob 參照追蹤才能安全 prune;非空間主要來源,優先度低 |

## 4. 如何跑起來(lab)

```powershell
# 全測試
py -3.11 -m pytest tests

# Control Plane
py -3.11 -m control_plane --root .lab-cp --port 8080
# Web Console(另一個 process;需自行接 in_process 或 http fetch,見 web_console/server.py)
```

## 4b. GUI 產品 IA 收斂(已採納規格,尚未實作)

`09_GUI_INTEGRATION.md`(2026-07-11 採納)確立:現有 Console(:8090)+
Device Portal(:8091)是 **lab 形態**,不是最終產品 IA。正式桌面入口統一為
**Native App Management Center**:裝置端區整合 Portal 的 update/rollback/
reconcile/GC,依角色另顯示中央 **Fleet** 區。`:8090` 正名 Fleet Web Console,
只保留遠端管理/Lab 與 Fleet 級功能,不是桌面管理員必須另開的入口。
Device Portal 降為診斷工具;一般使用者不開 `:8090`/`:8091`、不輸入 CLI。

遷移順序 UI-1 → UI-4。**UI-1 的 native_Provision 側已完成**(照 `11_TASK_UI1.md`):

1. ✅ Agent 非同步操作 + 結構化進度事件(整數 operation_id / stage / percent /
   message_key / can_cancel)—— `operations.py` + `state.operation_events`;
   `update()` 拆成 `plan_update`+`execute_update`,行為與拆分前一致。
2. ✅ `/management/...` device-local API + per-app mutation lock + 合作式取消 + RBAC。
3. ✅ `ApplicationManagementView` + `ApplicationManagementService`(agent state +
   remote release + 可選 catalog;無 `cv-reviewer` 特判,第二 app 測試證明)。
4. ⬜ **跨 repo 部分**(從 Native App 殼進入、iframe/bridge route、Management Center
   畫面)在 `nativeApp`,**未動**;`/management` API 與事件 schema 已備妥待接。

現有 `test_device_portal.py` 依驗收 #10 保留為 Agent diagnostic regression(零修改全綠);
Portal 已改為 `/management` 服務的 client。

## 5. 下一步建議

1. **Phase UI-1 native_Provision 側:✅ 完成**(照 `11_TASK_UI1.md`,335 passed)。
   **下一步 = 跨 repo 接入**:在 `nativeApp` 把 Management Center 的
   application list/detail 接到本 repo 的 `/management` API 與進度事件
   (iframe/bridge,不重編 Rust 殼);再做 UI-2 統一 catalog。
2. 向使用者確認 `cv_reviewer` repo(P1)→ 完成 Slice 4 真實 E2E。
3. 在 CI 拉起 PostgreSQL 16 + MinIO(釘 digest)→ 讓 12 個 skip 轉綠。
4. 實作 `Ed25519Signer/Verifier` 一組類別接上簽章服務。
