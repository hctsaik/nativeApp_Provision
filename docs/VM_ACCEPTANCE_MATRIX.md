# 乾淨 Windows VM 驗收矩陣（P3）

> **狀態**：清單已定，**尚未在乾淨 VM 上執行**（開發機無 VM 基礎設施；本檔是待執行的
> 驗收契約，不是已通過的紀錄）。執行後把結果欄填上日期與 PASS/FAIL，任何 FAIL 附 log 路徑。
> 對應 `NATIVEAPP_DEPLOYMENT_RECOMMENDATION.md` §8「發布前最低驗收標準」。

## 前置

- VM 基準：乾淨 Windows 10/11 x64，**沒有** Python、Node、Rust、Git；一般 User（非管理員）。
- 受測物：一份 `release.py build` 產出並 `verify` 通過的 release 目錄（USB 或共享資料夾搬入），
  以及一份 Store 佈局交付樹（`export_full_tree` 產出）。
- 每項測完記錄：日期／VM 快照名／PASS 或 FAIL／證據（截圖或 log 路徑）。

## 矩陣

| # | 情境 | 步驟 | 通過標準 | 結果 |
|---|------|------|----------|------|
| 1 | 零依賴安裝 | USB 複製 Store 樹 → 雙擊 `start-<app>.bat` | 首啟深驗跑完、App 視窗開啟；全程不裝任何東西、不需管理員 | 未執行 |
| 2 | 無 WebView2 | 在未裝 WebView2 的快照上啟動 | 明確的中文指引（含離線安裝檔位置），**不**誤判為版本壞掉、不觸發回滾 | 未執行 |
| 3 | SmartScreen / MotW | 從網路下載 zip 解壓後啟動 | 被 MotW 標記時有可照做的指引；解除後可啟動 | 未執行 |
| 4 | 離線 release 驗證 | 斷網，`runtime\python311\python.exe release.py verify <release>` | 逐檔 sha256 全過；故意改一個 byte → 非零 exit 且點名該檔 | 未執行 |
| 5 | 半份 USB 複製 | 複製到一半拔 USB，再啟動 | 首啟深驗抓到缺檔並 fail loud，不留半套可啟動假象 | 未執行 |
| 6 | App 執行中 staging | App 開著時放入新版本並 stage | current 檔案零位元組變動；關閉重開後才 promote | 未執行 |
| 7 | 壞版自動回滾 | stage 一個 health check 必失敗的版本 → 重啟 | 自動回到 LKG；壞版進 failed_versions 不再重試 | 未執行 |
| 8 | 更新中斷電 | promote 進行中強制關機（VM power off）→ 重啟 | 啟動到 LKG 或新版之一，絕不半套；state.json 完整 | 未執行 |
| 9 | Defender 即時保護 | 開啟即時保護下做 runtime 安裝與版本切換 | rename 被暫鎖時退避重試或 copy-verify fallback；不誤報失敗 | 未執行 |
| 10 | 三版連續更新 + GC | v1→v2→v3 各啟動一次後跑 GC | current/PREV/LKG/active lease/data 全保留；孤兒被回收；被鎖項進 deferred 並在下輪清掉 | 未執行 |
| 11 | production 驗簽 | 用 trust store 驗 production release；換一把不在 store 的鑰簽的包 | 前者 PASS；後者被拒且訊息點名 untrusted key_id | 未執行 |
| 12 | 中文/空白路徑 + 非 C 槽 | 部署到 `D:\測試 空白\` | 安裝、啟動、更新全部照常 | 未執行 |
| 13 | exFAT USB 搬運 | Store 樹經 exFAT USB 搬到 NTFS 後啟動 | 深驗通過（USB 上不執行，只搬運）；hardlink 失敗時訊息指引「先複製到本機 NTFS」 | 未執行 |
| 14 | UNC 中斷重試 | update source 指到網路共享，中途斷線 | 安全失敗、current 不動；恢復後重試成功（`.part` 續傳不重抓整包） | 未執行 |
| 15 | 平台 Store 化真機啟動 | `pack-platform` → release → Agent update → `platform_launcher`（不帶 --dry-run）| 真 cim-light.exe 開窗、engine 起、portal 載入；換版重啟後新 engine 生效且 `data\engine-default` 內容保留 | 未執行 |
| 16 | Store 通道簽章現場演練 | 裝 trust store + `require_signed_updates` → 餵一個重生 files.json 的假 payload | 更新被拒且訊息點名簽章不符；合法簽章版照常安裝 | 未執行 |

## 降級路徑

任一關鍵項 FAIL：退回現行全量 fat 包交付（schema v2 launcher 向下相容），
並把 FAIL 項開成缺陷單修復後重跑整張矩陣（不做單項豁免）。
