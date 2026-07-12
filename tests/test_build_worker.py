from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

import pytest

from build_worker import BuildCancelled, BuildRecordStore, BuildRequest, BuildWorker, run_subprocess
from provision_builder.blob_store import FileBlobStore
from provision_builder.napp import DevHmacSigner, verify_napp
from provision_builder.package_services import FileObjectStore, PackageService, SQLiteRegistry

APP_JSON = {"id": "cv-reviewer", "version": "1.4.2", "entrypoint": "app:main", "requires": ["numpy"]}


@pytest.fixture
def app_source(tmp_path: Path) -> Path:
    root = tmp_path / "src"
    root.mkdir()
    (root / "app.py").write_text("def main():\n    return 'ok'\n", encoding="utf-8")
    return root


@pytest.fixture
def worker(tmp_path: Path) -> BuildWorker:
    service = PackageService(SQLiteRegistry(tmp_path / "r.db"), FileObjectStore(tmp_path / "obj"))
    blobs = FileBlobStore(tmp_path / "blobs")
    return BuildWorker(service, blobs, tmp_path / "jobs", signer=DevHmacSigner(), verifier=DevHmacSigner())


def _request(app_source: Path, **kw) -> BuildRequest:
    return BuildRequest("cv-reviewer", "1.4.2", app_source, APP_JSON, source_commit="c0ffee", **kw)


def test_build_publishes_and_promotes(worker: BuildWorker, app_source: Path) -> None:
    result = worker.run(_request(app_source, channel="production"))
    assert result.status == "succeeded"
    assert result.release is not None and result.release.status == "published"
    assert worker.service.resolve("cv-reviewer", "production").version == "1.4.2"
    verify_napp(result.napp_path, verifier=DevHmacSigner())
    assert result.log_path.is_file()
    assert {"validate", "build", "verify", "publish", "promote", "done"} <= set(_log_steps(result.log_path))


def test_selfcheck_and_healthcheck_hooks_run_in_order(worker: BuildWorker, app_source: Path) -> None:
    calls: list[str] = []
    result = worker.run(_request(
        app_source,
        selfcheck=lambda ws: calls.append("selfcheck"),
        healthcheck=lambda ws: calls.append("healthcheck"),
    ))
    assert result.status == "succeeded"
    assert calls == ["selfcheck", "healthcheck"]


def test_manifest_mismatch_fails_without_publishing(tmp_path: Path, worker: BuildWorker, app_source: Path) -> None:
    bad = BuildRequest("cv-reviewer", "9.9.9", app_source, APP_JSON)  # version disagrees with manifest
    result = worker.run(bad)
    assert result.status == "failed" and "!=" in result.error
    assert worker.service.get_release("cv-reviewer", "9.9.9") is None


def test_selfcheck_failure_aborts_before_publish(worker: BuildWorker, app_source: Path) -> None:
    def boom(ws: Path) -> None:
        raise RuntimeError("offline resolve failed")

    result = worker.run(_request(app_source, selfcheck=boom))
    assert result.status == "failed" and "offline resolve failed" in result.error
    assert worker.service.get_release("cv-reviewer", "1.4.2") is None


def test_cancellation_stops_before_publish(worker: BuildWorker, app_source: Path) -> None:
    cancel = threading.Event()

    def cancel_here(ws: Path) -> None:
        cancel.set()  # cancel observed at the next checkpoint

    result = worker.run(_request(app_source, selfcheck=cancel_here), cancel=cancel)
    assert result.status == "cancelled"
    assert worker.service.get_release("cv-reviewer", "1.4.2") is None


def test_log_is_valid_jsonl(worker: BuildWorker, app_source: Path) -> None:
    result = worker.run(_request(app_source))
    lines = result.log_path.read_text(encoding="utf-8").strip().splitlines()
    for line in lines:
        entry = json.loads(line)
        assert {"ts", "level", "step", "message"} <= set(entry)


def _log_steps(path: Path) -> list[str]:
    return [json.loads(line)["step"] for line in path.read_text(encoding="utf-8").splitlines()]


# ── real subprocess control ─────────────────────────────────────────────────

def test_run_subprocess_returns_exit_code() -> None:
    assert run_subprocess([sys.executable, "-c", "import sys; sys.exit(0)"]) == 0
    assert run_subprocess([sys.executable, "-c", "import sys; sys.exit(3)"]) == 3


def test_run_subprocess_cancel_kills_tree() -> None:
    cancel = threading.Event()
    threading.Timer(0.3, cancel.set).start()
    start = time.monotonic()
    with pytest.raises(BuildCancelled):
        run_subprocess([sys.executable, "-c", "import time; time.sleep(30)"], cancel=cancel)
    assert time.monotonic() - start < 10  # killed promptly, not after 30s


def test_run_subprocess_timeout() -> None:
    with pytest.raises(TimeoutError):
        run_subprocess([sys.executable, "-c", "import time; time.sleep(30)"], timeout=0.3)


# ── build record persistence ────────────────────────────────────────────────

def test_build_records_persist_success_and_failure(tmp_path: Path, app_source: Path) -> None:
    from provision_builder.blob_store import FileBlobStore
    from provision_builder.napp import DevHmacSigner
    from provision_builder.package_services import FileObjectStore, PackageService, SQLiteRegistry

    service = PackageService(SQLiteRegistry(tmp_path / "r.db"), FileObjectStore(tmp_path / "obj"))
    records = BuildRecordStore(tmp_path / "builds.db")
    worker = BuildWorker(service, FileBlobStore(tmp_path / "blobs"), tmp_path / "jobs",
                         signer=DevHmacSigner(), verifier=DevHmacSigner(), records=records)

    worker.run(_request(app_source))                                   # succeeds
    worker.run(BuildRequest("cv-reviewer", "9.9.9", app_source, APP_JSON))  # manifest mismatch → fails

    all_builds = records.list_builds("cv-reviewer")
    by_version = {b.version: b for b in all_builds}
    assert by_version["1.4.2"].status == "succeeded"
    assert by_version["9.9.9"].status == "failed"
    assert all_builds[0].build_id > all_builds[1].build_id  # newest first
    assert by_version["1.4.2"].digest  # succeeded build has a digest
