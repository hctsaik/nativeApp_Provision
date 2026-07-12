# 02 — 目標架構(定稿)

## 1. 全景

```text
Native App Management Center → Fleet(正式桌面 UI)
／Fleet Web Console(遠端管理與 Lab)
    │ HTTPS
Control Plane(FastAPI,獨立子專案)──── PostgreSQL / Oracle-like Registry(metadata)
    │                                        │
    ├── Build Job ── Build Worker(重用 native_Provision)
    │                                        │
    └────────────────────────────── MinIO / S3(immutable artifacts + blobs)
                                             │
                                   Native_App Python Agent(sidecar,非 Rust 殼)
                                             │
                                   本機 cache / venv / 使用者資料
```

第一個成功情境(整條垂直線的 Definition of Done 見 `07_CHECKLISTS.md` §3):

```text
cv_reviewer 指定 commit
→ 建置不可變 application package
→ 上傳 artifact store → 建立 release → promote 到 production
→ Native_App 查 desired version
→ 只下載本機缺少的 source / blob
→ 驗證、準備 venv、healthcheck
→ 原子啟用新版 → 啟動成功 → 晉升 last-known-good
```

任一步失敗:目前 active version 與使用者資料**不得**被破壞;
Registry / MinIO 斷線時,已安裝版本仍正常啟動。

## 2. 責任邊界

| 元件 | 責任 |
|------|------|
| `cv_reviewer` repo | `app.yaml`、程式碼、資源、requirements、healthcheck、migration、測試 |
| `native_Provision` | package builder、dep-pack、offline selfcheck、apply、warmup、E2E、Build Worker 核心 |
| Control Plane | application / release / channel、desired state、rollout、RBAC、audit、download URL |
| Registry(DB) | 只存 metadata、desired/observed state、audit;不存 binary |
| MinIO | immutable package、content-addressed blob、build log、E2E artifact;不決定 production 指向 |
| Native_App **Python Agent** | resolve、download、verify、source install、venv、healthcheck、activate、rollback、observed state |
| Native App Management Center／Fleet | 正式桌面 UI:build、release、promotion、rollout、device 管理;依角色顯示 |
| Fleet Web Console | 相同中央能力的遠端管理／Lab 入口;不作桌面使用者必須另開的產品 |
| Native_App Portal | 使用者看更新進度、本機版本、重試與 rollback |
| Tkinter | 只保留既有離線打包、bootstrap、診斷;不再擴充 |

## 3. Package 格式(定稿:**big-deps 不內嵌**)

`.napp` 只放 source、小型 metadata 與 blob **參照**:

```text
cv-reviewer-1.4.2.napp
├─ package.json              # build 產物證明(見下)
├─ application/              # 應用 source(逐檔 SHA-256 在 checksums.json)
├─ dependency-manifest.json  # 依 core.deppack 格式的相依宣告(權威)
├─ blob-references.json      # 大型 wheel/模型的 sha256 參照清單(不含本體)
├─ migrations/
├─ checksums.json
└─ signature.json            # production 必須;格式待 P2 簽章 ADR
```

大型 wheel / 模型本體以內容定址存放:

```text
MinIO:     blobs/sha256/<hash>
裝置本機:  <NativeApp data>/blobs/sha256/<hash>
```

Agent 只下載本機缺少的 hash,再以 hardlink 或 copy 組裝 per-tool wheelhouse,
交給既有 `core.deppack` 驗證。**只改 source、dependency fingerprint 未變時:
不重下 torch、不重建 venv。**

`app.yaml`(repo 內宣告)與 `package.json`(Build Worker 產的建置證明)
是兩份文件,不可混為一份。`package.json` 至少記錄:app/version、source commit、
platform API、OS/arch、Python/ABI、dependency fingerprint、data schema、
entrypoint、healthcheck、build provenance、artifact hashes。

## 4. Source install 與 dependency apply 是兩件事

```text
Dependency Apply(已存在)
└─ 重用 apply.py / core.deppack(只管 wheel/venv cache)

Application Install(Slice 4 要做)
├─ 驗 source manifest(逐檔 SHA-256)
├─ 安裝到 versions/<version>.staging
└─ 不直接切換 active

Application Activate(Slice 7 要做)
├─ dependency / import check
├─ migration checkpoint
├─ healthcheck
├─ atomic active pointer 切換
└─ observation → last-known-good 晉升
```

## 5. 裝置本機佈局

```text
NativeApp data/
├─ agent/state.db                 # 本機權威狀態(SQLite,含 journal)
├─ applications/cv-reviewer/
│  ├─ active.json                 # runtime 快速讀取入口(由 state.db 導出)
│  ├─ versions/<version>/         # 每版 source,immutable
│  ├─ venvs/<dependency-hash>/    # 以 fingerprint 命名,跨版本重用
│  └─ data/{config,projects,cache,logs}/   # 使用者資料,更新不得動
└─ blobs/sha256/<hash>            # content-addressed 大檔
```

## 6. 更新狀態機

```text
CHECKING
├─ remote unavailable → START_CACHED(啟動現有 active)
├─ pointer == active  → START_ACTIVE
└─ pointer != active(update found)
   → DOWNLOADING(斷點續傳;只抓缺的 blob)
   → VERIFYING(SHA-256 + 簽章)
   → EXTRACTING(→ versions/<v>.staging)
   → DEPS_READY(venv fingerprint 比對;必要時建 venv + warmup)
   → MIGRATION_READY(migration checkpoint)
   → HEALTHCHECK(pre-start)
   ├─ 失敗 → FAILED(記錄)→ ROLLBACK → START_CACHED
   └─ 成功 → ACTIVATING(原子切 pointer)→ START_NEW
            → OBSERVING → 晉升 LAST_KNOWN_GOOD
```

**版本比較語意**:只比較 desired channel pointer 與本機 active identity
**是否相等**(pointer equality),不做 semver 大小排序。
Rollback = channel 指回舊版;Agent 不因版本字串「變小」而拒絕。

**失敗記憶**:同一壞 release 記 failed 狀態;除非使用者明確重試、
metadata 變更或出現新版本,每次啟動不重跑相同失敗流程(避免無限重試)。

## 7. Agent journal 與斷電恢復

`agent/state.db` 至少記錄(每次操作一筆 journal):

```text
operation_id, app_id, from_version, to_version,
current_step, previous_active, desired_identity,
started_at, updated_at, last_error
```

開機 reconcile 規則:

| 發現狀態 | 動作 |
|----------|------|
| 未驗證的 staging | 刪除或安全續作 |
| verified 但未 activation | 從 journal 繼續 |
| active 已切換、observation 未完成 | 重做 healthcheck;失敗切回 previous LKG |
| migration 狀態不明 | **fail closed**,不猜資料狀態,停在安全側 |
| active pointer 不完整 | 由 journal + LKG 修復 |

## 8. Last-known-good 晉升條件(第一版)

依序全部成立才晉升:

1. install / verify / warmup 成功。
2. pre-start healthcheck 成功。
3. 應用真實啟動成功。
4. observation window 內完成指定次數 post-start healthcheck,
   或穩定運行達設定時間。
5. → 更新 last-known-good。
