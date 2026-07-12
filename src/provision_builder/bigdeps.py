"""大型相依隔離（SPEC §6）—— 本專案最重要的自訂邏輯。

超過門檻（預設 100 MB）的 wheel 從各工具的 `wheels/` **搬到**頂層 `big-deps/`，
只存一份、跨工具去重。使用者因此能一眼看到「哪些是大東西」，並把 `big-deps/`
與其餘部分分開搬運（例如另用一顆硬碟帶）。

心智模型（不要改動）：`deppack.json` **永遠保持完整**——它描述的是「apply 之後」
的形狀。大 wheel 缺席只是**搬運期的暫態**。apply 把它們放回各工具 wheelhouse 後，
平台自己的 sha256 驗證就會通過；等於用平台的驗章證明了重組正確。平台零改動。
"""

from __future__ import annotations

import shutil
from pathlib import Path

from ._util import sha256_file


class BigDepConflict(Exception):
    """big-deps 內出現同檔名但不同內容的 wheel。

    wheel 檔名含「套件-版本-python標籤-abi-平台」，同名不同內容不該發生；
    真的發生代表某一邊的檔案損毀或被竄改。靜默挑一個 = 難查的執行期錯誤，故中止。
    """


def isolate_pack(pack_dir: Path, big_deps_dir: Path, threshold_bytes: int) -> list[str]:
    """把 pack 的 wheels/ 內超過門檻的 wheel 移到 big_deps_dir，回被移走的檔名（sorted）。

    - threshold_bytes <= 0 → 關閉隔離，回空清單。
    - big_deps_dir 已有同名檔：sha256 相同 → 刪掉 pack 內那份（跨工具去重）；
      不同 → 拋 BigDepConflict。
    - **不動 deppack.json**（見模組 docstring）。
    """
    pack_dir = Path(pack_dir)
    big_deps_dir = Path(big_deps_dir)
    if threshold_bytes <= 0:
        return []

    wheels_dir = pack_dir / "wheels"
    if not wheels_dir.is_dir():
        return []

    moved: list[str] = []
    for wheel in sorted(wheels_dir.glob("*.whl")):
        if wheel.stat().st_size <= threshold_bytes:
            continue
        big_deps_dir.mkdir(parents=True, exist_ok=True)
        dest = big_deps_dir / wheel.name
        if dest.exists():
            src_digest, _ = sha256_file(wheel)
            dst_digest, _ = sha256_file(dest)
            if src_digest != dst_digest:
                raise BigDepConflict(
                    f"big-deps 內已有同名但內容不同的 wheel：{wheel.name}\n"
                    f"  既有：{dest}（sha256 {dst_digest[:12]}…）\n"
                    f"  新的：{wheel}（sha256 {src_digest[:12]}…）\n"
                    f"這代表其中一份損毀或被竄改。請刪掉整個產出目錄後用 --force 重產。"
                )
            wheel.unlink()  # 去重：內容相同，pack 內那份不需要
        else:
            shutil.move(str(wheel), str(dest))
        moved.append(wheel.name)
    return sorted(moved)


def classify_pack_wheels(pack_dir: Path, big_deps_dir: Path, wheel_names: list[str]) -> list[str]:
    """對已存在的 pack，判斷 manifest 列的哪些 wheel 目前住在 big-deps（供沿用快取時重建報告）。"""
    big_deps_dir = Path(big_deps_dir)
    wheels_dir = Path(pack_dir) / "wheels"
    return sorted(
        name for name in wheel_names
        if not (wheels_dir / name).exists() and (big_deps_dir / name).exists()
    )


def exclusive_wheels(prev_big_deps: list[dict], tool_id: str) -> list[str]:
    """從舊 provision.json 的 big_deps 清單找出「只有 tool_id 在用」的 wheel 檔名。

    重建某工具前要釋放它獨佔的大 wheel；被別的工具共用的**絕不能刪**
    （SPEC §8.1：不確定就留著，寧可多佔空間不可誤刪）。
    """
    names: list[str] = []
    for entry in prev_big_deps or []:
        used_by = list(entry.get("used_by") or [])
        if used_by == [tool_id]:
            names.append(str(entry.get("name", "")))
    return sorted(n for n in names if n)


def prune_orphans(big_deps_dir: Path, referenced: set[str]) -> list[str]:
    """刪掉 big-deps 內沒有任何工具引用的 wheel，回被刪的檔名。

    **只在「全量 build」（沒有 --tools 篩選）後呼叫**：有篩選時我們看不到全部工具的
    引用關係，刪除會誤傷。
    """
    big_deps_dir = Path(big_deps_dir)
    if not big_deps_dir.is_dir():
        return []
    removed: list[str] = []
    for wheel in sorted(big_deps_dir.glob("*.whl")):
        if wheel.name not in referenced:
            wheel.unlink()
            removed.append(wheel.name)
    return removed
