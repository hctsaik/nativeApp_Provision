from __future__ import annotations

from pathlib import Path

import pytest

from native_agent import NativeAgent
from native_agent.agent import (
    FAILED,
    ROLLED_BACK,
    SKIPPED_FAILED,
    SKIPPED_YANKED,
    START_ACTIVE,
    START_CACHED,
    UPDATED,
)
from provision_builder.blob_store import FileBlobStore
from provision_builder.napp import AppManifest, DevHmacSigner, build_napp
from provision_builder.package_errors import RegistryUnavailable
from provision_builder.package_services import FileObjectStore, PackageService, SQLiteRegistry

SIGNER = DevHmacSigner()


@pytest.fixture
def remote(tmp_path: Path):
    service = PackageService(SQLiteRegistry(tmp_path / "reg.db"), FileObjectStore(tmp_path / "obj"))
    blobs = FileBlobStore(tmp_path / "remote_blobs")
    return service, blobs


@pytest.fixture
def agent(tmp_path: Path, remote):
    service, blobs = remote
    return NativeAgent(
        tmp_path / "device", service, blobs,
        verifier=SIGNER,
        healthcheck=lambda _p: True,
        observe=lambda _p: True,
    )


def publish(remote, tmp_path: Path, version: str, *, body: bytes, big: bytes | None = None,
            requires=("numpy==1.26.0",)) -> None:
    service, blobs = remote
    src = tmp_path / f"src-{version}"
    src.mkdir(exist_ok=True)
    (src / "app.py").write_bytes(b"# app\n" + body)
    manifest = AppManifest.from_dict(
        {"id": "cv-reviewer", "version": version, "entrypoint": "app:main", "requires": list(requires)}
    )
    big_deps = {}
    if big is not None:
        blob_file = tmp_path / f"big-{version}.bin"
        blob_file.write_bytes(big)
        big_deps = {"torch.whl": blob_file}
    out = tmp_path / f"cv-{version}.napp"
    build_napp(manifest, src, out, big_deps=big_deps, blob_store=blobs, signer=SIGNER)
    service.publish("cv-reviewer", version, out)
    service.promote("cv-reviewer", "production", version)


def test_happy_path_installs_and_activates(agent: NativeAgent, remote, tmp_path: Path) -> None:
    publish(remote, tmp_path, "1.0.0", body=b"one")
    outcome = agent.update("cv-reviewer", "production")
    assert outcome.state == UPDATED and outcome.active == "1.0.0"
    assert agent.state.active_version("cv-reviewer") == "1.0.0"
    assert agent.state.last_known_good("cv-reviewer") == "1.0.0"
    assert agent.read_active("cv-reviewer")["version"] == "1.0.0"
    assert (agent._versions_dir("cv-reviewer") / "1.0.0" / "app.py").is_file()


def test_same_version_is_noop(agent: NativeAgent, remote, tmp_path: Path) -> None:
    publish(remote, tmp_path, "1.0.0", body=b"one")
    agent.update("cv-reviewer", "production")
    assert agent.update("cv-reviewer", "production").state == START_ACTIVE


def test_source_only_update_reuses_venv(agent: NativeAgent, remote, tmp_path: Path) -> None:
    publish(remote, tmp_path, "1.0.0", body=b"one")
    first = agent.update("cv-reviewer", "production")
    assert first.venv_reused is False
    publish(remote, tmp_path, "1.0.1", body=b"two")  # same requires → same fingerprint
    second = agent.update("cv-reviewer", "production")
    assert second.state == UPDATED and second.venv_reused is True


def test_blob_cache_pulls_then_reuses(agent: NativeAgent, remote, tmp_path: Path) -> None:
    publish(remote, tmp_path, "1.0.0", body=b"one", big=b"pretend torch bytes")
    first = agent.update("cv-reviewer", "production")
    assert first.blobs_pulled == 1 and first.blobs_reused == 0
    publish(remote, tmp_path, "1.0.1", body=b"two", big=b"pretend torch bytes")  # same blob
    second = agent.update("cv-reviewer", "production")
    assert second.blobs_pulled == 0 and second.blobs_reused == 1


def test_remote_unavailable_starts_cached(agent: NativeAgent, remote, tmp_path: Path, monkeypatch) -> None:
    publish(remote, tmp_path, "1.0.0", body=b"one")
    agent.update("cv-reviewer", "production")  # now active 1.0.0
    def boom(*_a, **_k):
        raise RegistryUnavailable("registry down")
    monkeypatch.setattr(agent.remote, "resolve", boom)
    outcome = agent.update("cv-reviewer", "production")
    assert outcome.state == START_CACHED and outcome.active == "1.0.0"


def test_no_desired_version_starts_cached(agent: NativeAgent) -> None:
    assert agent.update("cv-reviewer", "production").state == START_CACHED


def test_tampered_artifact_fails_and_is_not_retried(agent: NativeAgent, remote, tmp_path: Path) -> None:
    service, _ = remote
    publish(remote, tmp_path, "1.0.0", body=b"one")
    release = service.get_release("cv-reviewer", "1.0.0")
    service.objects._path(release.object_key).write_bytes(b"tampered payload")  # type: ignore[attr-defined]
    first = agent.update("cv-reviewer", "production")
    assert first.state == FAILED and first.active is None
    assert agent.state.is_failed("cv-reviewer", "1.0.0")
    assert agent.update("cv-reviewer", "production").state == SKIPPED_FAILED


def test_pre_start_healthcheck_failure_keeps_active(tmp_path: Path, remote) -> None:
    service, blobs = remote
    agent = NativeAgent(tmp_path / "device", service, blobs, verifier=SIGNER, healthcheck=lambda _p: False)
    publish(remote, tmp_path, "1.0.0", body=b"one")
    outcome = agent.update("cv-reviewer", "production")
    assert outcome.state == FAILED and outcome.active is None
    assert agent.read_active("cv-reviewer") is None


def test_observation_failure_rolls_back(tmp_path: Path, remote) -> None:
    service, blobs = remote
    healthy = {"value": True}
    agent = NativeAgent(tmp_path / "device", service, blobs, verifier=SIGNER,
                        observe=lambda _p: healthy["value"])
    publish(remote, tmp_path, "1.0.0", body=b"one")
    assert agent.update("cv-reviewer", "production").state == UPDATED  # active 1.0.0, LKG 1.0.0
    healthy["value"] = False
    publish(remote, tmp_path, "2.0.0", body=b"two")
    outcome = agent.update("cv-reviewer", "production")
    assert outcome.state == ROLLED_BACK and outcome.active == "1.0.0"
    assert agent.read_active("cv-reviewer")["version"] == "1.0.0"
    assert agent.state.is_failed("cv-reviewer", "2.0.0")


def test_yanked_channel_is_skipped(agent: NativeAgent, remote, tmp_path: Path) -> None:
    service, _ = remote
    publish(remote, tmp_path, "1.0.0", body=b"one")
    service.yank("cv-reviewer", "1.0.0")  # channel still points at the yanked release
    assert agent.update("cv-reviewer", "production").state == SKIPPED_YANKED


def test_reconcile_reverts_incomplete_update(agent: NativeAgent) -> None:
    # Simulate a crash during DOWNLOADING: a running op, no active version on disk.
    op = agent.state.begin_operation("cv-reviewer", from_version=None, to_version="1.0.0",
                                     previous_active=None, desired_identity="1.0.0")
    agent.state.update_step(op, "DOWNLOADING")
    outcomes = agent.reconcile("cv-reviewer")
    assert len(outcomes) == 1 and outcomes[0].state == ROLLED_BACK
    assert agent.state.active_version("cv-reviewer") is None
    assert agent.state.running_operations("cv-reviewer") == []


def test_reconcile_adopts_fully_installed_version(agent: NativeAgent, remote, tmp_path: Path) -> None:
    publish(remote, tmp_path, "1.0.0", body=b"one")
    agent.update("cv-reviewer", "production")  # 1.0.0 fully installed + active
    # Simulate a crash recorded as still-running even though activation completed.
    op = agent.state.begin_operation("cv-reviewer", from_version=None, to_version="1.0.0",
                                     previous_active=None, desired_identity="1.0.0")
    agent.state.update_step(op, "ACTIVATING")
    outcomes = agent.reconcile("cv-reviewer")
    assert outcomes[-1].state == UPDATED
    assert agent.state.active_version("cv-reviewer") == "1.0.0"
    assert agent.state.running_operations("cv-reviewer") == []
