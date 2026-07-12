"""One-command lab: build + publish a sample app, then serve the Control Plane
and the Web Console so you can click through the whole flow.

    py -3.11 demo/lab_serve.py

Then open:
    Web Console:   http://127.0.0.1:8090/
    Control Plane: http://127.0.0.1:8080/api/v1/applications

Stdlib only — no third-party packages, no docker. State lives under .lab/ and is
safe to delete. Re-running is idempotent (already-published versions are skipped).
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from build_worker import BuildRecordStore, BuildRequest, BuildWorker  # noqa: E402
from control_plane.http_api import HttpApi  # noqa: E402
from control_plane.rollout import RolloutService, RolloutStore  # noqa: E402
from control_plane.server import make_server  # noqa: E402
from native_agent import (  # noqa: E402
    ApplicationManagementService,
    ManagementApi,
    NativeAgent,
    OperationRunner,
    PortalApp,
    make_portal_server,
)
from provision_builder.blob_store import FileBlobStore  # noqa: E402
from provision_builder.napp import DevHmacSigner  # noqa: E402
from provision_builder.package_services import FileObjectStore, PackageService, SQLiteRegistry  # noqa: E402
from web_console import ConsoleApp  # noqa: E402
from web_console.server import in_process_fetch, in_process_post  # noqa: E402
from web_console.server import make_server as make_console_server  # noqa: E402

APP_ID = "cv-reviewer"
SIGNER = DevHmacSigner()  # dev-only symmetric signer (see docs/adr/0001-package-signing.md)


def build_services(root: Path) -> dict:
    service = PackageService(SQLiteRegistry(root / "registry.db"), FileObjectStore(root / "objects"))
    blobs = FileBlobStore(root / "minio_blobs")
    rollout = RolloutService(RolloutStore(root / "rollout.db"))
    builds = BuildRecordStore(root / "builds.db")
    worker = BuildWorker(service, blobs, root / "jobs", signer=SIGNER, verifier=SIGNER, records=builds)
    return {"service": service, "blobs": blobs, "rollout": rollout, "builds": builds, "worker": worker}


def seed(services: dict, root: Path) -> None:
    service: PackageService = services["service"]
    worker: BuildWorker = services["worker"]
    rollout: RolloutService = services["rollout"]

    src = root / "sample_app"
    src.mkdir(parents=True, exist_ok=True)
    # app.json travels with the source so the "Build" button in the Console can
    # build a new version without the operator retyping the manifest.
    (src / "app.json").write_text(
        '{"id": "cv-reviewer", "entrypoint": "app:main", "requires": ["numpy==1.26.0"]}', encoding="utf-8"
    )
    for version, body in (("1.0.0", "v1"), ("1.1.0", "v1.1 — source-only change")):
        (src / "app.py").write_text(f"def main():\n    return 'cv-reviewer {version} ({body})'\n", encoding="utf-8")
        if service.get_release(APP_ID, version) is not None:
            continue  # already published on a previous run
        manifest = {"id": APP_ID, "version": version, "entrypoint": "app:main", "requires": ["numpy==1.26.0"]}
        result = worker.run(BuildRequest(APP_ID, version, src, manifest, channel="production" if version == "1.0.0" else None,
                                         source_commit=f"demo-{version}"))
        print(f"  built {APP_ID}@{version}: {result.status}")

    for device in ("device-lab-01", "device-lab-02", "device-lab-03"):
        rollout.register_device(device, "canary")


def build_lab(root: Path, cp_port: int = 8080, web_port: int = 8090, portal_port: int = 8091):
    root.mkdir(parents=True, exist_ok=True)
    services = build_services(root)
    seed(services, root)
    api = HttpApi(services["service"], root / "staging", rollout=services["rollout"],
                  builds=services["builds"], worker=services["worker"],
                  default_build_source=str(root / "sample_app"))
    console = ConsoleApp(in_process_fetch(api), in_process_post(api))
    # A local "device" so the Portal GUI can update/rollback against the same lab.
    agent = NativeAgent(root / "device", services["service"], services["blobs"], verifier=SIGNER)
    portal = PortalApp(agent, APP_ID, "production")
    # Device-local /management API (what the Native App would iframe), served on
    # the same port as the diagnostic Portal.
    catalog = {
        "app-ai4bi": {"display_name": "AI4BI", "category": "app", "enabled": True},
        "app-lv": {"display_name": "Large Vision", "category": "app", "enabled": True},
        "sheet-annotation": {"display_name": "Annotation", "category": "sheet", "enabled": True},
    }
    mgmt = ManagementApi(ApplicationManagementService(agent, OperationRunner(agent), "production", catalog=catalog))
    cp_server = make_server(api, port=cp_port)
    web_server = make_console_server(console, port=web_port)
    portal_server = make_portal_server(portal, port=portal_port, management_api=mgmt)
    return api, console, portal, cp_server, web_server, portal_server


def main() -> int:
    root = ROOT / ".lab"
    print(f"lab state: {root}")
    _api, _console, _portal, cp_server, web_server, portal_server = build_lab(root)
    for server in (cp_server, web_server, portal_server):
        threading.Thread(target=server.serve_forever, daemon=True).start()
    print("\n  Web Console (operator) →  http://127.0.0.1:8090/")
    print("  Device Portal (device) →  http://127.0.0.1:8091/")
    print("  Control Plane API      →  http://127.0.0.1:8080/api/v1/applications")
    print("\nPress Ctrl+C to stop.\n")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("stopping…")
    finally:
        for server in (cp_server, web_server, portal_server):
            server.shutdown(); server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
