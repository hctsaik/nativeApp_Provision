# PROD/PRE/NEXT 三槽 + 共用相依 Store — Multi-Agent 設計結論

> 日期:2026-07-12
> 產生方式:4 個平行探索 agent(Windows 機制實測 / store 設計 / repo 既有機制盤點 / 維運與交付)
> + 1 個對抗性審查 agent 裁決。共 122 次工具呼叫,含在本機的七組真實檔案系統實驗。
> 每個主張標注【實測】【讀碼】或【推測】。
>
> 狀態:**設計結論,尚未實作。** 對應需求:「專案資料夾分 PROD/PRE/NEXT 可切換、
> 相依按版本共用不重複 copy、快速 rollback」。

---

## 0. 一句話結論

**方向正確且可行,但兩個直覺的實作方式被實驗否決了:**
PROD/PRE/NEXT 不能用「三個實體資料夾搬來搬去」,也不能用 junction/symlink —— 正確做法是
**不可變版本資料夾 + 三個單行文字指標檔(PROD.txt / PRE.txt / NEXT.txt)**;
相依不能「按 library 一顆一個資料夾」拼裝,而是**整份 runtime 按 requirements 指紋為單位共用**。
效果:每個版本槽從 474MB 縮到 ~17MB,rollback = 改寫一個文字檔(原子、秒級)。

---

## 1. 被實驗否決的兩條路(為什麼不能照直覺做)

### 1.1 junction / symlink 三槽 ❌

實測結果(本機 Windows 11、非 admin):

| 實驗 | 結果 |
|------|------|
| `mklink /J`(junction)非 admin 建立 | ✅ 成功(symlink 則失敗,重現先前 spike 的 error 1314)【實測】 |
| 執行中程序受 junction 重指/刪除影響? | 不受影響(handle 已定錨)【實測】 |
| **xcopy / robocopy / Copy-Item 複製含 junction 的樹** | **junction 被追進並「實體化」成真目錄** —— 資料翻倍;若 junction 指向包外的共用 runtime,457MB 被整個吸進每份複本【實測】 |
| `robocopy /E /SJ`(保留 junction) | 保留的是**指回來源機絕對路徑**的斷鏈(junction 目標一律存絕對路徑)【實測】 |
| USB FAT32/exFAT | reparse point 無法存在於 FAT 家族【查證】 |

**裁決:全樹禁用 junction/symlink。** 本產品的哲學是「複製資料夾=部署/備份」,
這棵樹遲早會被整根複製 —— 任何含 junction 的交付樹,不是膨脹就是斷鏈。

### 1.2 實體資料夾輪轉 rename(NEXT→PROD→PRE)❌

| 實驗 | 結果 |
|------|------|
| 目錄內有程序 cwd 時 `os.rename(目錄)` | ❌ WinError 32【實測】 |
| 目錄內只有開著的檔案 handle 時 rename | ❌ WinError 5(Python 預設 open 無 FILE_SHARE_DELETE)【實測】 |

launcher 的 log FileHandler 與 Streamlit 的 log 檔 handle **全程開著**【讀碼:launch.py】——
只要有一個 User 沒關視窗,promote 的 rename 必失敗;而且三步 rename 走到一半崩潰,
槽名錯位且無法從名字推斷處於哪一步(需要 journal 才能修復)。

### 1.3 per-library 一顆一個資料夾 ❌

Python 的 site-packages 不能安全地用多個資料夾 + PYTHONPATH 拼裝:
console_scripts 內嵌絕對路徑、.pyd 的 ABI 綁定、dist-info/RECORD 的一致性、.pth 執行順序……
地雷太多【shared-store agent 誠實評估後否決】。
改用**整份 runtime 為共用單位**,粒度粗但零風險;同 requirements 的效果其實一樣好(見 §3)。

---

## 2. 最終佈局(審查後收斂版)

```
<ROOT>\                                ← 唯一部署根;全樹零 junction、零 symlink
├─ apps\
│  └─ <app-id>\
│     ├─ start.bat                    ← User 唯一入口(讀 PROD.txt → 找 runtime → 交棒 launch.py)
│     ├─ switch.bat                   ← 管理員:promote / rollback / status(純 batch)
│     ├─ PROD.txt                     ← 一行:v1.2.0(現行版;唯一真相)
│     ├─ PRE.txt                      ← 一行:v1.1.0(前一版;rollback 目標)
│     ├─ NEXT.txt                     ← 一行:v1.3.0-rc1(待上線;可空)
│     ├─ STATUS.txt                   ← switch.bat 產出的人類可讀快照(純顯示)
│     ├─ data\                        ← logs + 使用者資料;跨版本共用;切換/GC 永不觸碰
│     └─ versions\
│        ├─ v1.2.0\                   ← 不可變,~17MB
│        │  ├─ app-package.json       (schema v2:多 runtime_fingerprint 欄位)
│        │  ├─ runtime.txt            (一行:cp311-a1b2c3d4;建置時寫死)
│        │  ├─ files.json             (逐檔 sha256,verify 用)
│        │  ├─ application\  launcher\  shell\cim-light.exe
│        │  └─ .complete              (完整性 sentinel)
│        └─ v1.3.0-rc1\ …
└─ deps\
   ├─ runtimes\
   │  └─ cp311-a1b2c3d4\              ← 不可變整份 runtime(CPython+site-packages,~457MB)
   │     ├─ python.exe …(唯讀,無 pip)
   │     ├─ runtime.json  files.json
   │     └─ .complete                 (目標機首啟深度驗證通過後才寫;USB 出貨不帶)
   └─ tools\  gc.bat / verify.bat
```

**你的三個資料夾心智模型保留了** —— 只是 PROD/PRE/NEXT 從「資料夾」變成「指標檔」:
- 用 Notepad 打開 PROD.txt 就一行版本號,一眼看懂;
- batch 的 `set /p` 可直讀,start.bat 免 Python 即可自舉;
- 切換 = 寫 tmp → `move /Y` 單檔替換,**原子**、崩潰安全(任一時刻 PROD.txt 都指向存在且完整的版本)。

### Promote / Rollback

```
promote:  檢查 NEXT 的版本與其 runtime 都 .complete → PRE.txt←舊PROD → PROD.txt←舊NEXT → 清NEXT
rollback: PROD.txt ←→ PRE.txt 對調(每步單檔原子)
```
執行中的實例**繼續跑舊版直到重啟**(啟動時已解析絕對路徑;不強殺 —— launcher 只殺自己 spawn 的樹)。

---

## 3. 省多少(實測數字推算)

| 情境 | 現行(每包全量) | 新設計 |
|------|-----------------|--------|
| 1 app × 3 槽 | 3 × 474MB ≈ 1.4GB | 457MB(runtime 一份)+ 3×17MB ≈ **510MB** |
| 3 app × 3 槽(同 Streamlit 技術棧) | ≈ 4.3GB | 457MB + 9×17MB ≈ **610MB** |
| 一次版本更新(requirements 沒變) | 搬 474MB | **搬 17MB** |
| 一次版本更新(requirements 有變) | 搬 474MB | 搬 17MB + 新指紋 runtime 457MB |
| rollback | 重發一整包 | **改一個 txt,秒級** |

指紋 = sha256(python 版本 + 平台 + 排序後的 pip freeze 完整釘板)【沿用 repo 既有 `requires_fingerprint` 骨架,讀碼證實】。
**指紋只在建置機算一次**,launcher 只做字串比對(雙實作漂移是 repo 踩過的雷)。
前置硬條件:專案必須有 lock 檔(pip freeze 產物),否則每次重 build 都可能生出新指紋 × 457MB,store 無界膨脹。

---

## 4. 交付流程(維持「複製=部署」)

- **首次部署**:整個 `<ROOT>` 複製到 USB(排除 runtime 的 `.complete`)→ 目標機整根複製 → 雙擊 start.bat
  → 首啟對 runtime 做一次逐檔 sha256 深度驗證(半份 USB 複製在這裡被抓)→ 寫 sentinel → 起 app。
- **更新**:USB 放新版本目錄(17MB)+ 目標機缺的 runtime(若指紋變了)→ 複製進對應位置 →
  NEXT.txt 寫版本名 → `start.bat --slot NEXT` 試跑 → `switch.bat promote`。
- **移除**:刪 `apps\<app>\` = app 連 data 一起消失;store 孤兒由 gc.bat 事後回收
  (「刪資料夾=零殘留」哲學的唯一讓步,寫進 README)。
- **GC**:手動觸發、預設 dry-run;keep-set = 掃所有 app 所有槽指標的指紋聯集;
  刪除順序「先刪 .complete 再 rmtree」(中斷=半殘不可見,fail-closed)。

---

## 5. 對現有程式的改動(小)

1. `launch.py`:data 目錄上提到 app 根(log handle 不再 pin 住版本目錄);
   runtime 依 manifest 的 `runtime_fingerprint` 解析到 `..\..\deps\runtimes\<fp>`;
   `.complete` 缺 → 逐檔驗證(stdlib);其餘逃逸檢查照舊。
2. `start.bat`:改為兩段 `set /p` 讀 PROD.txt → runtime.txt。
3. `builder.py`:加「輸出到 store 佈局」模式(store 命中即跳過 457MB 安裝);
   現行單包 fat 格式保留為相容輸出。
4. `switch.bat` + `gc.py` + `verify.py`:新增,約各數十行。

**明確不做**(審查裁決):junction 任何用途、實體槽 rename、FSCTL 原地重指、
SQLite journal/LKG 狀態機(txt 指標+sentinel 已達同等崩潰安全)、shell 進 store(16.6MB 且凍結,留槽內)、
per-library 拼裝、檔案級 hardlink 去重(留待實際痛點)。

---

## 6. 上線前必補(30 分鐘)

所有實驗都在開發機做的;**離線目標機**需重驗:
(1) `move /Y` 檔案原子替換 (2) 從 deps 路徑載入 python.exe/DLL 在 enforced WDAC 下可執行
(3) 首啟 457MB 深度驗證的實際秒數(HDD 情境)。
任一失敗有降級路徑:退回現行全量 fat 包(schema v2 launcher 向下相容)。

---

## 7. 待你拍板

1. **store 層級**:全機一個 `deps\`(省最多,你原話傾向這個)還是每 app 一個?→ 建議全機一個。
2. **三槽 or 兩槽**:txt 指標讓 NEXT 成本趨近零 → 建議三槽一次做。
3. **更新流程**:純資料夾複製(哲學一致,靠 sentinel 兜底)為正典;`import.bat` 只做可選輔助。同意?
4. **lock 檔硬性化**:沒有 pip freeze 釘板就拒絕建置。同意?
