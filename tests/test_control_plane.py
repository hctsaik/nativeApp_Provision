from __future__ import annotations

import json
import threading
import urllib.request
from pathlib import Path

import pytest

from control_plane.http_api import STATUS_BY_CODE, HttpApi
from control_plane.server import make_server
from provision_builder.package_services import FileObjectStore, PackageService, SQLiteRegistry


@pytest.fixture
def api(tmp_path: Path) -> HttpApi:
    service = PackageService(SQLiteRegistry(tmp_path / "r.db"), FileObjectStore(tmp_path / "obj"))
    return HttpApi(service, tmp_path / "staging")


def _post(api: HttpApi, path: str, payload: object = None, raw: bytes | None = None):
    body = raw if raw is not None else (json.dumps(payload).encode() if payload is not None else b"")
    return api.handle("POST", path, body)


def _get(api: HttpApi, path: str):
    return api.handle("GET", path)


def _json(resp) -> dict:
    return json.loads(resp.body.decode("utf-8"))


def test_http_closed_loop_matches_cli(api: HttpApi) -> None:
    pub = _post(api, "/api/v1/applications/cv-reviewer/releases/1.0.0", raw=b"the package bytes")
    assert pub.status == 201
    assert _json(pub)["status"] == "published"

    promoted = _post(api, "/api/v1/applications/cv-reviewer/releases/1.0.0/promote", {"channel": "production"})
    assert promoted.status == 200

    resolved = _get(api, "/api/v1/applications/cv-reviewer/channels/production")
    assert resolved.status == 200 and _json(resolved)["version"] == "1.0.0"

    url = _post(api, "/api/v1/artifacts/cv-reviewer/1.0.0/download-url")
    assert url.status == 200 and _json(url)["url"] == "/api/v1/artifacts/cv-reviewer/1.0.0"

    got = _get(api, "/api/v1/artifacts/cv-reviewer/1.0.0")
    assert got.status == 200 and got.body == b"the package bytes"


def test_duplicate_version_returns_409(api: HttpApi) -> None:
    _post(api, "/api/v1/applications/cv-reviewer/releases/1.0.0", raw=b"a")
    dup = _post(api, "/api/v1/applications/cv-reviewer/releases/1.0.0", raw=b"a")
    assert dup.status == 409 and _json(dup)["error"]["code"] == "duplicate_version"


def test_invalid_identifier_returns_400(api: HttpApi) -> None:
    resp = _post(api, "/api/v1/applications/bad%20id/releases/1.0.0", raw=b"a")
    # %20 is not decoded by our router, but the literal 'bad id' would be; here the
    # raw path segment 'bad%20id' still fails the identifier regex.
    assert resp.status == 400 and _json(resp)["error"]["code"] == "invalid_identifier"


def test_unknown_channel_returns_404(api: HttpApi) -> None:
    _post(api, "/api/v1/applications/cv-reviewer/releases/1.0.0", raw=b"a")
    resp = _get(api, "/api/v1/applications/cv-reviewer/channels/production")
    assert resp.status == 404 and _json(resp)["error"]["code"] == "unknown_channel"


def test_yanked_release_download_url_returns_410(api: HttpApi) -> None:
    _post(api, "/api/v1/applications/cv-reviewer/releases/1.0.0", raw=b"a")
    _post(api, "/api/v1/applications/cv-reviewer/releases/1.0.0/yank")
    resp = _post(api, "/api/v1/artifacts/cv-reviewer/1.0.0/download-url")
    assert resp.status == 410 and _json(resp)["error"]["code"] == "release_yanked"


def test_download_url_missing_release_returns_409(api: HttpApi) -> None:
    resp = _post(api, "/api/v1/artifacts/cv-reviewer/9.9.9/download-url")
    assert resp.status == 409 and _json(resp)["error"]["code"] == "release_not_published"


def test_unknown_route_is_404(api: HttpApi) -> None:
    assert api.handle("GET", "/nope").status == 404


def test_download_missing_object_returns_404(api: HttpApi) -> None:
    _post(api, "/api/v1/applications/cv-reviewer/releases/1.0.0", raw=b"bytes")
    _post(api, "/api/v1/applications/cv-reviewer/releases/1.0.0/promote", {"channel": "production"})
    release = api.service.get_release("cv-reviewer", "1.0.0")
    api.service.objects._path(release.object_key).unlink()  # type: ignore[attr-defined]
    resp = _get(api, "/api/v1/artifacts/cv-reviewer/1.0.0")
    assert resp.status == 404 and _json(resp)["error"]["code"] == "artifact_missing"


def test_status_map_covers_every_domain_code() -> None:
    from provision_builder import package_errors as pe

    codes = {
        cls.code
        for cls in vars(pe).values()
        if isinstance(cls, type) and issubclass(cls, pe.PackageDomainError) and cls is not pe.PackageDomainError
    }
    assert codes <= set(STATUS_BY_CODE), f"unmapped codes: {codes - set(STATUS_BY_CODE)}"


def test_live_http_server_smoke(tmp_path: Path) -> None:
    service = PackageService(SQLiteRegistry(tmp_path / "r.db"), FileObjectStore(tmp_path / "obj"))
    server = make_server(HttpApi(service, tmp_path / "staging"), port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        base = f"http://{host}:{port}"
        req = urllib.request.Request(
            f"{base}/api/v1/applications/cv-reviewer/releases/1.0.0", data=b"live bytes", method="POST"
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            assert resp.status == 201
        with urllib.request.urlopen(f"{base}/api/v1/applications/cv-reviewer/releases", timeout=5) as resp:
            listed = json.loads(resp.read().decode())
        assert listed[0]["version"] == "1.0.0"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
