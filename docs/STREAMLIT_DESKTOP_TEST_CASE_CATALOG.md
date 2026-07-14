# Streamlit Desktop 完整 Test Case Catalog

> 目的：一次整理目前可預見的測試案例，作為後續 AI、開發者與 reviewer 的共同驗收清單。
>
> 日期：2026-07-15
>
> 狀態：測試設計基準；表中的案例不代表目前都已實作或通過。實作者必須以 test ID 回填自動化位置與結果。
>
> 相關規格：
>
> - [`SIMPLE_STREAMLIT_TAURI_FOLDER_BUILDER.md`](SIMPLE_STREAMLIT_TAURI_FOLDER_BUILDER.md)
> - [`SIMPLE_STREAMLIT_TAURI_FOLDER_BUILDER_PHASE0_AND_DESIGN.md`](SIMPLE_STREAMLIT_TAURI_FOLDER_BUILDER_PHASE0_AND_DESIGN.md)
> - [`STREAMLIT_DESKTOP_ATOMIC_UPDATE_IMPLEMENTATION_SPEC.md`](STREAMLIT_DESKTOP_ATOMIC_UPDATE_IMPLEMENTATION_SPEC.md)
> - [`STREAMLIT_DESKTOP_STORE_AND_SLOTS_DESIGN.md`](STREAMLIT_DESKTOP_STORE_AND_SLOTS_DESIGN.md)

## 1. 使用方式

### 1.1 優先級

| 等級 | 定義 | 合併要求 |
|------|------|----------|
| P0 | 可能交付壞包、破壞版本狀態、遺失資料、錯誤 rollback、謊報成功或讓產線無法使用 | 必須自動化；相關功能不得帶著失敗案例合併 |
| P1 | 跨模式、跨程序、Windows、離線、並行或復原可靠性 | 發布前必須通過；可按影響範圍選擇 PR gate |
| P2 | 診斷品質、文案、容量提示、較少見的相容性 | 可分期，但不可用假成功掩蓋 |

### 1.2 測試層級

| 縮寫 | 層級 | 定義 |
|------|------|------|
| U | Unit | 純函式／單模組；不以 mock 掩蓋最終契約 |
| C | Contract | 兩個實作端必須對同一輸入得出相同答案 |
| I | Integration | 真實檔案系統、subprocess、runtime 或多模組流程 |
| W | Windows | 真實 `cmd.exe`、路徑、encoding、檔案鎖或 WebView2 |
| E | E2E | 從管理工具或交付包啟動到真實 Tauri/Streamlit UI |
| M | Model | 參考狀態機、生成事件序列或 property-based test |

### 1.3 統一成功 Oracle

以下任一項單獨成立，都不能稱為 App 成功：process 存活、port 開啟、`/_stcore/health` 200、Tauri 視窗存在、iframe DOM 存在、exit code 0、marker 檔存在。

P0 E2E 的成功至少同時要求：

1. 產物 manifest、files manifest 與 runtime integrity 通過。
2. Tauri 視窗存在。
3. User 真正按下 Start，或測試走過等價的 portal action。
4. iframe 顯示 App 的版本／READY 識別內容。
5. iframe 及相關 logs 沒有 `Traceback`、`ModuleNotFoundError` 或預期外 fatal error。
6. state transition 符合 reference model。
7. 關閉後本次 process tree 與 port 被清除。

## 2. 全域不可違反的 Invariants

| ID | 優先級 | Invariant |
|----|--------|-----------|
| INV-001 | P0 | 未實際執行 App 程式碼的版本不得成為 last-known-good。 |
| INV-002 | P0 | Builder／GUI 宣告成功時，隔離後的交付物必須能自行啟動，不得借用原專案檔案。 |
| INV-003 | P0 | `state.json` 永遠是完整的舊狀態或完整的新狀態，不得出現半份交易。 |
| INV-004 | P0 | `current`／`pending` 不得指向未驗證或缺少 `.complete` 的版本。 |
| INV-005 | P0 | Shared shell/runtime 故障不得把 application version 標記為 failed。 |
| INV-006 | P0 | Application version 故障不得被誤報為 machine/shared component 故障。 |
| INV-007 | P0 | 掃描、容量估算、Fat copy、Store copy、export 必須使用同一套 inclusion decision。 |
| INV-008 | P0 | 建置端 import gate 與交付端 preflight 對 REQUIRED imports 的判定必須一致。 |
| INV-009 | P0 | Requirements fingerprint 相同時不得複製或修改第二份 runtime。 |
| INV-010 | P0 | Runtime 一旦完成驗證便不可被 probe、App 或另一個 build 修改。 |
| INV-011 | P0 | Cleanup／GC 只能回報真正刪除的 bytes 與 trees，不得回報計畫值。 |
| INV-012 | P0 | GUI 選擇的 app、entrypoint、version、runtime lock 必須一路傳到實際產物。 |
| INV-013 | P0 | 任何取消或失敗都不得覆蓋上一份成功產物。 |
| INV-014 | P0 | 執行中 lease 引用的 version/runtime/data 不得被 GC。 |
| INV-015 | P0 | 第二次啟動不得誤殺、誤標失敗或促進 pending。 |
| INV-016 | P0 | 下載或複製中斷不得產生可被 promote 的 NEXT。 |
| INV-017 | P1 | 同一輸入在中文、空白與一般 ASCII 路徑的行為等價。 |
| INV-018 | P1 | 使用者可見的錯誤訊息必須符合實際自動復原行為。 |

## 3. Project Discovery 與 GUI 分流

建議檔案：`tests/test_streamlit_desktop_discover.py`、GUI contract tests。

| ID | P | 層 | 情境／操作 | 預期結果 |
|----|---|----|-----------|----------|
| DSC-001 | P0 | U | 選擇標準 Streamlit root，根目錄有 `app.py` | 判定為 Streamlit project，入口為 `app.py`。 |
| DSC-002 | P0 | U | 入口在巢狀 `src/app.py` | 回傳相對於 project 的正確入口，不截斷路徑。 |
| DSC-003 | P0 | U | 多個 `.py` 都 import Streamlit，只有一個包含 render call | 選真正 page/entrypoint，不選 helper。 |
| DSC-004 | P0 | U | 專案同時有 `tests/fixtures/plugin.yaml` | stray fixture 不得把 Streamlit project 判成 CIM module collection。 |
| DSC-005 | P0 | U | 選到直接包含多個 module folders 的 layer | 正確判定 module root，不多上移或下移。 |
| DSC-006 | P0 | U | User 選到單一 module folder | 明確拒絕並指出應選它的上一層。 |
| DSC-007 | P0 | U | 18 個真正 module + 1 個 stray manifest | 以真正 collection 為主，不被 stray manifest 拉走。 |
| DSC-008 | P0 | U | 在 CIM 分頁選到 Streamlit project | 提示移到 Streamlit 分頁，不能靜默接受錯模式。 |
| DSC-009 | P0 | U | 在 Streamlit 分頁選到 module collection | 提示移到 CIM 分頁。 |
| DSC-010 | P0 | U | `.venv`／`venv` 內有大量 Streamlit scripts | 完全不 descend，不影響入口或 UI latency。 |
| DSC-011 | P1 | U | `node_modules`、`.git`、cache 內有假入口 | 全部忽略。 |
| DSC-012 | P1 | U | 深度超過 discovery max depth 的檔案 | 不被掃描；掃描時間有界。 |
| DSC-013 | P1 | I | 不可讀子目錄 | 其他可讀部分仍完成掃描；提供 warning 而非整體 traceback。 |
| DSC-014 | P0 | U | 只有中文 display name | 建議 app_id 不碰撞；需要明確 app id 時 GUI 要求輸入。 |
| DSC-015 | P0 | U | 兩個不同中文名稱 | 不能產生相同 app_id。 |
| DSC-016 | P0 | I | 既有 app_id 對應不同 display name/project | 寫入任何 bytes 前拒絕，訊息不得稱為 version collision。 |
| DSC-017 | P1 | U | `requirements.lock.txt`、`requirements.txt`、`pyproject.toml` 同時存在 | 使用規格定義的優先順序並顯示來源。 |
| DSC-018 | P1 | U | Shell/runtime env override 合法 | override 優先且來源明示。 |
| DSC-019 | P1 | U | Env override 指向不存在檔案 | 不靜默 fallback 成看似有效的來源；顯示可行動錯誤。 |
| DSC-020 | P1 | I | 專案含十萬個 junk files | UI 背景掃描可取消，Tk main thread 不凍結。 |

## 4. Inclusion、Exclusion 與 Artifact 完整性

建議檔案：`tests/test_streamlit_desktop_builder.py`，並建立 Fat/Store differential contract。

| ID | P | 層 | 情境／操作 | 預期結果 |
|----|---|----|-----------|----------|
| INC-001 | P0 | C | 對同一 project 執行 scan 與實際 copy | inclusion file set 完全一致。 |
| INC-002 | P0 | C | 同一 project 建 Fat 與 Store version | application file set 完全一致。 |
| INC-003 | P0 | U | 任意深度 `.git/.venv/__pycache__/node_modules/site-packages` | 全部排除。 |
| INC-004 | P0 | U | project root 的 `dist/build/wheels/wheelhouse/vendor` | 視為 root build artifact 排除。 |
| INC-005 | P0 | U | nested `component/frontend/dist` | 保留；custom component 可運作。 |
| INC-006 | P0 | U | root `release.zip` | 預設排除。 |
| INC-007 | P0 | U | nested `assets/data.zip` | 保留。 |
| INC-008 | P0 | U | nested `models/weights.tar.gz` | 保留。 |
| INC-009 | P0 | U | 任意深度 `*.whl` | 排除，不當 runtime data。 |
| INC-010 | P0 | U | `.provisionignore` 使用 `!model.zip` | 能 rescue 被內建 root-only 規則排除的檔案。 |
| INC-011 | P0 | U | User rule 先 include 後 exclude | 最後一條 matching rule 生效。 |
| INC-012 | P0 | U | User rule 先 exclude 後 include | 最後一條 matching rule 生效。 |
| INC-013 | P0 | U | `data/*` | 只作用於 data subtree，不退化成全域 `*`。 |
| INC-014 | P0 | U | `docs\draft\*` | Windows 反斜線正規化後與 `/` 等價。 |
| INC-015 | P1 | U | Directory-only pattern `recordings/` | 只排目錄及其 subtree，不誤排同名普通檔案。 |
| INC-016 | P1 | U | Pattern 命中 project root 本身 | 不得排掉整個 project。 |
| INC-017 | P0 | I | 被排除的檔案其實由 App 執行期讀取 | scan warning 必須列出檔案/大小及 `!path` escape hatch。 |
| INC-018 | P1 | I | 單一模型檔占 version slot 80% | 檢查階段點名檔案與每版成本。 |
| INC-019 | P1 | I | 大 project 但沒有 dominating file | 不產生誤導性的單檔警告。 |
| INC-020 | P0 | I | Build 完成後刪掉原 project，再啟動交付包 | 仍可運行，證明未借用 source path。 |
| INC-021 | P0 | I | `files.json` 缺檔、多檔、tamper | verify fail closed。 |
| INC-022 | P0 | I | Manifest 含 `../`、絕對路徑或 drive path | 拒絕 path escape。 |
| INC-023 | P0 | I | Artifact 含 symlink/junction/reparse point | 拒絕或依明確 policy 處理，不跟隨至 package 外。 |
| INC-024 | P1 | I | Project 位於含空白／中文路徑 | inclusion 結果與 ASCII path 等價。 |
| INC-025 | P1 | C | Capacity estimate 與真正 application bytes | 數值在明確容許範圍內，不漏算不可讀檔案為 0。 |

## 5. Requirements、Import Closure 與 Runtime Probe

建議檔案：`tests/test_streamlit_desktop_requirements.py`、`tests/test_streamlit_desktop_safety.py`。

| ID | P | 層 | 情境／操作 | 預期結果 |
|----|---|----|-----------|----------|
| IMP-001 | P0 | C | 同一 source set 餵給 build gate 與 delivered preflight | REQUIRED import set 完全一致。 |
| IMP-002 | P0 | U | Entrypoint module-level missing import | Build 阻擋。 |
| IMP-003 | P0 | U | `pages/2_report.py` module-level missing import | Build 阻擋。 |
| IMP-004 | P0 | U | `st.Page("pages/x.py")` 宣告頁面含 missing import | Build 阻擋。 |
| IMP-005 | P0 | U | `.streamlit/pages.toml` 宣告頁面含 missing import | Build 阻擋。 |
| IMP-006 | P0 | U | Function 內 import，但 function 在 module scope 被呼叫 | 視為 REQUIRED，Build 阻擋。 |
| IMP-007 | P1 | U | Function 內 lazy import，啟動時不呼叫 | Warning，不因不完整推論誤擋正常 App。 |
| IMP-008 | P1 | U | `try/except ImportError` protected import | 視為 optional warning。 |
| IMP-009 | P0 | U | Local first-party module 位於 project root | 不當成 PyPI dependency。 |
| IMP-010 | P0 | U | Local first-party module位於 `src/` | 不當成 missing distribution。 |
| IMP-011 | P0 | U | Page import 同一 pages folder 的 shared module | 正確辨認 first-party root。 |
| IMP-012 | P0 | U | `import grpc/psycopg2/Crypto/PIL/cv2/yaml` | 透過 metadata/alias 找到真正 distribution，不因名稱不同誤殺。 |
| IMP-013 | P1 | U | Metadata 無法回答 import→distribution | Build-side prediction降為 warning；post-install `find_spec` 決定。 |
| IMP-014 | P0 | I | Lock 宣告 package，但 staged interpreter `find_spec` 失敗 | Build 阻擋。 |
| IMP-015 | P0 | I | Probe subprocess 無法啟動或回傳 invalid JSON | 明確 probe error，不能當成「全部 missing」或成功。 |
| IMP-016 | P0 | I | Probe staged runtime | 使用 `-B`/`PYTHONDONTWRITEBYTECODE`，probe 前後 runtime hash 不變。 |
| IMP-017 | P0 | I | Runtime reuse path | 仍執行 app-specific missing import gate，不被 early return 跳過。 |
| IMP-018 | P0 | I | 第二個 App reuse runtime，但需要額外 package | Build 阻擋或要求新 runtime，不交付壞版。 |
| IMP-019 | P1 | U | `pyproject` 只有 direct deps，transitive dep 未列 | 不把「未直接宣告」當成必定 missing；安裝後 probe 為真相。 |
| IMP-020 | P0 | U | 完全 pinned lock | 缺少 REQUIRED distribution 可在安裝前 fail closed。 |
| IMP-021 | P0 | U | Loose `>=`、未釘版本、editable、VCS/local URL | Store lock依契約拒絕並指出問題行。 |
| IMP-022 | P0 | U | `pip freeze` 含目前 project editable line | 丟掉本 project line，不拒絕整份 lock。 |
| IMP-023 | P0 | U | `pip freeze` 含第三方 local wheel/path | 拒絕，不能靜默丟掉使 package 消失。 |
| IMP-024 | P1 | U | Lock comments、blank lines、大小寫、`-/_`、順序不同 | 正規化後 fingerprint 相同。 |
| IMP-025 | P1 | U | Python/ABI/platform/builder version不同 | Fingerprint 不同。 |
| IMP-026 | P0 | I | App syntax error | 在殼開啟前阻擋並分類為 version/app failure。 |
| IMP-027 | P0 | E | App 第一頁正常、第二頁 missing import | E2E 必須實際點第二頁並抓到；不能只驗首頁。 |

## 6. Builder、Cancellation 與原子輸出

建議檔案：`tests/test_streamlit_desktop_builder.py`、`tests/test_streamlit_desktop_export.py`。

| ID | P | 層 | 情境／操作 | 預期結果 |
|----|---|----|-----------|----------|
| BLD-001 | P0 | I | 首次成功 build | 完整 layout、manifest、hash、sentinel 正確。 |
| BLD-002 | P0 | I | Rebuild 同一 Fat package | 新包完整替換舊包，不混合檔案。 |
| BLD-003 | P0 | I | Rebuild 中途失敗 | 上一份成功 package 完整保留。 |
| BLD-004 | P0 | I | Cancel during pip | 子程序終止；不 swap partial package。 |
| BLD-005 | P0 | I | Cancel during 500MB runtime copy | 在 copy 中段停止；不等全部複製後才取消。 |
| BLD-006 | P0 | I | Cancel between stages | 不進入下一 stage，不產生完成 sentinel。 |
| BLD-007 | P0 | I | Cancellation cleanup 成功 | 只有驗證 staging 不存在後才宣告清乾淨。 |
| BLD-008 | P0 | I | Cancellation cleanup 被檔案鎖阻擋 | 回報殘留 path/bytes，不謊稱已清除。 |
| BLD-009 | P1 | I | 下一次 build 遇到 orphan staging | 列出並安全清理；不碰成功 package。 |
| BLD-010 | P1 | I | Defender 造成暫時 rename/replace lock | 有界 retry 後成功。 |
| BLD-011 | P0 | I | Lock 永遠不解除 | 放棄並給清楚錯誤；上一包不受影響。 |
| BLD-012 | P0 | C | scan ignore 與 copy ignore | 同一 decision function，不能漂移。 |
| BLD-013 | P0 | I | Store runtime fingerprint 已存在 | 不執行 runtime install/copy；仍跑 app gate。 |
| BLD-014 | P0 | I | Store runtime 不存在 | staging、驗證、`.complete` 最後建立。 |
| BLD-015 | P0 | I | 兩個 build 同時建立相同 fingerprint | 單一 writer，最後一份完整 runtime。 |
| BLD-016 | P0 | I | Store version已 `.complete` | 不可原地覆寫；相同內容 idempotent 或依契約拒絕。 |
| BLD-017 | P0 | I | GUI selected version 與 state.current 不同 | 匯出 selected version，不得偷偷匯出 current。 |
| BLD-018 | P0 | I | Full-tree export | 只帶指定 current + 合法 rollback version，不帶建置機 pending/data/leases/logs。 |
| BLD-019 | P0 | I | Export 單一 App | 不刪掉其他 App 的 tools/entry 或 shared runtime。 |
| BLD-020 | P1 | I | WebView2 installer 未提供 | Build 結果明確 warning，不宣告離線 prerequisites 齊全。 |
| BLD-021 | P0 | I | 提供 Evergreen bootstrapper 當 offline installer | 明確拒絕或標示非離線，不偽裝成 full installer。 |
| BLD-022 | P1 | I | Operator 提供真正 offline installer | 保留正確檔名/內容，不改名成 bootstrapper。 |
| BLD-023 | P1 | U | 所有 operator-facing strings 在 cp950 encode | 不拋 UnicodeEncodeError；替代文字仍可理解。 |

## 7. State Machine、Candidate、LKG 與 Rollback

建議檔案：`tests/test_streamlit_desktop_state.py`，另建立 model-based tests。

| ID | P | 層 | 情境／事件序列 | 預期結果 |
|----|---|----|-----------------|----------|
| STA-001 | P0 | U | `pending=v2,current=v1` → promote | `previous=v1,current=v2,pending=null,candidate=v2` 一次完成。 |
| STA-002 | P0 | U | Promote 時 pending 不完整 | 清除／隔離 pending；current 保持 v1。 |
| STA-003 | P0 | U | Candidate v2 真正 Start、UI ready、正常關閉 | candidate 清空，LKG=v2。 |
| STA-004 | P0 | U | Candidate 開 portal 但從未按 Start，正常關閉 | 不 commit、不 fail；下次仍需驗證 candidate。 |
| STA-005 | P0 | U | Candidate 延遲 2 分鐘才 Start，之後 import crash | 標記 v2 failed並 rollback，不因固定抵達窗漏判。 |
| STA-006 | P0 | U | Candidate health server ready但 App script未執行 | 不 commit LKG。 |
| STA-007 | P0 | U | Candidate App ready後正常關閉 | commit LKG。 |
| STA-008 | P0 | U | Candidate App ready後發生致命 process crash | 依明確證據 fail/rollback，不因舊 marker commit。 |
| STA-009 | P0 | U | Candidate working，User Task Manager kill Tauri | 不把版本標 failed；下次重新驗證。 |
| STA-010 | P0 | U | Stable version被 Task Manager kill | 不改 failed/LKG。 |
| STA-011 | P0 | U | Stable version啟動即 app error | 提示管理員手動 rollback；不可無 target 自動亂跳。 |
| STA-012 | P0 | U | Shared shell缺少 | exit machine-broken；state完全不變。 |
| STA-013 | P0 | U | Shared runtime缺少／損毀 | exit machine-broken；state完全不變。 |
| STA-014 | P0 | U | Version manifest/entrypoint缺少 | version failed；若有 LKG則 rollback。 |
| STA-015 | P0 | U | Candidate failed且 previous本身已 failed | 跳過 previous，選完整且未 failed 的 LKG/版本。 |
| STA-016 | P0 | U | 沒有任何 rollback target | Loud failure；不得宣稱已恢復。 |
| STA-017 | P0 | U | Manual rollback | 離開的壞版本依證據加入 failed；target必須完整。 |
| STA-018 | P0 | U | Manual rollback target=current | Idempotent no-op。 |
| STA-019 | P0 | U | Manual rollback至failed version | 預設拒絕，只有 explicit force 可做。 |
| STA-020 | P0 | U | 同版本新 revision | 可重新 stage；舊 failed revision不能阻擋修正版。 |
| STA-021 | P0 | I | `state.json` 在 replace 前中斷 | 舊 state 完整。 |
| STA-022 | P0 | I | `state.json` replace 後中斷 | 新 state 完整且 generation正確。 |
| STA-023 | P0 | I | 兩個 mutation並行 | Serialize，無 lost update。 |
| STA-024 | P0 | I | Corrupt/missing state | 指名檔案與復原方式，不產生默認新 state 蓋掉證據。 |
| STA-025 | P0 | M | 隨機 stage/promote/start/healthy/fail/kill/rollback 序列 | Production result與reference model一致。 |
| STA-026 | P0 | M | 每個序列後 property check | current完整；LKG曾真實成功；failed不含純 machine failure。 |
| STA-027 | P1 | M | 同一事件序列重播 | 結果 deterministic，operation IDs/timestamps除外。 |

## 8. Launcher、Engine Shim、Port 與 Process Lifecycle

建議檔案：`tests/test_streamlit_desktop_launcher.py`、真實 process integration。

| ID | P | 層 | 情境／操作 | 預期結果 |
|----|---|----|-----------|----------|
| LCH-001 | P0 | I | Preferred/random port可用 | Streamlit bind成功，shim回報同一個health-checked URL。 |
| LCH-002 | P0 | I | 選到 port 後被另一程序搶走 | 最多5次重新選擇；不把未ready URL交給殼。 |
| LCH-003 | P0 | I | 8501已占用 | App仍用其他port啟動。 |
| LCH-004 | P0 | I | Streamlit ready前退出 | 不開空白Tauri；回version/app failure。 |
| LCH-005 | P0 | I | Health endpoint超時 | 終止自己的process tree，回清楚錯誤。 |
| LCH-006 | P0 | I | Health 200但script import traceback | 不視為UI成功；preflight/log/UI oracle抓到。 |
| LCH-007 | P0 | I | Shim收到`--control-port/--log-dir` | 只bind 127.0.0.1指定port，log落正確位置。 |
| LCH-008 | P0 | I | Shim `/tools/{id}/start` | 只有Streamlit真正ready後回URL。 |
| LCH-009 | P0 | I | Portal `/tools/stop` | 真正通知launcher停掉Streamlit，port釋放後回成功。 |
| LCH-010 | P0 | I | Stop後再次Start | 啟動新process，可使用新port/URL。 |
| LCH-011 | P0 | I | Shim控制請求token錯誤 | 拒絕；正確token只在loopback有效。 |
| LCH-012 | P0 | I | 關閉Tauri | Launcher清除Streamlit/shim等本次process tree。 |
| LCH-013 | P0 | I | Cleanup | 不用名稱掃描殺其他`python.exe`。 |
| LCH-014 | P0 | I | 同一App雙擊兩次 | 第二份不啟動、不promote、不fail current，立即提示已執行。 |
| LCH-015 | P0 | I | 第一份crash留下stale instance lock | 下一次能識別stale lock並啟動。 |
| LCH-016 | P1 | I | 兩個不同App同時執行 | Port、token、logs、process cleanup互不干擾。 |
| LCH-017 | P0 | I | Shared shell被防毒隔離 | Machine error，不標version failed。 |
| LCH-018 | P1 | I | App data/cache/home/tmp | 全寫到app data；version/runtime hash不變。 |
| LCH-019 | P1 | I | Logs長期累積 | 每次啟動執行有界rotation，不旋轉當前session log。 |
| LCH-020 | P0 | C | Healthy marker writer與bootstrap reader | Marker body contract完全一致。 |
| LCH-021 | P0 | U | Marker=`no-session` | Bootstrap不得commit。 |
| LCH-022 | P0 | U | Marker=實際URL且clean exit | Candidate可commit。 |

## 9. Runtime Store、Lock、Lease 與 GC

建議檔案：`tests/test_streamlit_desktop_store_flow.py`、`tests/test_streamlit_desktop_state.py`。

| ID | P | 層 | 情境／操作 | 預期結果 |
|----|---|----|-----------|----------|
| STO-001 | P0 | I | 首次使用runtime | 深度verify後才建立`.complete`。 |
| STO-002 | P0 | I | 半份runtime | Fail closed，不建立sentinel。 |
| STO-003 | P0 | I | Fingerprint與runtime.json不符 | Fatal shared component error。 |
| STO-004 | P0 | I | 相同lock建第二版 | Reuse同一runtime，hash/mtime/content不變。 |
| STO-005 | P0 | I | Lock改一個pin | 建新fingerprint，不改舊runtime。 |
| STO-006 | P0 | I | 兩App共用runtime | 只有一份runtime；兩App均能啟動。 |
| STO-007 | P0 | I | Runtime verification並行 | 單一驗證writer；其他等待後重讀。 |
| STO-008 | P0 | I | Lock owner crash | Stale lock可安全接管。 |
| STO-009 | P0 | I | PID reuse | 以process start time識別，不偷走live lock/lease。 |
| STO-010 | P1 | I | Filesystem不支援hardlink lock | fallback `O_EXCL`仍正確。 |
| STO-011 | P0 | I | Empty lock正被建立 | 短時間視為claim-in-progress，不當垃圾刪除。 |
| STO-012 | P1 | I | Abandoned empty lock | 逾時後可回收。 |
| STO-013 | P0 | I | Active lease建立／正常關閉 | Lifecycle完整。 |
| STO-014 | P0 | I | Active lease對應live process | GC keep version/runtime。 |
| STO-015 | P0 | I | Stale lease | 確認owner死亡後才清理。 |
| STO-016 | P0 | I | GC keep-set | current/previous/pending/candidate/LKG/lease全部保留。 |
| STO-017 | P0 | I | Scoped GC單一App | 不刪另一App仍引用runtime。 |
| STO-018 | P0 | I | GC與updater並行 | 使用同一store lock；刪除前重新掃keep-set。 |
| STO-019 | P0 | I | Dry run | 不刪任何bytes；列出原因。 |
| STO-020 | P0 | I | Apply部分刪除 | 回報實際刪除與殘留，不用plan bytes。 |
| STO-021 | P0 | I | 全部刪除失敗 | 專用nonzero exit，不等同empty plan。 |
| STO-022 | P1 | I | Empty plan | 明確clean tree exit code，不進Y/N prompt。 |
| STO-023 | P1 | I | Full disk但store不是主因 | 說明剩餘空間、store大小，提示往其他位置查。 |
| STO-024 | P1 | I | Logs/cache才是主因 | GC plan包含真正可回收logs/cache。 |
| STO-025 | P0 | I | GC從準備刪除的orphan內執行 | 不因cwd鎖住自己而謊稱empty或成功。 |
| STO-026 | P0 | C | GC exit codes與generated `.bat` branches | 完全一致。 |

## 10. Update Provider、Stage、Notification 與離線

| ID | P | 層 | 情境／操作 | 預期結果 |
|----|---|----|-----------|----------|
| UPD-001 | P0 | I | 沒有新版 | 不下載、不改state。 |
| UPD-002 | P0 | I | 新版同fingerprint | 只stage app version，不下載runtime。 |
| UPD-003 | P0 | I | 新版新fingerprint | Stage app及缺少runtime。 |
| UPD-004 | P0 | I | Release屬於另一app_id | 拒絕，不改state。 |
| UPD-005 | P0 | I | Download中斷 | 無pending；partial只留staging且可清理/續傳。 |
| UPD-006 | P0 | I | Artifact hash錯誤 | Quarantine；無pending。 |
| UPD-007 | P0 | I | Runtime完成、app失敗 | 無pending；既有current可用。 |
| UPD-008 | P0 | I | App完成、runtime失敗 | 無pending。 |
| UPD-009 | P0 | I | `state.pending`寫入成功 | 之後才通知User。 |
| UPD-010 | P1 | I | Notification失敗 | Pending保留；寫log；更新交易仍完成。 |
| UPD-011 | P0 | I | 同revision已failed | 不自動restage。 |
| UPD-012 | P0 | I | 同version新revision | 可stage修正版。 |
| UPD-013 | P1 | I | Update source暫時offline | Current正常啟動；清楚記錄check失敗。 |
| UPD-014 | P0 | I | 更新完成後斷網，重啟 | Pending可promote並完全離線啟動。 |
| UPD-015 | P0 | I | User正在使用current時新版stage完成 | 不切換、不殺App，只通知下次重啟。 |
| UPD-016 | P1 | I | 兩個updater同時stage同release | 單一有效version；state無競態。 |
| UPD-017 | P1 | I | Payload folder被User改名 | 仍依manifest辨認，不依資料夾顯示名。 |
| UPD-018 | P0 | I | Payload解壓含zip-slip/reparse point | 拒絕，不能寫出staging root。 |

## 11. Windows、Batch、Encoding 與檔案系統

這一節必須在真實 Windows 執行，不能只用 Python 模擬。

| ID | P | 層 | 情境／操作 | 預期結果 |
|----|---|----|-----------|----------|
| WIN-001 | P0 | W | 所有generated `.bat` 含純ASCII command text | `cmd.exe` 不因UTF-8位元組offset偶發跳到行中間。 |
| WIN-002 | P0 | W | Error branch含括號、`%RC%`、pipe、`for /f` | Parse正確，保留真正exit code。 |
| WIN-003 | P0 | W | Batch連續執行100次 | 無偶發exit 255或跳行。 |
| WIN-004 | P1 | W | Console code page 950 | 訊息可顯示／可降級，不影響command parsing。 |
| WIN-005 | P1 | W | 中文＋空白安裝路徑 | Build、start、update、rollback、GC全通。 |
| WIN-006 | P1 | W | 接近Windows長路徑限制 | 要嘛成功，要嘛在建置前給明確限制，不半途壞包。 |
| WIN-007 | P0 | W | Defender短暫鎖住staging/runtime | Retry有界；成功或保留舊產物。 |
| WIN-008 | P0 | W | 永久PermissionError | 不謊報清理/切換成功。 |
| WIN-009 | P1 | W | Read-only install root | 啟動前說明需移至可寫位置；不顯示Python traceback。 |
| WIN-010 | P1 | W | UNC/network share source | Copy/verify行為明確；執行位置政策符合規格。 |
| WIN-011 | P1 | W | FAT/exFAT delivery media | 不依賴symlink/junction/hardlink保存交付語意。 |
| WIN-012 | P0 | W | WebView2不存在 | Shared machine error；不fail version。 |
| WIN-013 | P0 | W | WebView2 offline installer存在且斷網 | 能安裝或明確回報；不嘗試Evergreen網路bootstrap。 |
| WIN-014 | P1 | W | 系統時間／時區不同 | Generation、revision、lock/lease stale判定不因local timezone錯亂。 |

## 12. GUI-to-Artifact Trace Tests

建議新增 GUI backend contract，避免只測 widget 有值。

| ID | P | 層 | 情境／操作 | 預期結果 |
|----|---|----|-----------|----------|
| GUI-001 | P0 | C | GUI選project | Service收到完全相同canonical path。 |
| GUI-002 | P0 | C | GUI選nested entrypoint | Manifest保留正確相對路徑。 |
| GUI-003 | P0 | C | GUI選version v2，但state.current=v1 | Export artifact是v2。 |
| GUI-004 | P0 | C | GUI選lock file | Runtime fingerprint由該檔產生。 |
| GUI-005 | P0 | C | GUI選Store/Fat | 呼叫正確builder模式；不可混用預設。 |
| GUI-006 | P0 | I | Background worker取消 | Cancellation傳到pip/copy，UI不無條件覆蓋result訊息。 |
| GUI-007 | P0 | I | Builder回staging_left | GUI顯示實際path/bytes，不顯示「已清乾淨」。 |
| GUI-008 | P1 | I | 建置中拖曳／重繪視窗 | UI thread保持responsive。 |
| GUI-009 | P1 | C | Scan warning、excluded bytes、slot cost | GUI顯示service結果，不自行重算。 |
| GUI-010 | P1 | C | GC applied result | GUI顯示reclaimed actual與survivors，不顯示dry-run forecast。 |
| GUI-011 | P0 | I | 中文display name與explicit app_id | 產物identity一致，不碰撞。 |
| GUI-012 | P1 | I | Invalid input | 寫任何output前阻擋，焦點/訊息指出正確欄位。 |

## 13. 真實 App 與 Tauri E2E Journeys

Fixture只能做smoke；下列Journey至少使用一個真實App。建議為每個App建立可機器讀取的journey manifest。

| ID | P | 層 | Journey | 預期結果 |
|----|---|----|---------|----------|
| E2E-001 | P0 | E | Minimal fixture build→relocate→offline start | 顯示READY；close後無process/port。 |
| E2E-002 | P0 | E | CV Viewer首頁啟動 | 顯示真實App identity，不是portal空殼。 |
| E2E-003 | P0 | E | CV Viewer主要檔案選取／載入流程 | 真正功能可使用，非只驗首頁文字。 |
| E2E-004 | P0 | E | Multi-page app點遍每個declared page | 所有頁無traceback/missing module。 |
| E2E-005 | P0 | E | Custom Streamlit component | nested frontend `dist`實際render。 |
| E2E-006 | P0 | E | App讀取nested archive/model | 在刪除原project後仍讀得到。 |
| E2E-007 | P0 | E | Store v1運行時stage v2同runtime | 只傳code；通知User；v1不中斷。 |
| E2E-008 | P0 | E | 關閉後重啟promote v2 | UI顯示v2；previous/LKG正確。 |
| E2E-009 | P0 | E | Stage broken v3，重啟 | 自動rollback並顯示v2；v3 failed。 |
| E2E-010 | P0 | E | Stage v4新runtime | 新runtime一份；舊runtime供rollback。 |
| E2E-011 | P0 | E | Candidate portal開啟但不按Start就關閉 | 不commit LKG、不fail candidate。 |
| E2E-012 | P0 | E | Candidate等待超過arrival window再按Start並失敗 | 仍能抓到並rollback。 |
| E2E-013 | P0 | E | Shared shell被移除 | 不退版、不fail version，顯示machine guidance。 |
| E2E-014 | P0 | E | Port 8501被占用 | 真實Tauri顯示其他port上的App。 |
| E2E-015 | P1 | E | 中文／空白路徑完整更新流程 | Build、stage、promote、rollback皆通。 |
| E2E-016 | P1 | E | 斷網更新後啟動 | 已stage版本可離線promote；不碰網路。 |
| E2E-017 | P0 | E | 雙擊start兩次 | 只有一個App instance；state不變。 |
| E2E-018 | P1 | E | Stop→Restart→Close | Port釋放、重新ready、最後無殘留。 |
| E2E-019 | P1 | E | 連續更新/rollback 20輪 | 無state漂移、runtime重複、log無界成長。 |
| E2E-020 | P0 | E | 把交付樹複製到另一台/隔離目錄且移除source | 全journey仍成立。 |

## 14. Metamorphic 與 Differential Test Set

| ID | P | 層 | 變形 | 必須維持的性質 |
|----|---|----|------|----------------|
| MET-001 | P0 | C | `/`換成`\` | Exclusion結果相同。 |
| MET-002 | P1 | C | Project搬到中文/空白path | Manifest相對路徑、file set、fingerprint相同。 |
| MET-003 | P1 | U | Lock重新排序/改大小寫 | Fingerprint相同。 |
| MET-004 | P0 | I | Payload folder改名 | Install結果相同。 |
| MET-005 | P0 | U | 加入unreachable fixture/plugin manifest | Project classification不變。 |
| MET-006 | P0 | I | 加入`.venv`含大量假import | Import closure與artifact不變。 |
| MET-007 | P0 | C | Fat→Store | Application file set相同。 |
| MET-008 | P0 | C | Build gate→delivered preflight | REQUIRED imports相同。 |
| MET-009 | P0 | C | Scan→copy→export | Included files相同。 |
| MET-010 | P0 | I | Build相同輸入兩次 | 除允許metadata外artifact reproducible。 |
| MET-011 | P0 | I | 第二App使用同lock | Runtime不變且只一份。 |
| MET-012 | P0 | C | GC dry-run→apply | Apply只刪plan允許項；報告取實測。 |

## 15. Fault Injection Matrix

對每一個有副作用的流程，至少在下列位置注入例外／process kill。共同預期：current仍可啟動、pending不指向半份、舊成功產物不被覆蓋、訊息不謊報成功。

| ID | P | 注入點 | 額外預期 |
|----|---|--------|----------|
| FLT-001 | P0 | App artifact下載前 | State完全不變。 |
| FLT-002 | P0 | App artifact下載50% | Partial只在staging。 |
| FLT-003 | P0 | Runtime下載50% | 無runtime `.complete`。 |
| FLT-004 | P0 | Hash verify中途 | 無pending。 |
| FLT-005 | P0 | App `.complete`建立前 | Version不可見。 |
| FLT-006 | P0 | Runtime `.complete`建立前 | Runtime不可用。 |
| FLT-007 | P0 | State tmp寫入50% | 舊state完整。 |
| FLT-008 | P0 | `os.replace`前 | 舊state完整。 |
| FLT-009 | P0 | `os.replace`後、讀回前 | 新state完整。 |
| FLT-010 | P0 | Build runtime copy中途 | Cancel可停；舊package完整。 |
| FLT-011 | P0 | Atomic package swap前 | 舊package完整。 |
| FLT-012 | P0 | Cleanup刪除一半 | 回報partial與survivors。 |
| FLT-013 | P0 | GC刪除一半 | Reclaimed bytes實測；protected項不受影響。 |
| FLT-014 | P1 | Notification API失敗 | Pending仍有效。 |
| FLT-015 | P0 | Launcher啟動shell前 | 清除Streamlit process。 |
| FLT-016 | P0 | Shell啟動後、Start前被kill | 不commit/fail未執行candidate。 |
| FLT-017 | P0 | App ready後machine power-loss模擬 | 下次依marker/state規則安全恢復。 |

## 16. Mutation Tests

下列mutation應由測試必然抓到；若沒有紅，代表對應安全網仍是假的。

| ID | P | Mutation | 應失敗的測試群 |
|----|---|----------|----------------|
| MUT-001 | P0 | Marker只看存在，不讀`no-session`內容 | STA-004、STA-006、LCH-021、E2E-011 |
| MUT-002 | P0 | Marker出現立即commit，不等clean exit | STA-008、STA-009 |
| MUT-003 | P0 | GUI不傳selected version | BLD-017、GUI-003 |
| MUT-004 | P0 | Runtime reuse early-return跳過import gate | IMP-017、IMP-018 |
| MUT-005 | P0 | `dist`在任何深度排除 | INC-005、E2E-005 |
| MUT-006 | P0 | `*.zip`在任何深度排除 | INC-007、E2E-006 |
| MUT-007 | P0 | `data/*`退化成basename `*` | INC-013 |
| MUT-008 | P0 | SharedComponentError由ManifestError先catch | STA-012、STA-013、E2E-013 |
| MUT-009 | P0 | GC回報planned bytes | STO-020、MET-012 |
| MUT-010 | P0 | Probe移除`-B` | IMP-016、STO-004 |
| MUT-011 | P0 | Export帶建置機pending | BLD-018 |
| MUT-012 | P0 | 第二次start等待後繼續啟動 | LCH-014、E2E-017 |
| MUT-013 | P0 | Candidate初次交付設為null | STA-003～STA-008 |
| MUT-014 | P0 | Batch加入非ASCII command/comment | WIN-001～WIN-003 |

## 17. 建議的 CI／驗收 Gates

### Gate A：每次修改，目標 2–5 分鐘

- 受影響 focused unit tests。
- 所有 P0 Contract tests。
- State model deterministic seed set。
- Import build/device differential。
- Scan/copy Fat/Store differential。

### Gate B：每個 PR，目標 10–20 分鐘

- 全部 `test_streamlit_desktop_*.py`。
- 真實 filesystem/subprocess integration。
- Fault injection fast set。
- Generated Batch parse/exit-code tests。
- Mutation smoke set MUT-001～MUT-012。

### Gate C：每日或 release candidate

- 真實 Windows `cmd.exe`／CP950。
- 真實 portable runtime。
- 真實 Tauri/WebView2。
- Minimal、multipage、custom component及真實CV Viewer journeys。
- 斷網、占port、中文路徑、Defender transient lock。
- Update→promote→rollback→GC完整循環。

### Gate D：發布前長跑

- E2E-019連續20輪。
- Model-based隨機事件序列多seed。
- 兩App共用runtime並行更新/啟動/GC。
- 搬到隔離目錄並刪除build source後重跑。

## 18. Bug 發現後的擴展規則

每次新增bug，不得只加「重現原事故」一個test。必須填寫：

```text
Bug ID:
違反的 invariant:
最小重現:
相鄰狀態:
另一個實作端:
可證明安全網有效的 mutation:
Unit test:
Contract test:
Real artifact / E2E test:
```

最低擴展要求：

1. 原事故 focused regression。
2. 同一規則的相鄰狀態至少兩個。
3. 若規則跨兩端，加入 differential contract。
4. 若曾出現假成功，加入真實 artifact 或 E2E oracle。
5. 加一個反轉關鍵條件的 mutation，證明測試真的會紅。

## 19. 實作追蹤表

後續 AI 應另以此格式回填，不要改動 test ID：

| Test ID | 自動化檔案/函式 | 狀態 | 最近結果 | 備註 |
|---------|-----------------|------|----------|------|
| 例：STA-004 | `tests/test_streamlit_desktop_state.py::test_...` | automated | PASS | candidate no-session |

狀態只允許：`planned`、`implemented`、`automated`、`blocked`。`PASS` 是執行結果，不是實作狀態。

## 20. 完成定義

這份 catalog 的 P0 部分只有在以下條件同時成立時才算完成：

- 每個 P0 test ID 都對應到自動化測試，或有具體且核准的硬體／環境 blocker。
- 每個全域 invariant 至少有 Unit/Contract 與 Integration/E2E 兩層證據。
- Model-based lifecycle tests 能涵蓋未手寫的事件排列。
- Mutation set 能證明關鍵安全網不是和實作共享同一錯誤假設。
- 真實交付物在刪除原始專案、斷網、搬移後仍完成主要User journey。
- 測試報告區分「未執行」「被跳過」「失敗」「通過」，不得把 skipped 當綠燈。
