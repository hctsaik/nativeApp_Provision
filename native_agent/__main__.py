"""Device-side agent CLI against a local lab (shares the lab's registry/blobs).

    py -3.11 -m native_agent --lab .lab --device .device update cv-reviewer production
    py -3.11 -m native_agent --lab .lab --device .device status cv-reviewer
    py -3.11 -m native_agent --lab .lab --device .device rollback cv-reviewer
    py -3.11 -m native_agent --lab .lab --device .device reconcile cv-reviewer
    py -3.11 -m native_agent --lab .lab --device .device gc cv-reviewer

In the lab the "remote" (Control Plane + MinIO) is just the lab's local
registry.db / objects / minio_blobs — the agent code is identical to production.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from native_agent import NativeAgent  # noqa: E402
from provision_builder.blob_store import FileBlobStore  # noqa: E402
from provision_builder.napp import DevHmacSigner  # noqa: E402
from provision_builder.package_services import FileObjectStore, PackageService, SQLiteRegistry  # noqa: E402


def _agent(lab: Path, device: Path) -> NativeAgent:
    service = PackageService(SQLiteRegistry(lab / "registry.db"), FileObjectStore(lab / "objects"))
    blobs = FileBlobStore(lab / "minio_blobs")
    return NativeAgent(device, service, blobs, verifier=DevHmacSigner())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Native_App device update agent (lab)")
    parser.add_argument("--lab", type=Path, default=ROOT / ".lab")
    parser.add_argument("--device", type=Path, default=ROOT / ".device")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("update", "status", "rollback", "reconcile", "gc"):
        p = sub.add_parser(name)
        p.add_argument("app_id")
        if name in ("update",):
            p.add_argument("channel", nargs="?", default="production")
            p.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)

    agent = _agent(args.lab.resolve(), args.device.resolve())
    if args.command == "update":
        outcome = agent.update(args.app_id, args.channel, force=args.force)
        print(json.dumps(outcome.__dict__, ensure_ascii=False, indent=2))
        return 0 if outcome.state in {"UPDATED", "START_ACTIVE", "START_CACHED"} else 1
    if args.command == "status":
        print(json.dumps({
            "active": agent.state.active_version(args.app_id),
            "last_known_good": agent.state.last_known_good(args.app_id),
            "active_json": agent.read_active(args.app_id),
        }, ensure_ascii=False, indent=2))
        return 0
    if args.command == "rollback":
        print(json.dumps(agent.rollback(args.app_id).__dict__, ensure_ascii=False, indent=2))
        return 0
    if args.command == "reconcile":
        print(json.dumps([o.__dict__ for o in agent.reconcile(args.app_id)], ensure_ascii=False, indent=2))
        return 0
    if args.command == "gc":
        print(json.dumps(agent.gc(args.app_id), ensure_ascii=False, indent=2))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
