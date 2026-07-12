"""Pure request router: ``handle(method, path, body) -> Response``.

Kept socket-free so the whole API surface can be unit-tested deterministically;
``control_plane.server`` wraps it in a real ``http.server`` for live use and a
single smoke test.
"""

from __future__ import annotations

import json
import re
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

from provision_builder.package_errors import (
    PackageDomainError,
    ReleaseNotPublished,
    ReleaseYanked,
    UnknownChannel,
)
from provision_builder.package_services import YANKED, PackageService, Release

# 03_DOMAIN_SPEC.md §4 — the one place HTTP status is derived from a domain code.
STATUS_BY_CODE: dict[str, int] = {
    "invalid_identifier": 400,
    "unknown_application": 404,
    "unknown_channel": 404,
    "artifact_missing": 404,
    "duplicate_version": 409,
    "artifact_already_exists": 409,
    "release_not_published": 409,
    "release_yanked": 410,
    "hash_mismatch": 500,
    "artifact_corrupted": 500,
    "registry_unavailable": 503,
    "object_store_unavailable": 503,
    # governance (control_plane.rollout) — not part of the 12 registry codes
    "rollout_error": 409,
    "unauthorized": 403,
}
DEFAULT_ERROR_STATUS = 500
STATUS_BY_CODE["not_enabled"] = 501


class _Disabled(PackageDomainError):
    code = "not_enabled"


@dataclass
class Response:
    status: int
    body: bytes
    content_type: str = "application/json; charset=utf-8"


def _json(status: int, payload: object) -> Response:
    return Response(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"))


def _error(code: str, message: str, status: int) -> Response:
    return _json(status, {"error": {"code": code, "message": message}})


def _release_json(status: int, release: Release) -> Response:
    return _json(status, asdict(release))


Handler = Callable[..., Response]


class HttpApi:
    def __init__(self, service: PackageService, staging_dir: Path | str, *,
                 rollout=None, builds=None, worker=None, default_build_source=None):
        self.service = service
        self.staging = Path(staging_dir)
        self.staging.mkdir(parents=True, exist_ok=True)
        self.rollout = rollout   # optional control_plane.rollout.RolloutService
        self.builds = builds     # optional build_worker.BuildRecordStore
        self.worker = worker     # optional build_worker.BuildWorker (build-from-GUI)
        self.default_build_source = default_build_source  # lab: default source dir to build from
        v = r"/api/v1"
        app = r"(?P<app_id>[^/]+)"
        ver = r"(?P<version>[^/]+)"
        chan = r"(?P<channel>[^/]+)"
        dev = r"(?P<device_id>[^/]+)"
        rid = r"(?P<rollout_id>\d+)"
        self._routes: list[tuple[str, re.Pattern[str], Handler]] = [
            ("POST", re.compile(rf"^{v}/applications/{app}/releases/{ver}/promote$"), self._promote),
            ("POST", re.compile(rf"^{v}/applications/{app}/releases/{ver}/yank$"), self._yank),
            ("POST", re.compile(rf"^{v}/applications/{app}/releases/{ver}$"), self._publish),
            ("GET", re.compile(rf"^{v}/applications/{app}/releases$"), self._list),
            ("GET", re.compile(rf"^{v}/applications/{app}/channels/{chan}$"), self._resolve),
            ("GET", re.compile(rf"^{v}/applications/{app}/builds$"), self._builds),
            ("POST", re.compile(rf"^{v}/applications/{app}/build$"), self._build),
            ("POST", re.compile(rf"^{v}/applications/{app}/rollouts$"), self._start_rollout),
            ("GET", re.compile(rf"^{v}/applications/{app}/rollout$"), self._latest_rollout),
            ("GET", re.compile(rf"^{v}/applications/{app}/devices/{dev}/desired$"), self._desired),
            ("GET", re.compile(rf"^{v}/applications$"), self._list_apps),
            ("POST", re.compile(rf"^{v}/rollouts/{rid}/advance$"), self._advance),
            ("POST", re.compile(rf"^{v}/rollouts/{rid}/approve$"), self._approve),
            ("POST", re.compile(rf"^{v}/rollouts/{rid}/pause$"), self._pause),
            ("POST", re.compile(rf"^{v}/rollouts/{rid}/resume$"), self._resume),
            ("POST", re.compile(rf"^{v}/rollouts/{rid}/report$"), self._report),
            ("GET", re.compile(rf"^{v}/rollouts/{rid}$"), self._get_rollout),
            ("POST", re.compile(rf"^{v}/devices$"), self._register_device),
            ("GET", re.compile(rf"^{v}/devices$"), self._list_devices),
            ("GET", re.compile(rf"^{v}/audit$"), self._audit),
            ("POST", re.compile(rf"^{v}/artifacts/{app}/{ver}/download-url$"), self._download_url),
            ("GET", re.compile(rf"^{v}/artifacts/{app}/{ver}$"), self._download),
        ]

    # ── routing ──────────────────────────────────────────────────────────────

    def handle(self, method: str, path: str, body: bytes = b"") -> Response:
        try:
            for verb, pattern, fn in self._routes:
                if verb != method:
                    continue
                match = pattern.match(path)
                if match:
                    return fn(body=body, **match.groupdict())
            return _error("not_found", f"no route: {method} {path}", 404)
        except PackageDomainError as exc:
            return _error(exc.code, str(exc), STATUS_BY_CODE.get(exc.code, DEFAULT_ERROR_STATUS))
        except (KeyError, ValueError) as exc:
            return _error("invalid_request", f"malformed request: {exc}", 400)

    def _need_rollout(self):
        if self.rollout is None:
            raise _Disabled("rollout/governance not enabled on this control plane")
        return self.rollout

    @staticmethod
    def _body_json(body: bytes) -> dict:
        if not body:
            return {}
        try:
            value = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    # ── handlers ─────────────────────────────────────────────────────────────

    def _publish(self, *, app_id: str, version: str, body: bytes) -> Response:
        fd_dir = self.staging
        with tempfile.NamedTemporaryFile(dir=fd_dir, delete=False, suffix=".napp") as tmp:
            tmp.write(body)
            tmp_path = Path(tmp.name)
        try:
            release = self.service.publish(app_id, version, tmp_path)
        finally:
            tmp_path.unlink(missing_ok=True)
        return _release_json(201, release)

    def _list_apps(self, *, body: bytes) -> Response:
        return _json(200, {"applications": self.service.list_applications()})

    def _list(self, *, app_id: str, body: bytes) -> Response:
        releases = self.service.list_releases(app_id)
        return _json(200, [asdict(r) for r in releases])

    def _promote(self, *, app_id: str, version: str, body: bytes) -> Response:
        channel = self._body_json(body).get("channel", "")
        release = self.service.promote(app_id, channel, version)
        return _release_json(200, release)  # promote validated channel; release is set

    def _yank(self, *, app_id: str, version: str, body: bytes) -> Response:
        release = self.service.yank(app_id, version)
        return _release_json(200, release)

    def _resolve(self, *, app_id: str, channel: str, body: bytes) -> Response:
        release = self.service.resolve(app_id, channel)
        if release is None:
            raise UnknownChannel(f"channel not found: {app_id}/{channel}")
        return _release_json(200, release)

    def _require_downloadable(self, app_id: str, version: str) -> Release:
        release = self.service.get_release(app_id, version)
        if release is None:
            raise ReleaseNotPublished(f"release not found: {app_id}@{version}")
        if release.status == YANKED:
            raise ReleaseYanked(f"release is yanked: {app_id}@{version}")
        return release

    def _download_url(self, *, app_id: str, version: str, body: bytes) -> Response:
        release = self._require_downloadable(app_id, version)
        return _json(200, {
            "url": f"/api/v1/artifacts/{app_id}/{version}",
            "method": "GET",
            "sha256": release.sha256,
            "size_bytes": release.size_bytes,
        })

    def _download(self, *, app_id: str, version: str, body: bytes) -> Response:
        release = self._require_downloadable(app_id, version)
        with self.service.open_artifact(release) as source:
            data = source.read()
        return Response(200, data, content_type="application/octet-stream")

    # ── builds / rollout / devices (governance) ──────────────────────────────

    def _builds(self, *, app_id: str, body: bytes) -> Response:
        if self.builds is None:
            raise _Disabled("build records not enabled")
        return _json(200, [asdict(b) for b in self.builds.list_builds(app_id)])

    def _build(self, *, app_id: str, body: bytes) -> Response:
        if self.worker is None:
            raise _Disabled("building is not enabled on this control plane")
        from build_worker import BuildRequest  # lazy: build_worker not needed to import the API

        data = self._body_json(body)
        version = data["version"]
        source_dir = data.get("source_dir") or self.default_build_source
        if not source_dir:
            raise _Disabled("no build source configured")
        source = Path(source_dir)
        app_json = source / "app.json"
        base = json.loads(app_json.read_text("utf-8")) if app_json.is_file() else {
            "entrypoint": data.get("entrypoint", "app:main"), "requires": data.get("requires", []),
        }
        manifest = {**base, "id": app_id, "version": version}
        result = self.worker.run(BuildRequest(
            app_id, version, source, manifest,
            channel=data.get("channel") or None, source_commit=data.get("commit", ""),
        ))
        status = 201 if result.status == "succeeded" else 409
        return _json(status, {"status": result.status, "version": version,
                              "digest": result.canonical_digest, "error": result.error})

    def _start_rollout(self, *, app_id: str, body: bytes) -> Response:
        data = self._body_json(body)
        r = self._need_rollout().start_rollout(
            app_id, data["version"], stage_percent=int(data.get("stage_percent", 10)),
            baseline=data.get("baseline"), actor=data.get("actor", "system"),
        )
        return _json(201, asdict(r))

    def _latest_rollout(self, *, app_id: str, body: bytes) -> Response:
        r = self._need_rollout().latest_rollout(app_id)
        if r is None:
            return _error("not_found", f"no rollout for {app_id}", 404)
        return _json(200, asdict(r))

    def _desired(self, *, app_id: str, device_id: str, body: bytes) -> Response:
        version = self._need_rollout().desired_for_device(app_id, device_id)
        return _json(200, {"app_id": app_id, "device_id": device_id, "version": version})

    def _get_rollout(self, *, rollout_id: str, body: bytes) -> Response:
        return _json(200, asdict(self._need_rollout().get_rollout(int(rollout_id))))

    def _advance(self, *, rollout_id: str, body: bytes) -> Response:
        data = self._body_json(body)
        r = self._need_rollout().advance(int(rollout_id), int(data["stage_percent"]),
                                         actor=data.get("actor", "system"))
        return _json(200, asdict(r))

    def _approve(self, *, rollout_id: str, body: bytes) -> Response:
        actor = self._body_json(body).get("actor", "system")
        return _json(200, asdict(self._need_rollout().approve(int(rollout_id), actor=actor)))

    def _pause(self, *, rollout_id: str, body: bytes) -> Response:
        actor = self._body_json(body).get("actor", "system")
        return _json(200, asdict(self._need_rollout().pause(int(rollout_id), actor=actor)))

    def _resume(self, *, rollout_id: str, body: bytes) -> Response:
        actor = self._body_json(body).get("actor", "system")
        return _json(200, asdict(self._need_rollout().resume(int(rollout_id), actor=actor)))

    def _report(self, *, rollout_id: str, body: bytes) -> Response:
        data = self._body_json(body)
        r = self._need_rollout().report(int(rollout_id), data["device_id"], bool(data["success"]),
                                        actor=data.get("actor", "device"))
        return _json(200, asdict(r))

    def _register_device(self, *, body: bytes) -> Response:
        data = self._body_json(body)
        self._need_rollout().register_device(data["device_id"], data.get("group", "default"),
                                             actor=data.get("actor", "system"))
        return _json(201, {"device_id": data["device_id"], "group": data.get("group", "default")})

    def _list_devices(self, *, body: bytes) -> Response:
        return _json(200, {"devices": self._need_rollout().list_devices()})

    def _audit(self, *, body: bytes) -> Response:
        return _json(200, {"events": self._need_rollout().audit_log()})
