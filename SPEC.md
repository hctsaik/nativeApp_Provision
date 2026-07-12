# native_Provision — 離線補給包產生器（Provision Builder）開發規格

> **給實作 AI 的說明**:本文件是完整開發規格。「已拍板決策」(§3) 不要重新設計、不要提替代方案;
> 有疑義時以本文件為準,本文件沒寫的細節參考平台既有程式碼(§14 列出必讀清單)。
> 對話用繁體中文,commit message 用英文。

---

## 1. 目的(一句話)

**在有網路的開發機上,掃描一個 CIM 平台專案,把所有 plugin 宣告的 Python 相依
(`plugin.yaml` 的 `requires:`)預先下載成「離線補給包」資料夾;
把這個資料夾複製到沒有網路的電腦後,執行一步 apply,平台引擎即可全程離線安裝所有工具相依。**

### 1.1 它在整體離線部署的位置

一台全新離線機的完整安裝物 = 三個資料夾,全部在連網機產:

| 產物 | 由誰產 | 內容 |
|------|--------|------|
| `runtime\` | 平台的 `scripts\win\build-runtime.bat`(已存在,不歸本專案管) | 可攜 Python 3.11 + 核心相依 |
| 平台專案資料夾 | `git clone --recursive`(已存在,不歸本專案管) | engine + 所有 plugin 原始碼 |
| `provision\` | **本專案(要開發的東西)** | 所有工具的離線 wheelhouse + 套用腳本 |

### 1.2 為什麼需要它(問題陳述)

平台已有「單一工具」的離線包機制(dep-pack,見 §2),但沒有「掃整個專案、一次產齊」的批次工具。
沒有它,部署者要對每個工具手動跑一次 `build_deppack.py`,漏掉哪個工具要到工廠現場才發現。
另外,torch 這類 2GB 級的 wheel 混在一般相依裡,搬運時無法辨識、無法分開處理。

---

## 2. 背景:平台既有機制(本專案是「編排者」,不重造這些)

平台 = `C:\code\claude\nativeApp`(開發機上的路徑;本工具一律以 CLI 參數收專案根路徑,不寫死)。
engine 根 = `<平台專案>\sidecar\python-engine`。

已存在且**必須重用**的機制:

1. **相依宣告**:每個工具的 `plugin.yaml` 有 `requires:`(pip requirement specifier 清單)。
   engine 啟動工具時由 `core/tool_deps.py` 建 per-tool venv 安裝。
2. **dep-pack 格式**(`core/deppack.py`):一個工具的離線包 =
   `<cache_root>\<tool_id>\{wheels\*.whl, deppack.json}`。
   - `deppack.json` = manifest:tool_id、requires、requires_fingerprint、python_tag、
     platform_tag、每個 wheel 的 `{name, sha256, size}`。
   - 裝置端 `prepare_tool_wheelhouse()` 驗證 sha256 與 requires 指紋,通過才給
     `pip --no-index --find-links` 用;驗證失敗 **fail-closed**(不退回連網)。
   - cache_root = 環境變數 `CIM_DEPPACK_CACHE`,未設則 `<engine_root>\.deppack-cache`。
     可攜模式下 launcher 已把它指到 `<APP_ROOT>\data\<project-key>\deppack-cache`。
3. **產包 API**(`core/deppack.py`,本工具直接呼叫,不要自己組 pip 指令):
   ```python
   build_wheelhouse(tool_id, requires, dest_root, *,
                    python_cmd=None, platform_tag=None,
                    python_version=None, abi=None) -> DepPackManifest
   # 產出 <dest_root>/<tool_id>/{wheels/, deppack.json};內部跑 pip download,
   # 指定 platform/python-version/abi 時自動強制 --only-binary=:all:
   ```
   以及 `load_manifest() / verify_wheelhouse() / verify_deppack_dir() / requires_fingerprint()`。
4. **單工具 CLI 前例**:`tools/build_deppack.py`(讀一個工具資料夾產一包)、
   `tools/plugin_pack.py`(含「離線可裝自檢」的做法,§8.2 會要求模仿)。

**本專案新增的價值** = 批次掃描 + 大 wheel 隔離 + 離線機套用腳本 + 人讀報告 + 增量重建。
**平台端程式碼零改動**——產出落地後的形狀就是 engine 已經認得的 dep-pack 形狀。

---

## 3. 已拍板決策(不要重新設計)

| # | 決策 | 理由 |
|---|------|------|
| D1 | **無簽章**。不做 Ed25519 簽章鏈 | 內部自用自部署;正式對外散佈時平台已有 `plugin_pack` 簽章版可用 |
| D2 | **本工具 runtime 零第三方相依**(純 Python 3.11 stdlib;pip 一律以 subprocess 呼叫;pytest 只是 dev 相依) | 離線機的 apply 腳本必須在只有可攜 runtime 的機器上跑 |
| D3 | **超過門檻的大 wheel 物理隔離到頂層 `big-deps\` 資料夾**,預設門檻 100 MB,可調 | 使用者要能一眼看到大相依、搬運時另外處理(例如單獨用硬碟帶) |
| D4 | **產包/驗證邏輯 import 被掃描專案自己的 `core.deppack`**(把 `<專案>\sidecar\python-engine` 加進 `sys.path` 後 `from core import deppack`) | 包格式永遠跟「將要吃這包的那個版本的 engine」一致,不會漂移 |
| D5 | **輸出形狀 = `CIM_DEPPACK_CACHE` 的形狀**(apply 後逐位元組等同 engine 期待的佈局) | 離線機零轉換、engine 零改動 |
| D6 | 本專案是獨立資料夾/repo(`C:\code\claude\native_Provision`),不放進平台 repo、不放進任何 plugin repo | 定位是開發機工具,跟平台與 plugin 的生命週期都不同 |
| D7 | 目標平台標籤預設 `win_amd64` / python `3.11` / abi `cp311`,**一律明示傳給 pip**(可用旗標覆寫) | 防止「用本機直譯器的 ABI 下載」事故(實際發生過:cp314 wheel 到 3.11 平台全數不可裝) |
| D8 | apply 腳本(`apply.py`)**自足單檔、只用 stdlib、不 import 本專案其它模組** | 它會被逐字複製進每個 provision 產出資料夾,在離線機獨立執行 |

---

## 4. CLI 規格

入口:`provision.py`(repo 根),`py -3.11 provision.py <子指令> ...`。

### 4.1 `build` — 在連網機產補給包

```
py -3.11 provision.py build <平台專案根路徑>
    [--dest DIR]              # 產出根,預設 .\dist\provision(gitignored)
    [--tools id1,id2]         # 只包這些工具;省略 = 全部有 requires 的工具
    [--big-threshold-mb 100]  # 大 wheel 門檻;0 = 關閉隔離
    [--platform win_amd64] [--python-version 3.11] [--abi cp311]
    [--force]                 # 忽略增量指紋,全部重產
    [--dry-run]               # 只掃描並印計畫表(工具/requires/是否會重建),不下載
```

行為順序:掃描(§7)→ 增量判斷(§8.1)→ 逐工具 `build_wheelhouse`(§2 D4)→
大 wheel 隔離(§6)→ 離線可裝自檢(§8.2)→ 寫 `provision.json` + `REPORT.md` + 複製 `apply.py`。
任一工具失敗:記錄、**繼續處理其它工具**,結束時列失敗清單並以非零 exit code 退出。

### 4.2 `verify` — 驗證一包 provision 完整性(搬運後、apply 前用)

```
py -3.11 provision.py verify <provision目錄>
```
對每個 pack:重算 wheels 的 sha256 對 `deppack.json`;被隔離的大 wheel 到 `big-deps\` 找。
再對照 `provision.json` 的工具清單(缺 pack、多出未知檔案都要報)。
輸出:每工具一行 OK/FAIL + 摘要;有 FAIL 則非零 exit code。
`big-deps\` 缺檔要**明確列出缺哪些檔、影響哪些工具**(因為使用者可能刻意分開搬運,見 §6.3)。

### 4.3 `apply` — 在離線機套用(也可在連網機測試)

```
py -3.11 apply.py --deppack-cache <目標資料夾>
```
注意:離線機上使用者拿到的是 provision 資料夾,裡面自帶 `apply.py`(D8),
所以這條指令在離線機是 `runtime\python311\python.exe apply.py --deppack-cache ...`。
`provision.py apply <provision目錄> --deppack-cache <dir>` 只是開發機上的便利轉呼叫。

`--deppack-cache` 的值(寫進 REPORT.md 讓使用者照抄):
- 可攜模式:`<APP_ROOT>\data\<project-key>\deppack-cache`(先跑一次 `start.bat` 讓它長出來)
- dev 模式:engine 根的 `.deppack-cache`(或使用者自設的 `CIM_DEPPACK_CACHE`)

行為見 §9。

---

## 5. 產出佈局與兩個設定檔

```
<dest>\provision\
├─ packs\
│  ├─ module_016\
│  │  ├─ wheels\*.whl          ← 一般 wheel;大 wheel「不在這」但列於 deppack.json
│  │  └─ deppack.json          ← 平台原生 manifest(build_wheelhouse 產的,完整、含大 wheel)
│  └─ <其它 tool_id>\...
├─ big-deps\                   ← 超過門檻的 wheel,只存一份(跨工具共用、一眼可見)
│  ├─ torch-2.6.0-cp311-cp311-win_amd64.whl
│  └─ ...
├─ provision.json              ← 總 manifest(§5.1)
├─ REPORT.md                   ← 人讀報告(§5.2)
└─ apply.py                    ← 自足 stdlib 套用腳本(§9),build 時逐字複製進來
```

### 5.1 `provision.json` 欄位

```json
{
  "format_version": 1,
  "builder_version": "<本專案版本>",
  "created_at": "<ISO8601>",
  "source_project": "<絕對路徑>",
  "git": {"platform_commit": "<hash或null>", "submodules": {"<路徑>": "<hash>"}},
  "target": {"platform_tag": "win_amd64", "python_version": "3.11", "abi": "cp311"},
  "scanned_roots": ["scripts/*/plugin.yaml", "plugins/*/modules/*/plugin.yaml"],
  "big_threshold_mb": 100,
  "tools": [
    {"tool_id": "module_016", "requires": ["torch", "ultralytics"],
     "wheel_count": 38, "total_bytes": 123456,
     "big_wheels": ["torch-2.6.0-cp311-cp311-win_amd64.whl"]}
  ],
  "big_deps": [
    {"name": "torch-2.6.0-cp311-cp311-win_amd64.whl", "sha256": "...",
     "size": 2147483648, "used_by": ["module_016", "module_006"]}
  ],
  "skipped_tools": [{"tool_id": "module_003", "reason": "no requires"}],
  "failed_tools": [{"tool_id": "...", "reason": "<pip stderr 摘要>"}]
}
```
git hash 用 `git -C <專案> rev-parse HEAD` 與 `git submodule status` 取;取不到(非 git)填 null,不報錯。

### 5.2 `REPORT.md`(人讀,繁體中文)

必含區塊:
1. 總覽:專案、時間、目標 ABI、工具數、總大小(人類單位 MB/GB)。
2. **大型相依(醒目、放前面)**:big-deps 逐檔表(檔名/大小/哪些工具用),
   加一段說明:「這個資料夾很大,可以與 provision 其餘部分分開搬運;
   到離線機後把檔案放回 `provision\big-deps\` 再執行 apply 即可。」
3. 每工具一列的表:tool_id / requires 摘要 / wheel 數 / 大小 / 本次「重建 or 沿用快取」。
4. 跳過與失敗清單(失敗含可行動的原因)。
5. **離線機操作步驟**(照抄即可的三步:複製 → 確認 big-deps 就位 → 跑 apply 指令,含 §4.3 的路徑說明)。

---

## 6. 大相依(big-deps)機制 — 本專案最重要的自訂邏輯

### 6.1 產包端(build 之後的後處理)

1. `build_wheelhouse` 正常產完 pack(此時 wheels\ 是完整的、deppack.json 已含全部 wheel 的 sha256)。
2. 掃該 pack 的 `wheels\`,檔案大小 > 門檻者:**移動**到 `provision\big-deps\`。
   - big-deps 內已存在同檔名:比對 sha256,相同 → 直接刪 pack 內那份(去重);
     不同 → **中止並報錯**(同名不同內容不該發生,wheel 檔名含版本+ABI)。
3. `deppack.json` **保持完整不改**——它是「apply 之後」形狀的權威描述,
   大 wheel 缺席只是搬運期的暫態。

### 6.2 為什麼這樣設計(給實作 AI 的心智模型,不要改動)

engine 端 `prepare_tool_wheelhouse` 驗的是「wheelhouse 內容 == deppack.json」。
所以隔離只能是**搬運層**的事:apply 把大 wheel 放回各工具 wheelhouse 後,
驗證自然通過;等於用平台自己的 sha256 驗證證明了「重組正確」。平台零改動。

### 6.3 「另外處理」的支援

使用者可能把 big-deps 資料夾抽走、用別的媒體搬運。因此:
- `verify` 與 `apply` 遇到 big-deps 缺檔:**不是靜默失敗**,要列出
  「缺哪些檔案 → 影響哪些工具 → 把檔案放回 provision\big-deps\ 即可」。
- apply 遇缺檔:受影響的工具跳過(不寫進目標、不留半套 pack),
  其餘工具照常套用;結束時列跳過清單、非零 exit code。
  **禁止留下「wheels 不完整但 deppack.json 存在」的目標目錄**(會觸發 engine fail-closed 而且訊息難懂)。

---

## 7. 掃描規則

1. engine 根 = `<專案根>\sidecar\python-engine`;不存在 → 立即報錯
   「這不是 CIM 平台專案」。
2. 收集 `plugin.yaml`,glob 相對 engine 根:
   - `scripts/*/plugin.yaml`
   - `plugins/*/modules/*/plugin.yaml`
   這兩條**必須**與 engine 的 `_scan_and_register_plugins()` 一致
   (實作前先讀該函式確認;若 engine 有第三個掃描根,補上並更新本節)。
   junction / submodule 目錄照掃(glob 會跟進去),這是正常路徑不是特例。
3. 逐檔讀 YAML(stdlib 沒有 yaml——見 §7.1):取 `id`、`requires`、`enabled`。
   - `enabled: false` → 跳過(記入 skipped)。
   - `requires` 空/缺 → 跳過(記入 skipped,reason "no requires")。
   - `id` 重複(不同資料夾同 id)→ **中止報錯**列出兩個路徑(平台歷史上真發生過)。
4. `--tools` 給定時只留交集;指定了但掃不到的 id → 報錯列出。

### 7.1 YAML 讀取(D2 的推論)

本專案 runtime 不裝 PyYAML。兩個合規做法,擇一:
- **首選**:用被掃描專案的直譯器跑一小段 subprocess
  (`py -3.11 -c "import yaml, json, sys; print(json.dumps(yaml.safe_load(open(sys.argv[1], encoding='utf-8').read())))" <檔案>`)
  ——平台環境必有 PyYAML(在核心 requirements 裡),而 build 本來就只在開發機跑。
  一次 subprocess 掃一個檔案太慢的話,可一次傳多個檔案路徑批次轉。
- 不接受:在本專案 vendor 一份 yaml parser、或手寫「夠用就好」的 YAML 子集 parser。
  (plugin.yaml 有多行字串、巢狀結構,子集 parser 遲早踩雷。)

`apply.py` 完全不需要讀 YAML(它只看 deppack.json 與 provision.json,都是 JSON),D2/D8 不受影響。

---

## 8. 增量與自檢

### 8.1 增量(重跑 build 應該是秒級,除非有變動)

對每個工具,滿足以下全部 → 跳過重建(REPORT 標「沿用快取」):
1. `packs\<tool_id>\deppack.json` 存在且可解析;
2. manifest 的 `requires_fingerprint` == 本次掃描到的 requires 的指紋
   (用平台 `core.deppack.requires_fingerprint()` 算,勿自己發明);
3. manifest 的 platform/python 標籤 == 本次目標標籤;
4. pack 的 wheels\ + big-deps\ 合起來能通過 sha256 驗證(= §4.2 verify 對單一工具的邏輯)。

`--force` 略過以上全部。重建一個工具前,先刪它的舊 pack 目錄與 big-deps 中「只有它引用」的檔
(引用計數看 provision.json;不確定就留著,寧可多佔空間不可誤刪別的工具在用的)。

### 8.2 離線可裝自檢(在開發機就證明「離線裝得起來」)

每個(重)建完的工具跑一次 pip 重解依賴圖:
`--no-index --find-links=<該工具wheels> --find-links=<big-deps>` + 目標三標籤,
確認 requires 在**只有本地檔案**的世界裡解得開。
具體 pip 指令組法**參考平台 `tools/plugin_pack.py` 的自檢步驟**(它已解過這題,照抄思路)。
自檢失敗 = 該工具 build 失敗(進 failed_tools),常見原因是某相依只有 sdist 沒有目標平台 wheel
——錯誤訊息要點名是哪個套件。

---

## 9. `apply.py`(離線機套用腳本)行為

輸入:`--deppack-cache <目標>`(必填)、`--tools a,b`(選,預設全部)、`--dry-run`。
自身位置就是 provision 根(用 `__file__` 定位 packs\ 與 big-deps\)。

對每個 pack,依序:
1. 讀 `deppack.json`,確認所需 wheel 都找得到(pack 內或 big-deps),逐檔驗 sha256。
   缺/壞 → 本工具跳過(§6.3),繼續下一個。
2. 組裝到**暫存目錄**(目標同磁碟區下,如 `<目標>\.applying-<tool_id>`):
   copy pack 的 wheels + deppack.json;大 wheel 從 big-deps 補進 wheels\
   (先試 hardlink,失敗回退 copy——跨磁碟區或檔案系統不支援時)。
3. 組裝完整後再次整包 sha256 驗證,通過 → **原子性換位**:
   目標已有同名舊目錄先改名為 `<tool_id>.old-<n>` 再把暫存目錄 rename 就位,成功後刪舊的。
   任何一步失敗 → 清掉暫存,目標維持原樣。
4. 印每工具結果;結尾總結(成功/跳過/失敗),有跳過或失敗 → 非零 exit code。

禁止事項:不得對目標目錄做「先刪再拷」(中途斷電=半套);不得連網;不得呼叫 pip
(安裝是 engine 的事,apply 只負責把檔案放到 engine 認得的位置)。

---

## 10. 專案骨架

```
native_Provision\
├─ SPEC.md                      ← 本文件
├─ README.md                    ← 簡短:目的、三條指令、指回 SPEC
├─ provision.py                 ← CLI 入口(argparse,dispatch 到 src)
├─ src\provision_builder\
│  ├─ __init__.py
│  ├─ scan.py                   ← §7(掃描與 YAML 轉 JSON subprocess)
│  ├─ gateway.py                ← §3 D4(sys.path 注入 + import core.deppack 的唯一入口;
│  │                               所有平台 API 呼叫都經過這裡,測試好 monkeypatch)
│  ├─ build.py                  ← §4.1 主流程(增量、失敗續行)
│  ├─ bigdeps.py                ← §6(隔離/去重/引用計數)
│  ├─ selfcheck.py              ← §8.2
│  ├─ manifest.py               ← provision.json 讀寫
│  ├─ report.py                 ← REPORT.md 產生
│  └─ verify.py                 ← §4.2
├─ apply.py                     ← §9(自足單檔;build 時複製進產出)
├─ tests\                       ← §11
├─ requirements-dev.txt         ← 只有 pytest
└─ .gitignore                   ← dist/、__pycache__/
```

`apply.py` 放 repo 根不放 src 底下,強調它是獨立產物(D8);
在 tests 裡直接以 subprocess 跑它,確保它真的不依賴 src。

## 11. 里程碑與驗收(依序做,每關全綠才進下一關)

> **狀態:M1–M5 全數完成(2026-07-10)。** as-built 紀錄見 §16。

| 里程碑 | 內容 | 驗收 |
|--------|------|------|
| M1 掃描+計畫 | scan.py + `build --dry-run` 印計畫表 | 對 tests 的假專案 fixture:正確列出有/無 requires、enabled:false、重複 id 報錯;**對真實 nativeApp 跑 dry-run**,工具清單與 engine 啟動 log 中註冊的工具一致(人工比對一次,結果記進 PR 描述) |
| M2 產包 | gateway.py + build.py(增量、失敗續行),先不做 big-deps | fixture 用假 gateway(monkeypatch)驗流程;真實跑一個小 requires 工具(如 `cowsay`)產出的 pack 能被平台 `core.deppack.verify_deppack_dir` 驗過 |
| M3 big-deps + apply | bigdeps.py + apply.py | 門檻設很低(如 1KB)讓小 wheel 也被隔離 → apply 到暫存 cache → 平台 `prepare_tool_wheelhouse` 驗證通過;big-deps 抽走一檔 → apply 跳過該工具、訊息可行動、目標無半套殘留 |
| M4 自檢+驗證+報告 | selfcheck.py、verify.py、manifest.py、report.py | verify 能抓出:改一個 byte 的 wheel、缺檔、多出未知檔;REPORT.md 含 §5.2 全部區塊 |
| M5 端到端 | 真實對 nativeApp 全量 build(含 torch 級大相依)→ 模擬離線機(斷網或乾淨資料夾)apply → 啟動平台、開一個有 requires 的工具 | engine log 出現 dep-pack 離線安裝成功(`pip --no-index`);全程無網路存取 |

## 12. 測試要求

- pytest;單元測試**一律不連網**(gateway 全部 monkeypatch;假 pack 用 tmp_path 手工組)。
- 需要真下載的整合測試標 `@pytest.mark.network`,預設不跑。
- apply.py 的測試用 subprocess 呼叫真檔案(驗 D8 自足性),涵蓋:正常、缺 big-dep、
  sha256 壞檔、目標已有舊版(換位)、`--dry-run`。
- Windows 專屬行為(hardlink 回退 copy)至少一個測試,用兩個 tmp 目錄模擬。

## 13. 地雷與否決表

| 行為 | 判定 | 原因 |
|------|------|------|
| 不帶 `--platform/--python-version/--abi` 就 pip download | ❌ 永遠明示 | cp314 事故:用本機直譯器 ABI 下載,目標機全裝不起來 |
| 自己組 pip 指令 / 自寫 manifest 格式 | ❌ | 一律走被掃描專案的 `core.deppack`(D4),格式漂移=工廠現場才爆 |
| 在本專案 vendor yaml parser 或手寫 YAML parser | ❌ | §7.1,用平台直譯器 subprocess 轉 JSON |
| apply 改動 deppack.json 內容 | ❌ | 它是 engine 驗章的權威,動它=破壞 fail-closed 語意 |
| apply 先刪目標再拷 | ❌ | 斷電=半套;一律暫存組裝+原子換位(§9) |
| 目標目錄留下 wheels 不完整的 pack | ❌ | engine fail-closed 會拒裝且訊息難懂;寧可整個工具跳過 |
| 在離線機跑 build | ❌ | pip download 需網路;build 只在連網機,apply 才是離線機的事 |
| big-deps 同名不同 sha256 時「挑一個」 | ❌ | 中止報錯(§6.1);靜默挑錯=難查的執行期錯誤 |
| .bat 包裝腳本寫中文註解 | ❌ | CP950 主控台會讓 cmd 解析錯位(平台實際踩過);.bat 一律純 ASCII |
| 掃描 glob 自己發明第三種 | ⚠️ | 必須鏡射 engine `_scan_and_register_plugins`,改前先讀它 |

## 14. 與平台的耦合契約(平台若改這些,本專案要跟著改)

本專案依賴平台的介面,**全部集中在 `gateway.py` 經手**:

1. `core.deppack`:`build_wheelhouse`、`load_manifest`、`verify_wheelhouse`、
   `verify_deppack_dir`、`requires_fingerprint`、`deppack.json` 檔名與欄位。
2. dep-pack 佈局:`<cache_root>\<tool_id>\{wheels\, deppack.json}` 與 `CIM_DEPPACK_CACHE` 語意。
3. 掃描 glob(§7.2)= engine `_scan_and_register_plugins` 的行為。
4. `plugin.yaml` 欄位:`id`、`requires`、`enabled`。

實作前必讀(在 nativeApp repo):`core/deppack.py`、`tools/build_deppack.py`、
`tools/plugin_pack.py`(只看自檢那步)、`engine.py` 的 `_scan_and_register_plugins`、
`docs/platform/per-tool-dependencies.md`、`docs/platform/portable-runtime-and-project-data.md`。

## 15. 非目標(不做,也不要「順手」做)

- 簽章 / 信任鏈(D1;要簽章走平台 `plugin_pack`)。
- 打包 plugin 程式碼(那是 bundle 的事;本工具只管相依 wheel)。
- 產 runtime(`build-runtime.bat` 的事)。
- 內網 registry 上傳/下載(平台 `plugin_publish/plugin_pull` 的事)。
- 離線端的完整安裝 GUI（本期仍以 `apply.py` / `warmup.py` 為準）。

### 13.1 連網建置機打包 GUI（2026-07-11 新增）

提供獨立 Tkinter GUI `provision_gui.py`，由 `start-gui.bat` 以 Python 3.11 啟動。
GUI 是既有核心上方的薄介面，不複製 build 邏輯：

- 以平台 Python 解析所選 Module 資料夾內的 `plugin.yaml`，不另寫簡化 YAML parser。
- 使用者明確選擇單一 Module 目錄或 Modules 根目錄；預設勾選其中所有啟用 Module。
- GUI 路徑順序固定為 Module → 平台 → 輸出；掃描與開始打包是上方固定主要操作，
  不依賴視窗底部按鈕（避免高 DPI 下不可見）。
- 所有啟用 Module 都能打包原始碼，即使沒有 `requires:`。
- 原始碼輸出到 `source-packages/<tool-id>/{source/,source-manifest.json}`，與 wheel pack 分離；
  Source manifest 記錄版本、requires 與逐檔 SHA-256，更新採暫存目錄原子換位。
- Source 原子換位使用唯一舊版備份名；新版已就位後，OneDrive／防毒造成的舊版清理
  `PermissionError` 採 best-effort，不得反向把成功建置判為失敗。
- 只有宣告 `requires:` 的已選 Module 才進入既有 dependency pack 流程。
- 固定目標 `win_amd64 / Python 3.11 / cp311`。
- 正式建置啟動 `provision.py build` 子程序，逐行顯示輸出。
- 使用者取消時終止完整子程序樹，包含正在執行的 pip。
- 完成時讀取 `provision.json` 顯示工具數、總大小並可開啟輸出資料夾。
- SHA-256 manifest 與離線可裝自檢仍由原本 build 核心自動執行，不增加手動 Verify 步驟。
- 打包完成後可選擇隔離驗證資料夾與工具，執行 Apply → 斷網 Warmup → Tauri Start。
- 驗證工具清單同時讀 dependency manifest 與 Source Package manifest；沒有 `requires:` 的
  Source-only Module 也能驗證，並自動跳過不適用的 Apply／Warmup。
- `category: module` 不假設會出現在 Portal 下拉選單：改驗證 PluginLoader process 載入契約
  及 Tauri engine／Portal ready；app／sheet 則維持真實選取與 Start。
- GUI 驗證通過後保留 Tauri 殼開啟，不立即 teardown，供發布人員繼續操作。
- Tauri 驗收要求 iframe 有實質內容且無 Python traceback，並要求 engine log 出現該工具的
  `Per-tool deps ready`；結果、logs 與截圖保存在驗證資料夾。
- 驗證只重建使用者明確選定資料夾內的 cache／venv／logs／WebView2 profile，不碰正式資料。

GUI 不改變 provision 格式，也不取代 CLI；CI 與進階維護仍可使用 `provision.py`。
- 解跨工具版本衝突(每工具獨立 venv,天生不衝突)。
- 非 Python 相依(CUDA、系統套件)。
- 平台端任何程式碼改動。

---

## 16. 實作紀錄(as-built, 2026-07-10)

### 落點

| 規格章節 | 實作 | 備註 |
|----------|------|------|
| §4 CLI | `provision.py`(build/verify/apply) | `apply` 以 subprocess 轉呼叫包內 `apply.py`,持續驗證 D8 自足性 |
| §5 產出佈局 | `manifest.py` + `report.py` | `--dest` **就是**補給包根(不再多一層 `provision\`);§5 的樹狀圖以預設路徑 `dist/provision` 呈現 |
| §6 big-deps | `bigdeps.py` | isolate / 去重 / `exclusive_wheels` 引用計數 / `prune_orphans`(僅全量 build 時清孤兒) |
| §7 掃描 | `scan.py` | glob 常數 `PLUGIN_GLOBS` 有守門測試;YAML 經平台直譯器 subprocess 轉 JSON |
| §8.1 增量 | `build.decide_action` | `deep=False` 供 dry-run(不算 sha256) |
| §8.2 自檢 | `selfcheck.py` | 沿用 `plugin_pack._offline_dryrun` 思路 |
| §9 apply | `apply.py` | stdlib-only;暫存組裝 + 全量 sha256 + 原子換位 |
| §14 耦合 | `gateway.py` | `_assert_contract()` 守門:平台改名 API 立刻爆,不會產出壞包 |

### 與規格的三處刻意偏離(皆為改善)

1. **新增 `src/provision_builder/_util.py`**(§10 未列):`sha256_file` / `human_size` /
   `run` / `guard_console_encoding` 被三個以上模組共用,不放共用檔會重複。
2. **`verify.py` 不經 gateway**:§14 把 `load_manifest` 列為契約,但 `verify` 必須能在
   **沒有平台專案的機器**上跑(隨身碟插上就先驗)。故改用 stdlib 讀 JSON + 自算 sha256,
   並加測試 `test_agrees_with_platform_verify_deppack_dir` 對照平台 `verify_deppack_dir`,
   證明兩者語意一致。gateway 仍是 **build** 的唯一耦合點。
3. **`apply.py` 也拒絕「manifest 未列的多餘 wheel」**(§9 未要求):隔離只會拿走檔案、
   不會多出檔案,所以多餘檔 = 這個 pack 被動過手腳。平台 `verify_deppack_dir` 也拒絕,
   提早攔下比讓 engine 在工具啟動時 fail-closed 更好懂。

### 一個真實 bug(規格沒預見,實測抓到)

`scan.py` 的 YAML→JSON 橋接原本用 `json.dumps(..., ensure_ascii=False)`。子程序的 stdout
在 CP950 主控台下是 cp950 編碼,plugin.yaml 的中文 `name:` 會被轉碼再由父程序以 utf-8 解讀
→ 亂碼。改成 `ensure_ascii=True`(純 ASCII `\uXXXX` 逸出)後與管道編碼完全無關。
這與 §13 的「`.bat` 一律純 ASCII」是同一類地雷的不同面向。

### 測試

```
py -3.11 -m pytest tests                                                 # 132 passed, 6 skipped
py -3.11 -m pytest tests --network --project-root C:\code\claude\nativeApp  # 138 passed
```

- 單元測試不連網、不需要平台(假 gateway + 假 wheel)。
- `--network`:真的 `pip download`(`test_selfcheck`、`test_e2e_offline`)。
- `--project-root`:真的 import 平台 `core.deppack` 驗契約、驗章語意一致。
- `test_e2e_offline.py` 把 `PIP_INDEX_URL` 指向死位址,**若相依鏈任一環漏掉 `--no-index`,
  pip 會去連那個位址而失敗**——「真的沒連網」因此是被測試證明的性質,不是宣稱。

### 真實驗收(對 nativeApp 的 app-lv,14 個 requires 含 torch==2.6.0)

| 步驟 | 結果 |
|------|------|
| dry-run 掃描 | 28 個 plugin.yaml,與 engine glob 一致(1 個有 requires + 27 個跳過),無重複 id |
| build | 44 個 wheel / 342.1 MB;torch(194.7 MB)進 `big-deps\`;全部 cp311/win_amd64 |
| verify | 逐檔 sha256 通過 |
| apply(缺 big-dep) | exit 1、訊息可行動、目標零殘留 |
| apply(完整) | exit 0、342 MB / **1.3 秒**(hardlink) |
| engine `prepare_tool_wheelhouse` | 驗章通過,44 個 wheel |
| engine `ensure_tool_deps`(死 index) | `ok=True`「相依安裝完成。」venv 2.0 GB |
| 裝出來能用 | `import torch(2.6.0+cpu), transformers(4.49.0), sklearn, plotly, umap` 全過 |
| 第二次 ensure | **1.5 秒**「相依已齊備(指紋命中,跳過 pip)」 |

### 未做(後續)

- `--allow-sdist` 旗標(疑難排解表提及,目前 `build_deppack.py` 有、本工具刻意不開:
  離線機沒有編譯器,允許 sdist 等於把失敗推遲到現場)。
- provision 包的 zip 打包/壓縮(目前是資料夾;搬運方式交給使用者)。
- 產包端的 wheel hardlink 去重(現況:big-deps 已跨工具只存一份,足夠)。
- 綁進 plugin 發布流程(每次釘 submodule 指標時自動重產並歸檔)。

---

## 17. GUI E2E 補做(2026-07-10, 第二輪)

原本的 as-built 只驗到 CLI + `ensure_tool_deps` 的程序內呼叫。使用者要求「E2E 的 GUI
操作過程」,補做之後**發現兩個 CLI 測不到的問題**,並因此新增一支腳本。

### 新增檔案

| 檔案 | 用途 |
|------|------|
| `warmup.py` | 離線機:借平台 `core.tool_deps` 把相依裝進 per-tool venv(需要平台專案、會跑 `pip --no-index`)。**隨補給包附帶**(build 一併複製)。 |
| `e2e/gui_offline_e2e.mjs` | 真 Tauri 殼 + 真 WebView2(Playwright over CDP)+ 斷網,三個對照組,逐步截圖。 |
| `e2e/make_figures.py` | 截圖 → 縮圖 + JPEG + base64 data URI(Artifact 的 CSP 擋外部請求)。 |
| `e2e/build_html.py` | 把實測數字 + 截圖注入模板 → `docs/offline-deploy.html`。模板有未填欄位就 fail。 |
| `docs/offline-deploy.template.html` | 圖文說明書模板。 |
| `tests/test_warmup.py` | warmup 的單元測試(用「迷你平台專案」注入假的 ensure_tool_deps)。 |

### 發現 1:既有 e2e 的 `verifyRendered` 有假陽性

`apps/host-tauri/e2e/lib.mjs` 只要 `[data-testid="stApp"]` 存在且 body 沒有 "Not Found"
就判 `RENDERED`。但 **Streamlit script 在 import 階段崩潰時,stApp 容器仍然存在**——
畫面是一段 `ModuleNotFoundError` 卻會被判成通過。

本專案的 harness 因此用**三個互相獨立**的證據:iframe 有內容且不是 traceback、
engine.log 出現 `Per-tool deps ready`、以及該工具 venv 的 `python.exe` 真的 `import torch` 成功。

### 發現 2:首次離線安裝會撞殼的 30 秒 HTTP 逾時(→ warmup.py)

`apps/host-tauri/src-tauri/src/bridge.rs::api_post` 對 engine 的請求設
`timeout(Duration::from_secs(30))`。而 engine 是在 `POST /tools/<id>/start` 的處理過程中
**同步**安裝相依(`_prewarm_deps_and_timeout`)。torch 級相依實測 76 秒 →
殼先放棄,portal 顯示 `Failed to start tool: undefined`,**但 engine 仍把相依裝完、
Streamlit 也起來了**(再按一次 Start 就成功)。

- 短期解(不用動 Rust):`warmup.py`,把安裝成本移出「按下 Start」那一刻。
- 根治(需在非 WDAC 機器重編殼):放寬該路由的逾時,或把相依安裝改成非同步 + 回報進度。

### 三個對照組(真實 `app-lv`,14 requires 含 torch==2.6.0,`PIP_INDEX_URL` 指死位址)

| 組 | 條件 | 畫面 | engine 相依 | venv import torch | 秒數 | 結果 |
|---|------|------|------------|-------------------|-----:|------|
| A | 沒有補給包 | `ModuleNotFoundError` | unavailable | 否 | 24 | PASS |
| B | 有包,直接按 Start | 首次 33s 逾時失敗;再按 → LV 介面 | ready | 是 | 82 | PASS |
| C | 有包,**先 warmup** | LV 介面 | ready (cached) | 是 | **12** | PASS |

warmup 本身耗時 66 秒(離線安裝 44 個 wheel)。B 的「再按一次」證明相依確實裝好了。

### 測試

```
py -3.11 -m pytest tests    # 143 passed, 6 skipped（含 test_warmup.py 11 個）
```

### 文件

`docs/OFFLINE_DEPLOY.md`(Markdown)與 `docs/offline-deploy.html`(圖文,6 張內嵌截圖)
內容一致;後者由 `build_html.py` 從實測結果生成,**模板有未填欄位就直接失敗**,
所以文件裡的數字不可能與實測脫節。

---

## 18. Source Package + GUI 內建 Tauri 驗證的 E2E(2026-07-11, 第三輪)

第二輪(§17)驗到 CLI + `gui_offline_e2e.mjs` 的三對照組,但那支腳本只跑 `app-lv`
一條路徑,且不經 GUI 後端。第三輪把兩個新功能——**Source Package(原始碼獨立打包)**
與 **GUI 內建的單工具 Tauri 驗證**——收進一支可重跑的端到端測試。

### 新增檔案

| 檔案 | 用途 |
|------|------|
| `src/provision_builder/source_pack.py` | Module 掃描 + 原子性 Source Package;即使工具**沒有** `requires:` 也打包原始碼並逐檔簽 sha256。 |
| `e2e/validate_package.mjs` | 單一工具的 GUI 驗證 driver:apply → warmup → 真 Tauri 殼 → Portal Start / 載入契約 → iframe 與 engine log 雙重證據。 |
| `e2e/gui_flow_e2e.py` | **本輪 E2E 入口**。對每個工具走真實 GUI 後端(`BuildProcess.run` + `validate_package.mjs`),斷言 `validation-result.json` 的 `pass`。 |
| `tests/test_source_pack.py` | source_pack 的專屬單元測試(排除 `__pycache__`/`.pyc`、逐檔簽章、多模組根探索、重複/缺 id 報錯、原子重打包)。 |

### 兩條刻意覆蓋的驗證分支

`validate_package.mjs` 依 `category` 分兩路,E2E 各挑一個真實工具打穿:

| 工具 | category | 相依 | 驗證路徑 |
|------|----------|------|----------|
| `app-lv` | app | 14 個(含 torch==2.6.0) | 真的在 Portal 選工具、按 Start,要求 iframe 畫出**非 traceback** 的 UI |
| `module_001` | module | 無 | module-load-contract(Sheet 內元件不進 Portal 工具選單,改驗 `PluginLoader.load_module_dev`) |

`category: module` 的工具走載入契約而非 Portal Start——這是刻意的:Sheet 內部元件本來
就不會出現在 Portal 的工具下拉選單,硬要 `selectAndStart` 只會誤判失敗。

### 實測結果(`PIP_INDEX_URL` 指死位址,證明離線可裝)

```
py -3.11 e2e/gui_flow_e2e.py        # 預設 --tools app-lv,module_001 → exit 0
```

| 工具 | 原始碼包 | deppack | warmup | Tauri 證據 | 結果 |
|------|----------|---------|--------|-----------|------|
| app-lv | `source-packages/app-lv`(逐檔簽章) | 增量命中既有包(**不連網**) | 80 秒(死 index 下離線裝 44 wheel + torch venv) | Portal Start → iframe `stApp=1`、`bodyLen≈800`、非 traceback;engine log `Per-tool deps ready for app-lv` | PASS |
| module_001 | `source-packages/module_001` | 無(無 requires,跳過 apply/warmup) | — | Portal 就緒 + `load_module_dev('module_001','process')` 通過 | PASS |

`app-lv` 的 deppack build 走 `decide_action`(deep=True)對既有 `dist/provision` 驗 sha256
通過 → REUSE,**整趟零網路存取**;原始碼包則是純本機檔案複製,與 deppack 分開更新。

### 與 GUI 的接線一致性

`gui_flow_e2e.py` 不另寫流程,直接呼叫 GUI「開始打包」與「Tauri 驗證」兩顆按鈕背後的
同一批後端(`gui_backend.BuildProcess` / `validate_package.mjs`);唯一差別是自動化不帶
`--keep-open`,好讓多工具序列跑完自行 teardown。因此這支 E2E 綠燈 = GUI 對應操作也會綠。

### 測試

```
py -3.11 -m pytest tests    # 158 passed, 6 skipped(含 test_source_pack.py 9 個、test_warmup.py 11 個)
```
