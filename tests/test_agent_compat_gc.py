from __future__ import annotations

from pathlib import Path

import pytest

from native_agent import NativeAgent
from native_agent.agent import FAILED, UPDATED
from provision_builder.blob_store import FileBlobStore
from provision_builder.napp import AppManifest, DevHmacSigner, build_napp
from provision_builder.package_services import FileObjectStore, PackageService, SQLiteRegistry

SIGNER = DevHmacSigner()


@pytest.fixture
def remote(tmp_path: Path):
    service = PackageService(SQLiteRegistry(tmp_path / "reg.db"), FileObjectStore(tmp_path / "obj"))
    return service, FileBlobStore(tmp_path / "remote_blobs")


def publish(remote, tmp_path: Path, version: str, *, requires=("numpy==1.26.0",),
            platform: dict | None = None) -> None:
    service, blobs = remote
    src = tmp_path / f"src-{version}"
    src.mkdir(exist_ok=True)
    (src / "app.py").write_bytes(b"# app " + version.encode())
    manifest = AppManifest.from_dict(
        {"id": "cv-reviewer", "version": version, "entrypoint": "app:main", "requires": list(requires)}
    )
    out = tmp_path / f"cv-{version}.napp"
    build_napp(manifest, src, out, signer=SIGNER, platform=platform)
    service.publish("cv-reviewer", version, out)
    service.promote("cv-reviewer", "production", version)


def _agent(tmp_path: Path, remote, **kw) -> NativeAgent:
    service, blobs = remote
    return NativeAgent(tmp_path / "device", service, blobs, verifier=SIGNER, **kw)


def test_compatible_platform_installs(remote, tmp_path: Path) -> None:
    publish(remote, tmp_path, "1.0.0", platform={"os": "windows", "arch": "amd64", "abi": "cp311"})
    agent = _agent(tmp_path, remote, expected_platform={"os": "windows", "abi": "cp311"})
    assert agent.update("cv-reviewer", "production").state == UPDATED


def test_incompatible_abi_is_rejected(remote, tmp_path: Path) -> None:
    publish(remote, tmp_path, "1.0.0", platform={"os": "windows", "arch": "amd64", "abi": "cp310"})
    agent = _agent(tmp_path, remote, expected_platform={"abi": "cp311"})
    outcome = agent.update("cv-reviewer", "production")
    assert outcome.state == FAILED and "incompatible" in (outcome.error or "").lower()
    assert agent.state.active_version("cv-reviewer") is None
    assert agent.state.is_failed("cv-reviewer", "1.0.0")


def test_gc_prunes_old_versions_but_keeps_active(remote, tmp_path: Path) -> None:
    agent = _agent(tmp_path, remote)
    for version in ("1.0.0", "1.0.1", "1.0.2"):
        publish(remote, tmp_path, version)  # same requires → same fingerprint
        agent.update("cv-reviewer", "production")
    assert agent.state.active_version("cv-reviewer") == "1.0.2"

    result = agent.gc("cv-reviewer")
    assert result["removed_versions"] == ["1.0.0", "1.0.1"]
    assert result["kept_versions"] == ["1.0.2"]
    assert (agent._versions_dir("cv-reviewer") / "1.0.2").is_dir()
    assert not (agent._versions_dir("cv-reviewer") / "1.0.0").exists()
    assert result["removed_venvs"] == []  # shared fingerprint venv is still in use


def test_gc_prunes_unreferenced_venv(remote, tmp_path: Path) -> None:
    agent = _agent(tmp_path, remote)
    publish(remote, tmp_path, "1.0.0", requires=("numpy==1.26.0",))
    agent.update("cv-reviewer", "production")
    fp_old = agent._version_fingerprint("cv-reviewer", "1.0.0")
    publish(remote, tmp_path, "2.0.0", requires=("numpy==2.0.0", "pandas"))  # different deps → new fp
    agent.update("cv-reviewer", "production")
    fp_new = agent._version_fingerprint("cv-reviewer", "2.0.0")
    assert fp_old != fp_new

    result = agent.gc("cv-reviewer")
    assert fp_old in result["removed_venvs"] and fp_new not in result["removed_venvs"]
    assert (agent._app_dir("cv-reviewer") / "venvs" / fp_new).is_dir()
    assert not (agent._app_dir("cv-reviewer") / "venvs" / fp_old).exists()
