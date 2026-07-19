"""P1（全域 runtime store）與 P2（續傳、deferred GC、鎖檔韌性）的 Agent 行為。

- P1：同指紋 runtime 跨 App 共用一份；GC 的 keep-set 跨所有 App 計算；
  收斂前的 per-app venv 仍可續用（read-only fallback）。
- P2：下載中斷後 .part 續傳（seek 或跳讀），壞的 .part 只重試一次乾淨下載；
  GC 刪不掉的樹記入 deferred-gc.json、下次重試，不靜默謊報已回收。
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from native_agent import NativeAgent
from native_agent.agent import UPDATED
from provision_builder import winfs
from provision_builder.blob_store import FileBlobStore
from provision_builder.napp import AppManifest, DevHmacSigner, build_napp, read_package_json
from provision_builder.package_errors import HashMismatch
from provision_builder.package_services import FileObjectStore, PackageService, SQLiteRegistry

SIGNER = DevHmacSigner()


@pytest.fixture
def remote(tmp_path: Path):
    service = PackageService(SQLiteRegistry(tmp_path / "reg.db"), FileObjectStore(tmp_path / "obj"))
    return service, FileBlobStore(tmp_path / "remote_blobs")


def publish(remote, tmp_path: Path, app_id: str, version: str, *,
            requires=("numpy==1.26.0",)) -> Path:
    service, _ = remote
    src = tmp_path / f"src-{app_id}-{version}"
    src.mkdir(exist_ok=True)
    (src / "app.py").write_bytes(b"# " + app_id.encode() + b" " + version.encode())
    manifest = AppManifest.from_dict(
        {"id": app_id, "version": version, "entrypoint": "app:main", "requires": list(requires)}
    )
    out = tmp_path / f"{app_id}-{version}.napp"
    build_napp(manifest, src, out, signer=SIGNER)
    service.publish(app_id, version, out)
    service.promote(app_id, "production", version)
    return out


def _agent(tmp_path: Path, remote, **kw) -> NativeAgent:
    service, blobs = remote
    return NativeAgent(tmp_path / "device", service, blobs, verifier=SIGNER, **kw)


def _runtime_dirs(agent: NativeAgent) -> list[str]:
    root = agent._runtimes_dir()
    return sorted(p.name for p in root.iterdir()
                  if p.is_dir() and not p.name.startswith(".staging-")) if root.is_dir() else []


# ---------------------------------------------------------------------------
# P1：全域 runtime store
# ---------------------------------------------------------------------------

def test_runtime_shared_across_apps(remote, tmp_path: Path) -> None:
    agent = _agent(tmp_path, remote)
    publish(remote, tmp_path, "app-a", "1.0.0")
    publish(remote, tmp_path, "app-b", "1.0.0")  # 同 requires → 同指紋

    first = agent.update("app-a", "production")
    second = agent.update("app-b", "production")
    assert first.state == UPDATED and second.state == UPDATED
    assert first.venv_reused is False
    assert second.venv_reused is True          # 第二個 App 直接共用
    assert len(_runtime_dirs(agent)) == 1      # 磁碟上只有一份


def test_gc_keeps_runtime_referenced_by_another_app(remote, tmp_path: Path) -> None:
    agent = _agent(tmp_path, remote)
    publish(remote, tmp_path, "app-a", "1.0.0", requires=("numpy==1.26.0",))
    publish(remote, tmp_path, "app-b", "1.0.0", requires=("numpy==1.26.0",))
    agent.update("app-a", "production")
    agent.update("app-b", "production")
    fp_shared = agent._version_fingerprint("app-a", "1.0.0")

    # app-b 換到新的相依集，舊版本被它自己的 gc 清掉
    publish(remote, tmp_path, "app-b", "2.0.0", requires=("pandas==2.2.0",))
    agent.update("app-b", "production")
    result = agent.gc("app-b")
    assert "1.0.0" in result["removed_versions"]
    # 共用指紋仍被 app-a 引用 → 不得回收
    assert fp_shared not in result["removed_runtimes"]
    assert agent.runtime_dir(fp_shared).is_dir()

    # app-a 也離開該指紋後，才真正回收
    publish(remote, tmp_path, "app-a", "2.0.0", requires=("pandas==2.2.0",))
    agent.update("app-a", "production")
    result = agent.gc("app-a")
    assert fp_shared in result["removed_runtimes"]
    assert not agent.runtime_dir(fp_shared).exists()


def test_legacy_per_app_venv_is_reused(remote, tmp_path: Path) -> None:
    napp = publish(remote, tmp_path, "app-a", "1.0.0")
    fingerprint = read_package_json(napp)["dependency_fingerprint"]
    agent = _agent(tmp_path, remote)
    legacy = agent._legacy_venv_dir("app-a", fingerprint)
    legacy.mkdir(parents=True)
    (legacy / ".complete").write_text("{}", encoding="utf-8")

    outcome = agent.update("app-a", "production")
    assert outcome.state == UPDATED and outcome.venv_reused is True
    assert not agent.runtime_dir(fingerprint).exists()  # 不重複建全域份
    assert legacy.is_dir()


def test_lost_creation_race_adopts_winner(remote, tmp_path: Path, monkeypatch) -> None:
    napp = publish(remote, tmp_path, "app-a", "1.0.0")
    fingerprint = read_package_json(napp)["dependency_fingerprint"]
    agent = _agent(tmp_path, remote)
    agent._ensure_layout("app-a")
    winner = agent.runtime_dir(fingerprint)

    real_rename = winfs.robust_rename

    def lose_race(src, dst, **kw):
        if Path(dst) == winner and not winner.exists():
            winner.mkdir(parents=True)
            (winner / ".complete").write_text("{}", encoding="utf-8")
            raise FileExistsError(str(dst))
        return real_rename(src, dst, **kw)

    monkeypatch.setattr("native_agent.agent.winfs.robust_rename", lose_race)
    reused = agent._prepare_venv("app-a", fingerprint)
    assert reused is False                     # 自己有建，只是輸了換位
    assert (winner / ".complete").is_file()
    assert not list(agent._runtimes_dir().glob(".staging-*"))  # staging 清乾淨


# ---------------------------------------------------------------------------
# P2：下載續傳
# ---------------------------------------------------------------------------

class _SeekTrackingSource(io.BytesIO):
    def __init__(self, data: bytes):
        super().__init__(data)
        self.seeked_to: int | None = None

    def seek(self, offset: int, whence: int = 0):
        self.seeked_to = offset
        return super().seek(offset, whence)


def test_download_resumes_from_part(remote, tmp_path: Path) -> None:
    service, _ = remote
    publish(remote, tmp_path, "app-a", "1.0.0")
    release = service.resolve("app-a", "production")
    agent = _agent(tmp_path, remote)
    agent._ensure_layout("app-a")

    full = service.open_artifact(release).read()
    offset = len(full) // 2
    part = agent._app_dir("app-a") / "staging" / "1.0.0.napp.part"
    part.parent.mkdir(parents=True, exist_ok=True)
    part.write_bytes(full[:offset])            # 上次中斷留下的前半

    source = _SeekTrackingSource(full)
    agent.remote = _FixedArtifactRemote(service, source)
    dest = agent._download("app-a", release)
    assert source.seeked_to == offset          # 真的從斷點續傳，不是重抓整包
    assert dest.read_bytes() == full
    assert not part.exists()


def test_stale_part_triggers_one_clean_retry(remote, tmp_path: Path) -> None:
    service, _ = remote
    publish(remote, tmp_path, "app-a", "1.0.0")
    release = service.resolve("app-a", "production")
    agent = _agent(tmp_path, remote)
    agent._ensure_layout("app-a")

    part = agent._app_dir("app-a") / "staging" / "1.0.0.napp.part"
    part.parent.mkdir(parents=True, exist_ok=True)
    part.write_bytes(b"garbage from another life")  # 內容錯的殘檔

    dest = agent._download("app-a", release)   # 應清掉殘檔、重抓一次成功
    assert dest.is_file()
    from provision_builder._util import sha256_file
    digest, _ = sha256_file(dest)
    assert digest == release.sha256


def test_download_gives_up_after_second_mismatch(remote, tmp_path: Path, monkeypatch) -> None:
    service, _ = remote
    publish(remote, tmp_path, "app-a", "1.0.0")
    release = service.resolve("app-a", "production")
    agent = _agent(tmp_path, remote)
    agent._ensure_layout("app-a")
    agent.remote = _FixedArtifactRemote(service, io.BytesIO(b"corrupted artifact bytes"))
    with pytest.raises(HashMismatch):
        agent._download("app-a", release)


class _FixedArtifactRemote:
    """open_artifact 永遠回傳同一個(可重讀的)資料流，其餘轉呼叫真 service。"""

    def __init__(self, service, source):
        self._service = service
        self._source = source

    def open_artifact(self, release):
        self._source.seek(0)
        return _NonClosing(self._source)

    def __getattr__(self, name):
        return getattr(self._service, name)


class _NonClosing:
    def __init__(self, inner):
        self._inner = inner

    def __enter__(self):
        return self._inner

    def __exit__(self, *exc):
        return False


# ---------------------------------------------------------------------------
# P2：deferred GC
# ---------------------------------------------------------------------------

def test_gc_defers_locked_tree_and_clears_next_run(remote, tmp_path: Path, monkeypatch) -> None:
    agent = _agent(tmp_path, remote)
    publish(remote, tmp_path, "app-a", "1.0.0")
    agent.update("app-a", "production")
    publish(remote, tmp_path, "app-a", "2.0.0")
    agent.update("app-a", "production")
    doomed = agent._versions_dir("app-a") / "1.0.0"
    assert doomed.is_dir()

    real = winfs.robust_rmtree

    def locked(path, **kw):
        if Path(path) == doomed:
            return False                       # 模擬防毒/開啟中的 handle
        return real(path, **kw)

    monkeypatch.setattr("native_agent.agent.winfs.robust_rmtree", locked)
    result = agent.gc("app-a")
    assert str(doomed) in result["deferred"]
    assert "1.0.0" not in result["removed_versions"]   # 沒刪成就不謊報
    assert doomed.is_dir()
    assert str(doomed) in agent._read_deferred()

    monkeypatch.setattr("native_agent.agent.winfs.robust_rmtree", real)
    result = agent.gc("app-a")                 # 下一輪開頭先重試 deferred
    assert str(doomed) in result["deferred_cleared"]
    assert not doomed.exists()
    assert agent._read_deferred() == []
