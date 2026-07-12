"""End-to-end vertical: Build Worker → Registry/Blobs → Native Agent.

Proves the first success scenario from 02_ARCHITECTURE.md §1 with the lab
substitutes (PackageService + FileBlobStore standing in for Control Plane +
MinIO): build a signed .napp, publish + promote, then have a device agent
resolve, download only what it lacks, verify, install and atomically activate —
and refuse a tampered follow-up without disturbing the active version.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from build_worker import BuildRequest, BuildWorker
from native_agent import NativeAgent
from native_agent.agent import FAILED, UPDATED
from provision_builder.blob_store import FileBlobStore
from provision_builder.napp import DevHmacSigner
from provision_builder.package_services import FileObjectStore, PackageService, SQLiteRegistry

SIGNER = DevHmacSigner()
APP = {"id": "cv-reviewer", "version": "", "entrypoint": "app:main", "requires": ["numpy==1.26.0"]}


@pytest.fixture
def world(tmp_path: Path):
    service = PackageService(SQLiteRegistry(tmp_path / "reg.db"), FileObjectStore(tmp_path / "obj"))
    blobs = FileBlobStore(tmp_path / "minio_blobs")
    worker = BuildWorker(service, blobs, tmp_path / "jobs", signer=SIGNER, verifier=SIGNER)
    agent = NativeAgent(tmp_path / "device", service, blobs, verifier=SIGNER)
    return service, blobs, worker, agent, tmp_path


def _build_and_publish(worker: BuildWorker, tmp_path: Path, version: str, body: bytes, big: bytes) -> None:
    src = tmp_path / f"src-{version}"
    src.mkdir(exist_ok=True)
    (src / "app.py").write_bytes(b"# app\n" + body)
    big_file = tmp_path / f"torch-{version}.bin"
    big_file.write_bytes(big)
    manifest = {**APP, "version": version}
    result = worker.run(BuildRequest(
        "cv-reviewer", version, src, manifest,
        big_deps={"torch.whl": big_file}, channel="production", source_commit=version,
    ))
    assert result.status == "succeeded", result.error


def test_full_pipeline_build_publish_update_activate(world) -> None:
    service, blobs, worker, agent, tmp_path = world

    _build_and_publish(worker, tmp_path, "1.0.0", body=b"one", big=b"pretend torch 2GB")
    first = agent.update("cv-reviewer", "production")
    assert first.state == UPDATED and first.active == "1.0.0"
    assert first.blobs_pulled == 1 and first.venv_reused is False
    assert (agent._versions_dir("cv-reviewer") / "1.0.0" / "app.py").is_file()

    # Source-only follow-up: same deps + same big dep → reuse venv and blob.
    _build_and_publish(worker, tmp_path, "1.0.1", body=b"two", big=b"pretend torch 2GB")
    second = agent.update("cv-reviewer", "production")
    assert second.state == UPDATED and second.active == "1.0.1"
    assert second.blobs_pulled == 0 and second.blobs_reused == 1 and second.venv_reused is True

    # Tampered follow-up must not disturb the running version.
    _build_and_publish(worker, tmp_path, "2.0.0", body=b"three", big=b"pretend torch 2GB")
    bad = service.get_release("cv-reviewer", "2.0.0")
    service.objects._path(bad.object_key).write_bytes(b"corrupted")  # type: ignore[attr-defined]
    outcome = agent.update("cv-reviewer", "production")
    assert outcome.state == FAILED
    assert agent.state.active_version("cv-reviewer") == "1.0.1"      # untouched
    assert agent.read_active("cv-reviewer")["version"] == "1.0.1"
