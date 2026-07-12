# 01 — 硬約束與禁止事項

> 這一頁的每一條都經過確認。不要「順手優化」掉任何一條。

## 1. 環境硬限制(2026-07-11 實測確認)

### W1:WDAC 擋 cargo,且沒有無 WDAC 的建置機

- Native_App 的 Rust / Tauri 殼**凍結**,不可重編。
- 因此 **Local Agent 第一版必須是 Python sidecar / engine service**,
  走既有殼可用的 IPC 或啟動路徑。
- 任何 Slice 都**不得**把「修改或重編 Rust 殼」設為前置條件。
- Rust 殼解凍(取得建置機)是外部里程碑,不阻塞 Control Plane、Build Worker、
  package contract 與 Python Agent。

### W2:前端建置鏈未驗證

- Node / Vite / esbuild(原生 exe)在 WDAC 下能否執行**未實測**。
- Slice 6(Web Console)開工前必須先實測;若受阻,改用 server-rendered UI
  (FastAPI + Jinja),或在可建置的 CI / Worker 機器產靜態檔。

### W3:本機沒有 docker

- PostgreSQL / MinIO 的 integration tests 必須支援
  **以環境變數指定外部 endpoint**,可在 CI 或另一台機器執行。
- 本機持續跑 SQLite / filesystem 的 contract tests。
- 不要在此機嘗試安裝 docker(很可能同樣被 WDAC 擋)。

### W4:Python 版本

- 一律 `py -3.11`。runtime 目標是可攜 Python 3.11(平台 runtime\ 資料夾)。

## 2. 十二條不可違反的架構決策

1. DB 只保存 metadata,不保存 Python source 或 package binary。
2. Object store artifact 不可變;同一 `(app_id, version)` 不可覆蓋。
3. Runtime 不直接執行 DB、MinIO 或共享磁碟上的 Python;只從本機 cache 執行。
4. 下載一律先到 staging,驗證成功後才進本機版本 cache。
5. active version 只在 verify、dependencies、healthcheck 全部成功後切換(原子)。
6. Control Plane / MinIO 中斷時,已安裝的 last-known-good 必須仍可啟動。
7. 程式版本、venv、使用者資料三者分離;更新不能覆蓋 config / project data。
8. Python 相依依 dependency fingerprint 建 venv;只改 source 時重用既有 venv。
9. 正式 rollout 用 desired state(channel 指標),不對裝置送一次性「立即安裝」命令。
10. GUI、CLI、API 共用同一 application service 層,不複製建置/發布邏輯。
11. 既有 `core.deppack` 是 dependency manifest 的權威;不另創 wheel manifest。
12. 經 Control Plane 自動下載執行的 production package 最終必須驗簽;
    SHA-256 只證完整性,不證發布者身分。
    (既有內部 USB provision 仍照 `SPEC.md` D1,不強制簽章。)

## 3. 不可重造清單(這些已存在且經真實 E2E 驗證)

| 能力 | 位置 / 依據 |
|------|-------------|
| dep-pack 原生 manifest + fail-closed 驗證 | `core.deppack`(權威見 `SPEC.md` §2) |
| scan / build / verify / report | `native_Provision` 主流程(`SPEC.md` §4) |
| big-deps 隔離與去重 | `SPEC.md` §6 |
| source package + 逐檔 SHA-256 | `SPEC.md` §18 |
| stdlib-only `apply.py` 原子套用(dependency) | `SPEC.md` §9 |
| `warmup.py` 預建 per-tool venv | `SPEC.md` §17 |
| 真實 Tauri / Portal E2E + engine log 證據 | `SPEC.md` §17–18、`e2e/` |

新系統**編排**這些能力;不自行組另一套 pip、wheel manifest 或離線安裝格式。

## 4. 已知缺口(不要被舊文件誤導)

- **legacy `apply.py` 只處理 dependency cache,不套用 `source-packages`**(仍是手動)。
  新系統的 source install/assemble 已由 `provision_builder/napp/` + `native_agent` 實作(見 Slice 4/7)。
- SQLite 與 PostgreSQL schema 的欄位差異已在 `docs/REGISTRY_LOGICAL_SCHEMA.md` 收斂為單一權威。
- `.napp` assembly、JSON schema、開發簽章:**已實作**(`provision_builder/napp/`);
  production Ed25519 signer 待簽章服務(P2,ADR 0001 已定)。

## 5. 阻斷性前置(未滿足前,不進對應 Slice)

### P1 → 阻斷 Slice 4:確認 cv_reviewer 真實 repo

開始 Slice 4 前**直接問使用者**:

- repo 絕對路徑與正式名稱
- `plugin.yaml` 路徑
- entrypoint 與 category
- source root、assets、必須排除的本機資料
- config / project / cache / logs 的實際資料目錄
- requirements 與目前 data schema

### P2 → 阻斷 Slice 4 的格式定稿:簽章 ADR

`signature.json` 格式、演算法(優先評估 Ed25519)、canonical payload、
key ID 與 trusted public key 分發、rotation / revoke、dev 測試金鑰政策、
Build Worker 如何取用 signing key。ADR 未核准前可做 hash 與 package 原型,
但**不得凍結** production package format。

### P3 → 阻斷 Slice 3 的 integration 驗收:外部 PG / MinIO endpoint

本機無 docker;integration tests 走環境變數指向外部服務,在 CI 或別台機器跑。
Compose image 必須釘版本或 digest,不用 `:latest`。

## 6. 文件權威表

| 範圍 | 權威 |
|------|------|
| 現有 dep-pack、big-deps、apply、warmup、E2E | `native_Provision/SPEC.md` + 實際程式 |
| WDAC、Tauri、Native_App 擴充限制 | Native_App 上游 handoff + 實際程式 |
| cv_reviewer 實際入口與資料模型 | 經使用者確認的 cv_reviewer repo(P1) |
| Application package / Registry / Agent(新系統) | **本資料夾** + 後續核准的 ADR |
| 三 repo 整體計畫(上游) | `C:\code\claude\CV_Viewer\PLATFORM_ARCHITECTURE_AND_DEPLOYMENT.md` |

與上游文件衝突 → 停止,寫 ADR 到 `docs/adr/`,請使用者定奪;不可靜默選邊。
