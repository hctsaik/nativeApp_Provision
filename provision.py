#!/usr/bin/env python3
"""provision — 離線補給包產生器 CLI（在**有網路的開發機**執行）。

    py -3.11 provision.py build  <平台專案根> [--dest DIR] [--tools a,b] [--big-threshold-mb N] ...
    py -3.11 provision.py verify <補給包目錄>
    py -3.11 provision.py apply  <補給包目錄> --deppack-cache <目標>

`apply` 只是轉呼叫補給包內自足的 `apply.py`（離線機上直接跑那一支就好，
不需要本 repo）。用 subprocess 呼叫而非 import，是為了持續驗證它真的自足（SPEC D8）。

規格：SPEC.md
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from provision_builder import DEFAULT_BIG_THRESHOLD_MB, __version__  # noqa: E402
from provision_builder._util import guard_console_encoding  # noqa: E402
from provision_builder.bigdeps import BigDepConflict  # noqa: E402
from provision_builder.build import run_build  # noqa: E402
from provision_builder.gateway import GatewayError, Target  # noqa: E402
from provision_builder.scan import ScanError  # noqa: E402
from provision_builder.verify import format_verdict, verify_provision  # noqa: E402

DEFAULT_DEST = Path("dist") / "provision"


def _add_target_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--platform", dest="platform_tag", default="win_amd64",
                        help="目標平台標籤（預設 win_amd64）")
    parser.add_argument("--python-version", default="3.11",
                        help="目標 Python 版本（預設 3.11）")
    parser.add_argument("--abi", default="cp311", help="目標 ABI（預設 cp311）")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="provision",
        description="離線補給包產生器：掃描 CIM 平台專案的 plugin.yaml requires，"
                    "預先下載 wheel 供無網路電腦安裝",
    )
    parser.add_argument("--version", action="version", version=f"native_Provision {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    build = sub.add_parser("build", help="在連網機產補給包")
    build.add_argument("project_root", help="CIM 平台專案根（含 sidecar\\python-engine）")
    build.add_argument("--dest", default=str(DEFAULT_DEST), help=f"產出目錄（預設 {DEFAULT_DEST}）")
    build.add_argument("--tools", default=None, help="只包這些工具（逗號分隔）")
    build.add_argument("--big-threshold-mb", type=int, default=DEFAULT_BIG_THRESHOLD_MB,
                       help=f"大相依隔離門檻 MB（預設 {DEFAULT_BIG_THRESHOLD_MB}；0=關閉）")
    build.add_argument("--force", action="store_true", help="忽略增量指紋，全部重產")
    build.add_argument("--dry-run", action="store_true", help="只掃描並印計畫，不下載")
    build.add_argument("--python", dest="python_cmd", default=None,
                       help="pip download / 讀 YAML 用的直譯器（預設 = 跑本工具的那一個）")
    build.add_argument("--launch-mode", choices=("portable", "dev"), default="portable",
                       help="產出的 run-platform.bat 預設模式（portable=離線機／dev=本機測試）")
    _add_target_args(build)

    verify = sub.add_parser("verify", help="驗證補給包完整性（搬運後、apply 前）")
    verify.add_argument("provision_dir", help="補給包目錄")

    apply_cmd = sub.add_parser("apply", help="套用補給包（轉呼叫包內的 apply.py）")
    apply_cmd.add_argument("provision_dir", help="補給包目錄")
    apply_cmd.add_argument("--deppack-cache", required=True, help="目標（平台的 CIM_DEPPACK_CACHE）")
    apply_cmd.add_argument("--tools", default=None, help="只套用這些工具（逗號分隔）")
    apply_cmd.add_argument("--dry-run", action="store_true", help="只檢查，不寫入")

    return parser


def _cmd_build(args: argparse.Namespace) -> int:
    target = Target(
        platform_tag=args.platform_tag,
        python_version=args.python_version,
        abi=args.abi,
    )
    python_cmd = args.python_cmd.split() if args.python_cmd else None
    only_tools = [t.strip() for t in args.tools.split(",") if t.strip()] if args.tools else None

    result = run_build(
        Path(args.project_root),
        Path(args.dest),
        target=target,
        threshold_mb=args.big_threshold_mb,
        only_tools=only_tools,
        force=args.force,
        dry_run=args.dry_run,
        python_cmd=python_cmd,
        launch_mode=args.launch_mode,
    )
    return 0 if result.ok else 1


def _cmd_verify(args: argparse.Namespace) -> int:
    verdict = verify_provision(Path(args.provision_dir))
    print(f"補給包：{verdict.root}")
    print("")
    print(format_verdict(verdict))
    return 0 if verdict.ok else 1


def _cmd_apply(args: argparse.Namespace) -> int:
    script = Path(args.provision_dir).resolve() / "apply.py"
    if not script.is_file():
        print(f"[錯誤] 找不到 {script}——這個目錄不是補給包（或 build 未完成）。")
        return 2
    cmd = [sys.executable, str(script), "--deppack-cache", args.deppack_cache]
    if args.tools:
        cmd += ["--tools", args.tools]
    if args.dry_run:
        cmd.append("--dry-run")
    return subprocess.call(cmd)


def main(argv: list[str] | None = None) -> int:
    guard_console_encoding()
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "build":
            return _cmd_build(args)
        if args.command == "verify":
            return _cmd_verify(args)
        if args.command == "apply":
            return _cmd_apply(args)
    except (GatewayError, ScanError, BigDepConflict) as exc:
        print(f"[錯誤] {exc}")
        return 2
    except KeyboardInterrupt:
        print("\n[中斷] 使用者取消。產出目錄可能不完整，請重跑 build。")
        return 130
    return 2  # pragma: no cover - argparse required=True 已擋


if __name__ == "__main__":
    raise SystemExit(main())
