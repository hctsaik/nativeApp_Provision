"""Application management view model + service (UI-1, native_Provision side).

The Native App Management Center (and the diagnostic Device Portal) consume this
one service; neither stitches together Agent SQLite, the Control Plane and the
plugin catalog itself. Every long operation goes through the OperationRunner so
it is async, observable, and serialized per application. There is deliberately
no ``cv-reviewer`` special-casing here — a second application flows through the
same code.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from provision_builder.package_errors import PackageDomainError, validate_identifier
from provision_builder.package_services import YANKED
from native_agent.operations import CANCELLABLE_STAGES, OperationRunner

# update_state values (GUI decides the primary action from these).
NOT_INSTALLED = "NOT_INSTALLED"
UP_TO_DATE = "UP_TO_DATE"
UPDATE_AVAILABLE = "UPDATE_AVAILABLE"
UPDATE_FAILED = "UPDATE_FAILED"
REMOTE_UNAVAILABLE = "REMOTE_UNAVAILABLE"
YANKED_STATE = "YANKED"


@dataclass
class ApplicationManagementView:
    app_id: str
    display_name: str
    category: str
    installed: bool
    active_version: str | None
    last_known_good: str | None
    desired_version: str | None
    latest_version: str | None
    update_state: str
    enabled: bool
    can_launch: bool
    can_install: bool
    can_update: bool
    can_rollback: bool
    health: str
    current_operation: int | None


class ApplicationManagementService:
    def __init__(self, agent, runner: OperationRunner, channel: str = "production", *, catalog: dict | None = None):
        self.agent = agent
        self.runner = runner
        self.channel = channel
        # catalog: app_id -> {"display_name":..., "category":..., "enabled":bool}
        # In nativeApp this is the plugin/Management Store adapter; None → derive.
        self.catalog = catalog or {}

    # ── read side ────────────────────────────────────────────────────────────

    def _display_name(self, app_id: str) -> str:
        entry = self.catalog.get(app_id) or {}
        return entry.get("display_name") or app_id.replace("-", " ").replace("_", " ").title()

    def _running_op(self, app_id: str) -> int | None:
        ops = self.agent.state.running_operations(app_id)
        return ops[-1].op_id if ops else None

    def view(self, app_id: str) -> ApplicationManagementView:
        validate_identifier(app_id, "app_id")
        st = self.agent.state
        active = st.active_version(app_id)
        lkg = st.last_known_good(app_id)
        installed = active is not None

        remote_ok, release = True, None
        try:
            release = self.agent.check(app_id, self.channel)
        except PackageDomainError:
            remote_ok = False
        desired = release.version if release is not None else None
        yanked = release is not None and release.status == YANKED

        if not remote_ok:
            update_state = REMOTE_UNAVAILABLE
        elif release is None:
            update_state = UP_TO_DATE if installed else NOT_INSTALLED
        elif yanked:
            update_state = YANKED_STATE
        elif not installed:
            update_state = NOT_INSTALLED
        elif desired == active:
            update_state = UP_TO_DATE
        elif st.is_failed(app_id, desired):
            update_state = UPDATE_FAILED
        else:
            update_state = UPDATE_AVAILABLE

        entry = self.catalog.get(app_id) or {}
        current_op = self._running_op(app_id)
        installable = (not installed) and desired is not None and not yanked and remote_ok
        return ApplicationManagementView(
            app_id=app_id,
            display_name=self._display_name(app_id),
            category=entry.get("category", "app"),
            installed=installed,
            active_version=active,
            last_known_good=lkg,
            desired_version=None if yanked else desired,
            latest_version=desired,
            update_state=update_state,
            enabled=entry.get("enabled", True),
            can_launch=installed,
            can_install=installable and current_op is None,
            can_update=installed and update_state == UPDATE_AVAILABLE and current_op is None,
            can_rollback=installed and lkg is not None and lkg != active and current_op is None,
            health="UNKNOWN" if not installed else ("HEALTHY" if lkg == active else "DEGRADED"),
            current_operation=current_op,
        )

    def list_views(self) -> list[ApplicationManagementView]:
        ids = set(self.agent.state.known_apps()) | set(self.catalog)
        try:
            ids |= set(self.agent.remote.list_applications())
        except PackageDomainError:
            pass  # remote down → local-only list
        return [self.view(a) for a in sorted(ids)]

    def operation(self, op_id: int) -> dict | None:
        row = self.agent.state.operation_dict(op_id)
        if row is None:
            return None
        events = self.events(op_id)
        latest = events[-1] if events else None
        return {
            "operation_id": op_id,
            "app_id": row["app_id"],
            "kind": row["kind"],
            "status": row["status"],
            "current_step": row["current_step"],
            "last_error": row["last_error"],
            "stage": latest["stage"] if latest else None,
            "percent": latest["percent"] if latest else 0,
            "cancel_requested": bool(row["cancel_requested"]),
        }

    def events(self, op_id: int, after: int = 0) -> list[dict]:
        evts = self.agent.state.events_after(op_id, after)
        for e in evts:
            e["can_cancel"] = e["stage"] in CANCELLABLE_STAGES and e["state"] == "RUNNING"
        return evts

    # ── mutations (all serialized per app by the runner) ─────────────────────

    def update(self, app_id: str, *, force: bool = False):
        """Return ``(early_outcome | None, op_id | None)`` — one is set."""
        validate_identifier(app_id, "app_id")
        return self.runner.submit_update(app_id, self.channel, force=force)

    # Install is the same transaction as update; it just names the not-installed case.
    install = update

    def rollback(self, app_id: str):
        validate_identifier(app_id, "app_id")
        return self.runner.guarded_sync(app_id, lambda: self.agent.rollback(app_id))

    def reconcile(self, app_id: str):
        validate_identifier(app_id, "app_id")
        return self.runner.guarded_sync(app_id, lambda: self.agent.reconcile(app_id))

    def gc(self, app_id: str):
        validate_identifier(app_id, "app_id")
        return self.runner.guarded_sync(app_id, lambda: self.agent.gc(app_id))

    def cancel(self, op_id: int) -> None:
        self.agent.state.request_cancel(op_id)


def view_to_dict(view: ApplicationManagementView) -> dict:
    return asdict(view)
