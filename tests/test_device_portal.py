from __future__ import annotations

from pathlib import Path

import pytest

from native_agent import NativeAgent, PortalApp
from provision_builder.blob_store import FileBlobStore
from provision_builder.napp import AppManifest, DevHmacSigner, build_napp
from provision_builder.package_services import FileObjectStore, PackageService, SQLiteRegistry

SIGNER = DevHmacSigner()


def _publish(service, blobs, tmp_path: Path, version: str) -> None:
    src = tmp_path / f"src-{version}"
    src.mkdir(exist_ok=True)
    (src / "app.py").write_text(f"# {version}\n", encoding="utf-8")
    manifest = AppManifest.from_dict({"id": "cv-reviewer", "version": version, "entrypoint": "app:main"})
    out = tmp_path / f"{version}.napp"
    build_napp(manifest, src, out, signer=SIGNER)
    service.publish("cv-reviewer", version, out)
    service.promote("cv-reviewer", "production", version)


@pytest.fixture
def portal(tmp_path: Path) -> PortalApp:
    service = PackageService(SQLiteRegistry(tmp_path / "r.db"), FileObjectStore(tmp_path / "obj"))
    blobs = FileBlobStore(tmp_path / "remote_blobs")
    _publish(service, blobs, tmp_path, "1.0.0")
    agent = NativeAgent(tmp_path / "device", service, blobs, verifier=SIGNER)
    return PortalApp(agent, "cv-reviewer", "production")


def test_portal_shows_update_available_then_installs(portal: PortalApp) -> None:
    page = portal.handle("/")
    assert page.status == 200
    assert "有可用更新" in page.html and "1.0.0" in page.html

    done = portal.handle_post("/update", {})
    assert done.status == 303 and done.location == "/"

    after = portal.handle("/")
    assert "已更新" in after.html
    assert portal.agent.state.active_version("cv-reviewer") == "1.0.0"


def test_portal_up_to_date_after_install(portal: PortalApp) -> None:
    portal.handle_post("/update", {})
    portal._last = None  # clear the flash to see the steady state
    page = portal.handle("/")
    assert "已是最新版本" in page.html


def test_portal_rollback_reconcile_gc_return_redirect(portal: PortalApp) -> None:
    portal.handle_post("/update", {})
    for action in ("/rollback", "/reconcile", "/gc"):
        assert portal.handle_post(action, {}).status == 303


def test_portal_unknown_path_404(portal: PortalApp) -> None:
    assert portal.handle("/nope").status == 404
