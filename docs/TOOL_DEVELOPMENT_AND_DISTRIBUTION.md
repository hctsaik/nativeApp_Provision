# 平台工具的開發、離線相依與多機部署架構

> 狀態：架構討論稿  
> 適用情境：平台上的 Python 工具需要部署到多台 Windows 電腦，而 User 電腦無法連線 PyPI，甚至沒有預先安裝 Python。  
> 實際操作步驟請參考 [OFFLINE_DEPLOY.md](OFFLINE_DEPLOY.md)。
> `cv_reviewer` 的 GUI 建置、MinIO 發布與啟動時自動更新規畫，請參考 [CV_REVIEWER_GUI_UPDATE_PLAN.md](CV_REVIEWER_GUI_UPDATE_PLAN.md)。

## 1. 要解決的問題

平台希望達成以下目標：

1. 每個工具可以獨立開發、測試及發布。
2. 工具可以自行宣告 Python 相依，不必把所有套件塞進平台核心環境。
3. User 電腦即使離線、沒有 PyPI、沒有安裝 Python，也能執行工具。
4. 同一個工具可以部署到很多台電腦。
5. 未來開發者只要發布新版本到共用磁碟或內網 Registry，Runtime 就能取得並使用。
6. 更新失敗時不能破壞目前可用版本，且應能回退。

這個問題不能只靠「把 Python 原始碼複製到 User 電腦」解決。原始碼只是一部分，Runtime 還需要知道：

- 應使用哪個 Python 版本。
- 要安裝哪些套件及確切版本。
- 相依套件是否支援 Windows、Python 3.11 及目標 CPU/GPU。
- 在沒有 PyPI 時，wheel 要從哪裡取得。
- 程式碼與相依包是否屬於同一個工具版本。
- 下載的內容是否完整、可信且可回退。

## 2. 核心決策

建議把交付物拆成三層：

| 層級 | 內容 | 更新時機 |
|---|---|---|
| 平台 Runtime | 平台殼、engine、portable Python 3.11、平台核心相依 | 平台或 Python 升版時 |
| 工具 Bundle | 工具原始碼、`plugin.yaml`、版本、入口點及相容性資訊 | 每次工具發布時 |
| Dependency Pack | 工具所需的完整 wheel 集合、manifest 與 hash | 工具相依改變時 |

最重要的原則是：

> 共用磁碟是發布來源，本機 cache 才是執行來源。

Runtime 不應直接在共用磁碟上執行 Python 原始碼。它應先下載完整版本到本機暫存區，完成驗證後，再原子性切換為目前版本。

## 3. 整體架構

```text
開發／建置端（可連網）
  工具原始碼
      │
      ├─ 單元測試與平台契約測試
      ├─ 鎖定 Python 相依版本
      ├─ 下載完整 wheel dependency closure
      └─ 離線安裝驗證
      │
      ▼
  版本化 Tool Bundle
    ├─ plugin.yaml
    ├─ Python 原始碼
    ├─ requirements.lock
    ├─ deppack.json + wheels
    └─ manifest、SHA-256、發布者簽章
      │
      ▼
共用磁碟／內網 Registry
  catalog.json
  tools/module_042/1.2.0/
  tools/module_042/1.3.0/
  blobs/<sha256>.whl
      │
      ▼
User 離線電腦
  平台 Runtime + portable Python 3.11
  tool-cache/module_042/1.3.0/
  deppack-cache/module_042/
  tool-venvs/module_042/<dependency-hash>/
```

## 4. 平台目前已具備的能力

目前 `nativeApp` 與 `native_Provision` 已經具備大部分底層能力：

- 平台可攜式 Python 3.11 Runtime。
- 工具可在 `plugin.yaml` 使用 `requires:` 宣告相依。
- 每個工具建立隔離的 per-tool venv。
- 使用 `pip --no-index` 從本機 wheelhouse 安裝，不接觸 PyPI。
- dependency fingerprint 相同時沿用既有環境。
- dep-pack manifest 與逐檔 SHA-256 驗證。
- `native_Provision` 可掃描工具、下載目標 wheel、產生離線補給包並驗證完整性。
- `apply.py` 可將 dep-pack 原子性套用到離線機的 cache。
- `warmup.py` 可在開啟工具前預建 venv。
- 平台已有 Fleet／Registry 的程式碼分發基礎。

因此短期不需要重新發明 Python 安裝機制，而是要把現有能力統一成正式的工具開發與發布流程。

## 5. 工具的宣告方式

開發階段可以在 `plugin.yaml` 宣告直接相依：

```yaml
id: module_042
name: 缺陷量測
version: 1.3.0
runner: cv_framework
python: "3.11"
platform: win_amd64
platform_api: ">=2.1,<3"
entrypoint: 042_process.py
requires:
  - shapely>=2.0
  - scikit-image==0.24.*
```

正式發布時，建置流程應產生精確的 `requirements.lock`，例如：

```text
shapely==2.0.6
scikit-image==0.24.0
numpy==2.2.3
```

`requires:` 適合描述開發者的直接需求；`requirements.lock` 則描述該版本實際驗證過的完整環境。若正式發布仍只保留 `>=`，不同時間產出的離線包可能不是同一套環境，造成難以重現的問題。

## 6. 建議的工具開發流程

1. 使用平台 scaffold 或 SDK 建立工具骨架。
2. 在獨立開發 venv 中開發，不直接修改平台核心環境。
3. 在 `plugin.yaml` 宣告直接 Python 相依。
4. 執行工具單元測試及平台契約測試。
5. CI 或建置機解析完整相依，產生精確 lock。
6. 下載 `win_amd64 / CPython 3.11 / cp311` 對應的完整 wheel 集合。
7. 在乾淨環境執行 `pip --no-index`，證明不連 PyPI 也能安裝。
8. 執行 import、health check 及必要的 UI／整合測試。
9. 產生不可變的 Tool Bundle，加入 hash 與發布者簽章。
10. 先發布到 `staging`，驗收後再提升到 `production`。

建議至少提供三個 channel：

| Channel | 用途 |
|---|---|
| `dev` | 開發者快速整合 |
| `staging` | 離線安裝及整合驗證 |
| `production` | 一般 User 使用的正式版本 |

## 7. 目前可採用的離線部署流程

在完整 Registry 功能完成前，可以沿用現有的 provision 流程：

1. 平台交付 portable Python 3.11 Runtime。
2. 工具在 `plugin.yaml` 宣告 `requires:`。
3. 連網建置機使用 `native_Provision` 產生 provision／dep-pack。
4. 將平台、工具程式碼與 provision 複製到離線機。
5. 執行 `apply.py`，把 dep-pack 放入平台的 `deppack-cache`。
6. 執行 `warmup.py`，預先建立工具 venv。
7. 啟動平台並使用工具。

詳細命令、輸出與錯誤處理請參考 [OFFLINE_DEPLOY.md](OFFLINE_DEPLOY.md)。

### 連網建置機的打包 GUI

發布人員可以在 `native_Provision` 根目錄雙擊 `start-gui.bat`，不必自行輸入 build 命令。
GUI 可選擇平台專案、Module 資料夾、輸出位置與工具，並顯示即時進度、錯誤與完成摘要。
Module 資料夾可指向單一 Module 或含多個 Module 子目錄的根；所有啟用 Module 都可選。

GUI 會把原始碼輸出到 `source-packages/<tool-id>`，把 Python wheel 輸出到 `packs/<tool-id>`；
兩者有各自 manifest。沒有 `requires:` 的工具只建立原始碼包，原始碼更新也不會重建 wheel。

GUI 固定使用 Windows x64、Python 3.11、cp311 目標，底層仍呼叫同一套 Provision Builder；
增量快取、大型相依隔離、SHA-256 manifest 及完全離線安裝自檢都會自動執行。
發布人員不需要另外執行 Verify，但完整性驗證能力仍保留在產包與離線安裝流程中。

打包完成後也可以在同一個 GUI 選擇隔離驗證資料夾與工具，實際執行 Apply、完全斷網的
Warmup，再啟動 Tauri 殼於 Portal 按下 Start。驗證會保存 JSON 結果、engine／host log
及畫面截圖，因此「wheel 打得出來」和「User 經由桌面殼真的看得到工具」是同一條驗收流程。

### 為什麼需要 warmup

torch 等大型相依第一次安裝可能需要一分鐘以上。目前 Tauri HTTP bridge 的請求逾時短於大型相依的安裝時間；若在 User 按下 Start 時才同步安裝，畫面可能先顯示啟動失敗，但 engine 仍在背景安裝。

`warmup.py` 把這段成本移到正式操作之前。長期應將相依準備改為非同步工作，並在 Portal 顯示下載、驗證、安裝及完成進度。

## 8. 共用磁碟／Registry 的 Runtime 流程

未來 Runtime 發現新工具版本時，建議依序執行：

1. 讀取共用磁碟或 Registry 的 catalog。
2. 根據裝置設定選擇 channel 與允許版本。
3. 比對本機 cache，缺少時下載 Tool Bundle 與 dependency blobs。
4. 下載到 staging 目錄，不覆蓋目前可用版本。
5. 驗證發布者簽章、manifest、每個檔案的 SHA-256。
6. 驗證 Python ABI、作業系統、CPU/GPU 與平台 API 相容性。
7. 根據 dependency lock hash 尋找既有 venv；沒有才離線建立。
8. 執行 `pip --no-index`、import check 及工具 health check。
9. 全部成功後，原子性更新 active version 指標。
10. 保留上一個已知可用版本；新版失敗時自動回退。

這個流程能處理共用磁碟斷線、下載中斷、發布到一半、檔案損毀，以及新版無法啟動等情況。

## 9. 為什麼不直接執行共用磁碟上的原始碼

直接執行共享資料夾中的 `.py` 看似簡單，但會帶來下列問題：

- 網路磁碟暫時斷線，正在執行的工具立即受影響。
- 發布者更新多個檔案時，User 可能讀到一半新、一半舊的版本。
- 多台機器同時存取大型模型或 wheel，效能與穩定性難以控制。
- 無法可靠判斷本機正在執行哪個版本。
- 缺少簽章驗證時，共用磁碟上的程式碼被修改就可能直接執行。
- 更新失敗後沒有乾淨的回退點。

因此共享位置應被視為 artifact repository，而不是執行目錄。

## 10. 大型相依與重複儲存

torch、CUDA、OpenCV 等 wheel 可能很大。建議 Registry 使用內容定址儲存：

```text
blobs/
  <sha256-of-torch-wheel>.whl
  <sha256-of-numpy-wheel>.whl
```

每個 dep-pack manifest 只引用 wheel hash。多個工具若使用完全相同的 wheel，共用磁碟與本機 blob cache 都只需保存一份。

執行環境則分階段處理：

- 第一階段維持 per-tool venv，優先確保版本隔離及可移除性。
- 後續可讓 dependency lock hash 完全相同的工具共用唯讀環境。
- 不建議所有工具共用單一全域 `site-packages`，否則容易發生版本衝突及卸載困難。

## 11. Tool Bundle 建議內容

一個正式發布版本至少應包含：

```text
module_042-1.3.0/
├─ plugin.yaml
├─ requirements.lock
├─ src/
├─ assets/
├─ deppack.json
├─ manifest.json
└─ signature.json
```

`manifest.json` 至少記錄：

- tool ID 與版本。
- Git commit 或來源版本。
- Python 與平台標籤。
- 平台 API 相容範圍。
- dependency lock hash。
- 所有程式、資產及 wheel 的 SHA-256。
- 建置時間與建置工具版本。
- 發布者 identity。
- entrypoint 與 health-check 定義。

Tool Bundle 應為 immutable。同一個 `tool_id + version` 不得覆寫；若內容改變，就必須發布新版本。

## 12. 安全與治理

因為工具本質上是會在 User 電腦執行的程式碼，至少需要以下控制：

- 只信任核准的發布者金鑰。
- Bundle、manifest 與 dependency blobs 都必須驗章及驗 hash。
- 驗證失敗時 fail closed，不得「警告後繼續執行」。
- 一般 User 只能選擇被指派或允許的 production 工具。
- 開發模式可使用 dev channel，但不應關閉正式環境的驗章。
- 保存安裝、啟用、失敗及回退紀錄。
- 工具權限、外部程式啟動、檔案及網路存取應由平台 policy 控制。

## 13. 尚需補齊的功能

若要從目前的人工 provision 提升成「多台電腦持續更新」，建議依序補齊：

1. 定義正式 Tool Bundle 與 manifest schema。
2. 為每個正式版本產生精確 dependency lock。
3. 把 code artifact 與 dep-pack 綁定成同一個發布版本。
4. 定義共用磁碟 catalog、channel 與版本提升規則。
5. 實作本機下載、驗證、cache、原子切換及回退。
6. 將 dependency install 改為非同步工作並提供 UI 進度。
7. 完成發布者簽章與 trusted publisher 管理。
8. 加入平台 API／Python ABI／硬體相容性檢查。
9. 提供多台電腦的安裝狀態、版本盤點與失敗報告。
10. 建立垃圾回收策略，安全清除未使用的舊版本、venv 與 blobs。

## 14. 建議的分階段落地方式

### Phase 1：標準化現有離線流程

- 所有工具統一使用 `plugin.yaml requires:`。
- 使用 `native_Provision` 建置、verify、apply、warmup。
- 固定目標為 Windows、Python 3.11、cp311。
- 發布前必須通過完全斷網的安裝與啟動測試。

### Phase 2：版本化 Tool Bundle

- 加入工具版本、lock、manifest、hash 與平台相容性。
- Bundle 與 dep-pack 建立不可分割的版本關係。
- 本機保留上一個可用版本。

### Phase 3：共用磁碟更新

- 建立 catalog 與 dev／staging／production channel。
- Runtime 自動拉取、本機 cache、驗證後啟用。
- 不直接從共用磁碟執行程式。

### Phase 4：Fleet 與治理

- 管理者向多台電腦指派工具與版本。
- 收集安裝、啟動與回退狀態。
- 加入簽章、RBAC、稽核與 blob cache 管理。

## 15. 結論

推薦的長期模型是：

> 平台固定攜帶 Python Runtime；工具以版本化 Bundle 發布；相依在連網建置端預先解析成 wheel／dep-pack；離線 Runtime 從共用磁碟或 Registry 下載、驗證、本機快取，並建立隔離的 venv。

這個模型同時支援：

- 離線 User 電腦。
- 大量電腦部署。
- 工具獨立開發及版本管理。
- 不同工具使用不同 Python 套件版本。
- 將新版本放到共用磁碟後，由 Runtime 安全取得。
- 更新失敗時維持舊版本可用。

現有的 per-tool venv、dep-pack、`native_Provision` 與 Fleet 基礎都可以繼續沿用；下一步重點不是更換底層技術，而是把它們整合成一致、可版本化、可驗證及可回退的發布流程。
