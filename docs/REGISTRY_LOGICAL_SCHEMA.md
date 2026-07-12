# Registry Logical Schema(單一權威)

> 本文件是 application Registry 的 **logical schema 單一權威來源**。
> SQLite(lab)、PostgreSQL(正式)與未來 Oracle adapter 的 DDL 可以不同,
> 但**欄位、型別語意、約束與狀態機必須一致**,並由**同一組 contract tests**
> 驗證(Slice 3)。任何欄位或約束變更,先改本文件,再改各 adapter migration。
>
> 對應:`handoff/cv_reviewer_update_final/03_DOMAIN_SPEC.md` §6。

## 1. Identifier 規則(四處共用同一條)

`app_id`、`version`、`channel` 一律符合:

```regex
^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$
```

權威定義在 `src/provision_builder/package_errors.py` 的 `IDENTIFIER_RE`。
以下四層必須沿用同一條,不得各自發明:

1. Python domain validation(`validate_identifier`)
2. `app.yaml` / `package.json` JSON Schema(Slice 4)
3. HTTP request validation(Slice 2)
4. Registry migration / DB constraint(Slice 3)

## 2. 表

### applications

| 欄位 | 型別 / 約束 | 備註 |
|------|-------------|------|
| `app_id` | identifier(§1),PK | |
| `display_name` | text NULL | 人讀名稱 |
| `enabled` | bool NOT NULL DEFAULT true | 停用不刪 |
| `created_at` | timestamp(UTC ISO-8601)NOT NULL | |

### application_releases

| 欄位 | 型別 / 約束 | 備註 |
|------|-------------|------|
| `app_id` | → applications.app_id | |
| `version` | identifier(§1) | |
| `object_key` | text NOT NULL UNIQUE | artifact 在 object store 的鍵 |
| `sha256` | 64 hex chars NOT NULL | artifact 完整性 |
| `size_bytes` | int NOT NULL,`>= 0` | |
| `status` | `'published'` \| `'yanked'` NOT NULL | 見 §3 狀態機 |
| `dependency_fingerprint` | 64 hex chars NULL | Slice 4 起填 |
| `platform_constraint` | text NULL | Slice 4 起填 |
| `created_at` | timestamp NOT NULL | |
| **PK** | `(app_id, version)` | 第一版 release identity,無 surrogate `release_id` |

### application_channels

| 欄位 | 型別 / 約束 | 備註 |
|------|-------------|------|
| `app_id` | 與 version 一起 → application_releases | |
| `channel` | identifier(§1)(`dev`/`staging`/`production`…) | |
| `version` | | channel 目前指向的版本 |
| `updated_at` | timestamp NOT NULL | |
| **PK** | `(app_id, channel)` | 一個 app 的一個 channel 只指一版 |
| **FK** | `(app_id, version)` → application_releases | |

### audit_events

| 欄位 | 型別 / 約束 | 備註 |
|------|-------------|------|
| `event_id` | auto PK | |
| `actor` | text NOT NULL | |
| `action` | text NOT NULL | `publish` / `promote` / `yank` / … |
| `target` | text NOT NULL | `"cv-reviewer@1.4.2"` / `"cv-reviewer/production"` |
| `detail_json` | json NOT NULL DEFAULT `{}` | |
| `created_at` | timestamp NOT NULL | |

## 3. Release 狀態機

```text
published ──yank──▶ yanked(終態;object 不刪,不可再新安裝/promote)
```

- `promote` 目標必須是 `published`;指向 `yanked` → `ReleaseYanked`;
  不存在 → `ReleaseNotPublished`。
- `yank` 目標不存在 → `ReleaseNotPublished`;已 `yanked` → 冪等(no-op)。
- `resolve` 仍會回傳 `yanked` release(含 status 欄);修正 channel 是操作者的事,
  下載端(Agent / `download`)看到 `yanked` 一律拒絕(`ReleaseYanked`)。

## 4. 目前各 adapter 差異(Slice 3 收斂)

| 欄位 / 物件 | logical(本文件) | SQLite(`package_services.py`) | PostgreSQL(`deploy/local/init-registry.sql`) |
|-------------|------------------|-------------------------------|----------------------------------------------|
| `applications.display_name` / `enabled` | 有 | **缺** | 有 |
| `releases.status` 值域約束 | `IN ('published','yanked')` | ✅ 有 CHECK(Slice 1.5 補) | **缺**(自由字串) |
| `releases.dependency_fingerprint` / `platform_constraint` | 有(NULL 可) | **缺** | 有 |
| `audit_events` | 有 | **缺** | 有 |
| identifier 約束 | regex(§1) | 應用層 `validate_identifier` | 未於 DDL 表達 |

> Slice 1.5 只把 SQLite 的 `status` 收斂為受約束值域(已含 CHECK)並確立本文件;
> 其餘欄位差異與 identifier 的 DB 層約束留待 Slice 3 的 migration 一起補齊。
