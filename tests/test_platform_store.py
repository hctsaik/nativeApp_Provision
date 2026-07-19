"""B-2 phase 1：CIM 平台 Store 化（platform_store + platform_launcher）。

驗的契約：
- 平台打包成 cim-platform .napp：engine 樹進 payload（衍生物濾掉）、
  殼以 blob 旅行（不進 .napp）、shell.blobref.json 指標留在 payload。
- 完整鏈：build_platform_napp → P0 release → FileChannelRemote →
  NativeAgent.update →（不可變版本 + 原子切換 + 回滾語意全部繼承 Agent）→
  launcher 依 start.bat 契約解析出殼/cwd/env。
- 更新換版後，內建專案的 data 目錄**不變**（data 與版本分離）。
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from native_agent import NativeAgent
from native_agent.agent import UPDATED
from native_agent.file_remote import FileChannelRemote
from native_agent.platform_launcher import LaunchError, project_key, resolve_launch
from provision_builder.blob_store import FileBlobStore
from provision_builder.platform_store import PlatformPackError, build_platform_napp
from provision_builder.release_pipeline import build_release

SHELL_BYTES = b"MZ-fake-tauri-shell " * 500


def make_platform(tmp_path: Path, marker: str) -> Path:
    root = tmp_path / f"platform-{marker}"
    engine = root / "sidecar" / "python-engine"
    (engine / "core").mkdir(parents=True)
    (engine / "engine.py").write_text(f"MARKER = '{marker}'\n", encoding="utf-8")
    (engine / "core" / "forms.py").write_text("X = 1\n", encoding="utf-8")
    (engine / "config").mkdir()
    (engine / "config" / "seed.yaml").write_text("tools: []\n", encoding="utf-8")
    # 衍生物：全部不該進 payload
    (engine / "__pycache__").mkdir()
    (engine / "__pycache__" / "engine.cpython-311.pyc").write_bytes(b"x")
    (engine / ".tool-venvs" / "v1").mkdir(parents=True)
    (engine / ".tool-venvs" / "v1" / "pyvenv.cfg").write_text("x", encoding="utf-8")
    (engine / "logs").mkdir()
    (engine / "logs" / "engine.log").write_text("old", encoding="utf-8")
    (engine / "data").mkdir()
    (engine / "data" / "tools.sqlite").write_bytes(b"sqlite")
    shell_dir = root / "apps" / "host-tauri" / "prebuilt"
    shell_dir.mkdir(parents=True)
    (shell_dir / "cim-light.exe").write_bytes(SHELL_BYTES)
    return root


# ---------------------------------------------------------------------------
# 打包
# ---------------------------------------------------------------------------

def test_build_filters_derivatives_and_offloads_shell(tmp_path: Path) -> None:
    blobs = FileBlobStore(tmp_path / "blobs")
    platform = make_platform(tmp_path, "v1")
    result = build_platform_napp(platform, "1.0.0", tmp_path / "p.napp", blob_store=blobs)

    with zipfile.ZipFile(result.path) as zf:
        names = set(zf.namelist())
    assert "application/engine/engine.py" in names
    assert "application/engine/core/forms.py" in names
    assert "application/shell.blobref.json" in names
    assert not any("__pycache__" in n or ".tool-venvs" in n or "tools.sqlite" in n
                   or n.endswith(".log") for n in names)
    assert not any(n.endswith("cim-light.exe") for n in names)  # 殼不進 .napp
    assert len(result.blob_references) == 1
    assert blobs.has(result.blob_references[0]["sha256"])       # 殼在 blob store
    assert result.package["app_id"] == "cim-platform"


def test_missing_engine_and_shell_are_actionable(tmp_path: Path) -> None:
    blobs = FileBlobStore(tmp_path / "blobs")
    with pytest.raises(PlatformPackError, match="不是 CIM 平台專案"):
        build_platform_napp(tmp_path / "empty", "1.0.0", tmp_path / "p.napp", blob_store=blobs)
    platform = make_platform(tmp_path, "v1")
    (platform / "apps" / "host-tauri" / "prebuilt" / "cim-light.exe").unlink()
    with pytest.raises(PlatformPackError, match="build-shell.bat"):
        build_platform_napp(platform, "1.0.0", tmp_path / "p.napp", blob_store=blobs)


def test_project_key_matches_start_bat_rule(tmp_path: Path) -> None:
    project = tmp_path / "My Project(測試)"
    project.mkdir()
    key = project_key(project)
    name, _, digest = key.rpartition("-")
    assert name == "My_Project_測試_" and len(digest) == 8
    assert key == project_key(project)  # 穩定


# ---------------------------------------------------------------------------
# 完整鏈：napp → P0 release → Agent → launcher
# ---------------------------------------------------------------------------

def _publish_platform(tmp_path: Path, marker: str, version: str, release_id: str) -> Path:
    blobs = FileBlobStore(tmp_path / "build-blobs")
    platform = make_platform(tmp_path, marker)
    napp = build_platform_napp(platform, version, tmp_path / f"cim-platform-{version}.napp",
                               blob_store=blobs)
    release = build_release(tmp_path / "releases", [napp.path], channel="internal",
                            release_id=release_id, blob_root=tmp_path / "build-blobs")
    return release.path


def _agent_for(tmp_path: Path, release_dir: Path) -> NativeAgent:
    remote = FileChannelRemote(release_dir / "offline-channel")
    return NativeAgent(tmp_path / "device", remote, remote.blobs)


def test_release_to_agent_to_launcher_chain(tmp_path: Path) -> None:
    release1 = _publish_platform(tmp_path, "v1", "1.0.0", "r1")
    agent = _agent_for(tmp_path, release1)
    outcome = agent.update("cim-platform", "internal")
    assert outcome.state == UPDATED and outcome.blobs_pulled == 1

    plan = resolve_launch(tmp_path / "device")
    assert plan["version"] == "1.0.0"
    engine_py = Path(plan["env"]["CIM_ENGINE_EXE"])
    assert "MARKER = 'v1'" in engine_py.read_text(encoding="utf-8")
    shell = Path(plan["shell"])
    assert shell.read_bytes() == SHELL_BYTES        # blob materialize 後位元組正確
    assert shell.parent.parent.name == "shells"     # deps/shells/<sha>/cim-light.exe
    data_dir = Path(plan["cwd"])
    assert data_dir.name == "engine-default"
    for sub in ("logs", "tool-venvs", "deppack-cache", "wheel-store"):
        assert (data_dir / sub).is_dir()
    for key in ("CIM_TOOL_VENVS_DIR", "CIM_DEPPACK_CACHE", "CIM_WHEEL_STORE", "CIM_LOG_DIR"):
        assert plan["env"][key].startswith(str(data_dir))
    assert plan["env"]["PYTHONUTF8"] == "1"


def test_update_flips_version_but_keeps_data_dir(tmp_path: Path) -> None:
    release1 = _publish_platform(tmp_path, "v1", "1.0.0", "r1")
    agent = _agent_for(tmp_path, release1)
    agent.update("cim-platform", "internal")
    plan1 = resolve_launch(tmp_path / "device")
    (Path(plan1["cwd"]) / "logs" / "user.log").write_text("珍貴的現場紀錄", encoding="utf-8")

    release2 = _publish_platform(tmp_path, "v2", "1.1.0", "r2")
    agent2 = _agent_for(tmp_path, release2)          # 換新 release 當 update source
    outcome = agent2.update("cim-platform", "internal")
    assert outcome.state == UPDATED
    assert outcome.blobs_reused == 1                 # 殼沒變 → blob 重用，零重複下載

    plan2 = resolve_launch(tmp_path / "device")
    assert plan2["version"] == "1.1.0"
    assert "MARKER = 'v2'" in Path(plan2["env"]["CIM_ENGINE_EXE"]).read_text(encoding="utf-8")
    # data 與版本分離：換版後同一個 data 目錄、使用者資料還在
    assert plan2["cwd"] == plan1["cwd"]
    assert (Path(plan2["cwd"]) / "logs" / "user.log").read_text(encoding="utf-8") == "珍貴的現場紀錄"
    # 舊版本仍在（agent 的 LKG/回滾語意管它），版本目錄互不覆蓋
    versions = tmp_path / "device" / "applications" / "cim-platform" / "versions"
    assert (versions / "1.0.0").is_dir() and (versions / "1.1.0").is_dir()


def test_external_project_gets_its_own_stable_data(tmp_path: Path) -> None:
    release1 = _publish_platform(tmp_path, "v1", "1.0.0", "r1")
    agent = _agent_for(tmp_path, release1)
    agent.update("cim-platform", "internal")

    external = tmp_path / "my-external-project"
    external.mkdir()
    (external / "engine.py").write_text("MARKER = 'external'\n", encoding="utf-8")
    plan = resolve_launch(tmp_path / "device", project_dir=external)
    assert plan["project_key"] == project_key(external)
    assert plan["env"]["CIM_ENGINE_EXE"] == str(external / "engine.py")
    # 與內建專案的 data 隔離
    default_plan = resolve_launch(tmp_path / "device")
    assert plan["cwd"] != default_plan["cwd"]


def test_launcher_before_any_install_is_actionable(tmp_path: Path) -> None:
    (tmp_path / "device").mkdir()
    with pytest.raises(LaunchError, match="還沒有啟用中的平台版本"):
        resolve_launch(tmp_path / "device")


def test_cli_pack_platform_then_release(tmp_path: Path) -> None:
    from conftest import run_python

    cli = Path(__file__).resolve().parents[1] / "release.py"
    platform = make_platform(tmp_path, "cli")
    napp = tmp_path / "cim-platform-9.9.9.napp"

    packed = run_python([str(cli), "pack-platform", str(platform), "--version", "9.9.9",
                         "--out", str(napp), "--blobs", str(tmp_path / "blobs")])
    assert packed.returncode == 0, packed.stdout + packed.stderr
    built = run_python([str(cli), "build", "--out", str(tmp_path / "rel"),
                        "--napp", str(napp), "--blobs", str(tmp_path / "blobs"),
                        "--release-id", "r-cli"])
    assert built.returncode == 0, built.stdout + built.stderr
    ok = run_python([str(cli), "verify", str(tmp_path / "rel" / "r-cli")])
    assert ok.returncode == 0 and "可出貨" in ok.stdout
