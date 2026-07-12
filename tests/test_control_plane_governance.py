"""Control Plane governance routes: builds, rollout, devices (Slice 8/2)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from build_worker import BuildRecordStore
from control_plane.http_api import HttpApi
from control_plane.rollout import RolloutService, RolloutStore
from provision_builder.package_services import FileObjectStore, PackageService, SQLiteRegistry


@pytest.fixture
def api(tmp_path: Path) -> HttpApi:
    service = PackageService(SQLiteRegistry(tmp_path / "r.db"), FileObjectStore(tmp_path / "obj"))
    rollout = RolloutService(RolloutStore(tmp_path / "roll.db"))
    builds = BuildRecordStore(tmp_path / "builds.db")
    builds.record(app_id="cv-reviewer", version="1.0.0", status="succeeded", digest="abc")
    return HttpApi(service, tmp_path / "staging", rollout=rollout, builds=builds)


def _post(api, path, payload=None):
    body = json.dumps(payload).encode() if payload is not None else b""
    return api.handle("POST", path, body)


def _get(api, path):
    return api.handle("GET", path)


def _j(resp):
    return json.loads(resp.body.decode())


def test_builds_route(api: HttpApi) -> None:
    resp = _get(api, "/api/v1/applications/cv-reviewer/builds")
    assert resp.status == 200
    assert _j(resp)[0]["status"] == "succeeded"


def test_rollout_lifecycle_over_http(api: HttpApi) -> None:
    start = _post(api, "/api/v1/applications/cv-reviewer/rollouts", {"version": "2.0.0", "stage_percent": 10})
    assert start.status == 201
    rid = _j(start)["rollout_id"]

    advanced = _post(api, f"/api/v1/rollouts/{rid}/advance", {"stage_percent": 100})
    assert advanced.status == 200 and _j(advanced)["status"] == "completed"

    latest = _get(api, "/api/v1/applications/cv-reviewer/rollout")
    assert latest.status == 200 and _j(latest)["version"] == "2.0.0"


def test_desired_for_device_route(api: HttpApi) -> None:
    _post(api, "/api/v1/applications/cv-reviewer/rollouts", {"version": "2.0.0", "stage_percent": 100})
    resp = _get(api, "/api/v1/applications/cv-reviewer/devices/device-1/desired")
    assert resp.status == 200 and _j(resp)["version"] == "2.0.0"


def test_report_can_autopause_over_http(api: HttpApi) -> None:
    rid = _j(_post(api, "/api/v1/applications/cv-reviewer/rollouts", {"version": "2.0.0", "stage_percent": 50}))["rollout_id"]
    for i in range(5):
        resp = _post(api, f"/api/v1/rollouts/{rid}/report", {"device_id": f"d{i}", "success": False})
    assert _j(resp)["status"] == "paused"


def test_device_registration_and_audit(api: HttpApi) -> None:
    assert _post(api, "/api/v1/devices", {"device_id": "d1", "group": "canary"}).status == 201
    assert "d1" in _j(_get(api, "/api/v1/devices"))["devices"]
    assert any(e["action"] == "device.register" for e in _j(_get(api, "/api/v1/audit"))["events"])


def test_malformed_body_is_400(api: HttpApi) -> None:
    resp = _post(api, "/api/v1/applications/cv-reviewer/rollouts", {})  # missing version
    assert resp.status == 400 and _j(resp)["error"]["code"] == "invalid_request"


def test_governance_routes_501_when_disabled(tmp_path: Path) -> None:
    service = PackageService(SQLiteRegistry(tmp_path / "r.db"), FileObjectStore(tmp_path / "obj"))
    api = HttpApi(service, tmp_path / "staging")  # no rollout/builds configured
    assert _get(api, "/api/v1/applications/x/builds").status == 501
    assert _post(api, "/api/v1/applications/x/rollouts", {"version": "1"}).status == 501
