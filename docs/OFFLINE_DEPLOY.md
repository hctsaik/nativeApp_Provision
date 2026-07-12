# 讓模組在「沒有網路的電腦」上也能使用 — 操作說明書

> **圖文版（含 GUI 截圖）**：[offline-deploy.html](offline-deploy.html)
> — 同樣的內容，加上 6 張真實 E2E 截圖。要看「按下 Start 之後畫面長什麼樣」請看那份。
>
> 這份文件回答一個問題：**平台上的工具（模組）宣告了 Python 相依，離線機沒有 PyPI，
> 那要怎麼把它們裝起來？**
>
> 底下每一段輸出、每一個秒數，都是 2026-07-10 在真實 `nativeApp` 專案上實際跑出來的
> （工具 `app-lv`，14 個 requires，含 `torch==2.6.0`），不是示意稿。GUI 的部分由
> `e2e/gui_offline_e2e.mjs` 用 Playwright 經 CDP 連上真實 WebView2 驗證，
> **而且整台機器的 pip 索引被指向死位址**——任何連網嘗試都會立刻失敗。

---

## 0. 心智模型：一台離線機需要三個資料夾

全部在**連網機**準備好，複製過去即可。離線機**不需要**網路、不需要 admin、不需要安裝任何東西。

| 資料夾 | 是什麼 | 誰產的 | 多久做一次 |
|--------|--------|--------|-----------|
| `runtime\` | 可攜 Python 3.11 + 平台核心相依（fastapi / streamlit / numpy…） | 平台的 `scripts\win\build-runtime.bat` | Python 升版才做 |
| 平台專案資料夾 | engine + portal dist + 所有 plugin 原始碼 | `git clone --recursive` | 每次專案更新 |
| **`provision\`** | **所有工具的離線 wheel（本說明書的主角）** | **`native_Provision` 的 `provision.py build`** | 任何工具改 `requires:` 就重做 |

```
連網機                                        沒有網路的電腦
────────────────────────────                 ──────────────────────────────
build-runtime.bat      →  runtime\      ─┐
git clone --recursive  →  <專案>\       ─┼─ XCOPY ─→  <APP_ROOT>\
provision.py build     →  provision\    ─┘              ├─ runtime\python311\
                                                        ├─ engine\（= 專案）
                                                        ├─ cim-light.exe + start.bat
                                                        └─ data\<project-key>\
                                                             └─ deppack-cache\  ← apply 放這裡
```

**為什麼要有 `provision\`**：工具的重相依（torch 那類）不裝在平台核心裡，而是宣告在
各工具的 `plugin.yaml` `requires:`，由平台在工具首次啟動時裝進該工具專屬的 venv。
離線機沒有 PyPI，所以那些 wheel 必須事先下載好帶過去。

---

## 第一部分：在連網機上準備補給包

### 步驟 1-1　確認要包什麼（不下載，先看清單）

```powershell
cd C:\code\claude\native_Provision
py -3.11 provision.py build C:\code\claude\nativeApp --dry-run
```

實際輸出：

```
掃描專案：C:\code\claude\nativeApp
找到 1 個需要補給包的工具（另有 27 個不需要）

計畫（--dry-run，未下載任何東西）：
  [重建] app-lv（14 個 requires）— 尚未產包
           requires: torch==2.6.0, torchvision>=0.17.0, transformers==4.49.0, numpy==2.2.3, ...
  [跳過] app-ai4bi — no requires
  [跳過] module_002 — disabled（enabled: false）
  ...
目標標籤：win_amd64 / python 3.11 / cp311
大相依門檻：100 MB
```

**要看什麼**：

- 「找到 N 個需要補給包的工具」——應該等於你預期有 `requires:` 的工具數。
  少了就是某個 plugin.yaml 沒被掃到（多半是 submodule 沒 `git submodule update --init --recursive`）。
- 「目標標籤：win_amd64 / python 3.11 / cp311」——**這行永遠要對**。
  它代表 wheel 是為「離線機的 Python 3.11」下載的，而不是為「你這台開發機的 Python」下載的。

> **這是最容易出事的地方。** 曾經有人在 Python 3.14 的機器上 `pip download`，
> 抓到 51 個 `cp314` 的 wheel，帶到鎖 3.11 的平台上**一個都裝不起來**。
> 本工具永遠明示這三個標籤，就是為了讓那件事不可能再發生。

### 步驟 1-2　真的產包

```powershell
py -3.11 provision.py build C:\code\claude\nativeApp --dest D:\provision
```

```
[產包] app-lv（尚未產包）… pip download 14 個 requires
[完成] app-lv：44 個 wheel，342.1 MB，其中 1 個移入 big-deps\

產出：D:\provision
  工具 1 個、總大小 342.1 MB、大型相依 1 個
  下一步：讀 D:\provision\REPORT.md
```

產出長這樣：

```
D:\provision\
├─ packs\
│  └─ app-lv\
│     ├─ wheels\           43 個一般 wheel（147 MB）
│     └─ deppack.json      逐檔 sha256 的清單（描述全部 44 個）
├─ big-deps\
│  └─ torch-2.6.0-cp311-cp311-win_amd64.whl     194.7 MB  ← 大東西單獨放這
├─ provision.json          總清單（來源專案、commit、目標標籤、引用關係）
├─ REPORT.md               人讀報告（先讀這個）
├─ apply.py                離線機：搬檔案（自足，只用標準函式庫）
└─ warmup.py               離線機：先把相依裝好（見步驟 2-4，別跳過）
```

**它替你做了兩件你不會想手動做的事**：

1. **大相依隔離**：單檔 > 100 MB 的 wheel 被搬到 `big-deps\`，跨工具只存一份。
2. **離線可裝自檢**：產完包立刻用 `pip download --no-index --find-links=<本地>` 重解一次依賴圖。
   **在你的開發機就證明「這包離線裝得起來」**，而不是到工廠現場才發現少一顆 wheel。
   自檢不過 → 該工具進 `failed_tools`，`REPORT.md` 會點名是哪個套件出問題。

### 步驟 1-3　讀 `REPORT.md`，決定怎麼搬

第一個實質區塊就是大型相依：

```markdown
## 大型相依（單檔 > 100 MB）

下列 1 個 wheel 共 194.7 MB，已集中隔離在 `big-deps\` 資料夾（跨工具只存一份）。

| wheel | 大小 | 被哪些工具使用 |
|-------|-----:|----------------|
| `torch-2.6.0-cp311-cp311-win_amd64.whl` | 194.7 MB | `app-lv` |

> 這個資料夾很大，可以與補給包的其餘部分分開搬運（例如另用一顆隨身硬碟）。
```

你有兩個選擇：**整包一起搬**（342 MB，最簡單），或**分開搬**（`big-deps\` 走大容量媒體）。
忘了把 `big-deps\` 放回去也不會壞事——用到它的工具會被明確跳過（見步驟 2-3），其餘工具照常裝。

---

## 第二部分：在沒有網路的電腦上

### 步驟 2-1　複製 + 驗證（可選但建議）

```powershell
py -3.11 provision.py verify D:\provision
```

```
  [OK]   app-lv

  結果：全部通過（1/1 個工具可套用）
```

`verify` 逐檔重算 sha256，抓得到：改了一個 byte 的 wheel、被截斷的檔案、缺檔、
以及清單上沒有的多餘檔案。它**不需要平台專案**，隨身碟插上就能驗。

### 步驟 2-2　找出 `--deppack-cache` 要指到哪裡

這是唯一需要動腦的參數。它必須等於平台 engine 的 `CIM_DEPPACK_CACHE`：

| 啟動方式 | `--deppack-cache` |
|----------|-------------------|
| 可攜模式（`start.bat`） | `<APP_ROOT>\data\<project-key>\deppack-cache` |
| 開發模式（`start-dev.bat`） | `<平台專案>\sidecar\python-engine\.deppack-cache` |
| 有自訂 `CIM_DEPPACK_CACHE` | 該環境變數的值 |

> 可攜模式的 `<project-key>` 是 `start.bat` 算出來的（`<專案資料夾名>-<絕對路徑sha256前8碼>`）。
> **先雙擊一次 `start.bat` 讓 `data\<project-key>\` 長出來**，再照抄那個路徑。
> 這樣也順便確認 app 本身跑得起來（此時工具還不能用，因為相依還沒裝）。

### 步驟 2-3　套用（搬檔案）

```powershell
runtime\python311\python.exe D:\provision\apply.py --deppack-cache D:\CIM\data\engine-a1b2c3d4\deppack-cache
```

```
  [OK]   app-lv：已套用（44 個 wheel，342.1 MB）

總結：成功 1、跳過 0、失敗 0（共 1 個工具）
```

342 MB 在 **1.3 秒**完成——同磁碟區走 hardlink，沒有真的複製。

**如果 `big-deps\` 分開搬運、還沒放回去**：

```
  [跳過] app-lv
         大型相依未就位：torch-2.6.0-cp311-cp311-win_amd64.whl

缺少的大型相依（把檔案放回 D:\provision\big-deps 後重跑 apply 即可）：
  - torch-2.6.0-cp311-cp311-win_amd64.whl
影響的工具：app-lv
```

exit code = 1，目標資料夾**完全乾淨**。把檔案放回去、重跑一次就好。

`apply.py` 做的事很少，這是刻意的：

- 它**只搬檔案**：把 `packs\<工具>\` 放到目標位置，把 `big-deps\` 的大 wheel 補回各工具的 `wheels\`。
- 它**不執行 pip、不連網**。真正的安裝是平台 engine 在工具首次啟動時做的。
- 它採「暫存組裝 → 全量 sha256 驗證 → 原子性換位」。中途失敗或斷電時目標維持原樣，
  不會留下 wheels 不完整但 `deppack.json` 存在的目錄。

### 步驟 2-4　暖機（**別跳過**）

```powershell
runtime\python311\python.exe D:\provision\warmup.py ^
    --project       D:\CIM\engine ^
    --deppack-cache D:\CIM\data\engine-a1b2c3d4\deppack-cache ^
    --tool-venvs    D:\CIM\data\engine-a1b2c3d4\tool-venvs
```

```
  [暖機] app-lv（14 個 requires）… 離線安裝完成（66s）

全部就緒（1 個工具）。現在啟動平台，第一次點開工具就會直接算繪。
```

**為什麼需要這一步**（GUI E2E 實測發現，見下面的對照組 B）：

Tauri 殼的 HTTP bridge（`bridge.rs::api_post`）對 engine 的請求有 **30 秒逾時**；
而 engine 是在 `POST /tools/<id>/start` 的處理過程中**同步**安裝相依的
（`_prewarm_deps_and_timeout`）。torch 級的相依實測要 76 秒——殼會先放棄，
畫面顯示 `Failed to start tool: undefined`。相依其實裝完了，只是沒人收到回應。

`warmup.py` 把安裝成本移出「按下 Start」那一刻。它跟 `apply.py` 的分工：

| | 需要平台專案 | 跑 pip | 連網 | 只用 stdlib |
|---|---|---|---|---|
| `apply.py` | 否 | 否 | 否 | 是 |
| `warmup.py` | **是** | **是**（`--no-index`） | 否 | 否 |

`warmup.py` 借用平台自己的 `core.tool_deps`——同一套驗章、同一句 `pip --no-index`。
因此它**必須用平台會用的那顆 Python 跑**（venv 的 ABI 綁在建立它的直譯器上）。
重跑一次是秒過（指紋命中）。

### 步驟 2-5　啟動平台，把工具點開

照平常方式啟動（`start.bat` 或 `start-dev.bat`），在 portal 選工具、按 Start。

**實測：先跑過 warmup 的機器，第一次按 Start 到畫面算繪完成只花 12 秒。**
engine.log 出現：

```
Per-tool deps ready for app-lv: (cached)
```

`(cached)` 代表指紋命中，這次連 pip 都沒呼叫。全程沒有任何一個位元組來自網路。

截圖見 [offline-deploy.html](offline-deploy.html)。

---

## 這份說明書是被證明的，不是被宣稱的

`e2e/gui_offline_e2e.mjs` 跑三個對照組：真的 Tauri 殼、真的 WebView2、真的 Streamlit 子程序，
每組都把 `PIP_INDEX_URL` 指向 `http://127.0.0.1:1/simple`（死位址）。
**若 engine 的安裝路徑漏掉 `--no-index`，pip 會去連那個位址並失敗，B/C 組就會紅。**

| 對照組 | 條件 | 畫面 | engine 相依 | venv `import torch` | 秒數 | 結果 |
|---|---|---|---|---|---:|---|
| A | 沒有補給包 | `ModuleNotFoundError` | unavailable | 否 | 24 | PASS |
| B | 有補給包，直接按 Start | 首次於 33s 逾時失敗；再按一次 → LV 介面 | ready | 是 | 82 | PASS |
| C | 有補給包，**先跑 warmup** | LV 介面 | ready (cached) | 是 | **12** | PASS |

判準刻意用三個**互相獨立**的證據，缺一不可：iframe 畫出真正的 UI（而不是 Python traceback）、
engine.log 說相依 ready、以及直接拿該工具 venv 的 `python.exe` 去 `import torch`。

### 對照組 A — 忘了套用補給包

平台的 `_prewarm_deps_and_timeout()` 註解寫著 *never block launch on dep handling*
——相依裝不起來時，engine 只記一行 warning，**照樣把工具啟動起來**。
所以使用者不會看到「工具打不開」，而是看到工具打開了、工具列顯示 running、
畫面上是一段 `ModuleNotFoundError: No module named 'torch'`。

**這只有把 GUI 真的點開才會發現；CLI 測不到。** 看到那一頁，就是去跑 `apply.py` 和 `warmup.py`。

### 對照組 B — 套用了，但跳過 warmup

engine 開始在請求裡裝 torch。33 秒後殼的 HTTP bridge 逾時，portal 顯示
`Failed to start tool: undefined`——但 engine 沒有停，它在背景把相依裝完、
也把 Streamlit 拉起來了。**再按一次 Start 就會成功**（這次指紋命中，幾秒內回應）。

這是「看起來壞掉、其實只是慢」的典型樣子。步驟 2-4 就是為了讓這一跳不會發生。

### 順帶修掉一個假陽性

平台既有的 E2E harness（`apps/host-tauri/e2e/lib.mjs` 的 `verifyRendered`）只檢查
`[data-testid="stApp"]` 存在且沒有 “Not Found” 就判 `RENDERED`。
但 Streamlit script 在 import 階段崩潰時，**stApp 容器仍然存在**——
對照組 A 那張錯誤畫面會被判成通過。本次的 harness 因此改成必須
「有內容且不是 traceback」，並額外要求 engine.log 與 venv 的兩項獨立證據。

### 建議給平台的修正

真正的根治是讓 `bridge.rs` 對 `POST /tools/<id>/start` 放寬逾時
（或把相依安裝改成非同步、回報進度）。在殼重編之前，`warmup.py` 是不需要動 Rust 的完整解。
（本機 WDAC 擋 cargo，殼要在別台機器重編。）

---

## 第三部分：日常維護

### 有工具改了 `requires:`，怎麼更新補給包？

在連網機重跑同一條指令就好。**它是增量的**：

```
[沿用] module_016（requires 與目標標籤皆未變、既有包驗證通過）
[產包] app-lv（requires 已變更）… pip download 15 個 requires
```

判斷依據有四個，任一不符就重建：requires 指紋、python 標籤、平台標籤、既有包的 sha256 驗證。
所以**沒動的工具秒過，不會重抓 195 MB 的 torch**。想強制全部重來：加 `--force`。

### 只想處理某幾個工具

```powershell
py -3.11 provision.py build C:\code\claude\nativeApp --tools app-lv,module_016
```

用了 `--tools` 時，工具不會去清理 `big-deps\` 裡沒人引用的檔案（看不到全部引用關係，刪了可能誤傷）。
全量 build 才會清。

### 一個工具在離線機上壞了，想重裝

刪掉該工具的 venv（`data\<key>\tool-venvs\<tool_id>\`），重跑 `warmup.py`（或重啟平台後點開它）。
補給包不用重搬。

### 搬移整個 `<APP_ROOT>` 之後

venv 內含絕對路徑。搬完刪掉 `data\*\tool-venvs\`，重跑 `warmup.py`，離線重建。

---

## 疑難排解

| 症狀 | 原因 | 解法 |
|------|------|------|
| 工具開得起來，畫面是 `ModuleNotFoundError` | 忘了在這台機器跑 apply/warmup，或 `--deppack-cache` 指錯位置 | 對照步驟 2-2 的表確認路徑，重跑 apply + warmup |
| 按 Start 後顯示 `Failed to start tool: undefined` | 首次相依安裝超過殼的 30 秒逾時 | 先跑 `warmup.py`；或等 engine 裝完後再按一次 Start |
| build 說「這不是 CIM 平台專案」 | 第一個參數指錯 | 指向含 `sidecar\python-engine\engine.py` 的那一層 |
| build 說「找不到任何 plugin.yaml」 | submodule 沒 clone | `git submodule update --init --recursive` |
| build 說「工具 id 重複」 | 兩個資料夾用了同一個 `id:` | 先修 plugin.yaml；補給包以 tool_id 為資料夾名，重複會互相覆蓋 |
| build 某工具「離線可裝自檢失敗」 | 某個相依沒有 win/cp311 的 wheel（只有 sdist） | 訊息會點名套件。改用有 wheel 的版本——離線機沒有編譯器 |
| build 說「沒有 PyYAML」 | 用了錯的直譯器跑本工具 | 用 `py -3.11`，或 `--python "py -3.11"` |
| warmup 說「是空的，請先跑 apply.py」 | 順序反了 | 先 `apply.py` 再 `warmup.py` |
| warmup 警告 Python 版本不是 3.11 | 用了別顆直譯器 | 改用平台會用的那顆（可攜模式 = `runtime\python311\python.exe`） |
| verify / apply 說「大型相依未就位」 | `big-deps\` 分開搬運後沒放回去 | 把檔案放回 `big-deps\` 再跑一次 |
| verify / apply 說「sha256 不符」 | 搬運中檔案損毀 | 重新複製該檔；仍失敗就在連網機重產 |
| 工具啟動時「dep-pack 驗證失敗，拒絕安裝」 | 目標位置的包被改過或不完整 | 重跑 apply（原子性覆蓋） |

---

## 附錄：為什麼設計成這樣

**問：為什麼不把 torch 裝進平台核心，讓所有工具共用？**

三個理由。**版本綁架**：不同工具對同一個套件的版本約束會打架，同一個 site-packages
只能犧牲其中一個。**部署矛盾**：可攜模式的賣點是「複製資料夾＝部署」，torch 進核心會讓
每台機器、每個專案都背著 2 GB，即使它根本不跑 AI 模組。**卸載不乾淨**：工具移除後，
它留在共用環境裡的套件沒人敢刪。

per-tool venv 讓這三個問題都消失，代價是「相依必須事先下載好」——而那正是本工具的職責。

**問：為什麼 `apply.py` 不直接跑 pip 裝好？**

因為安裝需要知道「裝進哪個 venv、用哪顆 Python、什麼時候裝」，那是平台 engine 的知識。
`apply.py` 只負責把檔案放到 engine 找得到的位置，職責單一、失敗模式少、可以在只有標準
函式庫的環境跑。而且 engine 的驗章（fail-closed）因此仍然是最後一道關卡。
`warmup.py` 則是刻意分開的第二支腳本：它承認自己需要平台，也承認自己會跑 pip。

**問：為什麼大相依要獨立一個資料夾，而不是壓縮進去就好？**

因為「知道大東西在哪」本身就是使用者要的資訊。195 MB 的 torch 跟其餘 43 個 wheel
加起來是 342 MB，郵件寄不了、隨身碟要挑。把它單獨拉出來，使用者可以自己決定怎麼搬運。
`deppack.json` 保持完整、apply 時用 sha256 驗證重組，所以「分開搬運」不犧牲任何正確性保證。

---

## 重跑這份文件的證據

```powershell
node e2e/gui_offline_e2e.mjs dist/provision e2e/out   # 三個對照組 + 截圖
py -3.11 e2e/make_figures.py                          # 截圖 → 內嵌 data URI
py -3.11 e2e/build_html.py                            # 注入模板 → offline-deploy.html
```

`build_html.py` 會在模板還有未填欄位時直接失敗——**文件裡的數字不可能與實測脫節**。
