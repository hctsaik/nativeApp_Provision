# 12 — 平台 App 管理與開發模型(綜合設計)

> 產出方式:multi-agent workflow(2026-07-12)——3 個盤點者實讀兩個 repo 與決策文件
> → 3 個獨立設計提案(全 App 化 / 底座+精選 App 雙軌 / 分層收斂)
> → 2 個對抗性評審(維運現實視角、架構一致性視角;**兩者一致裁定「分層收斂」勝出**)
> → 1 個總設計師綜合。共 9 agents、103 次工具呼叫,關鍵事實均經實碼查證
> (含 `builder.py:91` 空 wheels、`agent.py` venv 重用缺陷、`plugins/labeling`
> 為 Junction → `C:\code\claude\ANnoTation` 等,主管線已人工抽查)。
>
> 回答的問題:「為什麼只有 cv_reviewer 有安裝流程?Annotation/AI4BI 怎麼上架?
> 到底要怎麼做一個平台的管理與開發?」

## 1. 直接回答使用者的困惑

**「為什麼只有 cv_reviewer 有安裝流程?」——因為那不是安裝系統的限制,而是 demo 資料只 seed 了它一個。**

具體事實:

- 新的 .napp 更新系統(`native_Provision` 的 `napp/` + `build_worker/` + `control_plane/` + `native_agent/` + `web_console/`)核心程式碼中 `cv-reviewer` 出現次數為 **0**。`native_agent/management.py` 的 docstring 明文寫著「There is deliberately no cv-reviewer special-casing here」。
- cv-reviewer 唯一的來源是 `demo/lab_serve.py` 的 `APP_ID = "cv-reviewer"`——一個合成的示範 app(`app.py` 只有一行,連 Streamlit 都不是),seed 進 `.lab/` 給你看流程長什麼樣。
- **Annotation 和 AI4BI 其實一直都「在平台裡」**,只是走另一條路:engine 的 `_scan_and_register_plugins`(`engine.py` 行 655-720)掃 `plugins/*/modules/*/plugin.yaml` 自動上架。AI4BI 在 `plugins/bi/modules/ai4bi/plugin.yaml`(id: `app-ai4bi`);Annotation 是 `plugins/labeling` junction 連到 `C:\code\claude\ANnoTation`,含 module_008~026 共 19 個 module 加 `sheet-annotation`。
- 它們在 prod 看不到,不是因為「沒安裝流程」,而是 prod 過濾(`engine.py` 行 1920 起)要求 `enabled_prod=1`,而 `seed.yaml` 的 `prod_enable_tools` 只列了 sheet-edge-analysis、sheet-annotation、management-center、labelme-dino——`app-ai4bi`/`app-lv` 從沒被 prod-enable 過。**注意:app-\* 類工具只需 enabled_prod 旗標,不需 snapshot**(評審實碼核對的修正)。

**「流程感覺不通用」——因為現在同時存在四條散佈路,而且互不認識:**

1. vendor submodule + editable install(AI4BI、LV)
2. junction/symlink 到外部 repo(labeling)
3. `fleet_publish.py` + `registry_server.py` 的 module 級簽章散佈(`CIM_DISTRIBUTION_SOURCE`)
4. 舊「整平台離線 provision」(`provision.py` → packs/wheels/big-deps → `apply.py`/`warmup.py`)

.napp 是第五條,而且它與 nativeApp 之間還缺兩段接線(見 §2)。你的困惑是結構性的,不是理解錯誤。

## 2. 現況地圖:兩套 pipeline 與兩個宣告世界

### 宣告世界 A:plugin.yaml(nativeApp,已存在、活著)

| 面向 | 機制 |
|---|---|
| 宣告 | `plugin.yaml`:id(必填)、name、version、runner、enabled、requires: 等(engine.py 655-720 讀取) |
| 分類 | **不是看 yaml 的 category 欄位**,是 `_derive_category(tool_id)` 由 id 前綴推導(行 769-780):`app-*`→app、`sheet-*`→sheet、其餘→module |
| 上架 | 開機或 `POST /reload` 掃描 → per-device `tools.sqlite`(衍生快取,可 `--rebuild-catalog` 重建)→ Portal `GET /tools` |
| 啟動 | `POST /tools/{id}/start` 按 category 分派;runner 對照表在 engine.py 668-673(`runner: bi` → `tools/bi_runner.py`) |
| 相依 | `requires:` → `core/tool_deps.py` 建 per-tool venv(`.tool-venvs/<tool_id>`) |
| prod | module 類:publish snapshot(`tool_versions` content_json,in-memory exec + sandbox deny-list)+ enabled_prod;app 類:只需 enabled_prod |
| 散佈 | git submodule / junction / `fleet_publish.py` module 快照 / 舊 provision |

### 宣告世界 B:app.yaml(.napp,已存在但兩處是空殼)

| 面向 | 機制 |
|---|---|
| 宣告 | `app.yaml`/`app.json`(`schemas/app.schema.json`):id/version/entrypoint 必填,requires/big_deps/healthcheck/data_dirs/migrations 選填 |
| 建置 | `POST /api/v1/applications/{id}/build` → BuildWorker(validate→selfcheck→build→verify→healthcheck→publish→promote) |
| 治理 | channel、分批 rollout(10→50→100%、auto-pause、approval gate)、yank、audit(`control_plane/rollout.py`) |
| 裝置 | NativeAgent 狀態機(下載→驗簽→解壓→venv→migration→healthcheck→原子換位→OBSERVING 失敗自動 revert LKG) |
| **空殼 1** | `napp/builder.py:91`:沒人傳 dependency_manifest 時 wheels **恆為空**——從未真正解析下載相依 |
| **空殼 2** | agent 的 `ensure_venv`/healthcheck 是注入式 hook,demo 沒注入 → `_prepare_venv` 只建空資料夾 |
| **未接** | 裝好之後沒有東西啟動它——engine 不掃 agent 的啟用目錄;nativeApp 殼側接入明載「未動」 |

### Annotation / AI4BI 今天在哪裡

- **AI4BI**:`plugins/bi/modules/ai4bi/plugin.yaml`(id: `app-ai4bi`, runner: bi, enabled: true, **無 requires**);原始碼是 submodule `vendor/AI4BI` editable 裝進 engine Python。dev 模式 Portal 可見可啟動;prod 不可見(未 enabled_prod);舊 provision 掃到但跳過(`docs/OFFLINE_DEPLOY.md`:「[跳過] app-ai4bi — no requires」)。**注意:「無 requires」是宣告缺席,不是相依不存在——它的相依經 editable install 寄生在 engine 核心環境裡。**
- **Annotation(labeling)**:junction → `C:\code\claude\ANnoTation`,19 個 module + `sheets/annotation.yaml` 聚合成 `sheet-annotation`(已在 prod_enable_tools,**prod 使用者今天就看得到**)。全部無 requires、共用核心環境、prod 走 DB snapshot。
- 兩者都**不在 .napp 系統**——原因只是「沒有人替它們發佈過 release」,加上上述兩個空殼與未接線。

## 3. 採納的模型:分層收斂 + 雙軌穩定終態

採納兩位評審一致的勝者「分層收斂模型」,並依裁決做三個手術:(a) USB/離線 channel 與指紋同源**前移**到相依接通那一階段;(b) labeling 留在底座軌、fleet_publish 續命為 module 熱修通道,「停在 Phase 3」是**官方認可的穩定終態**而非未完成;(c) AI4BI import 審計成為試點**前置條件**。

```text
┌─────────────────────────────────────────────────────────────┐
│  發佈側(native_Provision)                                    │
│                                                              │
│  底座軌(既有,不動行為)          App 軌(.napp,補兩個空殼)      │
│  provision.py → packs/big-deps    app.yaml → BuildWorker      │
│  → apply.py / warmup.py           → control_plane channel     │
│  fleet_publish → registry:9000    → rollout/yank/audit        │
│  (module 級熱修,續用)            → USB 匯出(離線 channel)     │
└──────────┬──────────────────────────────┬────────────────────┘
           │                              │
┌──────────▼──────────────────────────────▼────────────────────┐
│  裝置側(nativeApp)                                            │
│  engine 掃描根:plugins/(既有) + CIM_APPS_ROOT(新,env 開關)   │
│    → tools.sqlite → Portal 渲染 → runner 啟動(啟動權永遠在 engine)│
│  native_agent:下載/驗簽/venv(哨兵)/原子換位/LKG revert         │
│  Management Center:Tools|…|Applications(新,iframe)|Fleet(已有)│
└──────────────────────────────────────────────────────────────┘
```

**邊界定義(鐵律,承接 CV_Viewer PLATFORM_ARCHITECTURE §4.1 與 09 §4):**

| 誰 | 管什麼 | 不管什麼 |
|---|---|---|
| 工具 repo | plugin.yaml、原始碼、requires pin、healthcheck、migration、測試 | 不得有自己的更新 GUI、不得直連 Registry/blob、不得自作 updater/rollback/venv、不碰埠與殼 |
| nativeApp(平台) | 掃描/目錄/渲染/啟動/RBAC/prod 可見性;**啟動權永遠在 engine runner** | 不含工具業務邏輯 |
| native_Provision(部署) | 打包、驗簽、channel、rollout、裝置狀態機、venv 安裝 | 不碰渲染、不執行 entrypoint |

**軌道判準(寫成 ADR + CI gate,不靠口頭紀律):**進 .napp 軌的條件 = id 以 `app-` 開頭 **且**(有非空 requires 或有獨立 data schema/migration 或需要 canary)。其餘(cim-modules、labeling 19 個 module、seed.yaml 靜態工具)留底座軌,用既有 provision + fleet_publish 熱修。

## 4. 通用上架契約:「任何工具 → 可安裝的 application」

### 標準步驟(Tier 0 = 今天已可用;Tier 1 = 需完成 Phase 2/3)

**Tier 0(所有工具,零新程式):**
1. repo 根放 `plugin.yaml`:id 命名決定 category(app 類必須 `app-` 開頭);有核心環境以外的相依就寫完整 pin 的 `requires:`。
2. 放進掃描根 `plugins/<群>/modules/<id>/`(目錄/submodule 皆可)→ `POST /reload` → dev Portal 立即可見。
3. 要 prod 可見:module 類 publish snapshot + `PATCH /tools/{id}/prod-enabled`;app 類只需 prod-enabled。
4. 離線交付走既有 provision(packs + apply.py + warmup.py)。

**Tier 1(app 軌加值):**
5. repo 根加 `app.yaml`:id **必須等於** plugin.yaml 的 id(BuildWorker validate 強制,不符 fail build);healthcheck 必填才准 promote 到 production channel(新 gate);entrypoint 改為選填的 launch hint(**先修 schema**——啟動權在 engine runner,module/sheet 類 entrypoint 本質是佔位符,評審點名的契約說謊問題)。
6. `POST /api/v1/applications/{id}/build` → promote → 裝置 `POST /management/applications/{id}/install`(202 + operation_id 輪詢,install=update 同交易)。
7. engine 經 `CIM_APPS_ROOT` 掃描 agent 啟用目錄 → Portal 自動出現,照既有 runner 啟動。

### 演練一:AI4BI(第一個真實試點)

1. **前置(必做,兩位評審共識):import 審計**——掃 `vendor/AI4BI` 的 import vs engine 核心環境清單。結果二選一:補 `requires:` pin,或在 ADR 明文宣告「合法的空 manifest(吃核心環境),與 engine 版本綁定」。不做這步,試點成功會被誤讀為「相依交付已驗證」,底座瘦身時 AI4BI 無聲斷掉。
2. `vendor/AI4BI` 根放 `app.json`:`{"id":"app-ai4bi","version":"1.0.0"}`(id 用 plugin.yaml 的 `app-ai4bi`,不是目錄名 ai4bi)。
3. build → promote production → 裝置 install,原始碼落在 `applications/app-ai4bi/versions/1.0.0/application/`。
4. engine 新增 source_root resolver(env flag):有 .napp active 版就用它,否則 fallback vendor/ submodule——一鍵回退。
5. 因為零 requires(或審計後補齊),它繞過 builder.py:91 空 wheels 缺口,是最短真實路徑,同時解掉 P1「真實 app E2E」。

### 演練二:labeling 工具(例:只修 module_012 一個 bug)

**不進 .napp。**走既有 module 熱修通道:改 `C:\code\claude\ANnoTation\modules\module_012` → `fleet_publish.py --channel prod` 簽章發佈 → 裝置 engine `pull_distribution_into_catalog`(engine.py 行 2343)驗簽入庫 → Management Center 可 rollback。這是工廠最高頻動作,一發推送,不值得換成整包 .napp 重建 + channel/rollout。唯一必辦:把 `plugins/labeling` junction 物理化(搬入或改 submodule)——打包對 junction 的 deref 未驗證,有整組 19 個 module 靜默漏包風險。

## 5. Identity mapping 定案建議(需寫成 ADR;10 §11 點名、目前缺席)

1. **單一主鍵等式**:`plugin.yaml 的 id ≡ tools.sqlite 的 tool_id ≡ .napp 的 app_id`,一個字串三處通用。字元集相容已驗證(registry pattern `^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$` 涵蓋現有全部 id)。BuildWorker validate 強制等式;engine 對 .napp 管理的 id 加啟動時斷言(不等拒載、記 audit_events)。
2. **命名即分類**:因為 category 由前綴推導,凡進 .napp 軌者 id 必以 `app-` 開頭。cv-reviewer 真實化時改名 `app-cv-reviewer`(demo 的 `cv-reviewer` 只是 `.lab/` 合成資料,砍掉即消失,零遷移成本)。
3. **plugin_id 是群組命名空間**,不是安裝單位(ANnoTation 的 `plugin.manifest.yaml id: labeling` 那層)。若未來整群打包,採 `package.json tools:[]` 欄位——一個 application 對映 N 個 tool_id 的機器可讀形式,Portal/RBAC/sheets 繼續只認 tool_id。
4. **view 來源優先序**:installed/active/LKG/update_state 以 agent state 為真相(remote 掛掉仍可顯示並啟動已裝版本);latest/channel 以 Control Plane 為真相;display_name/category/enabled_dev/enabled_prod/RBAC 以 plugin catalog 為真相。禁止 frontend 用 display name 合併。
5. **per-category prod 執行真相寫死**:module = `tool_versions` active snapshot in-memory exec(保留執行期 sandbox deny-list,不做信任模型倒退);app = .napp `active.json` 指向的目錄,fallback vendor/。`tool_runs` 加記實際 source path,防「畫面顯示 1.1.0 實跑 1.0.0」。
6. **同 id 衝突裁決**:vendor submodule 與 .napp 並存時 .napp active 優先、submodule 以 enabled:false 停用,一旗標回退。
7. 另立第二份 ADR:中央 Fleet identity ↔ 裝置本機 RBAC 角色映射(09 §11 點名、缺席);落地前 Fleet Console 僅內網。

## 6. 分階段遷移路線(每階段可停留、可回退)

**Phase 0 — ADR + WDAC spike(純文件 + 一次實驗)**
- 做:寫 §5 兩份 ADR + 軌道判準 ADR;修 app.schema.json 的 entrypoint(改選填 launch hint);**在真 WDAC 映像上 spike**:pip --no-index 裝含原生碼 wheel 到新路徑 `applications/<id>/venvs/<hash>`、os.replace 後執行新落地 .py、junction 行為。這個 spike 決定後面全部投資是否成立,不能排在寫完程式之後。
- 驗收:文件過審;WDAC spike 三項全過(或得出白名單配套)。回退:刪文件。

**Phase 1 — 統一 catalog(GUI 先治好觀感,已存在零件接線)**
- 做:`ApplicationManagementService` 的 catalog 參數(management.py,**預留接點已存在**)接 nativeApp `GET /tools`;Management Center 照 `management_fleet.py` + `_page_fleet()` 的既有 iframe 模式新增 Applications 分頁接 device-local `/management`(零 Rust 重編)。
- 驗收:一個畫面同列 app-ai4bi/labeling/app-lv 的 installed/active/latest;「新增第二個 application 不修改頁面結構」(09 §12 原文);Control Plane 離線仍顯示本機狀態。回退:unset env var,分頁消失。
- **效果:「為什麼只有 cv-reviewer」的觀感問題在動任何打包程式之前消失。**

**Phase 2 — AI4BI 試點(前置:import 審計)**
- 做:§4 演練一;control_plane 加「無真 healthcheck 不得 promote production」gate;AI4BI 的 healthcheck 寫第一個真的(Streamlit port 200);單段 100% rollout profile(2-3 台裝置免分批儀式)。
- 驗收:斷網後仍可從已裝版本啟動(09 §13 #9);1.0.0→1.1.0 更新、rollback 回 LKG、409 拒重複 mutation 全過。回退:關 env flag 回 vendor/ 路徑。

**Phase 3 — 真相依接通(最大單塊,以 app-lv 驗收;含三個前移補丁)**
- 做(要新做,兩個空殼在此補完):
  - BuildWorker 接 `PlatformGateway`(gateway.py 五 API 契約**已存在**)填真 dependency_manifest + big_deps;**requires 非空而 wheels 空 = build fail**(合成模式留 env 旗標給測試,既有測試基線不破)。
  - **指紋同源強制令**:dependency_fingerprint 改呼叫 `gateway.requires_fingerprint`(與 core/tool_deps 同源),取代 builder.py 的 sha256(manifest JSON)——否則接上真 wheels 後 venv 重用靜默失效、torch 重下。
  - agent 注入 ensure_venv 實作(apply.py 語意:sha256 驗證、pip --no-index)+ **venv 完成哨兵**:成功後才寫 `.complete` 標記,`_prepare_venv` 只認標記不認目錄存在,開機 reconcile 清無標記殘缺 venv——修掉評審實碼查得的「斷電毒化 venv 永久復活」缺陷。
  - **USB/離線 channel**(前移):agent 的 remote 是建構子注入、介面僅 open_artifact/open,寫檔案目錄型 remote adapter + channel 解析結果/.napp/blobs 匯出工具。**這是離線廠區採用 .napp 軌的先決條件,不是終局選配。**
- 驗收:app-lv(14 requires 含 torch==2.6.0)乾淨離線裝置全程安裝;升版同 fingerprint 零下載零重建;斷電中斷後重裝不重用殘缺 venv;fingerprint 與舊 provision deppack 一致;USB 匯入路徑走通。回退:app-lv 退回舊 provision。

**Phase 3 即官方穩定終態。**做到這裡:底座 + labeling 走既有軌(provision + fleet_publish 熱修),重相依/獨立節奏的 app(ai4bi、lv、未來 cv-reviewer)走 .napp 軌。以下為選配:

**Phase 4(選配)— plugin.yaml → app.yaml 產生器**:等到有 ≥3 份手寫 app.yaml、真的感受到漂移痛再做;entrypoint schema 已在 Phase 0 修好,產生器不再複製謊言。

**Phase 5/6(明文不做,見 §8)。**

## 7. 平台管理功能總覽

**已存在、現在就能用:**

| 功能 | 位置 | 誰用 |
|---|---|---|
| 工具自動上架 | plugin.yaml 掃描 + `POST /reload` + `--rebuild-catalog` | 開發者 |
| 工具目錄/啟動 | Portal(GET /tools、/tools/{id}/start,category 分組渲染) | 使用者 |
| module 版本化 | Management Center Tools 頁:publish/rollback/prod-enable(tool_versions) | 本機管理員 |
| module 熱修散佈 | fleet_publish.py + registry_server.py + pull_distribution_into_catalog | 發佈者 |
| 整平台離線交付 | provision.py + apply.py + warmup.py(E2E 全綠,app-lv/torch 實證) | 部署者 |
| RBAC | core/rbac.py(permissions.yaml)+ auth_provider 角色 | 管理員 |
| 稽核/執行紀錄 | audit_events、tool_runs(Management Center 各分頁) | 管理員 |
| Fleet 治理 | control_plane(channel/rollout/yank/audit)+ Fleet Console iframe(已嵌 Management Center Fleet 分頁,CIM_FLEET_CONSOLE_URL) | Fleet 管理員 |
| 裝置更新狀態機 | native_agent(驗簽/原子換位/LKG revert/reconcile/gc)+ /management API | 裝置端 |

**缺、要新做(即 Phase 0-3 的內容):**identity/RBAC 映射 ADR ×2、WDAC 實機驗證、統一 catalog 接線(Phase 1)、真 dependency-manifest(builder 空殼)、真 ensure_venv + 哨兵(agent 空殼)、指紋同源、USB channel、engine 第三掃描根/resolver、healthcheck gate、Ed25519 簽章(DevHmacSigner 不可上正式)。

**你不知道但其實有的:**Annotation 的 sheet-annotation prod 今天就可見;AI4BI 在 dev 模式今天就可啟動;要它 prod 可見只差一個 `PATCH /tools/app-ai4bi/prod-enabled`——這是回答原始問題的最短誠實答案。

## 8. 風險與不做事項

**風險(依嚴重度):**
1. **WDAC 實機**(最高):stdlib-only 保護 agent 本身,保護不了 payload(torch 未簽章 DLL 從新路徑載入)。緩解:Phase 0 spike 前置,失敗即停損,回退舊 provision(行為明文不變)。
2. **跨 repo 無 owner**:engine 側改動(第三掃描根、resolver)handoff 明載「未動」,是所有方案的第一瓶頸。緩解:Phase 1/2 開工前先指定 owner,每個 engine 改動走 env var 開關(unset = 與今日 bit-for-bit 相同)+ `--rebuild-catalog`。
3. **中間態三真相**(Phase 2 起):dev 檔案系統 / DB snapshot / .napp 目錄並行。緩解:§5 第 5、6 條 ADR + tool_runs 記 source path。
4. **P1 鑰匙在使用者手上**:cv_reviewer 真實 repo 路徑/entrypoint 不可猜、只能問——AI4BI 先行正是為了不被卡。
5. **blob 無續傳/不回收**(IMPLEMENTATION_STATUS §3b 明列):Phase 3 驗收加入斷線重試情境;blob GC 需 per-version 參照追蹤,列為 .napp 軌擴大前的前置。

**明文不做(避免過度工程):**
- **不做全 App 化**:不把 26+ 個活 plugin 的「git pull 即生效」內圈換成 build→promote→install;不為 2-3 台裝置強制分批 rollout 儀式。
- **不退役 fleet_publish/registry_server**:它是 labeling 單 module 熱修的正確工具;若未來要落日,走「使用盤點→停新發佈→觀察一個 cycle→連續兩個 cycle 無回退才刪碼」程序。
- **不動 module 類 prod 信任模型**:DB snapshot in-memory exec + 執行期 sandbox deny-list 照舊;.napp 只接管「原始碼+plugin.yaml 的散佈」。
- **不改舊離線 USB provision 行為**(00_START_HERE 規則 3);它是底座交付與所有回退路徑的逃生門。
- **不把 labeling 打成單一 .napp**(19 個 module 綁一個 release 是粒度倒退),除非未來某個標注工具長成獨立重相依 app,依判準單獨改 `app-*` id 升軌。
- **不在只有兩份手寫 app.yaml 時就上產生器**;不在 Ed25519 落地前把 DevHmacSigner 簽章當正式信任來源。

---

## 附:評審結果紀錄

三個候選設計與兩位評審(維運現實 / 架構一致性)的一致排名:

1. **分層收斂模型**(勝出,本文件 §3 起採納並修正)
2. 底座 + 精選 App 雙軌模型(其「軌道判準」被搬進 §3)
3. 全 App 化(其 USB channel、`tools:[]` 群組打包欄位被搬進 §5/§6)
