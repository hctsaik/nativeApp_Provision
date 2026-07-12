# 00 — START HERE(開發 AI 唯一入口)

> 專案:CV Reviewer 長期更新系統(Registry / MinIO / Native_App 安全更新)
> 定稿日期:2026-07-11
> 本資料夾 = **唯一權威**。`../cv_reviewer_update/` 是歷史草稿,只供追溯,不要照它開發。

## 0. 你是誰、你要做什麼

你是接手開發的 AI。目標系統一句話:

> 讓 `cv_reviewer` 以「不可變、有版本、可驗證」的套件發布到 Registry/MinIO,
> 由 Native_App 安全下載、驗證、預熱、原子啟用;失敗時自動退回 last-known-good。

你**不需要重新討論架構**。所有架構決策已拍板(見 `02_ARCHITECTURE.md`),
你的工作是照 `05_TASK_SLICE_1_5.md` 開始逐一實作。

## 1. 閱讀順序(照做,不要跳)

| 順序 | 檔案 | 何時讀 |
|------|------|--------|
| 1 | `01_CONSTRAINTS.md` | **現在**。硬約束與禁止事項,違反任何一條 = 打掉重做 |
| 2 | `02_ARCHITECTURE.md` | 現在。目標架構、package 格式、狀態機 |
| 3 | `03_DOMAIN_SPEC.md` | 現在。資料模型、identifier、error taxonomy(實作要逐字遵守) |
| 4 | `04_CODE_MAP.md` | 現在。現有程式在哪、哪些能力不可重造、已知缺口 |
| 5 | `05_TASK_SLICE_1_5.md` | 讀完 1–4 後。**你的第一個實作任務**,含逐步指示 |
| 6 | `06_ROADMAP_SLICES.md` | Slice 1.5 完成後。後續 Slice 2–8 任務卡 |
| 7 | `07_CHECKLISTS.md` | 每個 Slice 開始前與結束前各查一次 |
| 8 | `08_GLOSSARY.md` | 遇到不懂的名詞時查 |
| 9 | `09_GUI_INTEGRATION.md` | **任何 GUI 工作(畫面/路由/入口)動手前必讀**。GUI 產品邊界:裝置端整合進 Native App Management Center、`:8090` 正名 Fleet Console、Device Portal 降為診斷;含 UI-1~UI-4 遷移順序與「與現有程式的對照」附錄 |
| 10 | `10_AI_UI1_DEVELOPMENT_GUIDANCE.md` | **UI-1 實作前必讀**。View Model、非同步 operation、進度事件、併發鎖、RBAC、跨 repo 工作拆分與必測場景。 |
| 11 | `11_TASK_UI1.md` | **UI-1 的實作任務卡(照這份動手)**。已定案設計決策(stage 對照表、整數 op_id、取消機制、RBAC v1)、文件校正清單、逐檔步驟、必寫測試與驗收 |
| 12 | `12_PLATFORM_APP_MODEL.md` | **平台 App 管理與開發模型(multi-agent 綜合設計)**。回答「任何工具(AI4BI/Annotation/…)怎麼上架」:雙軌模型與軌道判準、通用上架契約、identity mapping ADR 建議、Phase 0-3 遷移路線、平台功能總覽。**規劃任何新 app 上架或動兩套 pipeline 前必讀** |

## 2. 絕對規則(每一條都是強制)

1. **不猜**。路徑、repo 名稱、entrypoint 只要文件沒寫死,就直接問使用者。
   特別是 `cv_reviewer` 的真實 repo:**絕對不可**猜成 `CV_Viewer` 或工作區其它 CV 專案。
2. **不加第三方套件到 `src/provision_builder/`**。runtime 是 stdlib-only,
   FastAPI 等只能出現在獨立的 control plane 子專案(見 `06_ROADMAP_SLICES.md` Slice 2)。
3. **不改既有離線 USB provision 流程的行為**(build / verify / apply / warmup)。
   新系統是「編排」既有能力,不是取代。
4. **不覆蓋 immutable release**。修正內容 = 出新版本,永遠不覆蓋舊 object。
5. **每次動手前後都跑測試**(命令見 `07_CHECKLISTS.md`)。
   基線:`py -3.11 -m pytest tests` → 全 repo 335 passed / 18 skipped
   (2026-07-12 實測;18 skip = PG/MinIO contract,需 docker/CI。動手前重跑確認)。
6. **文件衝突時**:本資料夾 > repo 內 docs > `../cv_reviewer_update/`。
   若本資料夾內部自相矛盾 → 停止,寫一頁 ADR 到 `docs/adr/`,請使用者定奪。
7. **每完成一個 Slice**:更新 `04_CODE_MAP.md` 的現況節,重建 handoff zip
   (指令在 `07_CHECKLISTS.md` §4)。zip 是 generated artifact,以資料夾為權威。
8. Python 一律用 `py -3.11` 呼叫。本機**沒有 docker**、**WDAC 擋 cargo**(詳見 `01_CONSTRAINTS.md`)。

## 3. 現在的進度

**Slice 1–8 全部完成 + UI-1(native_Provision 側)已實作並測試(全 repo 335 passed / 18 skipped)。**
含第二輪驗收補齊(並發/中斷、compatibility、device GC、approval、key rotation、
build 紀錄、governance HTTP routes、Console 控制面)與 UI-1 的裝置端管理服務
(非同步 operation + 進度事件、`/management` API、per-app lock、取消、RBAC)。
完整實作狀態、每個 slice 的產物與「外部才能驗收」的缺口見
[`IMPLEMENTATION_STATUS.md`](IMPLEMENTATION_STATUS.md)。

- ✅ Slice 1 / 1.5:package domain + hardening(error taxonomy、identifier、yank、logical schema)
- ✅ Slice 2:HTTP Control Plane(`control_plane/`,stdlib http.server)
- ✅ Slice 3:blob store + 安全 GC + 參數化 contract tests;PG/MinIO adapters(`remote_adapters/`,env-gated)
- ✅ Slice 4:`.napp` 組包 + schema + 簽章(`provision_builder/napp/`)
- ✅ Slice 5:Build Worker(`build_worker/`)
- ✅ Slice 6:Web Console(`web_console/`,server-rendered)
- ✅ Slice 7:Native_App Python Agent(`native_agent/`,狀態機 + reconcile)
- ✅ Slice 8:Rollout 與治理(`control_plane/rollout.py`)

**外部才能收尾的項目**(非本機能驗,已在 `IMPLEMENTATION_STATUS.md` §缺口 詳列):
真實 `cv_reviewer` repo E2E(P1)、實體 PostgreSQL/MinIO 整合(P3,需 CI/docker)、
Ed25519 production signer(P2 ADR 已定,待簽章服務)、OIDC 身分提供者、Rust 殼解凍。

**GUI 下一步 = Phase UI-1(不是再加畫面)**:已採納 `09_GUI_INTEGRATION.md` 的
產品 IA 收斂——現有 Console(:8090)/Device Portal(:8091)是 lab 形態,
正式產品要把裝置端管理整合進 Native App Management Center。
**實作直接照 `11_TASK_UI1.md` 做**(它已把 10 號指引落成逐檔步驟與已定案決策;
第一步是其中 Part A 的文件基線校正)。缺口分析見 09 附錄 A2。

## 4. 卡住時的求助格式

問使用者時,一次問清楚、給選項,例如:

> Slice 4 需要 cv_reviewer 真實 repo 資訊才能繼續:
> (a) repo 絕對路徑?(b) plugin.yaml 位置?(c) entrypoint?
> 在你回答前我先繼續做不受影響的 ___。

不要空等;先做不被阻斷的部分。
