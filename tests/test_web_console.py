from __future__ import annotations

from pathlib import Path

import pytest

from build_worker import BuildRecordStore
from control_plane.http_api import HttpApi
from control_plane.rollout import RolloutService, RolloutStore
from provision_builder.package_services import FileObjectStore, PackageService, SQLiteRegistry
from web_console import ConsoleApp
from web_console.server import in_process_fetch, in_process_post


def _make_api(tmp_path: Path, *, governance: bool = False, worker: bool = False):
    service = PackageService(SQLiteRegistry(tmp_path / "r.db"), FileObjectStore(tmp_path / "obj"))
    for version in ("1.0.0", "1.1.0"):
        pkg = tmp_path / f"{version}.napp"
        pkg.write_bytes(f"body {version}".encode())
        service.publish("cv-reviewer", version, pkg)
    service.promote("cv-reviewer", "production", "1.0.0")
    service.publish("other-app", "0.1.0", _tmpfile(tmp_path, "o"))
    if not governance:
        return HttpApi(service, tmp_path / "staging"), service
    rollout = RolloutService(RolloutStore(tmp_path / "roll.db"))
    builds = BuildRecordStore(tmp_path / "builds.db")
    builds.record(app_id="cv-reviewer", version="1.1.0", status="succeeded", commit="deadbeef")
    build_worker = None
    src = None
    if worker:
        from build_worker import BuildWorker
        from provision_builder.blob_store import FileBlobStore
        from provision_builder.napp import DevHmacSigner
        src = tmp_path / "sample"
        src.mkdir()
        (src / "app.py").write_text("def main():\n    return 'ok'\n", encoding="utf-8")
        (src / "app.json").write_text('{"id":"cv-reviewer","entrypoint":"app:main","requires":["numpy"]}', encoding="utf-8")
        signer = DevHmacSigner()
        build_worker = BuildWorker(service, FileBlobStore(tmp_path / "blobs"), tmp_path / "jobs",
                                   signer=signer, verifier=signer, records=builds)
    return HttpApi(service, tmp_path / "staging", rollout=rollout, builds=builds,
                   worker=build_worker, default_build_source=str(src) if src else None), service


@pytest.fixture
def console(tmp_path: Path) -> ConsoleApp:
    api, _ = _make_api(tmp_path)
    return ConsoleApp(in_process_fetch(api))


def _tmpfile(tmp_path: Path, body: str) -> Path:
    p = tmp_path / f"pkg-{body}.napp"
    p.write_bytes(body.encode())
    return p


def test_index_lists_applications(console: ConsoleApp) -> None:
    page = console.handle("/")
    assert page.status == 200
    assert "cv-reviewer" in page.html and "other-app" in page.html
    assert "/applications/cv-reviewer" in page.html


def test_application_page_shows_releases_and_channels(console: ConsoleApp) -> None:
    page = console.handle("/applications/cv-reviewer")
    assert page.status == 200
    assert "1.0.0" in page.html and "1.1.0" in page.html
    assert "Channels" in page.html
    # production resolves to 1.0.0; dev/staging show a dash
    assert "production" in page.html
    assert "—" in page.html


def test_yanked_release_is_marked(tmp_path: Path) -> None:
    service = PackageService(SQLiteRegistry(tmp_path / "r.db"), FileObjectStore(tmp_path / "obj"))
    pkg = tmp_path / "p.napp"
    pkg.write_bytes(b"x")
    service.publish("cv-reviewer", "1.0.0", pkg)
    service.yank("cv-reviewer", "1.0.0")
    api = HttpApi(service, tmp_path / "staging")
    page = ConsoleApp(in_process_fetch(api)).handle("/applications/cv-reviewer")
    assert "yanked" in page.html


def test_unknown_page_is_404(console: ConsoleApp) -> None:
    assert console.handle("/nope").status == 404


def test_read_only_console_has_no_action_forms(console: ConsoleApp) -> None:
    page = console.handle("/applications/cv-reviewer")
    assert "Actions" not in page.html  # no post callable → no control surface


def test_control_surface_shows_builds_rollout_and_forms(tmp_path: Path) -> None:
    api, _ = _make_api(tmp_path, governance=True)
    console = ConsoleApp(in_process_fetch(api), in_process_post(api))
    page = console.handle("/applications/cv-reviewer")
    assert "Builds" in page.html and "deadbeef"[:8] in page.html
    assert "Rollout" in page.html
    assert "Actions" in page.html and "Promote" in page.html and "Yank" in page.html


def test_promote_via_console_post(tmp_path: Path) -> None:
    api, service = _make_api(tmp_path, governance=True)
    console = ConsoleApp(in_process_fetch(api), in_process_post(api))
    resp = console.handle_post("/applications/cv-reviewer/promote", {"version": "1.1.0", "channel": "production"})
    assert resp.status == 303 and resp.location == "/applications/cv-reviewer"
    assert service.resolve("cv-reviewer", "production").version == "1.1.0"


def test_yank_via_console_post(tmp_path: Path) -> None:
    api, service = _make_api(tmp_path, governance=True)
    console = ConsoleApp(in_process_fetch(api), in_process_post(api))
    console.handle_post("/applications/cv-reviewer/yank", {"version": "1.0.0"})
    assert service.get_release("cv-reviewer", "1.0.0").status == "yanked"


def test_start_rollout_via_console_post(tmp_path: Path) -> None:
    api, _ = _make_api(tmp_path, governance=True)
    console = ConsoleApp(in_process_fetch(api), in_process_post(api))
    resp = console.handle_post("/applications/cv-reviewer/rollout", {"version": "1.1.0", "stage_percent": "25"})
    assert resp.status == 303
    _, latest = in_process_fetch(api)("/api/v1/applications/cv-reviewer/rollout")
    assert latest["version"] == "1.1.0" and latest["stage_percent"] == 25


def test_read_only_console_rejects_post(console: ConsoleApp) -> None:
    assert console.handle_post("/applications/cv-reviewer/promote", {"version": "1.0.0"}).status == 405


def test_build_and_publish_via_console_post(tmp_path: Path) -> None:
    api, service = _make_api(tmp_path, governance=True, worker=True)
    console = ConsoleApp(in_process_fetch(api), in_process_post(api))
    assert "Build" in console.handle("/applications/cv-reviewer").html
    resp = console.handle_post("/applications/cv-reviewer/build", {"version": "3.0.0", "channel": "production"})
    assert resp.status == 303
    assert service.get_release("cv-reviewer", "3.0.0") is not None
    assert service.resolve("cv-reviewer", "production").version == "3.0.0"


def test_rollout_lifecycle_buttons_via_console(tmp_path: Path) -> None:
    api, _ = _make_api(tmp_path, governance=True)
    console = ConsoleApp(in_process_fetch(api), in_process_post(api))
    console.handle_post("/applications/cv-reviewer/rollout", {"version": "1.1.0", "stage_percent": "10"})
    _, r = in_process_fetch(api)("/api/v1/applications/cv-reviewer/rollout")
    rid = r["rollout_id"]
    # advance to 100 via the console button → completed
    console.handle_post("/applications/cv-reviewer/rollout/advance", {"rollout_id": str(rid), "stage_percent": "100"})
    _, r2 = in_process_fetch(api)("/api/v1/applications/cv-reviewer/rollout")
    assert r2["status"] == "completed"


def test_register_device_via_console(tmp_path: Path) -> None:
    api, _ = _make_api(tmp_path, governance=True)
    console = ConsoleApp(in_process_fetch(api), in_process_post(api))
    console.handle_post("/applications/cv-reviewer/device", {"device_id": "device-99", "group": "canary"})
    _, devices = in_process_fetch(api)("/api/v1/devices")
    assert "device-99" in devices["devices"]
