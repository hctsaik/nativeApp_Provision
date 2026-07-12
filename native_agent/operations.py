"""Async operation runner, stage vocabulary, and concurrency errors (UI-1).

The agent journal keeps its internal step names (unchanged, so Slice 7 reconcile
still works); this module is the single source that maps those steps to the
canonical, GUI-facing event stages (10_AI_UI1_DEVELOPMENT_GUIDANCE §6) and runs
mutations as background operations with a per-application lock.
"""

from __future__ import annotations

import threading

from provision_builder.package_errors import PackageDomainError

# internal journal step (unchanged) → canonical event stage (external/GUI)
STAGE_BY_STEP = {
    "CHECKING": "RESOLVING",
    "DOWNLOADING": "DOWNLOADING",
    "VERIFYING": "VERIFYING",
    "EXTRACTING": "EXTRACTING",
    "DEPS_READY": "PREPARING_DEPENDENCIES",
    "MIGRATION_READY": "MIGRATING",
    "HEALTHCHECK": "HEALTHCHECK",
    "ACTIVATING": "ACTIVATING",
    "OBSERVING": "OBSERVING",
}
STAGE_BY_FINAL_STATUS = {
    "succeeded": "COMPLETED",
    "failed": "FAILED",
    "rolled_back": "ROLLED_BACK",
    "cancelled": "CANCELLED",
}
PERCENT_BY_STAGE = {
    "QUEUED": 0, "RESOLVING": 5, "DOWNLOADING": 20, "VERIFYING": 35,
    "EXTRACTING": 45, "PREPARING_DEPENDENCIES": 65, "MIGRATING": 75,
    "HEALTHCHECK": 85, "ACTIVATING": 92, "OBSERVING": 97,
    "COMPLETED": 100, "FAILED": 100, "ROLLED_BACK": 100, "CANCELLED": 100,
}
# v1: only the pre-download / verify window is safely cancellable.
CANCELLABLE_STAGES = {"QUEUED", "RESOLVING", "DOWNLOADING", "VERIFYING"}


class OperationCancelled(Exception):
    """Control-flow signal (not a domain error): a running op saw a cancel request."""

    def __init__(self, op_id: int):
        super().__init__(f"operation {op_id} cancelled")
        self.op_id = op_id


class OperationInProgress(PackageDomainError):
    code = "operation_in_progress"


class Forbidden(PackageDomainError):
    code = "forbidden"


class OperationRunner:
    """One in-flight mutation per app; ``update``/``install`` run in a thread."""

    def __init__(self, agent):
        self.agent = agent
        self._lock = threading.Lock()
        self._threads: dict[int, threading.Thread] = {}
        self._results: dict[int, object] = {}

    def _guard(self, app_id: str) -> None:
        # running_operations reads DB status='running', so it also catches an op
        # left running by a crash — that one needs `reconcile` before mutating.
        if self.agent.state.running_operations(app_id):
            raise OperationInProgress(
                f"an operation is already running for {app_id}; wait for it or reconcile"
            )

    def submit_update(self, app_id: str, channel: str, *, force: bool = False):
        """Return ``(early_outcome, None)`` or ``(None, op_id)`` — exactly one set."""
        with self._lock:
            self._guard(app_id)
            early, desired, active = self.agent.plan_update(app_id, channel, force=force)
            if early is not None:
                return early, None
            op = self.agent.state.begin_operation(
                app_id, from_version=active, to_version=desired.version,
                previous_active=active, desired_identity=desired.version, kind="update",
            )
            thread = threading.Thread(target=self._run, args=(op, app_id, desired, active), daemon=True)
            self._threads[op] = thread
            thread.start()
            return None, op

    def _run(self, op, app_id, desired, active) -> None:
        self._results[op] = self.agent.execute_update(op, app_id, desired, active)

    def guarded_sync(self, app_id: str, fn):
        """Run a fast, synchronous mutation (rollback/reconcile/gc) under the lock guard."""
        with self._lock:
            self._guard(app_id)
        return fn()

    def wait(self, op_id: int, timeout: float | None = None) -> None:
        thread = self._threads.get(op_id)
        if thread is not None:
            thread.join(timeout)

    def result(self, op_id: int):
        return self._results.get(op_id)
