# 05 — 任務卡:Slice 1.5「Domain hardening」

> ✅ **已完成(2026-07-11)。** 本卡保留作為驗收回顧與後續 Slice 的樣板。
> 交付:`package_errors.py`(12 error + `validate_identifier`)、`package_services.py`
> 重排與 yank、`local_services.py` 的 `yank` 子命令與 `ERROR[code]:` 輸出、
> `docs/REGISTRY_LOGICAL_SCHEMA.md`、`tests/test_package_errors.py` 與升級後的
> `tests/test_package_services.py`。基線(當時):全 repo 201 passed / 6 skipped。
> **歷史任務卡:Slice 1–8 皆已完成,現行進度與下一步(UI-1)見 `00_START_HERE.md` §3
> 與 `11_TASK_UI1.md`;現行基線見 `07_CHECKLISTS.md`。**

> 目標一句話:把 Slice 1 的 domain 打磨到「任何相同輸入,在任何失敗點,
> 都回傳穩定的 domain error」,並補上 yank 模型與 identifier 驗證。
> 規模:純 stdlib、只動 3 個既有檔 + 新增 2 個檔。**不碰 HTTP、不碰 DB adapter。**

## 0. 前置檢查(動手前)

```powershell
py -3.11 -m pytest tests\test_package_services.py   # 必須 5 passed
```

## 1. 新增 `src/provision_builder/package_errors.py`

只用 stdlib。骨架(照抄後補齊全部 12 類,對照 `03_DOMAIN_SPEC.md` §4 的表):

```python
"""Stable domain errors shared by CLI, HTTP API, and future GUI."""

from __future__ import annotations

import re

IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class PackageDomainError(Exception):
    """Base class; `code` is a stable machine-readable identifier."""
    code = "package_domain_error"


class InvalidIdentifier(PackageDomainError):
    code = "invalid_identifier"


class DuplicateVersion(PackageDomainError):
    code = "duplicate_version"

# ... 其餘 10 類同樣模式,code 值逐字照 03_DOMAIN_SPEC.md §4 ...


def validate_identifier(value: str, label: str) -> str:
    if not IDENTIFIER_RE.fullmatch(value or ""):
        raise InvalidIdentifier(f"invalid {label}: {value!r}")
    return value
```

## 2. 修改 `src/provision_builder/package_services.py`

### 2a. 換掉 `_safe_part`

`app_id` / `version` / `channel` 的驗證一律改呼叫
`package_errors.validate_identifier`。刪除 `_safe_part`(或保留給 object key
內部組件用,但公開入口一律走新驗證)。

### 2b. `publish()` 重排(照 `03_DOMAIN_SPEC.md` §5 的 5 步順序)

```python
def publish(self, app_id, version, package):
    validate_identifier(app_id, "app_id")
    validate_identifier(version, "version")
    if self.registry.get_release(app_id, version) is not None:
        raise DuplicateVersion(f"release {app_id}@{version} already exists")
    object_key = f"applications/{app_id}/{version}/{app_id}-{version}.napp"
    if self.objects.exists(object_key):
        raise ArtifactAlreadyExists(
            f"orphan artifact exists for {app_id}@{version}; run GC or bump version")
    with Path(package).open("rb") as source:
        sha256, size = self.objects.put(object_key, source)
    release = Release(app_id, version, object_key, sha256, size, "published", _utc_now())
    try:
        self.registry.create_release(release)
    except sqlite3.IntegrityError as exc:          # 併發 race:constraint 仍是最終防線
        raise DuplicateVersion(f"release {app_id}@{version} already exists") from exc
    return release
```

注意:`sqlite3.IntegrityError` 的轉換放在 `SQLiteRegistry.create_release`
內做也可以(更乾淨,adapter 內把 DB 例外翻成 domain error)——**擇一,
但未來每個 Registry adapter 都必須遵守同一契約:重複 insert → `DuplicateVersion`**。

### 2c. `promote()` 錯誤升級

- release 不存在或 status 不是 published/yanked → `ReleaseNotPublished`
- release 是 `yanked` → `ReleaseYanked`
- (channel 名稱先過 `validate_identifier`)

### 2d. 新增 `yank()`

`Registry` Protocol 加一個方法,SQLiteRegistry 實作:

```python
def yank(self, app_id: str, version: str) -> None:
    # release 不存在 → ReleaseNotPublished
    # 已 yanked → 冪等,直接返回(重複 yank 不是錯)
    # published → UPDATE status = 'yanked'
```

### 2e. `download()` 錯誤升級

- resolve 回 None → `UnknownChannel`
- release.status == "yanked" → `ReleaseYanked`
- object 不存在(FileNotFoundError / store 回報)→ `ArtifactMissing`
- SHA-256 不符 → `HashMismatch`(取代現在的 `ValueError`)

### 2f. SQLite 保持 `yanked` 語意

schema 的 `status` 欄已存在,不用改表;只要 `promote`/`download` 依 §2c/§2e
檢查 status。(logical schema 的其它欄位差異留給 Slice 3,**不要**在這個
Slice 動 `init-registry.sql`。)

## 3. 修改 `local_services.py`

- 增加 `yank` 子命令:`yank <app_id> <version>`。
- except 區塊改抓 `PackageDomainError`,輸出格式固定:

```text
ERROR[duplicate_version]: release cv-reviewer@1.0.0 already exists
```

- 其它非 domain 例外照舊(OSError 等)。exit code 維持 1。

## 4. 新增 `docs/REGISTRY_LOGICAL_SCHEMA.md`

把 `03_DOMAIN_SPEC.md` §6 的 logical schema 抄成 repo 內正式文件,
開頭註明:「本文件是 Registry schema 的單一權威;SQLite / PostgreSQL /
Oracle adapter 的 DDL 可不同,但欄位與約束語意必須一致,並以共同
contract tests 驗證(Slice 3)」。

## 5. 測試(加到 `tests/test_package_services.py`,或新開 `tests/test_package_errors.py`)

**必須先更新 2 個既有測試**(行為刻意改變,不是回歸):

| 既有測試 | 原預期 | 新預期 |
|----------|--------|--------|
| `test_release_and_object_are_immutable` | `FileExistsError`("immutable object") | `DuplicateVersion` |
| `test_only_published_existing_release_can_be_promoted` | `KeyError` | `ReleaseNotPublished` |

**新增測試(全部要有)**:

1. `test_invalid_app_id_rejected` — `publish("../evil", ...)`、`publish("a b", ...)`、
   `publish("a:b", ...)` 都 raise `InvalidIdentifier`(version、channel 同測)。
2. `test_duplicate_publish_is_stable_across_retries` — 同版本 publish 兩次、三次,
   **每次**都是 `DuplicateVersion`(錯誤型別不隨失敗點漂移)。
3. `test_orphan_object_reports_artifact_already_exists` — 手工先放 object
   (用 `objects.put` 直接寫同一 key)但 registry 無 release → publish 回
   `ArtifactAlreadyExists`。
4. `test_yank_blocks_promote_and_download` — publish→promote→yank 後:
   再 promote 同版 → `ReleaseYanked`;download → `ReleaseYanked`;
   `resolve` 仍回傳 release 且 `status == "yanked"`。
5. `test_yank_is_idempotent` — yank 兩次不噴錯。
6. `test_yank_missing_release` — 不存在的版本 yank → `ReleaseNotPublished`。
7. `test_download_missing_object_reports_artifact_missing` — publish 後手工刪
   object 檔案 → download 回 `ArtifactMissing`(不是 FileNotFoundError)。
8. `test_hash_mismatch_error_code` — 竄改 object 後 download → `HashMismatch`
   且 `exc.code == "hash_mismatch"`(把既有 tampered 測試升級或並存)。
9. `test_unknown_channel` — 沒 promote 過就 download → `UnknownChannel`。
10. `test_error_codes_are_stable` — 逐一斷言 12 個類別的 `.code` 字串
    等於 `03_DOMAIN_SPEC.md` §4 表列值(防手滑改字串)。

## 6. 驗收(全部成立才算完成)

```powershell
py -3.11 -m pytest tests\test_package_services.py    # 全綠(≥15 個測試)
py -3.11 -m pytest tests                              # 不低於 166 passed / 6 skipped 基線
```

- [ ] 相同輸入在任何失敗點回傳同一 domain error(測試 2、3 證明)。
- [ ] `local_services.py` 錯誤輸出帶 `ERROR[code]:` 前綴,並有 `yank` 子命令。
- [ ] `docs/REGISTRY_LOGICAL_SCHEMA.md` 已建立。
- [ ] `src/provision_builder/` 仍然 **零第三方 import**(用 `import` 掃一遍確認)。
- [ ] 更新 `04_CODE_MAP.md` §5 進度表,重建 handoff zip(`07_CHECKLISTS.md` §4)。

## 7. 禁止事項(這個 Slice 特別容易犯)

- ❌ 不要開始寫 FastAPI / HTTP 任何東西(那是 Slice 2)。
- ❌ 不要動 `deploy/local/init-registry.sql`(Slice 3)。
- ❌ 不要實作 GC 本體(只要 `ArtifactAlreadyExists` 錯誤訊息提到 GC 即可;
  GC 在 Slice 3 與 object store 一起做)。
- ❌ 不要「順手」改 provision build/apply/warmup 相關程式。
- ❌ 不要讓任何 domain error 繼承 `KeyError`/`ValueError` 來遷就舊測試;
  舊測試照 §5 的表**改預期**。
