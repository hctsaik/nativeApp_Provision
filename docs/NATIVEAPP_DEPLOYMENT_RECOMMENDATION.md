# nativeApp 部署盤點與收斂建議

> 日期：2026-07-19  
> 範圍：`C:\code\claude\nativeApp`、`C:\code\claude\native_Provision`  
> 狀態：分析與建議；尚未開始改造部署程式

## 1. 結論

不應再把 `nativeApp`、`native_Provision\dist` 或整個 `<APP_ROOT>` 當成正式交付物，直接逐檔複製到 User 電腦。

建議把現有兩套能力收斂成同一條部署鏈：

- `.napp` 負責單檔傳輸、版本資訊、內容雜湊、簽章與大型 blob 引用。
- Store/slot 負責裝置端的不可變版本、共用 runtime、staging、原子切換、健康檢查與 rollback。
- 初次部署只交付一個簽章安裝程式；離線環境則交付一個完整 ZIP。
- 後續更新只傳新的 `.napp`，dependency fingerprint 未改變時不重送共用 runtime。
- 更新永遠不覆寫正在執行的版本。

這個方向可同時降低：

1. 交付檔案數量。
2. 重複 runtime／venv 所占空間。
3. Windows Defender、WebView2 或執行中 Python 鎖檔造成的更新失敗。
4. 半套更新、斷電與錯版無法回復的風險。

## 2. 現況量測

以下為 2026-07-19 開發機上的實際量測；均不含 `.git`：

| 位置 | 大小 | 檔案數 | 判斷 |
|---|---:|---:|---|
| `nativeApp` | 約 5.3 GB | 約 8.6 萬 | 開發環境，含 venv、node_modules、外部工具 |
| `native_Provision\dist` | 約 13.1 GB | 約 25.7 萬 | 多輪 fat/store/E2E/soak 產物累積，不是單一 release |
| `native_Provision\e2e` | 約 4.1 GB | 約 5.1 萬 | 測試工作區、WebView profile、截圖與 runtime |
| `dist\provision` | 約 2.22 GB | 約 2.38 萬 | 舊式補給輸出；其中 `_run` 約 1.93 GB |
| `dist\cv-store-v2` | 約 515 MB | 約 9,741 | 接近真正的 CV 首次部署內容 |

`dist\cv-store-v2` 的組成：

| 部分 | 大小 | 檔案數 |
|---|---:|---:|
| `deps` | 約 504 MB | 9,494 |
| `apps` | 約 11.2 MB | 231 |
| bootstrap／啟動工具 | 小於 1 MB | 少量 |

因此：

- 真正的 CV 首次部署約為 500 MB 等級，不是 17 GB。
- dependency fingerprint 不變時，後續更新應只需要 App 版本內容，不應再次傳送約 504 MB runtime。
- User 在傳輸過程應只看到一個安裝檔或少數 `.napp`／blob 檔；數千個 Python 檔案只在本機 staging 解開。

## 3. 問題根因

### 3.1 開發輸出與正式 Release 沒有清楚邊界

`native_Provision\dist` 同時保存多輪實驗、完整交付、增量更新、soak 與 E2E 結果。這會讓操作人員無法判斷哪個資料夾才是正式交付物，也容易把 `_run`、WebView profile、測試 venv 一起複製。

### 3.2 目前存在兩條重疊的更新架構

Streamlit Store 已具備：

- 不可變 `versions/<version>`。
- 共用 `deps/runtimes/<fingerprint>`。
- staging、`.complete`、SHA-256 驗證。
- pending/current/previous/last-known-good。
- health check、rollback、failed-version memory。
- Defender 鎖住 runtime rename 時的安全複製 fallback。

`.napp`／Native Agent 已具備：

- 單檔 ZIP artifact。
- package manifest 與逐檔 SHA-256。
- 簽章介面。
- content-addressed blobs。
- staging、版本安裝、active pointer、rollback 與 Fleet 管理介面。

兩邊都解決部分相同問題，但尚未共用同一套 runtime store、activation 與 production 簽章流程。

### 3.3 舊文件仍把整包 XCOPY 當主要部署方式

整包 XCOPY 適合早期 portable prototype，但不適合作為正式更新協定：

- 正在執行的 EXE、DLL、Python module 或 WebView 檔案可能被鎖住。
- 中途失敗會留下新舊檔案混合的目錄。
- 無法可靠判斷某一版是否完整。
- 使用者資料和程式版本容易混在一起。

## 4. 目標部署架構

```text
Build / Release Machine
  ├─ CIM-Setup-<platform-version>.exe
  ├─ channel.json
  ├─ artifacts/<app-id>-<version>.napp
  └─ blobs/<sha256>
                  │
                  ▼
User 電腦 staging
  ├─ 下載或從 USB 複製單檔 artifact
  ├─ 驗證 release SHA-256
  ├─ 驗證 package 簽章
  ├─ 解壓到新的版本槽
  ├─ 準備或重用 runtime fingerprint
  ├─ health check
  └─ 原子切換 active state；失敗則保留／回復上一版
```

### 4.1 建議的裝置端目錄

以不需系統管理員權限的 per-user 安裝為預設：

```text
%LOCALAPPDATA%\CIM\
├─ bootstrap\
├─ deps\
│  ├─ shells\<fingerprint>\
│  ├─ runtimes\<fingerprint>\
│  └─ blobs\<sha256>\
├─ apps\<app-id>\
│  ├─ state.json
│  ├─ versions\<version>\
│  ├─ staging\
│  └─ locks\
└─ data\<app-id>\
```

企業 all-users 部署可改放 `%ProgramData%\CIM`，但必須另外定義 ACL 與 updater 權限。第一版不建議直接安裝到 `Program Files` 後再讓一般 User 自行更新。

### 4.2 程式與資料必須分離

- `versions/<version>`：不可變，只放可重新取得的程式內容。
- `deps`：以 fingerprint 或 SHA-256 定址，可跨版本／App 共用。
- `data/<app-id>`：使用者資料、SQLite、設定與工作輸出；更新和 GC 不可碰。
- 移除 App 時應讓使用者選擇是否保留 data，不應把「刪 App」等同「刪資料」。

## 5. Windows 鎖檔處理原則

### App 正在執行

新版本寫入另一個 `versions/<new-version>`，不覆寫 current。下載與驗證可在背景完成，關閉並重新開啟後才 promote。

### Defender 鎖住 staging 或剛建好的 runtime

1. 在有限時間內退避重試 rename。
2. 仍失敗時，將已驗證內容安全複製到 final path。
3. 對 final copy 再做逐檔驗證。
4. 最後才寫 `.complete`。
5. 若仍失敗，current 保持不變，留下可診斷紀錄並允許重試。

### 舊版本或舊 runtime 刪不掉

GC 失敗不應讓安裝／更新失敗。應記錄 deferred cleanup，在下次啟動或重開機後重試；有效 lease 引用中的版本不可刪除。

### Tauri shell／bootstrap 被鎖住

執行中的 launcher 不可自我覆寫。平台殼更新應使用 A/B slot，或由簽章 base installer 在下次啟動前處理。App 更新與平台殼更新必須是兩種不同生命週期。

### 更新中斷或斷電

只有完整下載、驗證、runtime 就緒與版本 `.complete` 都成立後，才可寫入 pending。任何 staging 殘留都不能被啟動。

## 6. Release 輸出契約

正式建置應只有一個明確命令，輸出到全新的版本目錄：

```text
release/<release-id>/
├─ CIM-Setup-<platform-version>.exe
├─ offline-channel/
│  ├─ channel.json
│  ├─ artifacts/*.napp
│  └─ blobs/<sha256>
├─ release-manifest.json
├─ checksums.sha256
├─ SBOM.json
└─ RELEASE-REPORT.md
```

Release gate 必須拒絕下列內容：

- `_run`、`e2e`、logs、tmp、cache。
- `.git`、node_modules、開發用 venv。
- WebView user-data profile。
- 測試輸出、截圖、pytest cache。
- 未列入 manifest 的檔案。
- 未簽章的 production package。
- 同 fingerprint 重複攜帶的 runtime。

每次 release 必須報告：

- Artifact 數量與總大小。
- 解壓後檔案數與預估磁碟需求。
- App 與 runtime fingerprint。
- 哪些 runtime／blob 可被舊版重用。
- SHA-256、簽章 key ID 與驗證結果。
- 是否完成乾淨 VM 驗收。

## 7. 分階段下一步

### P0：建立唯一、乾淨的 Release Pipeline

> **狀態：已實作（2026-07-19）。** 落點：`release.py`（CLI）+
> `src/provision_builder/release_pipeline.py`；測試 `tests/test_release_pipeline.py`
> （20 項，含 gate、production 驗章、竄改偵測、FileChannelRemote 相容性）。
> 使用方式見 README「正式交付：release.py」節。

1. 新增單一 release command。✅（`release.py build` / `release.py verify`）
2. 每次使用全新輸出目錄，禁止從歷史 `dist` 就地增補。✅（目錄已存在即拒絕；staging 組裝＋原子換位，崩潰不留半套）
3. 產生 release manifest、checksums、大小與檔案數報告。✅（`release-manifest.json` / `checksums.sha256` / `SBOM.json` / `RELEASE-REPORT.md`）
4. 加入防誤包測試，明確拒絕 `_run`、E2E、cache、開發環境。✅（gate fail-loud 列出路徑；根層/任意深度兩層規則，沿用 `.provisionignore` 的 `dist` 教訓）
5. 把 `native_Provision\dist` 定義為可刪除 build workspace，不再當正式交付來源。✅（`verify` 對非 release 目錄直接判「不是交付物」）

本階段刻意未含（依 §6 契約仍屬後續）：`CIM-Setup-*.exe` 的**產製**（非 WDAC 機器的事，
`--setup` 只收現成檔）、production Ed25519（P3）、`offline-channel` 之外的 runtime/store
交付（走 store export，另收斂）。`channel.json` 直接沿用 `native_agent.file_remote`
的格式——release 即 update source，無第三種格式。

### P1：收斂 `.napp` 與 Store/slot

> **狀態：核心已實作（2026-07-19）**，落點 `native_agent/agent.py` + `tests/test_agent_p1_p2.py`。
> **第二輪追加（同日）——CIM 平台本身 Store 化 phase 1**：平台就是 Agent 底下的一個 app
> （`cim-platform`）。`platform_store.build_platform_napp()`（engine 樹進 payload、
> 17MB 殼以 blob 旅行）→ `release.py pack-platform` + P0 release →
> `NativeAgent.update("cim-platform", …)`（不可變版本/原子切換/LKG/回滾全部繼承）→
> `native_agent/platform_launcher.py` 依 start.bat 契約（CIM_ENGINE_EXE/四個 data env/
> cwd 技巧/project-key 規則）解析啟動；內建專案 data 固定 key，**換版不重置 user data**
> （有測試）。`tests/test_platform_store.py` 覆蓋全鏈。Phase 1 不含：可攜 runtime 的
> 版本化、真機 WebView2 spawn 驗證（launcher `--dry-run` 是已測面；真機驗證進 VM 矩陣）。

1. `.napp` 成為唯一 App 對外 artifact。✅（Agent 路徑原本即是；release pipeline 亦以 `.napp` 為唯一單位）
2. Native Agent 安裝 `.napp` 時，落入 Store 的 `apps/<id>/versions`。✅（形狀一致：`applications/<id>/versions/<ver>` 不可變 + meta sidecar；根目錄名未改以免破壞既有裝置）
3. 將 per-App venv 收斂到全域 `deps/runtimes/<fingerprint>`。✅（同指紋跨 App 共用一份；staging+原子換位、輸掉建立競賽時採用贏家；收斂前的 per-app venv 仍可續用（read-only fallback），GC 照舊回收）
4. `.napp` 只攜帶 App、小型 wheels 與 blob references。✅（原本即是）
5. Management Center 只呼叫同一個 Agent／Store service。✅（`management_api` 原本即是唯一入口）

### P2：補齊鎖檔與傳輸韌性

> **狀態：Agent 側已實作（2026-07-19）**，落點 `src/provision_builder/winfs.py` + `native_agent/agent.py`。

1. rename retry／copy-and-verify fallback 共用。✅（新共用模組 `winfs.py`：`robust_rename`/`robust_rmtree`，Agent 的 activation 與 GC 已改用；Streamlit Store 的 builder 與 device gc 原本已有等效處理，未動）
2. `.part` 續傳。✅（Agent `_download`：斷點以 `.part` 續傳（可 seek 則 seek、否則跳讀），壞殘檔只重試一次乾淨下載，重組後對 registry SHA-256 驗證。HTTP Range 屬未來 HTTP provider——同一 seek 介面即可接上）
3. GC deferred cleanup。✅（刪不掉的樹記入 `agent/deferred-gc.json`、結果如實回報 `deferred`、下輪 GC 先重試；global runtime 的 keep-set **跨所有 App** 計算，寧可多留不誤刪）
4. 平台 shell A/B slot 或 base installer。❌ **未做**（需在非 WDAC 機器重編殼；殼在 Store 已按內容雜湊共用、bootstrap 在版本目錄外，執行中不會被自我覆寫——缺的是殼自身的版本化更新路徑）

### P3：Production 安全與驗收

> **狀態：1/4 已實作、2 部分、3 清單就緒未執行（2026-07-19）**。

1. production Ed25519 signer/verifier 與 key rotation。✅（**純 Python RFC 8032**——原「需 cryptography」假設不成立，見 ADR 0001 實作紀錄；trust store 支援多鑰共存/retired/撤銷；`release.py keygen/sign` + RFC 官方向量測試）
2. 更新通道驗證發行者簽章。✅ **兩條通道都已接**（2026-07-19 第二輪）：`.napp` 通道（Native Agent／release pipeline production channel）；**Streamlit Store desktop 通道**（`device/update_signing.py`：簽 files.json 的 canonical digest，`release.py sign-version` 簽發、`_stage_version` 與 `--set-pending` 兩條路都驗、`require_signed_updates` + `apps/<app>/trusted_publishers.json` 控管；未配置的既有裝置不受影響，「攻擊者改 payload 後重生 files.json」有測試證明被抓）。
3. 乾淨 Windows VM 測試矩陣。**清單已定未執行**：[`VM_ACCEPTANCE_MATRIX.md`](VM_ACCEPTANCE_MATRIX.md)（14 情境含降級路徑）；開發機無 VM 基礎設施，執行需另排。
4. release promotion：internal → pilot → production。✅（`release.py promote`：同一批 bytes 換通道**全程重驗**，晉升 production 強制驗章；manifest 記 `promoted_from`）

## 8. 發布前最低驗收標準

- 乾淨 Windows，沒有 Python、Node、Rust，也能完成安裝與啟動。
- 一般 User、無系統管理員權限可使用既定功能。
- 無網路時可從 USB 完成首次部署及更新。
- UNC／HTTP 中斷後可安全重試，不破壞 current。
- App 執行中可完成背景 staging，且 current 檔案完全不被覆寫。
- Defender 鎖住 runtime rename 時能 fallback 或安全失敗。
- 更新途中強制關機後仍能啟動 last-known-good。
- 新版 health check 失敗時自動 rollback。
- 三版連續更新後 GC 不刪 current、previous、LKG、active lease 或 data。
- 安裝包與 `.napp` 均通過 production 簽章驗證。
- SmartScreen／Mark-of-the-Web 與 WebView2 缺少時有明確 User 指引。

## 9. 已驗證基礎與未完成缺口

本次執行下列核心測試：

- Streamlit Desktop safety/state/store flow/export。
- `.napp` build/verify/install。
- Native Agent update/rollback。
- concurrency/interruption。

結果：**402 tests passed**。

仍需完成：

- Store 更新通道與 `.napp` production 簽章整合。
- `.napp`／Native Agent 與全域 runtime store 的實際收斂。
- 真實下載續傳。
- 乾淨 Windows VM、SmartScreen、無 WebView2、USB/exFAT 測試。
- 正式 platform base installer／A-B shell 更新策略。

## 10. 決策摘要

正式方向不是「再做一個更大的 portable folder」，而是：

> **初次安裝單檔化、App 更新 `.napp` 化、大型相依內容定址、runtime fingerprint 共用、版本不可變、切換原子化、User data 永久分離。**

第一個實作工作應是 **P0：唯一 Release Pipeline**。在 P0 完成前，不應再把任何既有 `dist` 子目錄直接交付給 User。
