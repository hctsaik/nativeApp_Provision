"""補給包完整性驗證（SPEC §4.2）—— 搬運後、apply 前用。

**刻意不經過 gateway**：verify 要能在「沒有平台專案」的機器上跑（隨身碟插上就先驗一次）。
所以這裡用 stdlib 讀 deppack.json 並自己算 sha256——演算法與平台 `core.deppack` 相同
（sha256 of raw bytes），tests/test_verify.py 用平台的 verify_deppack_dir 對照驗證過。

split-aware：manifest 列的 wheel 可能住在 `packs/<id>/wheels/`（一般）或
`big-deps/`（大相依，被隔離）。少了 big-deps 的檔案不是「損毀」而是「未就位」，
訊息必須分開講清楚（使用者可能刻意把 big-deps 分開搬運，SPEC §6.3）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from . import BIG_DEPS_DIRNAME, DEPPACK_MANIFEST, PACKS_DIRNAME, PROVISION_MANIFEST, WHEELS_DIRNAME
from ._util import sha256_file


@dataclass
class PackVerdict:
    """單一工具 pack 的驗證結果。"""

    tool_id: str
    ok: bool = True
    errors: list[str] = field(default_factory=list)          # 真的壞掉（損毀 / 多餘檔 / manifest 壞）
    missing_big: list[str] = field(default_factory=list)     # 大 wheel 未就位（可補救）
    missing_other: list[str] = field(default_factory=list)   # 一般 wheel 不見了（包本身不完整）

    @property
    def applicable(self) -> bool:
        """能不能被 apply（缺任何檔案或有錯誤都不行）。"""
        return self.ok and not self.missing_big and not self.missing_other


@dataclass
class ProvisionVerdict:
    root: Path
    packs: list[PackVerdict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)  # 補給包層級問題（provision.json 缺/對不上）

    @property
    def ok(self) -> bool:
        return not self.errors and all(p.applicable for p in self.packs)


def _load_json(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def verify_pack(pack_dir: Path, big_deps_dir: Path, *, known_big: set[str] | None = None) -> PackVerdict:
    """驗證一個 pack：manifest 列的每個 wheel 都在（pack 內或 big-deps），且 sha256/大小相符。

    `known_big`：從 provision.json 得知「這些檔名本來就該住在 big-deps」。
    給定時，缺這些檔案歸類為 missing_big（可補救）；沒給時退而求其次——
    只要 pack 內沒有就當作 missing_big（因為 pack 內缺席的檔案照定義就是被隔離走的）。
    """
    pack_dir = Path(pack_dir)
    big_deps_dir = Path(big_deps_dir)
    tool_id = pack_dir.name
    verdict = PackVerdict(tool_id=tool_id)

    manifest_path = pack_dir / DEPPACK_MANIFEST
    if not manifest_path.is_file():
        verdict.ok = False
        verdict.errors.append(f"找不到 {DEPPACK_MANIFEST}（這不是一個 dep-pack 資料夾）")
        return verdict

    try:
        manifest = _load_json(manifest_path)
        entries = list(manifest["wheels"])
        tool_id = str(manifest.get("tool_id") or tool_id)
        verdict.tool_id = tool_id
    except (OSError, ValueError, KeyError) as exc:
        verdict.ok = False
        verdict.errors.append(f"{DEPPACK_MANIFEST} 無法解析：{exc}")
        return verdict

    wheels_dir = pack_dir / WHEELS_DIRNAME
    listed = {str(e["name"]) for e in entries}

    for entry in entries:
        name = str(entry["name"])
        local = wheels_dir / name
        big = big_deps_dir / name
        if local.is_file():
            source = local
        elif big.is_file():
            source = big
        else:
            is_big = name in known_big if known_big is not None else True
            (verdict.missing_big if is_big else verdict.missing_other).append(name)
            continue

        digest, size = sha256_file(source)
        if size != int(entry["size"]):
            verdict.ok = False
            verdict.errors.append(
                f"大小不符：{name}（manifest {entry['size']} / 實際 {size}）"
            )
        elif digest != str(entry["sha256"]):
            verdict.ok = False
            verdict.errors.append(f"sha256 不符（疑被竄改/損毀）：{name}")

    for extra in sorted({p.name for p in wheels_dir.glob("*.whl")} - listed):
        verdict.ok = False
        verdict.errors.append(f"{DEPPACK_MANIFEST} 未列的多餘 wheel：{extra}")

    verdict.missing_big.sort()
    verdict.missing_other.sort()
    return verdict


def verify_provision(root: Path) -> ProvisionVerdict:
    """驗證整包：provision.json 的工具清單 vs packs/ 實況，再逐 pack 驗檔案。"""
    root = Path(root)
    verdict = ProvisionVerdict(root=root)

    packs_dir = root / PACKS_DIRNAME
    big_deps_dir = root / BIG_DEPS_DIRNAME
    if not packs_dir.is_dir():
        verdict.errors.append(f"找不到 {PACKS_DIRNAME}\\ 目錄——{root} 不是一個補給包")
        return verdict

    known_big: set[str] | None = None
    expected_tools: set[str] | None = None
    manifest_path = root / PROVISION_MANIFEST
    if manifest_path.is_file():
        try:
            pm = _load_json(manifest_path)
            known_big = {str(b["name"]) for b in pm.get("big_deps", [])}
            expected_tools = {str(t["tool_id"]) for t in pm.get("tools", [])}
        except (OSError, ValueError, KeyError) as exc:
            verdict.errors.append(f"{PROVISION_MANIFEST} 無法解析：{exc}")
    else:
        verdict.errors.append(f"缺少 {PROVISION_MANIFEST}（仍可逐 pack 驗證，但無法比對工具清單）")

    present = sorted(p.name for p in packs_dir.iterdir() if p.is_dir())
    if expected_tools is not None:
        for missing in sorted(expected_tools - set(present)):
            verdict.errors.append(f"{PROVISION_MANIFEST} 列出的工具沒有對應的 pack：{missing}")
        for unknown in sorted(set(present) - expected_tools):
            verdict.errors.append(f"packs\\ 內有 {PROVISION_MANIFEST} 未列的目錄：{unknown}")

    for tool_id in present:
        verdict.packs.append(verify_pack(packs_dir / tool_id, big_deps_dir, known_big=known_big))

    # big-deps 內未被任何 manifest 引用的檔案 → 提示（不算錯，可能是使用者刻意留的）
    if big_deps_dir.is_dir() and known_big is not None:
        for stray in sorted({p.name for p in big_deps_dir.glob("*.whl")} - known_big):
            verdict.errors.append(f"{BIG_DEPS_DIRNAME}\\ 內有 {PROVISION_MANIFEST} 未列的檔案：{stray}")

    return verdict


def format_verdict(verdict: ProvisionVerdict) -> str:
    """人讀報告。缺 big-deps 時給可行動的指示（SPEC §6.3）。"""
    lines: list[str] = []
    for pack in verdict.packs:
        if pack.applicable:
            lines.append(f"  [OK]   {pack.tool_id}")
            continue
        lines.append(f"  [FAIL] {pack.tool_id}")
        for err in pack.errors:
            lines.append(f"         - {err}")
        for name in pack.missing_other:
            lines.append(f"         - 缺少 wheel（補給包不完整）：{name}")
        for name in pack.missing_big:
            lines.append(f"         - 大型相依未就位：{name}")

    all_missing_big = sorted({n for p in verdict.packs for n in p.missing_big})
    if all_missing_big:
        affected = sorted({p.tool_id for p in verdict.packs if p.missing_big})
        lines.append("")
        lines.append("  大型相依未就位（可補救）：")
        for name in all_missing_big:
            lines.append(f"    - {name}")
        lines.append(f"  影響的工具：{'、'.join(affected)}")
        lines.append(f"  解法：把上列檔案放回 {verdict.root / BIG_DEPS_DIRNAME}\\ 後重跑 verify。")

    for err in verdict.errors:
        lines.append(f"  [補給包] {err}")

    lines.append("")
    lines.append(f"  結果：{'全部通過' if verdict.ok else '有問題（見上）'}"
                 f"（{sum(1 for p in verdict.packs if p.applicable)}/{len(verdict.packs)} 個工具可套用）")
    return "\n".join(lines)
