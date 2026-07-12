"""UI-1 ApplicationManagementService: view model, async ops, lock, cancel."""

from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from native_agent import ApplicationManagementService, NativeAgent, OperationRunner
from native_agent.agent import CANCELLED
from native_agent.operations import OperationInProgress
from provision_builder.blob_store import FileBlobStore
from provision_builder.napp import AppManifest, DevHmacSigner, build_napp
from provision_builder.package_errors import RegistryUnavailable
from provision_builder.package_services import FileObjectStore, PackageService, SQLiteRegistry

SIGNER = DevHmacSigner()


def publish(remote, tmp_path: Path, version: str, *, app: str = "cv-reviewer", requires=("numpy==1.26.0",)) -> None:
    src = tmp_path / f"src-{app}-{version}"
    src.mkdir(exist_ok=True)
    (src / "app.py").write_text(f"# {app} {version}\n", encoding="utf-8")
    manifest = AppManifest.from_dict({"id": app, "version": version, "entrypoint": "app:main", "requires": list(requires)})
    out = tmp_path / f"{app}-{version}.napp"
    build_napp(manifest, src, out, signer=SIGNER)
    remote.publish(app, version, out)
    remote.promote(app, "production", version)


def _lab(tmp_path: Path, **agent_kw):
    remote = PackageService(SQLiteRegistry(tmp_path / "r.db"), FileObjectStore(tmp_path / "obj"))
    blobs = FileBlobStore(tmp_path / "remote_blobs")
    agent = NativeAgent(tmp_path / "device", remote, blobs, verifier=SIGNER, **agent_kw)
    runner = OperationRunner(agent)
    service = ApplicationManagementService(agent, runner, "production")
    return SimpleNamespace(remote=remote, blobs=blobs, agent=agent, runner=runner, service=service, tmp=tmp_path)


@pytest.fixture
def lab(tmp_path: Path):
    return _lab(tmp_path)


def _run_update(lab_) -> int:
    early, op_id = lab_.service.update("cv-reviewer")
    assert op_id is not None
    lab_.runner.wait(op_id, timeout=10)
    return op_id


# ── view model ──────────────────────────────────────────────────────────────

def test_view_not_installed(lab) -> None:
    publish(lab.remote, lab.tmp, "1.0.0")
    v = lab.service.view("cv-reviewer")
    assert v.update_state == "NOT_INSTALLED" and v.installed is False
    assert v.can_install is True and v.can_update is False and v.can_launch is False
    assert v.desired_version == "1.0.0"


def test_view_update_available(lab) -> None:
    publish(lab.remote, lab.tmp, "1.0.0")
    _run_update(lab)
    publish(lab.remote, lab.tmp, "2.0.0")
    v = lab.service.view("cv-reviewer")
    assert v.update_state == "UPDATE_AVAILABLE" and v.can_update is True
    assert v.active_version == "1.0.0" and v.desired_version == "2.0.0"


def test_view_up_to_date(lab) -> None:
    publish(lab.remote, lab.tmp, "1.0.0")
    _run_update(lab)
    v = lab.service.view("cv-reviewer")
    assert v.update_state == "UP_TO_DATE" and v.can_update is False and v.health == "HEALTHY"


def test_view_remote_unavailable_keeps_local_info(lab, monkeypatch) -> None:
    publish(lab.remote, lab.tmp, "1.0.0")
    _run_update(lab)

    def boom(*_a, **_k):
        raise RegistryUnavailable("registry down")

    monkeypatch.setattr(lab.agent, "check", boom)
    v = lab.service.view("cv-reviewer")
    assert v.update_state == "REMOTE_UNAVAILABLE"
    assert v.installed is True and v.active_version == "1.0.0" and v.last_known_good == "1.0.0"
    assert v.can_launch is True


def test_view_yanked(lab) -> None:
    publish(lab.remote, lab.tmp, "1.0.0")
    _run_update(lab)
    publish(lab.remote, lab.tmp, "2.0.0")
    lab.remote.yank("cv-reviewer", "2.0.0")  # production now points at a yanked release
    v = lab.service.view("cv-reviewer")
    assert v.update_state == "YANKED" and v.can_update is False and v.desired_version is None


# ── async operation + events ─────────────────────────────────────────────────

def test_async_update_lifecycle(lab) -> None:
    publish(lab.remote, lab.tmp, "1.0.0")
    op_id = _run_update(lab)
    op = lab.service.operation(op_id)
    assert op["status"] == "succeeded" and op["stage"] == "COMPLETED" and op["percent"] == 100
    events = lab.service.events(op_id)
    seqs = [e["sequence"] for e in events]
    assert seqs == sorted(seqs) and len(seqs) == len(set(seqs))  # strictly increasing
    stages = [e["stage"] for e in events]
    assert stages[0] == "QUEUED" and stages[-1] == "COMPLETED"
    assert "DOWNLOADING" in stages and "ACTIVATING" in stages
    assert lab.agent.state.active_version("cv-reviewer") == "1.0.0"


def test_events_survive_restart(lab) -> None:
    publish(lab.remote, lab.tmp, "1.0.0")
    op_id = _run_update(lab)
    # A fresh AgentState on the same DB file must still see the full event log.
    from native_agent.state import AgentState

    reopened = AgentState(lab.tmp / "device" / "agent" / "state.db")
    events = reopened.events_after(op_id, 0)
    assert events and events[-1]["stage"] == "COMPLETED"


def test_events_after_cursor(lab) -> None:
    publish(lab.remote, lab.tmp, "1.0.0")
    op_id = _run_update(lab)
    everything = lab.service.events(op_id)
    tail = lab.service.events(op_id, after=everything[2]["sequence"])
    assert [e["sequence"] for e in tail] == [e["sequence"] for e in everything[3:]]


# ── per-app mutation lock ─────────────────────────────────────────────────────

def test_second_update_is_rejected(tmp_path: Path) -> None:
    gate = threading.Event()
    lab_ = _lab(tmp_path, ensure_venv=lambda fp, d: gate.wait(10))  # holds op1 at PREPARING_DEPENDENCIES
    publish(lab_.remote, tmp_path, "1.0.0")
    _, op1 = lab_.service.update("cv-reviewer")
    assert op1 is not None
    try:
        with pytest.raises(OperationInProgress):
            lab_.service.update("cv-reviewer")
    finally:
        gate.set()
        lab_.runner.wait(op1, timeout=10)
    assert lab_.agent.state.active_version("cv-reviewer") == "1.0.0"


def test_gc_during_update_is_rejected(tmp_path: Path) -> None:
    gate = threading.Event()
    lab_ = _lab(tmp_path, ensure_venv=lambda fp, d: gate.wait(10))
    publish(lab_.remote, tmp_path, "1.0.0")
    _, op1 = lab_.service.update("cv-reviewer")
    try:
        with pytest.raises(OperationInProgress):
            lab_.service.gc("cv-reviewer")
    finally:
        gate.set()
        lab_.runner.wait(op1, timeout=10)


# ── cancellation ──────────────────────────────────────────────────────────────

class _GatedSource:
    def __init__(self, data: bytes, started: threading.Event, release: threading.Event):
        self._data, self._started, self._release, self._done = data, started, release, False

    def read(self, _n: int = -1) -> bytes:
        if not self._done:
            self._started.set()
            self._release.wait(10)
            self._done = True
            return self._data
        return b""

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False

    def close(self):
        pass


class _GatedRemote:
    def __init__(self, real):
        self._real = real
        self.started = threading.Event()
        self.release = threading.Event()

    def resolve(self, app_id, channel):
        return self._real.resolve(app_id, channel)

    def list_applications(self):
        return self._real.list_applications()

    def open_artifact(self, release):
        with self._real.open_artifact(release) as fh:
            data = fh.read()
        return _GatedSource(data, self.started, self.release)


def test_cancel_before_uncancellable_stage(tmp_path: Path) -> None:
    real = PackageService(SQLiteRegistry(tmp_path / "r.db"), FileObjectStore(tmp_path / "obj"))
    blobs = FileBlobStore(tmp_path / "remote_blobs")
    publish(real, tmp_path, "1.0.0")
    gated = _GatedRemote(real)
    agent = NativeAgent(tmp_path / "device", gated, blobs, verifier=SIGNER)
    runner = OperationRunner(agent)
    service = ApplicationManagementService(agent, runner, "production")

    _, op_id = service.update("cv-reviewer")
    assert gated.started.wait(10)      # thread is inside DOWNLOADING, blocked on read
    service.cancel(op_id)
    gated.release.set()                # download finishes → VERIFYING boundary → cancel fires
    runner.wait(op_id, timeout=10)

    outcome = runner.result(op_id)
    assert outcome.state == CANCELLED
    assert agent.state.active_version("cv-reviewer") is None
    assert agent.state.is_failed("cv-reviewer", "1.0.0") is False   # cancel ≠ bad version


# ── install alias + no app-specific behavior ─────────────────────────────────

def test_install_installs_when_not_installed(lab) -> None:
    publish(lab.remote, lab.tmp, "1.0.0")
    early, op_id = lab.service.install("cv-reviewer")
    assert op_id is not None
    lab.runner.wait(op_id, timeout=10)
    assert lab.agent.state.active_version("cv-reviewer") == "1.0.0"


def test_no_app_specific_behavior_second_app(lab) -> None:
    publish(lab.remote, lab.tmp, "1.0.0")
    _run_update(lab)
    publish(lab.remote, lab.tmp, "3.1.0", app="defect-inspector")
    early, op_id = lab.service.update("defect-inspector")
    lab.runner.wait(op_id, timeout=10)
    v = lab.service.view("defect-inspector")
    assert v.update_state == "UP_TO_DATE" and v.display_name == "Defect Inspector"
    ids = {view.app_id for view in lab.service.list_views()}
    assert {"cv-reviewer", "defect-inspector"} <= ids
