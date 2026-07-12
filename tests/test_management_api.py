"""UI-1 device-local /management API: routes, async 202, RBAC, 409, cancel."""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from native_agent import ApplicationManagementService, ManagementApi, NativeAgent, OperationRunner
from provision_builder.blob_store import FileBlobStore
from provision_builder.napp import AppManifest, DevHmacSigner, build_napp
from provision_builder.package_services import FileObjectStore, PackageService, SQLiteRegistry

SIGNER = DevHmacSigner()


def publish(remote, tmp_path: Path, version: str, *, app: str = "cv-reviewer") -> None:
    src = tmp_path / f"src-{app}-{version}"
    src.mkdir(exist_ok=True)
    (src / "app.py").write_text(f"# {app} {version}\n", encoding="utf-8")
    manifest = AppManifest.from_dict({"id": app, "version": version, "entrypoint": "app:main"})
    out = tmp_path / f"{app}-{version}.napp"
    build_napp(manifest, src, out, signer=SIGNER)
    remote.publish(app, version, out)
    remote.promote(app, "production", version)


def _api(tmp_path: Path, **agent_kw) -> ManagementApi:
    remote = PackageService(SQLiteRegistry(tmp_path / "r.db"), FileObjectStore(tmp_path / "obj"))
    blobs = FileBlobStore(tmp_path / "remote_blobs")
    agent = NativeAgent(tmp_path / "device", remote, blobs, verifier=SIGNER, **agent_kw)
    return ManagementApi(ApplicationManagementService(agent, OperationRunner(agent), "production"))


def _get(api, path, role="user"):
    return api.handle("GET", path, b"", {"X-Role": role})


def _post(api, path, payload=None, role="user"):
    body = json.dumps(payload).encode() if payload is not None else b""
    return api.handle("POST", path, body, {"X-Role": role})


def _j(resp):
    return json.loads(resp.body.decode("utf-8"))


@pytest.fixture
def api(tmp_path: Path) -> ManagementApi:
    a = _api(tmp_path)
    publish(a.service.agent.remote, tmp_path, "1.0.0")
    return a


def test_list_and_detail(api: ManagementApi) -> None:
    listing = _get(api, "/management/applications")
    assert listing.status == 200
    assert any(v["app_id"] == "cv-reviewer" and v["update_state"] == "NOT_INSTALLED" for v in _j(listing))
    detail = _get(api, "/management/applications/cv-reviewer")
    assert detail.status == 200 and _j(detail)["can_install"] is True


def test_update_202_then_operation_completed(api: ManagementApi) -> None:
    started = _post(api, "/management/applications/cv-reviewer/update")
    assert started.status == 202
    op_id = _j(started)["operation_id"]
    api.service.runner.wait(op_id, timeout=10)

    op = _get(api, f"/management/operations/{op_id}")
    assert op.status == 200 and _j(op)["status"] == "succeeded" and _j(op)["stage"] == "COMPLETED"
    events = _get(api, f"/management/operations/{op_id}/events")
    assert events.status == 200 and len(_j(events)["events"]) >= 3


def test_install_alias_202(api: ManagementApi) -> None:
    assert _post(api, "/management/applications/cv-reviewer/install").status == 202


def test_update_when_up_to_date_returns_200(api: ManagementApi) -> None:
    op_id = _j(_post(api, "/management/applications/cv-reviewer/update"))["operation_id"]
    api.service.runner.wait(op_id, timeout=10)
    again = _post(api, "/management/applications/cv-reviewer/update")
    assert again.status == 200 and _j(again)["state"] == "START_ACTIVE"


def test_rollback_requires_admin(api: ManagementApi) -> None:
    op_id = _j(_post(api, "/management/applications/cv-reviewer/update"))["operation_id"]
    api.service.runner.wait(op_id, timeout=10)
    assert _post(api, "/management/applications/cv-reviewer/rollback", role="user").status == 403
    assert _j(_post(api, "/management/applications/cv-reviewer/rollback", role="user"))["error"]["code"] == "forbidden"
    assert _post(api, "/management/applications/cv-reviewer/rollback", role="admin").status == 200


def test_gc_and_reconcile_require_admin(api: ManagementApi) -> None:
    assert _post(api, "/management/applications/cv-reviewer/gc", role="user").status == 403
    assert _post(api, "/management/applications/cv-reviewer/reconcile", role="user").status == 403
    assert _post(api, "/management/applications/cv-reviewer/reconcile", role="admin").status == 200


def test_second_update_returns_409(tmp_path: Path) -> None:
    gate = threading.Event()
    api = _api(tmp_path, ensure_venv=lambda fp, d: gate.wait(10))
    publish(api.service.agent.remote, tmp_path, "1.0.0")
    first = _post(api, "/management/applications/cv-reviewer/update")
    assert first.status == 202
    op_id = _j(first)["operation_id"]
    try:
        conflict = _post(api, "/management/applications/cv-reviewer/update")
        assert conflict.status == 409 and _j(conflict)["error"]["code"] == "operation_in_progress"
        gc = _post(api, "/management/applications/cv-reviewer/gc", role="admin")
        assert gc.status == 409
    finally:
        gate.set()
        api.service.runner.wait(op_id, timeout=10)


def test_cancel_endpoint_returns_202(tmp_path: Path) -> None:
    gate = threading.Event()
    api = _api(tmp_path, ensure_venv=lambda fp, d: gate.wait(10))
    publish(api.service.agent.remote, tmp_path, "1.0.0")
    op_id = _j(_post(api, "/management/applications/cv-reviewer/update"))["operation_id"]
    try:
        resp = _post(api, f"/management/operations/{op_id}/cancel")
        assert resp.status == 202 and _j(resp)["cancel_requested"] is True
    finally:
        gate.set()
        api.service.runner.wait(op_id, timeout=10)


def test_unknown_operation_404(api: ManagementApi) -> None:
    assert _get(api, "/management/operations/999999").status == 404


def test_invalid_app_id_400(api: ManagementApi) -> None:
    resp = _get(api, "/management/applications/bad id")
    assert resp.status == 400 and _j(resp)["error"]["code"] == "invalid_identifier"


def test_unknown_route_404(api: ManagementApi) -> None:
    assert _get(api, "/management/nope").status == 404
