# Handoff 審閱建議(cv_reviewer_update)

> 審閱日期:2026-07-11。只提建議、未動任何程式或 handoff 文件。
> 審閱範圍:`handoff/cv_reviewer_update/` 四份文件 + 交叉核對 repo 內被引用的程式
> (`src/provision_builder/package_services.py`、`local_services.py`、`tests/test_package_services.py`、
> `deploy/local/*`)。

## 總評

文件品質整體很好:自足、閱讀順序清楚、決策(12 條)明確、slice 切法合理、
「現有能力不可重造」清單能有效防止下一位 AI 重造輪子。引用的檔案全部存在,
測試數量(5 個)與描述相符,zip 與資料夾目前同步。

以下建議依「會直接誤導下一位 AI」→「設計矛盾」→「實作細節」→「流程」排序。

---

## A. 會直接誤導下一位 AI 的缺口(建議優先補文件)

### A1. 完全沒提 WDAC 約束 —— Slice 7 可能一開工就撞牆

本機已確認的硬約束:**WDAC 擋 cargo、且沒有無 WDAC 的建置機 → Native_App 的
Rust 殼(Tauri)凍結、不可重編**。但 handoff 隻字未提。

Slice 7「Native_App Local Agent」若被理解成「寫進 Tauri/Rust 殼」,在此環境
根本做不出來。建議在 `ARCHITECTURE_AND_DECISIONS.md` 明載:

- Local Agent 必須以 **Python sidecar** 形式實作(由既有殼以現有機制啟動),
  或走「免重編路徑」(既有殼可載入的擴充點);
- Rust 殼何時解凍(取得建置機)是外部依賴,不可作為任何 slice 的前置。

同理 Slice 6 Web Console:前端若走 vite/esbuild(原生 exe),在 WDAC 下能否
執行**未驗證**。建議 Slice 6 開工前先實測,或第一版改用 server-side rendering
(FastAPI + Jinja)完全避開前端建置鏈。

### A2. `apply.py` 的已知缺口沒寫進「現況限制」

`CURRENT_STATE_AND_FILE_MAP.md` §3 把「stdlib-only `apply.py` 原子套用」列為
可重用的完整能力,但已知現況是:**`apply.py` 不套用 source-packages(目前靠手
動),source-apply 與 assemble 整包是待辦**。

這直接影響 Slice 4 驗收(「完全斷網 apply/warmup/Tauri E2E 通過」)——下一位
AI 會以為只要「組包」就好,實際上還缺 source-apply 這塊。建議在 §4 現況限制
加一條,並在 Slice 4 的工作項明列 source-apply。

### A3. 缺文件鏈上游連結

三 repo 整體計畫在 `C:\code\claude\CV_Viewer\PLATFORM_ARCHITECTURE_AND_DEPLOYMENT.md`
(文件鏈:TAURI_NATIVEAPP_HANDOFF → NATIVEAPP_ARCH_REVIEW →
NATIVEAPP_NATIVE_TOOL_POC_DESIGN → 該文件)。handoff 自稱自足入口,卻沒鏈到
上游;若兩處決策分歧(例如 Phase 1 免重編 vs Slice 7 的 agent 位置),下一位
AI 無從發現。建議 README 加「相關文件」一節,並寫明衝突時以哪份為準。

### A4. zip 與資料夾雙份維護的發散風險

`cv_reviewer_update_ai_handoff.zip` 目前與資料夾同步,但之後改了 md 忘了重打
zip 就會發散,而且 zip 版本會「看起來一樣新」。建議三選一:

1. 刪掉 zip,要交接時再臨時打包;
2. README 附一行重打包指令,並約定「改 md 必重打」;
3. zip 內放一個 `GENERATED_AT` 標記,提示讀者以資料夾為準。

---

## B. 設計矛盾(建議定案後改文件)

### B1. `.napp` 內含 `big-deps/` 與「big-deps 隔離」哲學矛盾 ⭐ 最重要

- `SPEC.md` §1.2 的立項理由之一就是:torch 這類 2GB wheel 要能「辨識、分開處理」;
- `ARCHITECTURE_AND_DECISIONS.md` §6 客戶端已設計了內容定址的 `blobs/sha256/`;
- 但 §5 package contract 又把 `big-deps/` 塞進 `.napp` 本體。

後果:每個版本的 `.napp` 都內嵌 2GB → 只改 source 的小版更新也要重下 2GB、
MinIO 每版重複儲存,並且與決策 #8(只改 source 應重用既有 venv)的精神衝突。

建議:`.napp` 只放 big-deps 的 **sha256 參照清單**(manifest),blob 本體以
內容定址存 MinIO(`blobs/sha256/<hash>`),agent 比對本機 `blobs/` 只下載缺的。
`checksums.json` 仍涵蓋參照清單本身,完整性不受影響。

### B2. Slice 2 API 用了不存在的 `release_id`

`IMPLEMENTATION_PLAN.md` 的 `POST /api/v1/artifacts/{release_id}/download-url`
引用 `release_id`,但 domain model(`Release`、SQLite/PG schema)都是複合鍵
`(app_id, version)`,沒有 surrogate id。要嘛路由改成
`/{app_id}/{version}/download-url`,要嘛在 registry 加 id 欄位——建議先定案,
避免 API 與 schema 各自漂移。

### B3. 版本比較語意未定義

狀態機的「update found」需要判斷版本差異。建議明文寫死:**desired state 採
channel 指標的相等性比較(pointer equality),不做 semver 排序**。好處:

- 免去版本字串解析/排序的爭議;
- rollback 天然支援(把 channel 指回舊版即可,agent 不會因「版本變小」而拒絕)。

### B4. 狀態機缺兩個恢復細節

1. **last-known-good 何時晉升**:新版本進 OBSERVING 後,多久/憑什麼事件升格
   為 LKG?(建議:成功啟動 + healthcheck 通過 N 次或運行滿 T 時間)
2. **ACTIVATING 中斷電**:Slice 7 驗收有列斷電,但設計文件沒寫恢復機制。
   建議明載:activation 過程寫 journal 進 `agent/state.db`,開機時 reconcile
   (發現半套用狀態 → 回滾到 LKG)。

### B5. SQLite lab 與 PostgreSQL schema 已經漂移

`deploy/local/init-registry.sql` 有 `dependency_fingerprint`、
`platform_constraint`、`audit_events`;SQLite 版(`package_services.py`)都沒有。
Slice 3 的 contract tests 遲早會揭露,但建議先明定「schema 單一權威來源」策略
(例如:logical schema 定義一份,兩個 adapter 各自 migration 對齊),避免兩套
欄位各自演化。

---

## C. 現有程式的實作細節(建議寫進 Slice 2 前置)

### C1. `publish()` 順序問題 → 重複版本的錯誤不穩定

`package_services.py:207-215`:先 `objects.put()` 再 `registry.create_release()`。
version 重複時:

- 第一次:object 上傳成功、DB insert 失敗 → **留下孤兒 immutable object**;
- 之後重試同版本:直接在 object store 撞 `FileExistsError`。

Slice 2 驗收要求「重複版本…有穩定 error code」,但現況錯誤型別取決於失敗點。
建議在 service 層先查 registry 是否已有該版本,並定義 error taxonomy
(`DuplicateVersion` / `ArtifactMissing` / `HashMismatch` / `UnknownChannel`),
HTTP 層只做狀態碼映射,不自行判斷。

### C2. `ObjectStore.put` 回傳 sha256 的 MinIO 陷阱

contract 規定 `put` 回傳 `(sha256, size)`。MinIO 的 multipart ETag **不是**
sha256,所以 `MinioObjectStore.put` 必須在 client 端邊串流邊算 sha256。建議把
這點寫進 Slice 3 註記,contract tests 加一條「大檔 multipart 上傳後 sha256 正確」。

### C3. `_safe_part` 太寬鬆

`package_services.py:20-23` 只擋 `/ \ NUL . ..`,version 仍可含冒號、空白、`*`
等字元,會直接進 object_key;`FileObjectStore` 在 Windows 上遇冒號即炸。建議
收斂成 regex(如 `^[A-Za-z0-9._-]+$`),且 app_id/version 規則與 Slice 4 的
`app.yaml` JSON Schema 共用同一條定義。

### C4. `resolve`/`download` 沒有 release 撤回機制

`resolve` 不看 `status`(promote 時已檢查,但之後若要 yank 壞版本沒有路徑)。
不用現在做,但建議在 error taxonomy 預留 `ReleaseYanked`,並在決策清單註明
「撤回 = 加 yanked 狀態 + channel 指回舊版」,不是刪 object(不可變原則不破)。

---

## D. 流程與優先序建議

### D1. 插入「Slice 1.5」:error taxonomy + publish 順序修正

很小(半天內),但 Slice 2 的驗收直接依賴它(見 C1)。先做可避免 HTTP 層
把錯誤分類邏輯寫進 route handler。

### D2. 簽章 ADR 提前到 Slice 4 之前

決策 #12 要求 production 驗簽,但 `signature.json` 的格式、演算法(建議
Ed25519 / minisign 類)、**離線機的公鑰分發與輪替**都未定。這些影響
`package.json` schema(Slice 4)與 agent verify(Slice 7),晚定會回頭改
schema。建議先出一頁 ADR,不用實作。

### D3. Slice 3 的 Docker 依賴要有替代路徑

本機無 `docker`(handoff §4 已註明;可能同樣受 WDAC 影響,取得的可能性要先
確認)。建議 contract tests 設計成**吃環境變數指向外部 PG/MinIO endpoint**:
本機跑 SQLite/filesystem 版,PG/MinIO 版留給有 docker 的機器或 CI 跑。把這寫
進 Slice 3,免得下一位 AI 在此機浪費時間裝 docker。

### D4. `cv_reviewer` repo 路徑確認升級為 Slice 4 的阻斷性前置

handoff 已提醒「不可猜成工作區中其它 CV 專案」(很好),建議再升一級:列為
Slice 4 的 blocking precondition,且明定確認方式 = 直接問使用者。

---

## 附:核對紀錄(供追溯)

| 項目 | handoff 宣稱 | 實際核對 |
|------|--------------|----------|
| `package_services.py` 等 6 個檔案 | 存在 | ✅ 全部存在 |
| 聚焦測試 5 個 | 5 passed | ✅ 檔內確為 5 個 `def test_` |
| zip 與資料夾同步 | — | ✅ 時間戳同一分鐘(2026-07-11 16:41) |
| immutable put / SHA-256 download / promote 檢查 status | 已實作 | ✅ 與描述一致 |
| PG schema 與 SQLite schema 一致 | 未宣稱 | ⚠️ 已漂移(見 B5) |
