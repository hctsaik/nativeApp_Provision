# 07 — Checklist 與驗證

## 1. 每個 Slice 的開發門檻(開始前讀一次、結束前逐項勾)

1. [ ] 先更新 contract / ADR / acceptance criteria,再寫程式。
2. [ ] 順序:domain/service → adapter → GUI(不可跳過中間層直接接 GUI)。
3. [ ] 每個新行為有聚焦單元測試。
4. [ ] 正式 adapter 有 integration tests(可因無 endpoint 而 skip,不可 fail)。
5. [ ] 跑既有**不連網**回歸測試,不低於基線。
6. [ ] 涉及真實應用時,跑斷網 + Tauri E2E(手法沿用 `SPEC.md` §17–18)。
7. [ ] 更新 `04_CODE_MAP.md` §5 進度表。
8. [ ] 重建 handoff zip(§4)。

## 2. 驗證命令

```powershell
# 聚焦(新系統 domain)
py -3.11 -m pytest tests\test_package_services.py
# 全 repo 回歸
py -3.11 -m pytest tests
```

基線(Slice 1–8 + UI-1 native_Provision 側,2026-07-12):全 repo 335 passed / 18 skipped
(18 skipped = 原 6 + PG/MinIO contract 12,本機無 docker;CI 設 env 後轉綠)。
每完成一個階段、測試數增加後,更新本節與 `04_CODE_MAP.md` §3 的基線數字。

## 3. 第一階段 Definition of Done(全部成立才算「完成」)

- [ ] 發布人員能從中央 UI 對 cv_reviewer 建置、驗證、發布、promote。
- [ ] Native_App Python Agent 能依 production pointer 安全更新。
- [ ] 只改 source:不重下大 wheel、不重建相同 fingerprint 的 venv。
- [ ] Registry / MinIO 斷線時啟動 last-known-good。
- [ ] Hash / 簽章 / healthcheck 失敗時拒絕新版。
- [ ] 更新中斷或斷電後能 reconcile,active version 完好。
- [ ] 管理者能 rollback,且 data / config / projects 不受破壞。
- [ ] 現有 USB provision / apply / warmup 流程保持相容(回歸測試證明)。

## 4. 重建 handoff zip(文件更新後必做)

zip 是 generated artifact,資料夾內容才是權威。

```powershell
$src = "C:\code\claude\native_Provision\handoff\cv_reviewer_update_final"
$dst = "C:\code\claude\native_Provision\handoff\cv_reviewer_update_final_ai_handoff.zip"
if (Test-Path $dst) { Remove-Item $dst }
Compress-Archive -Path "$src\*" -DestinationPath $dst
```

## 5. ADR 流程(遇到文件衝突或未定決策時)

1. 停止該工作項(其它不受影響的照做)。
2. 在 `docs/adr/` 建 `NNN-短標題.md`:背景 → 選項 → 建議 → 影響範圍。
3. 請使用者定奪;核准後把結論回寫到本資料夾對應文件。
4. 不可靜默選邊、不可先做再補 ADR。

## 6. 常見失敗的正確反應

| 狀況 | 正確反應 |
|------|----------|
| 測試紅了 | 修到綠,或回報使用者;**不可**弱化斷言或 skip 掉 |
| 找不到 cv_reviewer repo | 問使用者(P1);不猜 |
| 想裝新第三方套件到 `src/provision_builder/` | 停,那是 stdlib-only;放 control_plane/ 或重想 |
| docker 不存在 | 預期行為;走環境變數外部 endpoint(P3) |
| cargo / npm 被 WDAC 擋 | 預期行為;見 `01_CONSTRAINTS.md` W1/W2 的替代路徑 |
| 舊文件(`../cv_reviewer_update/`)與本資料夾矛盾 | 以本資料夾為準 |
