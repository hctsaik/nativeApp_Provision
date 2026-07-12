# 03 — Domain 規格(實作時逐字遵守)

## 1. Release identity

- 第一版 identity = 複合鍵 **`(app_id, version)`**。
- **不存在 `release_id`**;API 路由、程式、schema 都不得引用它。
- Download URL 路由:`POST /api/v1/artifacts/{app_id}/{version}/download-url`。

## 2. Identifier 規則(單一定義,四處共用)

`app_id`、`version`、`channel` 一律符合:

```regex
^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$
```

同一條 regex 必須同時由以下四層遵守,**不得各自發明**:

1. Python domain validation(`src/provision_builder/` 內,Slice 1.5)
2. `app.yaml` / `package.json` JSON Schema(Slice 4)
3. HTTP request validation(Slice 2)
4. Registry migration / DB constraint(Slice 3)

## 3. Release status

| status | 意義 | 允許轉移 |
|--------|------|----------|
| `published` | 正常可用 | → `yanked` |
| `yanked` | 已撤回;object 不刪(不可變原則),但不得新安裝 | (終態) |

Yank 流程:標記 yanked → channel 指回已知良好版本 → 裝置停止新安裝。
語意細節:

- `promote` 到 yanked release → 拒絕(`ReleaseYanked`)。
- `resolve` 仍回傳 release(含 status 欄位)——channel 修正是操作者的事;
  Agent 看到 `yanked` 不得安裝。
- `download` yanked release → 拒絕(`ReleaseYanked`)。

## 4. Error taxonomy(Slice 1.5 建立,之後所有層共用)

Domain exception 類別(全部繼承 `PackageDomainError`,每類帶穩定 `code` 字串):

| Exception | code | 觸發時機 |
|-----------|------|----------|
| `InvalidIdentifier` | `invalid_identifier` | app_id/version/channel 不符 §2 regex |
| `DuplicateVersion` | `duplicate_version` | publish 已存在的 `(app_id, version)` |
| `ArtifactAlreadyExists` | `artifact_already_exists` | object key 已存在但 registry 無此 release(孤兒;前次 publish 半途失敗) |
| `ArtifactMissing` | `artifact_missing` | release 存在但 object 不在 store |
| `ArtifactCorrupted` | `artifact_corrupted` | object 可讀但結構/大小不符 metadata |
| `UnknownApplication` | `unknown_application` | app_id 查無 |
| `UnknownChannel` | `unknown_channel` | channel 未指向任何版本 |
| `ReleaseNotPublished` | `release_not_published` | promote/yank 目標 release 不存在或非 published |
| `ReleaseYanked` | `release_yanked` | promote/download 目標已 yanked |
| `HashMismatch` | `hash_mismatch` | 下載後 SHA-256 與 registry 不符 |
| `RegistryUnavailable` | `registry_unavailable` | DB 連不上/逾時 |
| `ObjectStoreUnavailable` | `object_store_unavailable` | object store 連不上/逾時 |

分層規則:

- **Service 層**產生 domain error(唯一會 raise 這些類別的地方)。
- **HTTP 層**只做映射,不重新判斷業務狀態:

| Exception | HTTP status |
|-----------|-------------|
| InvalidIdentifier | 400 |
| UnknownApplication / UnknownChannel / ArtifactMissing | 404 |
| DuplicateVersion / ArtifactAlreadyExists / ReleaseNotPublished | 409 |
| ReleaseYanked | 410 |
| HashMismatch / ArtifactCorrupted | 500 |
| RegistryUnavailable / ObjectStoreUnavailable | 503 |

HTTP error body 固定形狀:

```json
{"error": {"code": "duplicate_version", "message": "release cv-reviewer@1.0.0 already exists"}}
```

## 5. Publish 的正確順序與失敗語意

```text
1. 驗 identifier              → InvalidIdentifier
2. registry.get_release       → 已存在 → DuplicateVersion
3. objects.exists(object_key) → 已存在 → ArtifactAlreadyExists(孤兒,留給 GC;不 adopt)
4. objects.put(串流,邊寫邊算 SHA-256 與 size)
5. registry.create_release    → DB unique constraint 撞到(併發)→ DuplicateVersion
```

原則:

- 步驟 2/3 的預查只是**改善錯誤體驗**;真正唯一性由 DB constraint 與
  immutable object 保證(預查有 TOCTOU,無妨)。
- 步驟 5 失敗會留下 unreferenced object → 由**安全 GC** 清理
  (只刪「無 release 引用且超過時限」的 object),絕不發布半套 metadata。
- **MinIO 陷阱**:multipart ETag ≠ SHA-256。adapter 必須在 client 串流時
  自算 SHA-256 與 size(`ObjectStore.put` 契約回傳 `(sha256, size)`)。

## 6. Logical schema(單一權威;各 DB 的 DDL 可不同,語意必須一致)

```text
applications
  app_id        identifier(§2)  PK
  display_name  text NULL
  enabled       bool NOT NULL DEFAULT true
  created_at    timestamp(UTC ISO-8601)NOT NULL

application_releases
  app_id                 → applications.app_id
  version                identifier(§2)
  object_key             text NOT NULL UNIQUE
  sha256                 64 hex chars NOT NULL
  size_bytes             int NOT NULL, >= 0
  status                 'published' | 'yanked' NOT NULL
  dependency_fingerprint 64 hex chars NULL(Slice 4 起填)
  platform_constraint    text NULL(Slice 4 起填)
  created_at             timestamp NOT NULL
  PK (app_id, version)

application_channels
  app_id      →(app_id, version)REFERENCES application_releases
  channel     identifier(§2)
  version
  updated_at  timestamp NOT NULL
  PK (app_id, channel)

audit_events
  event_id    auto PK
  actor       text NOT NULL
  action      text NOT NULL      # publish / promote / yank / ...
  target      text NOT NULL      # "cv-reviewer@1.4.2" / "cv-reviewer/production"
  detail_json json NOT NULL DEFAULT {}
  created_at  timestamp NOT NULL
```

- SQLite(lab)與 PostgreSQL(正式)與未來 Oracle adapter,
  都必須通過**同一組 contract tests**(Slice 3)。
- 現況:SQLite 缺 `display_name`/`enabled`/`dependency_fingerprint`/
  `platform_constraint`/`audit_events`;PG 的 `init-registry.sql` 缺
  status 值域約束。Slice 3 收斂;Slice 1.5 只需把本節寫成
  `docs/REGISTRY_LOGICAL_SCHEMA.md` 並讓 SQLite 支援 `yanked` status。

## 7. Channel / desired state 語意

- channel(`dev` / `staging` / `production`)是一個指標:`(app_id, channel) → version`。
- 是否更新 = 指標與本機 active identity 的**相等性比較**,不做版本排序。
- promote 可把指標移到任何 `published` release(包括「舊」版 = rollback)。
- 正式 rollout(Slice 8)以 device group 分批改 desired state,不送即時命令。
