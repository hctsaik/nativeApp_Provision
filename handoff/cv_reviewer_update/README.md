# CV Reviewer 長期更新系統 — AI Handoff

> ⚠️ **本資料夾已由 [`../cv_reviewer_update_final/`](../cv_reviewer_update_final/00_START_HERE.md) 取代。**
> 開發請只讀 final 資料夾;本資料夾保留作歷史脈絡,兩者矛盾時一律以 final 為準。

這個資料夾是提供給下一位開發 AI 的自足入口。目標不是重新討論架構，而是接續實作：

> 讓 `cv_reviewer` 以不可變版本套件發布到 Registry／MinIO，由 Native_App 安全下載、驗證、預熱、原子啟用；失敗時使用 last-known-good。後續相同機制可服務更多應用。

## 建議閱讀順序

1. [DEVELOPMENT_KICKOFF.md](DEVELOPMENT_KICKOFF.md)：審閱後定稿的開發入口、阻斷前置與下一步。
2. [NATIVE_APP_GUI_INTEGRATION.md](NATIVE_APP_GUI_INTEGRATION.md)：Native App Management Center、Fleet Console 與 GUI 整併決策。
3. [ARCHITECTURE_AND_DECISIONS.md](ARCHITECTURE_AND_DECISIONS.md)：目標架構、已拍板決策與責任邊界。
4. [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md)：原始垂直切片與驗收條件；若有差異以 Kickoff 為準。
5. [CURRENT_STATE_AND_FILE_MAP.md](CURRENT_STATE_AND_FILE_MAP.md)：目前已完成的程式、測試及原 repo 文件位置。

資料夾內 Markdown 是權威來源；`cv_reviewer_update_ai_handoff.zip` 是 generated artifact。任何文件更新後都必須重建 ZIP。

## 交接摘要

- 正式長期 UI：中央 Web Console；Native_App 內建使用端更新 UI。
- Tkinter：只保留現有離線建置、bootstrap 與診斷用途。
- 正式資料層：PostgreSQL（或公司指定 Oracle）+ MinIO。
- 本機開發替身：SQLite + filesystem object store，現已可執行。
- 套件格式：有版本、不可變、可驗證的 `.napp`；DB 不保存 Python 原始碼。
- Native_App 只從本機 cache 執行，不直接執行 MinIO／DB／共享磁碟上的 `.py`。
- 更新採 desired state、staging、SHA-256／簽章、healthcheck、atomic activation、rollback。
- 現有 `native_Provision` dep-pack、big-deps、apply、warmup、Tauri E2E 必須重用。

## 下一個 AI 的第一個任務

**Slice 1.5：Domain hardening**（不是直接做 HTTP Control Plane）。
詳見 [DEVELOPMENT_KICKOFF.md](DEVELOPMENT_KICKOFF.md) §13 與
final 資料夾的 `05_TASK_SLICE_1_5.md`。

之後的 HTTP Control Plane（Slice 2）：API 與 GUI 必須呼叫既有
`PackageService`，不可自行重寫 object key、SQL 或 checksum 邏輯。
