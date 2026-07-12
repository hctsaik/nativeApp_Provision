#!/usr/bin/env python3
"""Exercise the self-contained local Registry/Object Store demo."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from provision_builder.package_errors import PackageDomainError  # noqa: E402
from provision_builder.package_services import FileObjectStore, PackageService, SQLiteRegistry  # noqa: E402


def service(root: Path) -> PackageService:
    return PackageService(SQLiteRegistry(root / "registry.db"), FileObjectStore(root / "objects"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Native App local package-services demo")
    parser.add_argument("--root", type=Path, default=ROOT / ".local-services")
    commands = parser.add_subparsers(dest="command", required=True)
    publish = commands.add_parser("publish")
    publish.add_argument("app_id"); publish.add_argument("version"); publish.add_argument("package", type=Path)
    promote = commands.add_parser("promote")
    promote.add_argument("app_id"); promote.add_argument("version"); promote.add_argument("--channel", default="production")
    resolve = commands.add_parser("resolve")
    resolve.add_argument("app_id"); resolve.add_argument("--channel", default="production")
    yank = commands.add_parser("yank")
    yank.add_argument("app_id"); yank.add_argument("version")
    download = commands.add_parser("download")
    download.add_argument("app_id"); download.add_argument("destination", type=Path); download.add_argument("--channel", default="production")
    listing = commands.add_parser("list")
    listing.add_argument("app_id")
    args = parser.parse_args(argv)
    app = service(args.root.resolve())
    try:
        if args.command == "publish": result = app.publish(args.app_id, args.version, args.package)
        elif args.command == "promote": result = app.promote(args.app_id, args.channel, args.version)
        elif args.command == "yank": result = app.yank(args.app_id, args.version)
        elif args.command == "resolve": result = app.resolve(args.app_id, args.channel)
        elif args.command == "download": result = app.download(args.app_id, args.channel, args.destination)
        else: result = app.list_releases(args.app_id)
    except PackageDomainError as exc:
        print(f"ERROR[{exc.code}]: {exc}", file=sys.stderr)
        return 1
    except (OSError, ValueError, KeyError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    if result is None:
        print("null")
        return 1
    payload = [asdict(item) for item in result] if isinstance(result, list) else asdict(result)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

