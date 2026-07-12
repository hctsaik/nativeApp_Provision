#!/usr/bin/env python3
"""apply.py — 把離線補給包套用到平台的 dep-pack 快取（在**沒有網路的電腦**上執行）。

這支檔案會被逐字複製進每一個補給包產出資料夾，並在離線機上獨立執行。
因此它：**只用 Python 標準函式庫、不 import 本專案任何模組、絕不連網、絕不呼叫 pip**
（SPEC D8 / §9）。安裝是平台 engine 在工具首次啟動時做的；apply 只負責把檔案
放到 engine 認得的位置。

用法（在補給包資料夾內）：
    python apply.py --deppack-cache <目標資料夾>
    python apply.py --deppack-cache <目標資料夾> --dry-run
    python apply.py --deppack-cache <目標資料夾> --tools module_016,app-lv

`<目標資料夾>` = 平台的 CIM_DEPPACK_CACHE：
    可攜模式  <APP_ROOT>\\data\\<project-key>\\deppack-cache
    開發模式  <平台專案>\\sidecar\\python-engine\\.deppack-cache

安全性質（刻意設計，勿簡化）：
  * 大 wheel 住在頂層 big-deps\\，組裝時補回各工具的 wheels\\（先 hardlink，跨磁碟區退回 copy）。
  * 組裝在暫存目錄完成 + 全量 sha256 驗證通過後，才用 rename **原子性換位**。
    中途失敗/斷電 → 目標維持原樣，絕不留下「wheels 不完整但 deppack.json 存在」的目錄
    （那會觸發 engine 的 fail-closed 驗章，訊息難懂）。
  * 缺檔的工具整個跳過並明確報告，其餘工具照常套用。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path

# 與 provision_builder/__init__.py 的常數一致（tests/test_apply.py 會比對，防漂移）
PACKS_DIRNAME = "packs"
BIG_DEPS_DIRNAME = "big-deps"
PROVISION_MANIFEST = "provision.json"
DEPPACK_MANIFEST = "deppack.json"
WHEELS_DIRNAME = "wheels"

_TEMP_PREFIX = ".applying-"
_OLD_SUFFIX = ".old-"
_CHUNK = 1024 * 1024


# ── 小工具（stdlib only）───────────────────────────────────────────────────────

def sha256_file(path: Path) -> tuple[str, int]:
    """串流 sha256 + 大小。與平台 core.deppack._sha256_file 同演算法。"""
    h = hashlib.sha256()
    size = 0
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(_CHUNK), b""):
            h.update(chunk)
            size += len(chunk)
    return h.hexdigest(), size


def human_size(num_bytes: int) -> str:
    value = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if abs(value) < 1024.0:
            return f"{int(value)} B" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{value:.1f} TB"


def guard_console_encoding() -> None:
    """CP950 繁中主控台印到非 CP950 字元會炸。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # type: ignore[union-attr]
        except Exception:
            pass


def link_or_copy(src: Path, dst: Path) -> None:
    """先試 hardlink（大 wheel 零成本、零磁碟）；跨磁碟區或檔案系統不支援時退回 copy。"""
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


# ── 結果型別 ───────────────────────────────────────────────────────────────────

class ToolOutcome:
    """單一工具的套用結果。`missing_big` 是可補救的（把檔案放回 big-deps 就好）。"""

    def __init__(self, tool_id: str) -> None:
        self.tool_id = tool_id
        self.status = "ok"                     # ok | skipped | failed
        self.messages: list[str] = []
        self.missing_big: list[str] = []
        self.wheel_count = 0
        self.total_bytes = 0

    def skip(self, messages: list[str]) -> "ToolOutcome":
        self.status = "skipped"
        self.messages.extend(messages)
        return self

    def fail(self, message: str) -> "ToolOutcome":
        self.status = "failed"
        self.messages.append(message)
        return self


# ── 核心 ───────────────────────────────────────────────────────────────────────

def load_json(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def read_known_big(root: Path) -> set[str] | None:
    """從 provision.json 讀「哪些 wheel 本來就住在 big-deps」。

    有了它才能把「大型相依未就位（可補救）」與「補給包本身缺 wheel（壞了）」分開講。
    provision.json 不存在/壞掉 → None，退化成保守訊息。
    """
    path = root / PROVISION_MANIFEST
    if not path.is_file():
        return None
    try:
        return {str(entry["name"]) for entry in load_json(path).get("big_deps", [])}
    except (OSError, ValueError, KeyError):
        return None


def locate_wheel(name: str, wheels_dir: Path, big_deps_dir: Path) -> Path | None:
    """一個 wheel 可能住在 pack 內（一般）或 big-deps（大相依被隔離）。"""
    local = wheels_dir / name
    if local.is_file():
        return local
    big = big_deps_dir / name
    if big.is_file():
        return big
    return None


def precheck_sources(
    manifest: dict, pack_dir: Path, big_deps_dir: Path, known_big: set[str] | None,
) -> tuple[list[tuple[str, Path]], list[str], list[str]]:
    """定位每個 wheel 並做**便宜**的檢查（存在 + 大小）。

    這裡刻意不算 sha256：對 2GB 的 torch 而言，雜湊一次的成本約等於複製一次。
    完整的 sha256 驗證在組裝**之後**做一次（verify_assembled），總共只掃一遍。
    大小不符已足以在複製前攔下截斷 / 半途中斷的檔案。

    回 (可用來源, 問題訊息, 未就位的大 wheel 檔名)。
    """
    wheels_dir = pack_dir / WHEELS_DIRNAME
    sources: list[tuple[str, Path]] = []
    problems: list[str] = []
    missing_big: list[str] = []
    listed: set[str] = set()
    for entry in manifest.get("wheels", []):
        name = str(entry["name"])
        listed.add(name)
        src = locate_wheel(name, wheels_dir, big_deps_dir)
        if src is None:
            is_big = name in known_big if known_big is not None else True
            if is_big:
                missing_big.append(name)
                problems.append(f"大型相依未就位：{name}")
            else:
                problems.append(f"補給包缺少 wheel：{name}")
            continue
        actual = src.stat().st_size
        if actual != int(entry["size"]):
            problems.append(f"大小不符：{name}（manifest {entry['size']} / 實際 {actual}）")
            continue
        sources.append((name, src))

    # manifest 未列的 wheel = 這個 pack 被動過手腳（隔離只會拿走檔案，不會多出檔案）。
    # 平台的 verify_deppack_dir 也拒絕多餘檔，這裡先攔下來。
    for extra in sorted({p.name for p in wheels_dir.glob("*.whl")} - listed):
        problems.append(f"{DEPPACK_MANIFEST} 未列的多餘 wheel：{extra}")

    return sources, problems, sorted(missing_big)


def verify_assembled(pack_dir: Path, manifest: dict) -> list[str]:
    """組裝完成的 pack 必須逐位元組等同 manifest（= 平台 verify_deppack_dir 的語意）。"""
    wheels_dir = pack_dir / WHEELS_DIRNAME
    errors: list[str] = []
    listed = set()
    for entry in manifest.get("wheels", []):
        name = str(entry["name"])
        listed.add(name)
        path = wheels_dir / name
        if not path.is_file():
            errors.append(f"組裝後仍缺少：{name}")
            continue
        digest, size = sha256_file(path)
        if size != int(entry["size"]):
            errors.append(f"大小不符：{name}")
        elif digest != str(entry["sha256"]):
            errors.append(f"sha256 不符（疑損毀/被竄改）：{name}")
    for extra in sorted({p.name for p in wheels_dir.glob("*.whl")} - listed):
        errors.append(f"多餘的 wheel：{extra}")
    return errors


def _cleanup(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)


def _atomic_swap(staged: Path, final: Path) -> None:
    """把 staged 換到 final。已有舊版 → 先改名保留，換位成功才刪。

    不做「先刪再拷」：那會在中途斷電時留下空目錄或半套 pack。
    """
    backup: Path | None = None
    if final.exists():
        index = 0
        while True:
            candidate = final.with_name(f"{final.name}{_OLD_SUFFIX}{index}")
            if not candidate.exists():
                backup = candidate
                break
            index += 1
        final.rename(backup)
    try:
        staged.rename(final)
    except OSError:
        if backup is not None:
            backup.rename(final)  # 還原，目標維持原樣
        raise
    if backup is not None:
        shutil.rmtree(backup, ignore_errors=True)


def apply_pack(
    pack_dir: Path, big_deps_dir: Path, cache_root: Path,
    *, known_big: set[str] | None, dry_run: bool,
) -> ToolOutcome:
    tool_id = pack_dir.name
    outcome = ToolOutcome(tool_id)

    if not (pack_dir / DEPPACK_MANIFEST).is_file():
        return outcome.fail(f"找不到 {DEPPACK_MANIFEST}（不是一個 dep-pack 資料夾）")
    try:
        manifest = load_json(pack_dir / DEPPACK_MANIFEST)
        tool_id = str(manifest.get("tool_id") or tool_id)
        outcome.tool_id = tool_id
    except (OSError, ValueError) as exc:
        return outcome.fail(f"{DEPPACK_MANIFEST} 無法解析：{exc}")

    sources, problems, missing_big = precheck_sources(manifest, pack_dir, big_deps_dir, known_big)
    if problems:
        outcome.missing_big = missing_big
        return outcome.skip(problems)

    outcome.wheel_count = len(sources)
    outcome.total_bytes = sum(int(e["size"]) for e in manifest.get("wheels", []))
    if dry_run:
        return outcome

    staged = cache_root / f"{_TEMP_PREFIX}{tool_id}"
    _cleanup(staged)
    try:
        (staged / WHEELS_DIRNAME).mkdir(parents=True)
        shutil.copy2(pack_dir / DEPPACK_MANIFEST, staged / DEPPACK_MANIFEST)
        for name, src in sources:
            link_or_copy(src, staged / WHEELS_DIRNAME / name)

        errors = verify_assembled(staged, manifest)
        if errors:
            _cleanup(staged)
            return outcome.fail("組裝後驗證失敗：" + "；".join(errors[:3]))

        _atomic_swap(staged, cache_root / tool_id)
    except OSError as exc:
        _cleanup(staged)
        return outcome.fail(f"套用失敗（目標維持原樣）：{exc}")
    return outcome


def clean_stale_temp(cache_root: Path) -> None:
    """清掉上次中斷留下的暫存/備份目錄（它們永遠不是有效的 dep-pack）。"""
    if not cache_root.is_dir():
        return
    for child in cache_root.iterdir():
        if child.is_dir() and (child.name.startswith(_TEMP_PREFIX) or _OLD_SUFFIX in child.name):
            shutil.rmtree(child, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    guard_console_encoding()
    parser = argparse.ArgumentParser(
        description="把離線補給包套用到平台的 dep-pack 快取（不連網、不跑 pip）",
    )
    parser.add_argument("--deppack-cache", required=True,
                        help="目標資料夾（= 平台的 CIM_DEPPACK_CACHE）")
    parser.add_argument("--tools", default=None,
                        help="只套用這些工具（逗號分隔）；省略 = 全部")
    parser.add_argument("--dry-run", action="store_true",
                        help="只檢查與列出計畫，不寫入任何檔案")
    args = parser.parse_args(argv)

    root = Path(__file__).resolve().parent
    packs_dir = root / PACKS_DIRNAME
    big_deps_dir = root / BIG_DEPS_DIRNAME
    if not packs_dir.is_dir():
        print(f"[錯誤] 找不到 {packs_dir}——這個資料夾不是補給包。")
        return 2

    cache_root = Path(args.deppack_cache).resolve()
    known_big = read_known_big(root)
    wanted = {t.strip() for t in args.tools.split(",") if t.strip()} if args.tools else None

    pack_dirs = sorted(p for p in packs_dir.iterdir() if p.is_dir())
    if wanted is not None:
        known = {p.name for p in pack_dirs}
        unknown = wanted - known
        if unknown:
            print(f"[錯誤] --tools 指定了補給包裡沒有的工具：{'、'.join(sorted(unknown))}")
            print(f"       這包裡有：{'、'.join(sorted(known)) or '（無）'}")
            return 2
        pack_dirs = [p for p in pack_dirs if p.name in wanted]

    print(f"補給包：{root}")
    print(f"目標  ：{cache_root}")
    if args.dry_run:
        print("模式  ：--dry-run（不寫入任何檔案）")
    print("")

    if not args.dry_run:
        cache_root.mkdir(parents=True, exist_ok=True)
        clean_stale_temp(cache_root)

    outcomes = [
        apply_pack(p, big_deps_dir, cache_root, known_big=known_big, dry_run=args.dry_run)
        for p in pack_dirs
    ]

    for outcome in outcomes:
        if outcome.status == "ok":
            verb = "可套用" if args.dry_run else "已套用"
            print(f"  [OK]   {outcome.tool_id}：{verb}"
                  f"（{outcome.wheel_count} 個 wheel，{human_size(outcome.total_bytes)}）")
        else:
            label = "跳過" if outcome.status == "skipped" else "失敗"
            print(f"  [{label}] {outcome.tool_id}")
            for message in outcome.messages:
                print(f"         {message}")

    skipped = [o for o in outcomes if o.status == "skipped"]
    failed = [o for o in outcomes if o.status == "failed"]
    succeeded = [o for o in outcomes if o.status == "ok"]

    print("")
    print(f"總結：成功 {len(succeeded)}、跳過 {len(skipped)}、失敗 {len(failed)}"
          f"（共 {len(outcomes)} 個工具）")

    missing_big = sorted({name for o in skipped for name in o.missing_big})
    if missing_big:
        affected = sorted({o.tool_id for o in skipped if o.missing_big})
        print("")
        print(f"缺少的大型相依（把檔案放回 {big_deps_dir} 後重跑 apply 即可）：")
        for name in missing_big:
            print(f"  - {name}")
        print(f"影響的工具：{'、'.join(affected)}")
    if failed:
        print("")
        print("失敗的工具，目標位置維持原樣（沒有留下半套的 dep-pack）。")

    return 0 if (not skipped and not failed) else 1


if __name__ == "__main__":
    raise SystemExit(main())
