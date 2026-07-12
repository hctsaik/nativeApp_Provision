"""Device-local `/management` JSON API (UI-1).

Socket-free router (same shape as control_plane.http_api) so it is deterministic
to test and can be mounted behind the existing Native App HTTP/iframe extension
point. It exposes ONLY device-local management — never Control Plane governance
(build/promote/rollout live in the Fleet Console). RBAC is enforced here on
every mutation (10_AI_UI1_DEVELOPMENT_GUIDANCE §13), not by hiding buttons.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

from provision_builder.package_errors import PackageDomainError
from native_agent.management import ApplicationManagementService, view_to_dict
from native_agent.operations import Forbidden

STATUS_BY_CODE = {
    "invalid_identifier": 400,
    "invalid_request": 400,
    "forbidden": 403,
    "unknown_application": 404,
    "not_found": 404,
    "operation_in_progress": 409,
}
DEFAULT_ERROR_STATUS = 500

# actions that require the "admin" local role
_ADMIN_ACTIONS = {"rollback", "reconcile", "gc"}


@dataclass
class Response:
    status: int
    body: bytes
    content_type: str = "application/json; charset=utf-8"


def _json(status: int, payload: object) -> Response:
    return Response(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"))


def _error(code: str, message: str, status: int) -> Response:
    return _json(status, {"error": {"code": code, "message": message}})


class ManagementApi:
    def __init__(self, service: ApplicationManagementService):
        self.service = service
        m = r"/management"
        app = r"(?P<app_id>[^/?]+)"
        opid = r"(?P<op_id>\d+)"
        self._routes = [
            ("GET", re.compile(rf"^{m}/applications$"), self._list),
            ("GET", re.compile(rf"^{m}/applications/{app}$"), self._detail),
            ("POST", re.compile(rf"^{m}/applications/{app}/install$"), self._update),
            ("POST", re.compile(rf"^{m}/applications/{app}/update$"), self._update),
            ("POST", re.compile(rf"^{m}/applications/{app}/rollback$"), self._rollback),
            ("POST", re.compile(rf"^{m}/applications/{app}/reconcile$"), self._reconcile),
            ("POST", re.compile(rf"^{m}/applications/{app}/gc$"), self._gc),
            ("GET", re.compile(rf"^{m}/operations/{opid}/events$"), self._events),
            ("GET", re.compile(rf"^{m}/operations/{opid}$"), self._operation),
            ("POST", re.compile(rf"^{m}/operations/{opid}/cancel$"), self._cancel),
        ]

    def handle(self, method: str, path: str, body: bytes = b"", headers: dict | None = None) -> Response:
        headers = headers or {}
        role = (headers.get("X-Role") or headers.get("x-role") or "user")
        parts = urlsplit(path)
        try:
            for verb, pattern, fn in self._routes:
                if verb != method:
                    continue
                match = pattern.match(parts.path)
                if match:
                    return fn(body=body, role=role, query=parts.query, **match.groupdict())
            return _error("not_found", f"no route: {method} {parts.path}", 404)
        except PackageDomainError as exc:
            return _error(exc.code, str(exc), STATUS_BY_CODE.get(exc.code, DEFAULT_ERROR_STATUS))
        except (KeyError, ValueError) as exc:
            return _error("invalid_request", f"malformed request: {exc}", 400)

    @staticmethod
    def _require_admin(action: str, role: str) -> None:
        if action in _ADMIN_ACTIONS and role != "admin":
            raise Forbidden(f"{action} requires the local admin role")

    @staticmethod
    def _outcome(outcome) -> dict:
        return {"state": outcome.state, "active": outcome.active, "target": outcome.target,
                "error": outcome.error}

    # ── handlers ─────────────────────────────────────────────────────────────

    def _list(self, *, body, role, query) -> Response:
        return _json(200, [view_to_dict(v) for v in self.service.list_views()])

    def _detail(self, *, app_id, body, role, query) -> Response:
        return _json(200, view_to_dict(self.service.view(app_id)))

    def _update(self, *, app_id, body, role, query) -> Response:
        force = bool(self._body(body).get("force"))
        early, op_id = self.service.update(app_id, force=force)
        if op_id is not None:
            return _json(202, {"operation_id": op_id, "state": "QUEUED",
                               "status_url": f"/management/operations/{op_id}"})
        return _json(200, self._outcome(early))

    def _rollback(self, *, app_id, body, role, query) -> Response:
        self._require_admin("rollback", role)
        return _json(200, self._outcome(self.service.rollback(app_id)))

    def _reconcile(self, *, app_id, body, role, query) -> Response:
        self._require_admin("reconcile", role)
        outcomes = self.service.reconcile(app_id)
        return _json(200, {"outcomes": [self._outcome(o) for o in outcomes]})

    def _gc(self, *, app_id, body, role, query) -> Response:
        self._require_admin("gc", role)
        return _json(200, self.service.gc(app_id))

    def _operation(self, *, op_id, body, role, query) -> Response:
        detail = self.service.operation(int(op_id))
        if detail is None:
            return _error("not_found", f"no such operation: {op_id}", 404)
        return _json(200, detail)

    def _events(self, *, op_id, body, role, query) -> Response:
        after = int((parse_qs(query).get("after") or ["0"])[0])
        return _json(200, {"events": self.service.events(int(op_id), after)})

    def _cancel(self, *, op_id, body, role, query) -> Response:
        self.service.cancel(int(op_id))
        return _json(202, {"operation_id": int(op_id), "cancel_requested": True})

    @staticmethod
    def _body(body: bytes) -> dict:
        if not body:
            return {}
        try:
            value = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}
        return value if isinstance(value, dict) else {}
