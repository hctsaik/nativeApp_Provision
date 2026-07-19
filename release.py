#!/usr/bin/env python3
"""release — 唯一的正式交付產生器 CLI（P0 Release Pipeline）。

    py -3.11 release.py build   --out DIR --napp X.napp [--napp Y.napp ...]
                                [--blobs DIR] [--setup EXE] [--extra name=DIR]
                                [--channel internal] [--release-id ID]
                                [--trust-store store.json] [--trust key_id:secret]
    py -3.11 release.py verify  <release目錄> [--trust-store store.json] [--trust k:v]
    py -3.11 release.py promote <release目錄> --to-channel production --out DIR
                                [--trust-store store.json] [--release-id ID]
    py -3.11 release.py keygen  --key-id team-a --out team-a.private.json
                                [--trust-store store.json]
    py -3.11 release.py sign    X.napp --key team-a.private.json

原則（docs/NATIVEAPP_DEPLOYMENT_RECOMMENDATION.md §6–§7 P0/P3）：
- 交付物只來自本命令的輸出；`dist\\` 與任何工作區都不是交付來源。
- 每次 build 用全新輸出目錄，拒絕就地增補；promote = 同一批 bytes 換通道全程重驗。
- 防誤包 gate 拒絕 `_run`、`__pycache__`、WebView profile 等開發殘留。
- `production` channel 拒絕未簽章／驗不過章的 artifact。
- 簽章：Ed25519（純 Python RFC 8032，見 docs/adr/0001）。`--trust-store` 給裝置信任的
  公鑰清單；`--trust key_id:secret` 是 dev HMAC，**只供測試**，不進 trust store。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from provision_builder._util import guard_console_encoding  # noqa: E402
from provision_builder.napp.errors import InvalidManifest, SignatureInvalid  # noqa: E402
from provision_builder.napp.signing import DevHmacSigner, MultiKeyVerifier  # noqa: E402
from provision_builder.napp.trust import (  # noqa: E402
    TrustStoreError,
    add_to_trust_store,
    generate_keypair,
    load_private_key,
    load_trust_store,
    sign_napp,
    trust_entry,
)
from provision_builder.release_pipeline import (  # noqa: E402
    ReleaseError,
    build_release,
    promote_release,
    verify_release,
)


def _parse_extra(value: str) -> tuple[str, Path]:
    name, sep, path = value.partition("=")
    if not sep or not name or not path:
        raise argparse.ArgumentTypeError(f"--extra 格式是 name=DIR：{value!r}")
    return name, Path(path)


def _build_verifier(args) -> MultiKeyVerifier | None:
    """Combine --trust-store（Ed25519 公鑰）與 --trust（dev HMAC，僅測試）。"""
    dev_keys: dict = {}
    for value in getattr(args, "trust", None) or []:
        key_id, sep, secret = value.partition(":")
        if not sep or not key_id or not secret:
            raise SystemExit(f"[FAIL] --trust 格式是 key_id:secret：{value!r}")
        dev_keys[key_id] = DevHmacSigner(secret.encode("utf-8"), key_id)
    store_path = getattr(args, "trust_store", None)
    if store_path is not None:
        return load_trust_store(store_path, extra=dev_keys)
    return MultiKeyVerifier(dev_keys) if dev_keys else None


def _add_trust_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--trust-store", type=Path, default=None,
                        help="裝置信任的公鑰清單（Ed25519 trust store JSON）")
    parser.add_argument("--trust", action="append", default=None, metavar="key_id:secret",
                        help="dev HMAC 金鑰（僅測試；可重複）")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="release",
        description="組裝可出貨的 release 目錄（唯一交付來源），或驗證一份 release 是否可出貨",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    build = sub.add_parser("build", help="從明確指定的輸入組一份全新 release")
    build.add_argument("--out", required=True, type=Path,
                       help="輸出父目錄（release 會建立在 <out>/<release-id>/，該目錄必須不存在）")
    build.add_argument("--napp", action="append", required=True, type=Path,
                       metavar="X.napp", help="要出貨的 .napp（可重複）")
    build.add_argument("--blobs", type=Path, default=None,
                       help="blob store 根（含 sha256/ 子目錄）；artifact 有 blob 引用時必填")
    build.add_argument("--setup", type=Path, default=None,
                       help="平台安裝檔（在非 WDAC 機器產的簽章 exe）")
    build.add_argument("--extra", action="append", type=_parse_extra, default=[],
                       metavar="name=DIR", help="額外 payload 目錄（會被防誤包 gate 掃描）")
    build.add_argument("--channel", default="internal",
                       help="通道名（預設 internal；production 會強制驗章）")
    build.add_argument("--release-id", default=None,
                       help="release 目錄名（預設 <channel>-<UTC時間戳>）")
    _add_trust_args(build)

    verify = sub.add_parser("verify", help="驗證一份 release 目錄是否可出貨（搬運後、交付前）")
    verify.add_argument("release_dir", type=Path)
    _add_trust_args(verify)

    promote = sub.add_parser("promote", help="把既有 release 晉升到更嚴的通道（全程重驗，production 強制驗章）")
    promote.add_argument("release_dir", type=Path)
    promote.add_argument("--to-channel", required=True, help="目標通道（internal → pilot → production）")
    promote.add_argument("--out", required=True, type=Path, help="輸出父目錄")
    promote.add_argument("--release-id", default=None)
    _add_trust_args(promote)

    keygen = sub.add_parser("keygen", help="產生 Ed25519 publisher 金鑰對（私鑰檔勿進 repo/包/裝置）")
    keygen.add_argument("--key-id", required=True)
    keygen.add_argument("--out", required=True, type=Path, help="私鑰檔輸出路徑（拒絕覆蓋）")
    keygen.add_argument("--trust-store", type=Path, default=None,
                        help="順便把公鑰加進這個 trust store（不存在則建立）")

    sign = sub.add_parser("sign", help="對已建好的 .napp 補上 detached 簽章（不動 payload）")
    sign.add_argument("napp", type=Path)
    sign.add_argument("--key", required=True, type=Path, help="keygen 產的私鑰檔")

    pack_platform = sub.add_parser(
        "pack-platform",
        help="把 CIM 平台專案打包成 cim-platform .napp（B-2；殼以 blob 旅行）")
    pack_platform.add_argument("platform_root", type=Path, help="nativeApp 專案根")
    pack_platform.add_argument("--version", required=True, help="平台版本號（如 1.0.0）")
    pack_platform.add_argument("--out", required=True, type=Path, help=".napp 輸出路徑")
    pack_platform.add_argument("--blobs", required=True, type=Path,
                               help="blob store 根（Tauri 殼會內容定址存進去）")
    pack_platform.add_argument("--shell", type=Path, default=None,
                               help="覆蓋殼路徑（預設 apps\\host-tauri\\prebuilt\\cim-light.exe）")
    pack_platform.add_argument("--key", type=Path, default=None, help="順便用私鑰簽章")

    sign_version = sub.add_parser(
        "sign-version",
        help="對 Streamlit Store 的版本槽補發行者簽章（P3.2 Store 通道；簽 files.json 的 canonical digest）")
    sign_version.add_argument("version_dir", type=Path,
                              help="store 樹裡的 versions/<版本>/ 目錄（須含 files.json）")
    sign_version.add_argument("--key", required=True, type=Path, help="keygen 產的私鑰檔")
    return parser


def _cmd_build(args) -> int:
    result = build_release(
        args.out,
        args.napp,
        channel=args.channel,
        release_id=args.release_id,
        blob_root=args.blobs,
        setup_exe=args.setup,
        extras=dict(args.extra),
        verifier=_build_verifier(args),
    )
    totals = result.manifest["totals"]
    print(f"OK: release 已建立 {result.path}")
    print(f"    channel={result.channel}  artifacts={len(result.manifest['artifacts'])}  "
          f"payload={totals['files']} files")
    print(f"    出貨前最後一步：py -3.11 release.py verify \"{result.path}\"")
    return 0


def _cmd_verify(args) -> int:
    problems = verify_release(args.release_dir, verifier=_build_verifier(args))
    if problems:
        for problem in problems:
            print(f"[FAIL] {problem}")
        print(f"共 {len(problems)} 個問題；此目錄**不可**出貨。")
        return 1
    print("OK: release 完整、與 manifest 一致，可出貨。")
    return 0


def _cmd_promote(args) -> int:
    result = promote_release(
        args.release_dir,
        args.out,
        to_channel=args.to_channel,
        release_id=args.release_id,
        verifier=_build_verifier(args),
    )
    print(f"OK: 已晉升為 {result.channel} → {result.path}")
    print(f"    來源：{result.manifest['promoted_from']}")
    return 0


def _cmd_keygen(args) -> int:
    if args.out.exists():
        print(f"[FAIL] 私鑰檔已存在，拒絕覆蓋：{args.out}")
        return 2
    doc = generate_keypair(args.key_id)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"OK: 私鑰已寫入 {args.out}（保管於 secret store，勿進 repo/包/裝置）")
    entry = {"key_id": doc["key_id"], "algorithm": doc["algorithm"],
             "public_key": doc["public_key"], "retired": False}
    if args.trust_store is not None:
        add_to_trust_store(args.trust_store, entry)
        print(f"OK: 公鑰已加入 trust store {args.trust_store}")
    else:
        print("trust store 項目（自行加入裝置信任清單）：")
        print(json.dumps(entry, ensure_ascii=False, indent=2))
    return 0


def _cmd_sign(args) -> int:
    signer = load_private_key(args.key)
    bundle = sign_napp(args.napp, signer)
    print(f"OK: {args.napp.name} 已簽章（key_id={bundle.key_id}, algorithm={bundle.algorithm}）")
    return 0


def _cmd_pack_platform(args) -> int:
    from provision_builder.blob_store import FileBlobStore
    from provision_builder.platform_store import PlatformPackError, build_platform_napp

    signer = load_private_key(args.key) if args.key else None
    try:
        result = build_platform_napp(
            args.platform_root, args.version, args.out,
            blob_store=FileBlobStore(args.blobs), shell_exe=args.shell, signer=signer)
    except PlatformPackError as exc:
        print(f"[FAIL] {exc}")
        return 2
    print(f"OK: {result.path}（{result.package['artifact']['source_files']} 個檔案，"
          f"殼 blob {result.blob_references[0]['sha256'][:16]}…）")
    print("    下一步：py -3.11 release.py build --out <DIR> --napp "
          f"\"{result.path}\" --blobs \"{args.blobs}\"")
    return 0


def _cmd_sign_version(args) -> int:
    # store_builder 依賴多，僅在用到時載入，保持其它子命令輕量。
    from provision_builder.streamlit_desktop.store_builder import StoreBuildError, sign_version_dir

    signer = load_private_key(args.key)
    try:
        bundle = sign_version_dir(args.version_dir, signer)
    except StoreBuildError as exc:
        print(f"[FAIL] {exc}")
        return 2
    print(f"OK: {args.version_dir} 已簽發行者簽章（key_id={bundle['key_id']}）")
    print("    裝置端啟用驗證：把 trust store 放到 apps\\<app>\\trusted_publishers.json；"
          "config.json 設 \"require_signed_updates\": true 可強制。")
    return 0


def main(argv: list[str] | None = None) -> int:
    guard_console_encoding()
    args = _build_parser().parse_args(argv)
    handlers = {"build": _cmd_build, "verify": _cmd_verify, "promote": _cmd_promote,
                "keygen": _cmd_keygen, "sign": _cmd_sign, "sign-version": _cmd_sign_version,
                "pack-platform": _cmd_pack_platform}
    try:
        return handlers[args.command](args)
    except (ReleaseError, TrustStoreError, SignatureInvalid, InvalidManifest) as exc:
        print(f"[FAIL] {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
