# Streamlit Desktop 本輪測試總結與下一輪測試方向

更新日期：2026-07-15  
專案：`native_Provision` / Streamlit Desktop Store 交付模式  
實際測試專案：`C:\code\claude\CV_Viewer`

## 1. 本文件目的

本文件整理本輪自動測試、真實交付測試、測試期間發現的問題與已完成修正，並定義下一輪應執行的真實 Windows 驗收項目。

本輪的結論是：

- 程式邏輯與 Store 狀態機已通過完整自動回歸。
- 真實 CV Viewer 已完成冷建置、完整交付與更新流程測試。
- 下一輪不需要繼續盲目增加一般單元測試，應轉向乾淨 Windows VM、真實 Tauri 視窗、權限、防毒、WebView2 與異常中斷等使用者環境驗收。

---

## 2. 本輪測試範圍

### 2.1 完整 pytest 回歸

執行命令：

```powershell
py -3.11 -m pytest
```

最終結果：

```text
935 passed, 18 skipped in 394.61s (0:06:34)
```

結果判定：

- 935 項測試全部通過。
- 18 項為既有條件式略過，沒有測試失敗。
- 本輪修正後重新執行完整回歸，確認沒有破壞其他功能。

### 2.2 真實 CV Viewer 交付 E2E

執行命令：

```powershell
py -3.11 e2e\streamlit_desktop_delivery_e2e.py
```

執行時間：約 7 分鐘。  
結果：12 個階段全部通過。

實際驗證內容：

1. 從全新 Store 建置 `v1.0.0`。
2. 複製可攜式 Python 並實際執行 pip install。
3. 清除 runtime 內的 `.pyc`。
4. 產生並驗證 runtime `files.json` SHA-256 清單。
5. 匯出可交付給新電腦的完整資料夾。
6. 確認版本與 runtime 的 `.complete` sentinel 契約。
7. 執行 bootstrap `--status`。
8. 建置 `v1.1.0`，確認相同 lock 會重用既有 runtime。
9. 匯出更新包。
10. 在目標交付樹執行 `--install`，確認只設定 pending、不立即替換目前版本。
11. 測試 `--clear-pending` 與 `--set-pending`。
12. 測試沒有可退版本時的 rollback 訊息，以及 cp950 繁中主控台下的 GC。

完整交付測試產物：

```text
C:\code\claude\native_Provision\dist\e2e-deliver
```

---

## 3. 本輪發現與完成的修正

### 3.1 離線 UNC 更新來源造成 WinError

問題：

- 設定 `\\server\share` 類型的更新來源時，Windows 在 VPN 中斷、伺服器離線或網路名稱失效的情況下，不一定只回傳「路徑不存在」。
- `Path.exists()` / `Path.is_dir()` 可能直接拋出 `WinError 53`、`64` 或 `67`。
- 原本行為會讓設定更新來源的操作崩潰。

修正：

- 對路徑只執行一次受保護的 `stat()`。
- 如果確實連線成功且證明它是檔案，才拒絕設定。
- 如果 UNC 暫時無法連線，仍保存設定並提示警告。
- 這符合實際使用情境：目前離線不代表更新來源設定無效，VPN 或伺服器之後可能恢復。

回歸測試：

```text
test_set_update_source_accepts_a_unc_share_when_windows_stat_raises
```

### 3.2 Windows Defender 長時間鎖住 runtime，導致 rename 失敗

問題：

- 真實 CV Viewer runtime 安裝完成後約有數百 MB、數萬個檔案。
- Windows Defender 會掃描剛建立的 Python runtime。
- 即使原本已重試約 76 秒，整個 staging 目錄仍可能因 `WinError 5` 無法 rename 成正式 fingerprint 目錄。
- 這不是 runtime 建置失敗；pip、import 驗證和 files.json 都已完成，只是目錄 rename 被防毒軟體阻擋。

修正後的發布流程：

```text
完整建置 staging
    ↓
產生 files.json
    ↓
優先嘗試目錄 rename
    ↓ rename 長時間被阻擋
安全複製到正式 fingerprint 目錄
    ↓
對複製後的正式目錄重新驗證 files.json
    ↓
驗證完全通過後，最後才寫入 .complete
```

安全性保證：

- `.complete` 是最後的 commit record。
- 複製中斷或電腦斷電時，正式目錄不會有 `.complete`。
- launcher/runtime store 不會使用沒有 `.complete` 的 runtime。
- 下一次建置會在鎖內清理不完整目錄後重試。

真實 E2E 已確認會實際進入這條 Defender fallback，且最後成功完成交付。

回歸測試：

```text
test_runtime_publish_falls_back_to_verified_copy_when_defender_blocks_rename
```

### 3.3 避免相同 runtime 被同時建置

問題：

- 兩個 GUI 或建置程序可能同時要求相同 fingerprint 的 runtime。
- 若沒有建置鎖，兩邊可能同時建立或發布同一個目錄。

修正：

- runtime 建置與發布期間持有 per-fingerprint lock。
- 取得鎖後重新檢查其他程序是否已完成 runtime。
- 若其他程序已完成，直接重用，不重複安裝。

### 3.4 鎖目錄被誤認成 runtime

問題：

- 如果 `.locks` 建立在 `deps/runtimes` 內，直接列舉子目錄的舊程式或測試可能把它當成第二個 runtime。

修正：

- runtime lock 移到 Store metadata 區：

```text
deps\.locks\runtimes\<fingerprint>.lock
```

- `deps\runtimes` 現在只保存 staging 與真正的 runtime 目錄。

---

## 4. 本輪測試已證明的能力

目前已有自動化證據支持以下行為：

- 同一份 dependency lock 可以重用同一個 runtime。
- 新版本可以只新增 application 版本槽，不必每次重建 Python libraries。
- 更新包安裝後只進入 pending，不會在 App 執行中直接取代目前版本。
- pending 可以取消，也可以重新指定。
- runtime 與版本資料夾都有完整性清單及 sentinel 契約。
- 不完整或複製中斷的 runtime 不會被當成可用 runtime。
- Windows Defender 阻擋 rename 時仍可安全完成發布。
- UNC 更新來源暫時離線時不會造成程式崩潰。
- 繁體中文 cp950 主控台不會因輸出字元造成 `UnicodeEncodeError`。
- 完整交付包可以在隔離的目標資料夾中執行 bootstrap 管理操作。

---

## 5. 本輪尚未完全證明的項目

本輪 E2E 主要驗證 Store、bootstrap、檔案完整性、更新狀態與交付資料夾，不代表以下真實桌面情境已全部驗收：

- 在完全乾淨、沒有開發工具的 Windows 電腦上雙擊 `start.bat`。
- 真實 Tauri WebView 視窗是否開到正確的動態 port。
- 使用者關閉視窗後，Streamlit/Python 是否完全結束。
- 沒有系統管理員權限時的完整行為。
- 沒有 WebView2、沒有網路時的首次啟動體驗。
- App 執行中下載更新、通知使用者、關閉後再切換的完整視覺流程。
- 更新或切版途中斷電、強制關機、程序被工作管理員終止。
- 中文 Windows 帳號、空白路徑、長路徑、唯讀位置。
- 多次連續更新後的磁碟空間與 staging 清理。

---

## 6. 下一輪測試方向

下一輪應定位為「真實 Windows 使用者驗收」，不是繼續增加大量一般單元測試。

### P0：發布前必測

#### TC-NEXT-001 乾淨 Windows VM 首次啟動

環境：

- 全新 Windows VM。
- 不安裝 Python、Git、Node.js、Rust 或本專案原始碼。
- 使用一般非系統管理員帳號。

步驟：

1. 將 `dist/e2e-deliver` 複製到 VM。
2. 中斷網路。
3. 雙擊 `start.bat`。
4. 等待 Tauri 視窗與 Streamlit 首頁。

通過條件：

- 不依賴系統 Python。
- 不需要系統管理員權限。
- App 可以離線啟動。
- 若缺少 WebView2，應提供可理解且可操作的提示。
- 不出現開發者 traceback。

#### TC-NEXT-002 port 被占用時自動換 port

步驟：

1. 先用另一個程序占用預設 port。
2. 雙擊 `start.bat`。
3. 取得實際啟動 port。
4. 檢查 Tauri 視窗載入網址。

通過條件：

- launcher 自動選擇可用 port。
- Tauri 載入本次 health check 成功的實際 URL。
- 不退化成開啟外部瀏覽器。
- 不會因 port 衝突啟動失敗。

#### TC-NEXT-003 App 執行中收到更新

步驟：

1. 啟動 `v1.0.0` 並保持 App 執行。
2. 將 `v1.1.0` 更新放入更新來源。
3. 觸發或等待更新檢查。
4. 檢查 pending/NEXT 狀態與使用者通知。

通過條件：

- 更新在背景完整下載或複製。
- 完整性驗證通過後才設定 pending。
- 使用者收到「下次重啟會更新」的訊息。
- 正在執行的 `v1.0.0` 不被覆寫、不閃退。
- App 執行中不直接替換 PROD。

#### TC-NEXT-004 關閉並重啟後切版

步驟：

1. 延續 TC-NEXT-003，確認 `v1.1.0` 已 pending。
2. 正常關閉 Tauri 視窗。
3. 確認舊 Streamlit/Python 程序已結束。
4. 再次雙擊 `start.bat`。

通過條件：

- 重啟時才將 pending 版本升為 current/PROD。
- 舊版保留為 previous/PREV。
- 新版正常啟動並通過 health check。
- state.json 的 current、previous、candidate、pending 一致。

#### TC-NEXT-005 新版本啟動失敗時自動退版

準備一個一定無法通過 health check 的測試版本。

通過條件：

- 失敗版本被記錄到 failed versions。
- current 自動回到最後確認可用版本。
- 不會再次自動選擇剛失敗的版本。
- 使用者看到「已退回上一個可用版本」而不是原始 traceback。
- `--status` 能清楚說明從哪一版退到哪一版及發生時間。

#### TC-NEXT-006 更新中斷與斷電恢復

分別在以下時間點強制終止程序或關閉 VM：

- 更新檔案複製一半。
- `files.json` 驗證期間。
- 正式目錄已出現但 `.complete` 尚未寫入。
- pending 已寫入但尚未重啟。

通過條件：

- 不完整版本永遠不會成為 current。
- 重新啟動後仍可使用舊版。
- 系統可以清理或覆蓋不完整 staging。
- 不需要人工修改 state.json。

### P1：第一批使用者前建議完成

#### TC-NEXT-007 視窗關閉與程序生命週期

- 關閉 Tauri 視窗後確認 Tauri、launcher、Streamlit/Python 都結束。
- 原本使用的 port 可以立即重新綁定。
- 連續開關 10 次不得累積殘留程序。
- 快速雙擊兩次時只允許一個 App instance。

#### TC-NEXT-008 權限與路徑矩陣

至少測試：

- 中文 Windows 使用者名稱。
- 交付路徑含空白與中文。
- 一般使用者帳號。
- Program Files 或其他不可寫位置。
- USB/exFAT 交付媒體。

通過條件：

- 可寫位置正常運作。
- 不可寫位置顯示明確修正方式。
- bat 不因括號、空白、中文或 cp950 編碼崩潰。

#### TC-NEXT-009 WebView2 矩陣

測試環境：

1. 已安裝 WebView2。
2. 未安裝 WebView2但可以上網。
3. 未安裝 WebView2且完全離線。
4. 附帶 Evergreen Standalone Installer。

通過條件：

- 已安裝時直接啟動。
- 缺少時提供正確安裝方式。
- 離線包不可誤放成需要連網的 bootstrapper。

#### TC-NEXT-010 三版連續更新與 GC

流程：

```text
v1.0.0 → v1.1.0 → v1.2.0 → rollback → 再更新
```

通過條件：

- current、previous、last-known-good 與 failed versions 正確。
- GC 不刪除 current、pending、previous 或仍有 lease 的 runtime。
- 可以清除真正不再使用的版本與 staging。
- 長時間使用後不會因暫存資料夾無限累積而耗盡磁碟。

### P2：壓力與長時間測試

- Defender 開啟下連續建置與更新 10 次。
- 同時啟動兩個 builder，要求同一個 runtime fingerprint。
- 更新來源短暫斷線後恢復。
- 大型檔案更新期間反覆拔除 USB 或中斷網路分享。
- 磁碟空間不足時的錯誤訊息與恢復能力。
- 24 小時持續執行後更新、關閉、重啟。

---

## 7. 下一輪建議執行順序

建議不要同時展開所有情境，依下列順序可以最快找到會阻擋實際交付的問題：

1. 乾淨 Windows VM 離線雙擊啟動。
2. port 衝突與真實 Tauri 動態 URL。
3. App 執行中下載更新與通知。
4. 關閉程序、重啟切版。
5. 新版本啟動失敗與自動退版。
6. 更新途中斷電與不完整資料恢復。
7. 中文路徑、一般使用者、WebView2 矩陣。
8. 三版更新、rollback 與 GC。
9. 壓力及長時間測試。

前六項全部通過後，才適合交給第一批實際使用者試用。

---

## 8. 下一輪整體完成標準

下一輪可判定完成，至少必須符合：

- 乾淨 Windows VM 在無 Python、無開發工具下可啟動。
- port 被占用時仍可在 Tauri 視窗中正確顯示 App。
- 執行中的版本不會被背景更新破壞。
- 只有完整且驗證通過的版本能成為 pending/current。
- 重啟才切換版本。
- 新版失敗可以自動退回最後可用版本。
- 中途斷電不會讓 App 進入無法啟動的永久狀態。
- Tauri 關閉後不殘留 Streamlit/Python 程序。
- 一般使用者看到的是可理解的中文訊息，不是 traceback。
- 所有失敗情境都有可操作的恢復方式。

---

## 9. 已知交付注意事項

### WebView2

目前交付測試有警告：尚未附 WebView2 離線安裝檔。

真正離線的電腦應附帶 Evergreen Standalone Installer；體積很小、執行時還要下載的 WebView2 bootstrapper 不能當作離線安裝檔。

### CV Viewer 模型檔

CV Viewer 專案含約 84 MB 的模型檔：

```text
models/dinov2_vits14.pth
```

目前每個 application 版本槽都是完整複製，因此：

- runtime libraries 可以跨版本共用。
- application 內的模型檔仍會在每一版重複出現。
- 本輪實際更新包約 96.3 MB，不是只有程式碼差異的十幾 MB。

若模型不隨版本更新，後續可評估把模型移到 Store 共用資源區；這是容量優化，不應與目前已通過的更新安全機制混在同一輪修改。

---

## 10. 最終判斷

本輪已完成「程式邏輯與真實交付流程」的收斂：

```text
完整 pytest：935 passed / 18 skipped
真實 CV Viewer delivery E2E：12/12 passed
```

下一輪的重點不是再證明函式會不會運作，而是證明一般使用者在真實 Windows 電腦上，遇到 port 衝突、防毒、缺少 WebView2、更新中斷、新版失敗與非管理員權限時，仍能啟動、更新並安全恢復。
