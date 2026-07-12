from pathlib import Path

from build_worker import BuildRequest, BuildWorker
from native_agent import ApplicationManagementService, FileChannelRemote, NativeAgent, OperationRunner, export_channel
from provision_builder.blob_store import FileBlobStore
from provision_builder.napp import AppManifest
from provision_builder.package_services import FileObjectStore, PackageService, SQLiteRegistry


def stores(tmp_path: Path):
    service = PackageService(SQLiteRegistry(tmp_path / "registry.db"), FileObjectStore(tmp_path / "objects"))
    return service, FileBlobStore(tmp_path / "blobs")


def test_entrypoint_is_optional_launch_hint() -> None:
    assert AppManifest.from_dict({"id": "app-ai4bi", "version": "1.0.0"}).entrypoint == ""


def test_strict_dependencies_rejects_empty_resolution(tmp_path: Path) -> None:
    service, blobs = stores(tmp_path)
    source = tmp_path / "source"
    source.mkdir()
    (source / "app.py").write_text("pass", encoding="utf-8")
    worker = BuildWorker(service, blobs, tmp_path / "jobs", strict_dependencies=True)
    request = BuildRequest("app-demo", "1.0.0", source,
                           {"id": "app-demo", "version": "1.0.0", "requires": ["demo==1"]})
    result = worker.run(request)
    assert result.status == "failed" and "PlatformGateway dependency resolution" in result.error


def test_production_healthcheck_gate(tmp_path: Path) -> None:
    service, blobs = stores(tmp_path)
    source = tmp_path / "source"
    source.mkdir()
    (source / "app.py").write_text("pass", encoding="utf-8")
    worker = BuildWorker(service, blobs, tmp_path / "jobs", require_production_healthcheck=True)
    result = worker.run(BuildRequest("app-demo", "1.0.0", source,
                                     {"id": "app-demo", "version": "1.0.0"}, channel="production"))
    assert result.status == "failed" and "requires a real healthcheck" in result.error


def test_incomplete_venv_is_rebuilt_and_marked_complete(tmp_path: Path) -> None:
    service, blobs = stores(tmp_path)
    calls = []
    agent = NativeAgent(tmp_path / "device", service, blobs,
                        ensure_venv=lambda fp, path: calls.append((fp, path)))
    poisoned = agent._app_dir("app-demo") / "venvs" / "fingerprint"
    poisoned.mkdir(parents=True)
    assert agent._prepare_venv("app-demo", "fingerprint") is False
    assert len(calls) == 1 and (poisoned / ".complete").is_file()
    assert agent._prepare_venv("app-demo", "fingerprint") is True


def test_catalog_only_apps_are_visible(tmp_path: Path) -> None:
    service, blobs = stores(tmp_path)
    agent = NativeAgent(tmp_path / "device", service, blobs)
    management = ApplicationManagementService(
        agent, OperationRunner(agent), catalog={"app-ai4bi": {"display_name": "AI4BI"}}
    )
    assert [view.app_id for view in management.list_views()] == ["app-ai4bi"]


def test_exported_file_channel_resolves_and_opens(tmp_path: Path) -> None:
    service, blobs = stores(tmp_path)
    artifact = tmp_path / "app.napp"
    artifact.write_bytes(b"package")
    service.publish("app-demo", "1.0.0", artifact)
    service.promote("app-demo", "production", "1.0.0")
    remote = FileChannelRemote(export_channel(service, blobs, tmp_path / "usb"))
    release = remote.resolve("app-demo", "production")
    assert release is not None
    with remote.open_artifact(release) as source:
        assert source.read() == b"package"
