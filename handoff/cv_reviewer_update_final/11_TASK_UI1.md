# 11 — 任務卡:UI-1「Application Management Service」(native_Provision 側)

> ✅ **native_Provision 側已完成(2026-07-12,全 repo 335 passed / 18 skipped)。**
> 交付:`native_agent/operations.py`(stage 對照表 + `OperationRunner` + per-app lock +
> 取消)、`state.py`(`operation_events`/cancel/kind + journal 三方法自動發事件)、
> `agent.py`(拆 `plan_update`/`execute_update` + CANCELLED,行為不變)、`management.py`
> (view + service)、`management_api.py`(`/management` API + RBAC)、Portal 改走 service、
> `lab_serve` 於 :8091 掛 `/management`;新增 `tests/test_management_service.py`(13)+
> `tests/test_management_api.py`(11)。Part A 文件校正已完成。
> **下一步 = 跨 repo:在 `nativeApp` 接 `/management` API 與進度事件(iframe/bridge),
> 見 `10` §2 與 §12 UI-1 驗收 #12。本卡以下保留作為實作紀錄。**
> 同一個 Native App Management Center 也應預留依角色顯示的 Fleet 區;
> `:8090` 只保留遠端管理/Lab,不可當作桌面 Fleet 管理員必須另開的正式入口。
>
> 讀本卡前先讀 `09_GUI_INTEGRATION.md` 與 `10_AI_UI1_DEVELOPMENT_GUIDANCE.md`。
> 本卡把 10 號的指引落成**可照做的步驟**:含已定案的設計決策、文件校正清單、
> 逐檔實作指示、必寫測試與驗收。**只涵蓋 `native_Provision` repo 這一側**;
> nativeApp 殼的接入(iframe/bridge、Management Center 畫面)是另一份工作,
> 完成本卡 ≠ UI-1 全部完成(10 號 §2 有明講)。
>
> 規模:純 stdlib。新增 2 個模組 + 改 3 個既有檔 + 文件校正。
> 期間**不得**動 provision build/verify/apply/warmup、package domain、Control Plane。

---

## Part 0 — 已定案的設計決策(照用,不要重新討論)

10 號指引留了三個未定案點,現在定案如下:

### D1. Stage 詞彙:單一對照表,journal 不改名

現有 `native_agent/agent.py` 的 journal step 名稱(`CHECKING`、`DEPS_READY`、
`MIGRATION_READY`…)**保持不變**(Slice 7 的 reconcile 測試依賴它們)。
對外事件用 10 號 §6 的 canonical stage,兩者以**一張對照表**銜接,
表放在新模組 `native_agent/operations.py`,是唯一來源:

```python
STAGE_BY_STEP = {
    "CHECKING":        "RESOLVING",
    "DOWNLOADING":     "DOWNLOADING",
    "VERIFYING":       "VERIFYING",
    "EXTRACTING":      "EXTRACTING",
    "DEPS_READY":      "PREPARING_DEPENDENCIES",
    "MIGRATION_READY": "MIGRATING",
    "HEALTHCHECK":     "HEALTHCHECK",
    "ACTIVATING":      "ACTIVATING",
    "OBSERVING":       "OBSERVING",
}
STAGE_BY_FINAL_STATUS = {          # finish_operation 的 status → 終態 stage
    "succeeded":   "COMPLETED",
    "failed":      "FAILED",
    "rolled_back": "ROLLED_BACK",
    "cancelled":   "CANCELLED",
}
PERCENT_BY_STAGE = {
    "QUEUED": 0, "RESOLVING": 5, "DOWNLOADING": 20, "VERIFYING": 35,
    "EXTRACTING": 45, "PREPARING_DEPENDENCIES": 65, "MIGRATING": 75,
    "HEALTHCHECK": 85, "ACTIVATING": 92, "OBSERVING": 97,
    "COMPLETED": 100, "FAILED": 100, "ROLLED_BACK": 100, "CANCELLED": 100,
}
CANCELLABLE_STAGES = {"QUEUED", "RESOLVING", "DOWNLOADING", "VERIFYING"}
```

(10 號 §6 的清單漏了 `MIGRATING` 但 §9 有——以上表為準,兩處都涵蓋。)

### D2. operation_id = 既有整數 op_id

沿用 `state.db` `operations.op_id`(INTEGER AUTOINCREMENT)。
API 路由 `/management/operations/17`,回傳 `"operation_id": 17`。
10 號範例的 `"op-..."` 字串**不採用**(範例性質,免改 schema)。

### D3. 非同步範圍與取消機制

- **async(202 + operation_id)**:`update` 與 `install`(install = 未安裝時的
  update,同一條程式路徑)。10 號 §5 只強制 update 非同步。
- **sync(200,但受同一把 per-app lock 保護)**:`rollback` / `reconcile` / `gc`
  ——本機快速操作,不下載。執行前檢查該 app 無 running mutation,否則 409。
- **取消 = 合作式,檢查點在 stage 邊界**:`POST /cancel` 只設
  `operations.cancel_requested = 1`;`AgentState.update_step()` 在每次 stage
  轉換時檢查,若已請求且**新 stage 屬 `CANCELLABLE_STAGES`** → raise
  `OperationCancelled`。粒度 = stage 邊界(下載中途不會立即中斷,到下一個
  邊界才停)。第一版可取消:QUEUED/RESOLVING/DOWNLOADING/VERIFYING;
  其後一律 `can_cancel: false`。
- 取消**不得**寫入 `failed_versions`(否則之後 SKIPPED_FAILED 擋住重試)。
- 取消/併發衝突的實作參考:`build_worker/worker.py` 已有
  `threading.Event` + checkpoint + `_kill_tree` 的成熟前例,照抄精神即可。

### D4. RBAC v1(裝置端)

mutation endpoint 讀 request header `X-Role`(`user`|`admin`,缺省 `user`):
`update`/`install` 兩者皆可;`rollback`/`reconcile`/`gc` 需 `admin`,否則
**403 + `error.code = "forbidden"`**。Native App 真實 RBAC → 這個 header 的
映射屬 nativeApp 側 + 未來 ADR(10 號 §13),本 repo 只做 enforcement 點與測試。

---

## Part A — 文件基線校正(先做,約 10 分鐘)

10 號 §3 指出的漂移屬實,且**還漏了一處最誤導的**。步驟:

1. 跑 `py -3.11 -m pytest tests`,記下實際結果(上次實測 `311 passed, 18 skipped`;
   以你跑出來的為準,下述「311/18」都替換成實際數字)。
2. 逐條修:

| 檔案 | 現況 | 改成 |
|------|------|------|
| `00_START_HERE.md` 規則 5 | 「聚焦 5 passed;全 repo 166 passed / 6 skipped」 | 「全 repo 311 passed / 18 skipped(動手前重跑確認)」——**這處 10 號沒點名,最舊、最誤導** |
| `00_START_HERE.md` §3 首行 | 304 / 18 | 311 / 18 |
| `07_CHECKLISTS.md` §2 | 304 / 18 | 311 / 18 |
| `01_CONSTRAINTS.md` §4 末條 | 「`.napp` 正式 assembly、schema 與簽章:尚未實作」 | 「`.napp` assembly/schema/簽章已實作(`provision_builder/napp/`);production Ed25519 signer 待簽章服務(P2)」 |
| `05_TASK_SLICE_1_5.md` 頂部註 | 「下一步是 Slice 2」 | 「歷史任務卡;全部 Slice 已完成,現況見 `00_START_HERE.md` §3」 |
| `06_ROADMAP_SLICES.md` 頂部註 | 「276 passed」+ Slice 2 寫 FastAPI | 數字改「完成當時 276;現行基線見 `07_CHECKLISTS.md`」;FastAPI 處加一句「(實作採 stdlib `http.server`,見 `IMPLEMENTATION_STATUS.md`)」 |

3. 重建 final zip(指令在 `07_CHECKLISTS.md` §4)。
4. **不因文件過期重做任何 Slice**(10 號 §3.4)。

---

## Part B — 實作(逐檔)

### B1. `native_agent/state.py`:events + cancel + kind

1. `operations` 表加兩欄(CREATE TABLE 裡加;為既有 `.lab`/`.device` DB 相容,
   `_initialize` 末尾補 best-effort migration):

```python
# 在 CREATE TABLE operations 內加:
#   kind TEXT NOT NULL DEFAULT 'update',
#   cancel_requested INTEGER NOT NULL DEFAULT 0
# 並新增表:
# CREATE TABLE IF NOT EXISTS operation_events (
#     sequence INTEGER PRIMARY KEY AUTOINCREMENT,
#     op_id INTEGER NOT NULL,
#     stage TEXT NOT NULL,
#     state TEXT NOT NULL,              -- QUEUED | RUNNING | 終態
#     percent INTEGER NOT NULL,
#     message_key TEXT NOT NULL,
#     detail_json TEXT NOT NULL DEFAULT '{}',
#     created_at TEXT NOT NULL
# );
for ddl in ("ALTER TABLE operations ADD COLUMN kind TEXT NOT NULL DEFAULT 'update'",
            "ALTER TABLE operations ADD COLUMN cancel_requested INTEGER NOT NULL DEFAULT 0"):
    try:
        db.execute(ddl)
    except sqlite3.OperationalError:
        pass  # column already exists
```

2. 新方法(照 D1 的表,從 `native_agent.operations` import):

```python
def add_event(self, op_id, stage, state, message_key, detail=None) -> int: ...
    # INSERT operation_events;percent 用 PERCENT_BY_STAGE[stage];回 sequence

def events_after(self, op_id, after_sequence: int = 0) -> list[dict]: ...
    # SELECT * WHERE op_id=? AND sequence>? ORDER BY sequence

def get_operation(self, op_id) -> Operation | None: ...

def request_cancel(self, op_id) -> None: ...
    # UPDATE operations SET cancel_requested=1 WHERE op_id=?

def cancel_requested(self, op_id) -> bool: ...
```

3. **事件在三個既有方法內自動發出**(agent.py 幾乎不用改就有事件流):
   - `begin_operation(...)`:簽名加 `kind: str = "update"`;寫入後
     `add_event(op_id, "QUEUED", "QUEUED", f"application.{kind}.queued")`。
   - `update_step(op_id, step)`:寫完 current_step 後,
     `stage = STAGE_BY_STEP[step]`,`add_event(op_id, stage, "RUNNING",
     f"application.update.{stage.lower()}")`;**接著檢查取消**:
     `if self.cancel_requested(op_id) and stage in CANCELLABLE_STAGES:
     raise OperationCancelled(op_id)`。
   - `finish_operation(op_id, status, last_error=None)`:寫完後
     `stage = STAGE_BY_FINAL_STATUS.get(status, "FAILED")`,
     `add_event(op_id, stage, stage, f"application.update.{stage.lower()}",
     detail={"error": last_error} if last_error else None)`。
4. `OperationCancelled` 定義在 `native_agent/operations.py`
   (繼承 `Exception`,不是 `PackageDomainError`——它是控制流,不是對外錯誤)。

### B2. `native_agent/agent.py`:三個小改(不重寫)

1. **拆 `update()` 成 plan + execute**(行為不變,既有 20+ 測試是護欄):

```python
def plan_update(self, app_id, channel, *, force=False):
    """回 (early_outcome | None, desired, active)。early_outcome 非 None 表示
    不需要 operation(START_CACHED / START_ACTIVE / SKIPPED_*)。"""
    # = 現在 update() 從開頭到 clear_failure 為止的內容,原樣搬過來

def execute_update(self, op, app_id, desired, active) -> UpdateOutcome:
    # = 現在 update() 從 activated=False 起的整個 try/except 本體,原樣搬過來

def update(self, app_id, channel, *, force=False) -> UpdateOutcome:
    early, desired, active = self.plan_update(app_id, channel, force=force)
    if early is not None:
        return early
    op = self.state.begin_operation(app_id, from_version=active,
        to_version=desired.version, previous_active=active,
        desired_identity=desired.version)
    return self.execute_update(op, app_id, desired, active)
```

2. `execute_update` 的 except 區**新增一個分支在 `except Exception` 之前**:

```python
except OperationCancelled:
    self.state.finish_operation(op, "cancelled", "cancelled by user")
    self._cleanup_staging(app_id, desired.version)
    # 注意:不呼叫 record_failure —— 取消不是壞版本
    return UpdateOutcome(CANCELLED, self.state.active_version(app_id),
                         target=desired.version, error="cancelled")
```

   並加常數 `CANCELLED = "CANCELLED"`。
3. 兩個 rollback 分支的 `finish_operation(op, "failed", ...)` 改成
   `finish_operation(op, "rolled_back", ...)`(observation 失敗處與
   `activated=True` 的例外處),讓終態事件正確映射為 `ROLLED_BACK`。

### B3. 新檔 `native_agent/operations.py`:對照表 + runner

D1 的四個表 + `OperationCancelled` +:

```python
class OperationInProgress(PackageDomainError):
    code = "operation_in_progress"

class Forbidden(PackageDomainError):
    code = "forbidden"

class OperationRunner:
    """每個 app 同時只允許一個 mutation;update 走背景 thread。"""
    def __init__(self, agent):
        self.agent = agent
        self._lock = threading.Lock()
        self._threads: dict[int, threading.Thread] = {}

    def _guard(self, app_id) -> None:
        # running_operations(app_id) 非空 → raise OperationInProgress
        # (同時涵蓋背景 thread 與上次 crash 留下的 running 紀錄;
        #  後者應提示先跑 reconcile)

    def submit_update(self, app_id, channel, *, force=False):
        """回 (early_outcome, op_id):二擇一非 None。"""
        with self._lock:
            self._guard(app_id)
            early, desired, active = self.agent.plan_update(app_id, channel, force=force)
            if early is not None:
                return early, None
            op = self.agent.state.begin_operation(app_id, from_version=active,
                to_version=desired.version, previous_active=active,
                desired_identity=desired.version)
            t = threading.Thread(target=self.agent.execute_update,
                                 args=(op, app_id, desired, active), daemon=True)
            self._threads[op] = t
            t.start()
            return None, op

    def guarded_sync(self, app_id, fn):
        with self._lock:
            self._guard(app_id)
        return fn()          # rollback / reconcile / gc

    def wait(self, op_id, timeout=None):  # 測試與 Portal 同步路徑用
        t = self._threads.get(op_id)
        if t: t.join(timeout)
```

註:`plan_update`/`begin_operation` 在 `_lock` 內完成,所以「送出即占鎖」,
不會有兩個 update 同時通過 `_guard`。

### B4. 新檔 `native_agent/management.py`:view model + service

```python
UPDATE_STATES = ("NOT_INSTALLED", "UP_TO_DATE", "UPDATE_AVAILABLE",
                 "UPDATE_FAILED", "REMOTE_UNAVAILABLE", "YANKED")

@dataclass
class ApplicationManagementView:
    app_id: str; display_name: str
    installed: bool; active_version: str | None
    last_known_good: str | None
    desired_version: str | None; latest_version: str | None
    update_state: str
    can_launch: bool; can_install: bool; can_update: bool; can_rollback: bool
    health: str                     # "HEALTHY" | "DEGRADED" | "UNKNOWN"
    current_operation: int | None   # running op_id or None

class ApplicationManagementService:
    def __init__(self, agent, runner, channel="production",
                 catalog=None):     # catalog: app_id -> {"display_name":...}(nativeApp adapter 掛這)
    def list_views(self) -> list[ApplicationManagementView]:
        # app 集合 = 本機已安裝 ∪ remote list_applications()(remote 失敗只用本機)
    def view(self, app_id) -> ApplicationManagementView:
        # desired:try agent.check() → PackageDomainError 則 REMOTE_UNAVAILABLE
        # yanked → YANKED;is_failed(desired) → UPDATE_FAILED
        # 規則:remote 不可用時 installed/active/LKG 照常回(10 號 §4)
    # mutations(全部經 runner):
    def update(self, app_id) -> tuple[UpdateOutcome | None, int | None]:
        return self.runner.submit_update(app_id, self.channel)
    install = update                     # 未安裝時同一路徑(D3)
    def rollback(self, app_id): return self.runner.guarded_sync(app_id, lambda: self.agent.rollback(app_id))
    def reconcile(self, app_id): ...
    def gc(self, app_id): ...
    def cancel(self, op_id): self.agent.state.request_cancel(op_id)
    def operation(self, op_id) -> dict: ...       # op row + 最新事件
    def events(self, op_id, after=0) -> list[dict]:
        # 每筆補 "can_cancel": stage in CANCELLABLE_STAGES and state == "RUNNING"
```

**禁止**:view/service 內出現任何 `cv-reviewer` 字面特判(驗收會用第二個
fixture app 檢查);display_name 無 catalog 時用 `app_id.replace("-", " ").title()`。

### B5. 新檔 `native_agent/management_api.py`:JSON API

自帶 `handle(method, path, body, headers) -> Response`(socket-free,
模式照抄 `control_plane/http_api.py`),路由 = 10 號 §10 + cancel:

```text
GET  /management/applications
GET  /management/applications/{app_id}
POST /management/applications/{app_id}/install    → 同 update
POST /management/applications/{app_id}/update     → early:200+outcome;否則 202+{operation_id,state:"QUEUED",status_url}
POST /management/applications/{app_id}/rollback   → admin;200
POST /management/applications/{app_id}/reconcile  → admin;200
POST /management/applications/{app_id}/gc         → admin;200
GET  /management/operations/{op_id}
GET  /management/operations/{op_id}/events?after=N
POST /management/operations/{op_id}/cancel        → 202
```

錯誤格式與 `03_DOMAIN_SPEC.md` §4 相同;本 API 的 status 映射自帶一張小表:
`operation_in_progress→409`、`forbidden→403`、`invalid_identifier→400`、
`unknown_application→404`,其餘 `PackageDomainError→500`。
RBAC 依 D4(`X-Role` header)。**這個 API 不碰 Control Plane 治理功能**
(build/promote/rollout 是 Fleet 的事,10 號 §10)。

### B6. `native_agent/portal.py`:改為同一 service 的 client

Portal 的按鈕改呼叫 `ApplicationManagementService`(不再直接呼叫
`agent.update()`):update 按鈕 → `service.update()`;若回 202 型(有 op_id)
→ `runner.wait(op_id)` 後再導回(Portal 是診斷工具,同步等待可接受)。
頁尾加一行「Diagnostics Only — 正式入口為 Native App Management Center」。
**`tests/test_device_portal.py` 必須不改斷言就全綠**(10 號驗收 #10 的
regression 保護;若紅了,是你改壞了,不是測試舊)。

### B7. `demo/lab_serve.py`:掛上 /management

`make_portal_server` 的 handler 加一條:path 以 `/management` 開頭 →
轉給 `ManagementApi.handle`(同 :8091 端口,nativeApp iframe 之後就接這裡)。

---

## Part C — 必寫測試(新檔 `tests/test_management_service.py` + `tests/test_management_api.py`)

對照 10 號 §15 的場景,本 repo 側至少:

1. `test_view_not_installed` / `test_view_update_available` / `test_view_up_to_date`
   —— view model 三態與 `can_*` 旗標。
2. `test_view_remote_unavailable_keeps_local_info` —— monkeypatch resolve 丟
   `RegistryUnavailable`;installed/active/LKG 仍在,`update_state=REMOTE_UNAVAILABLE`(場景 3)。
3. `test_view_yanked` —— channel 指向 yanked → `YANKED`、`can_update=False`。
4. `test_async_update_lifecycle` —— API POST update → 202 + op_id →
   `runner.wait()` → GET operation = COMPLETED;events 的 sequence 嚴格遞增、
   stage 依序出現 QUEUED→RESOLVING→…→COMPLETED(場景 1/2)。
5. `test_events_survive_restart` —— 完成後用**新的** `AgentState(同一路徑)`
   讀 events,仍完整(10 號 §5 持久化要求)。
6. `test_second_update_409` —— 用慢 hook(如 `ensure_venv` 等一個
   `threading.Event`)卡住第一個 update,再送第二個 → HTTP 409
   `operation_in_progress`;放行後第一個正常完成(場景 8)。
7. `test_gc_during_update_409` —— 同上手法,gc → 409(場景 9)。
8. `test_cancel_before_uncancellable_stage` —— 慢 hook 卡在下載前,
   `POST /cancel` → 放行 → 終態 CANCELLED、active 不變、
   **`is_failed()` 為 False**(可直接重試)。
9. `test_rollback_as_user_403` / `test_rollback_as_admin_200`(場景 10)。
10. `test_install_alias_installs_when_not_installed`(場景 1)。
11. `test_no_app_specific_behavior` —— 用第二個 fixture app(如
    `defect-inspector`)跑同一條 list→update→view 流程,結果結構相同(場景 11)。
12. 既有 `tests/test_device_portal.py`、`tests/test_native_agent.py` **不改動**、全綠(場景 13/14 的本 repo 部分)。

---

## Part D — 驗收(全部成立才算完成)

```powershell
py -3.11 -m pytest tests     # 全綠;不低於 Part A 校正後的基線,新測試 ≥12 個
```

- [ ] `update()` 公開行為與拆分前完全一致(既有 agent 測試零修改全綠)。
- [ ] 任何 update 都留下完整、持久化的事件流(重啟後可讀)。
- [ ] 同一 app 併發 mutation 一律 409;取消不污染 `failed_versions`。
- [ ] RBAC 在 backend 生效(403 有測試),不是靠藏按鈕。
- [ ] Portal 走同一 service;舊 Portal 測試原封不動全綠。
- [ ] `/management` API 可在 :8091 手動打通(lab_serve 起來後 curl 驗一次)。
- [ ] 無任何 `cv-reviewer` 特判(第二個 fixture app 測試證明)。
- [ ] 更新 `04_CODE_MAP.md`(UI-1 本 repo 側 → ✅,註明 nativeApp 側未動)、
      `IMPLEMENTATION_STATUS.md`,重建 final zip。

## Part E — 禁止事項

- ❌ 不動 `src/provision_builder/`、`control_plane/`、`build_worker/`、`web_console/`
  (Fleet 側與 package domain 與本卡無關)。
- ❌ 不引入第三方套件、不引入 WebSocket framework(10 號 §7;polling 就好,
  SSE 只在 stdlib 能穩定支撐時才加)。
- ❌ 不改 journal step 的既有名稱、不改 `reconcile()` 邏輯(D1)。
- ❌ 不弱化/跳過任何既有測試來遷就新程式。
- ❌ 不在本 repo 嘗試實作 nativeApp 的畫面或 bridge——那是跨 repo 工作,
  需要的只是把 `/management` API 與事件 schema 做穩。
