"""build 主流程（SPEC §4.1 / §8）—— 掃描 → 增量判斷 → 產包 → 隔離大相依 → 自檢 → 寫報告。

失敗續行：單一工具失敗（pip 解不開、自檢不過）不中斷其它工具；結束時列出失敗清單並回非零 exit code。
這很重要——工廠部署常常是「先把能包的包好，剩下一個明天再處理」。
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path

from . import (
    BIG_DEPS_DIRNAME,
    DEPPACK_MANIFEST,
    PACKS_DIRNAME,
    WHEELS_DIRNAME,
)
from . import bigdeps, manifest as manifest_mod, report as report_mod, verify as verify_mod
from ._util import human_size
from .gateway import GatewayError, PlatformGateway, Target
from .scan import PLUGIN_GLOBS, ScanResult, ToolSpec, make_subprocess_loader, scan_project
from .selfcheck import offline_resolve

REBUILD = "rebuild"
REUSE = "reuse"


@dataclass
class PlanEntry:
    tool: ToolSpec
    action: str
    reason: str


@dataclass
class BuildResult:
    dest: Path
    tools: list[dict] = field(default_factory=list)
    big_deps: list[dict] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)
    failed: list[dict] = field(default_factory=list)
    pruned: list[str] = field(default_factory=list)
    plan: list[PlanEntry] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failed


# ── 增量判斷（SPEC §8.1）───────────────────────────────────────────────────────

def decide_action(
    tool: ToolSpec,
    packs_dir: Path,
    big_deps_dir: Path,
    gateway: PlatformGateway,
    target: Target,
    *,
    force: bool = False,
    deep: bool = True,
) -> PlanEntry:
    """判斷這個工具要重建還是沿用快取。

    `deep=False`（dry-run 用）跳過 sha256 逐檔驗證，只看檔案在不在——快得多，
    但結論標註為「推測」。實際 build 一律 deep=True。
    """
    if force:
        return PlanEntry(tool, REBUILD, "--force")

    pack_dir = packs_dir / tool.tool_id
    manifest_path = pack_dir / DEPPACK_MANIFEST
    if not manifest_path.is_file():
        return PlanEntry(tool, REBUILD, "尚未產包")

    try:
        existing = gateway.load_manifest(manifest_path)
    except GatewayError as exc:
        return PlanEntry(tool, REBUILD, f"manifest 無法解析（{exc}）")

    if existing.get("requires_fingerprint") != gateway.requires_fingerprint(tool.requires):
        return PlanEntry(tool, REBUILD, "requires 已變更")

    if existing.get("python_tag") != target.python_tag:
        return PlanEntry(tool, REBUILD,
                         f"python 標籤不符（{existing.get('python_tag')} → {target.python_tag}）")
    if existing.get("platform_tag") != target.platform_tag:
        return PlanEntry(tool, REBUILD,
                         f"平台標籤不符（{existing.get('platform_tag')} → {target.platform_tag}）")

    if deep:
        verdict = verify_mod.verify_pack(pack_dir, big_deps_dir)
        if not verdict.applicable:
            detail = (verdict.errors + [f"缺 {n}" for n in verdict.missing_big + verdict.missing_other])
            return PlanEntry(tool, REBUILD, "既有包驗證未過（" + "；".join(detail[:2]) + "）")
    elif not (pack_dir / WHEELS_DIRNAME).is_dir():
        return PlanEntry(tool, REBUILD, "wheels 目錄不存在")

    return PlanEntry(tool, REUSE, "requires 與目標標籤皆未變、既有包驗證通過")


def make_plan(
    scan_result: ScanResult,
    packs_dir: Path,
    big_deps_dir: Path,
    gateway: PlatformGateway,
    target: Target,
    *,
    force: bool = False,
    deep: bool = True,
) -> list[PlanEntry]:
    return [
        decide_action(tool, packs_dir, big_deps_dir, gateway, target, force=force, deep=deep)
        for tool in scan_result.tools
    ]


# ── 單一工具產包 ───────────────────────────────────────────────────────────────

def _tool_entry(pack_dir: Path, big_deps_dir: Path, tool: ToolSpec, wheels: list[dict]) -> dict:
    big = bigdeps.classify_pack_wheels(pack_dir, big_deps_dir, [str(w["name"]) for w in wheels])
    return {
        "tool_id": tool.tool_id,
        "requires": list(tool.requires),
        "wheel_count": len(wheels),
        "total_bytes": sum(int(w["size"]) for w in wheels),
        "big_wheels": big,
    }


def build_one(
    tool: ToolSpec,
    packs_dir: Path,
    big_deps_dir: Path,
    gateway: PlatformGateway,
    target: Target,
    threshold_bytes: int,
    prev_big_deps: list[dict],
    *,
    run_selfcheck: bool = True,
) -> dict:
    """重建一個工具的 pack。失敗時清掉半成品並拋 GatewayError（呼叫端記錄後續行）。"""
    pack_dir = packs_dir / tool.tool_id

    # 1. 釋放這個工具「獨佔」的舊大 wheel（共用的絕不刪，見 bigdeps.exclusive_wheels）
    for name in bigdeps.exclusive_wheels(prev_big_deps, tool.tool_id):
        stale = big_deps_dir / name
        if stale.is_file():
            stale.unlink()

    # 2. 舊 pack 整個刪掉——pip download 到既有目錄會混入陳舊 wheel，
    #    而 compute_manifest 是掃目錄產生的，會把它們一起簽進 manifest。
    if pack_dir.exists():
        shutil.rmtree(pack_dir)

    try:
        manifest = gateway.build_wheelhouse(tool.tool_id, tool.requires, packs_dir, target)
        moved = bigdeps.isolate_pack(pack_dir, big_deps_dir, threshold_bytes)

        if run_selfcheck:
            find_links = [pack_dir / WHEELS_DIRNAME]
            if moved and big_deps_dir.is_dir():
                find_links.append(big_deps_dir)
            ok, msg = offline_resolve(gateway.python_cmd, tool.requires, find_links, target)
            if not ok:
                raise GatewayError(
                    f"{tool.tool_id}：離線可裝自檢失敗（這包到離線機也會裝不起來）\n{msg}"
                )
    except Exception:
        if pack_dir.exists():
            shutil.rmtree(pack_dir, ignore_errors=True)  # 不留半套 pack
        raise

    return _tool_entry(pack_dir, big_deps_dir, tool, list(manifest.get("wheels", [])))


# ── 主流程 ─────────────────────────────────────────────────────────────────────

def _copy_runtime_scripts(dest: Path) -> None:
    """把離線機要跑的兩支腳本逐字複製進產出。

    apply.py  — 自足、stdlib-only、不需要平台專案（SPEC D8）。
    warmup.py — 選配；借平台的 core.tool_deps 先把相依裝好，避免首次按 Start 時
                殼的 30 秒 HTTP 逾時（GUI E2E 實測，見 docs/OFFLINE_DEPLOY.md）。
    """
    repo_root = Path(__file__).resolve().parents[2]
    for name in ("apply.py", "warmup.py"):
        source = repo_root / name
        if not source.is_file():
            raise FileNotFoundError(f"找不到 {name}（預期在 {source}）——專案結構被破壞了？")
        shutil.copy2(source, Path(dest) / name)


def _write_launcher(dest: Path, project_root: Path, launch_mode: str) -> None:
    """把一鍵啟動 bat（run-platform.bat）產進補給包，並把設定烤進去。

    範本存在 repo 根；這裡只替換兩行設定：
      - MODE        ：離線機預設 portable；本機測試選 dev（見 GUI「啟動模式」）。
      - DEV_PROJECT ：自動填成這次打包的平台專案，dev 模式即開即用、不用手輸。
    範本本身是 ASCII + CRLF（cmd.exe 在任何碼頁都讀得到），逐行替換保留這個性質。
    """
    repo_root = Path(__file__).resolve().parents[2]
    template = repo_root / "run-platform.bat"
    if not template.is_file():
        raise FileNotFoundError(f"找不到 run-platform.bat 範本（預期在 {template}）。")
    mode = launch_mode if launch_mode in ("portable", "dev") else "portable"
    out_lines = []
    for line in template.read_text(encoding="utf-8").splitlines():
        if line.startswith('set "MODE='):
            line = f'set "MODE={mode}"'
        elif line.startswith('set "DEV_PROJECT='):
            line = f'set "DEV_PROJECT={Path(project_root).resolve()}"'
        out_lines.append(line)
    (Path(dest) / "run-platform.bat").write_text(
        "\r\n".join(out_lines) + "\r\n", encoding="utf-8", newline="",
    )
    readme = repo_root / "run-platform.README.txt"
    if readme.is_file():
        shutil.copy2(readme, Path(dest) / "run-platform.README.txt")


def run_build(
    project_root: Path,
    dest: Path,
    *,
    target: Target,
    threshold_mb: int,
    only_tools: list[str] | None = None,
    force: bool = False,
    dry_run: bool = False,
    python_cmd: list[str] | None = None,
    launch_mode: str = "portable",
    log=print,
) -> BuildResult:
    gateway = PlatformGateway(project_root, python_cmd=python_cmd)
    dest = Path(dest).resolve()
    packs_dir = dest / PACKS_DIRNAME
    big_deps_dir = dest / BIG_DEPS_DIRNAME
    threshold_bytes = max(0, int(threshold_mb)) * 1024 * 1024

    log(f"掃描專案：{gateway.project_root}")
    scan_result = scan_project(
        gateway.engine_root,
        make_subprocess_loader(gateway.python_cmd),
        only_tools=only_tools,
    )
    log(f"找到 {len(scan_result.tools)} 個需要補給包的工具"
        f"（另有 {len(scan_result.skipped)} 個不需要）")

    result = BuildResult(dest=dest)
    result.skipped = list(scan_result.skipped)

    if not scan_result.tools:
        log("沒有任何工具宣告 requires:——不需要補給包。")
        if not dry_run:
            dest.mkdir(parents=True, exist_ok=True)
            packs_dir.mkdir(exist_ok=True)
            _finalize(result, gateway, dest, target, threshold_mb, big_deps_dir, only_tools, launch_mode, log)
        return result

    result.plan = make_plan(
        scan_result, packs_dir, big_deps_dir, gateway, target,
        force=force, deep=not dry_run,
    )

    if dry_run:
        log("")
        log("計畫（--dry-run，未下載任何東西）：")
        for entry in result.plan:
            mark = "重建" if entry.action == REBUILD else "沿用快取"
            log(f"  [{mark}] {entry.tool.tool_id}"
                f"（{len(entry.tool.requires)} 個 requires）— {entry.reason}")
            log(f"           requires: {', '.join(entry.tool.requires)}")
        for skip in result.skipped:
            log(f"  [跳過] {skip['tool_id']} — {skip['reason']}")
        log("")
        log(f"目標標籤：{target.platform_tag} / python {target.python_version} / {target.abi}")
        log(f"大相依門檻：{threshold_mb} MB" if threshold_mb > 0 else "大相依隔離：關閉")
        return result

    dest.mkdir(parents=True, exist_ok=True)
    packs_dir.mkdir(exist_ok=True)
    prev = manifest_mod.read_provision_manifest(dest) or {}
    prev_big_deps = list(prev.get("big_deps", []))

    for entry in result.plan:
        tool = entry.tool
        if entry.action == REUSE:
            log(f"[沿用] {tool.tool_id}（{entry.reason}）")
            pack_dir = packs_dir / tool.tool_id
            wheels = gateway.load_manifest(pack_dir / DEPPACK_MANIFEST).get("wheels", [])
            result.tools.append(_tool_entry(pack_dir, big_deps_dir, tool, list(wheels)))
            continue

        log(f"[產包] {tool.tool_id}（{entry.reason}）… pip download {len(tool.requires)} 個 requires")
        try:
            tool_entry = build_one(
                tool, packs_dir, big_deps_dir, gateway, target, threshold_bytes, prev_big_deps,
            )
        except Exception as exc:  # noqa: BLE001 — 失敗續行是設計要求
            log(f"[失敗] {tool.tool_id}：{exc}")
            result.failed.append({"tool_id": tool.tool_id, "reason": str(exc)})
            continue
        big_note = f"，其中 {len(tool_entry['big_wheels'])} 個移入 {BIG_DEPS_DIRNAME}\\" \
            if tool_entry["big_wheels"] else ""
        log(f"[完成] {tool.tool_id}：{tool_entry['wheel_count']} 個 wheel，"
            f"{human_size(tool_entry['total_bytes'])}{big_note}")
        result.tools.append(tool_entry)

    _finalize(result, gateway, dest, target, threshold_mb, big_deps_dir, only_tools, launch_mode, log)
    return result


def _finalize(
    result: BuildResult,
    gateway: PlatformGateway,
    dest: Path,
    target: Target,
    threshold_mb: int,
    big_deps_dir: Path,
    only_tools: list[str] | None,
    launch_mode: str,
    log,
) -> None:
    """組 big-deps 清單 → 清孤兒 → 寫 provision.json / REPORT.md / apply.py。"""
    result.big_deps = manifest_mod.collect_big_deps(result.tools, big_deps_dir)

    if only_tools is None:
        referenced = {b["name"] for b in result.big_deps}
        result.pruned = bigdeps.prune_orphans(big_deps_dir, referenced)
        for name in result.pruned:
            log(f"[清理] 移除沒有工具引用的大相依：{name}")
    elif big_deps_dir.is_dir():
        # 有 --tools 篩選時看不到全部引用關係，不敢刪（SPEC §8.1）
        referenced = {b["name"] for b in result.big_deps}
        strays = sorted({p.name for p in big_deps_dir.glob("*.whl")} - referenced)
        if strays:
            log(f"[提示] {BIG_DEPS_DIRNAME}\\ 內有 {len(strays)} 個本次未引用的檔案"
                f"（因為用了 --tools，未自動清理）")

    provision = manifest_mod.build_provision_manifest(
        project_root=gateway.project_root,
        target=target.as_dict(),
        scanned_roots=list(PLUGIN_GLOBS),
        big_threshold_mb=threshold_mb,
        tools=result.tools,
        big_deps=result.big_deps,
        skipped_tools=result.skipped,
        failed_tools=result.failed,
    )
    manifest_mod.write_provision_manifest(provision, dest)
    plan_by_id = {e.tool.tool_id: e.action for e in result.plan}
    (dest / "REPORT.md").write_text(
        report_mod.render_report(provision, plan_by_id, pruned=result.pruned),
        encoding="utf-8",
    )
    _copy_runtime_scripts(dest)
    _write_launcher(dest, gateway.project_root, launch_mode)

    total = sum(t["total_bytes"] for t in result.tools)
    log("")
    log(f"產出：{dest}")
    log(f"  工具 {len(result.tools)} 個、總大小 {human_size(total)}"
        f"、大型相依 {len(result.big_deps)} 個")
    if result.failed:
        log(f"  失敗 {len(result.failed)} 個：{'、'.join(f['tool_id'] for f in result.failed)}")
    log(f"  下一步：讀 {dest / 'REPORT.md'}")
