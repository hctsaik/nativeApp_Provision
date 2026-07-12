"""build.py — 增量判斷、失敗續行、產出完整性（SPEC §4.1、§8.1）。

全部用假 gateway：**不連網、不跑 pip**。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from conftest import add_plugin_yaml, make_pack
from provision_builder import bigdeps
from provision_builder.build import (
    REBUILD,
    REUSE,
    build_one,
    decide_action,
    run_build,
)
from provision_builder.gateway import GatewayError, Target
from provision_builder.scan import ToolSpec

# 2 MB：讓 threshold_mb=1（= 1 MB）真的能把它隔離出去
BIG = b"x" * (2 * 1024 * 1024)
SMALL = b"y" * 10


class FakeGateway:
    """行為像 PlatformGateway，但 build_wheelhouse 只是造假 wheel（不連網）。"""

    def __init__(self, project_root: Path, wheels: dict[str, bytes] | None = None,
                 fingerprints: dict[str, str] | None = None) -> None:
        self.project_root = Path(project_root).resolve()
        self.engine_root = self.project_root / "sidecar" / "python-engine"
        self.python_cmd = [sys.executable]
        self._wheels = wheels or {"tiny-1.0.whl": SMALL, "torch-2.6.0.whl": BIG}
        self._fingerprints = fingerprints or {}
        self.built: list[str] = []
        self.fail_on: set[str] = set()

    def requires_fingerprint(self, requires: list[str]) -> str:
        return self._fingerprints.get(",".join(sorted(requires)), "fp:" + ",".join(sorted(requires)))

    def build_wheelhouse(self, tool_id, requires, dest_root, target) -> dict:
        if tool_id in self.fail_on:
            raise GatewayError(f"{tool_id}：pip download 失敗：no matching distribution")
        self.built.append(tool_id)
        return make_pack(
            Path(dest_root), tool_id, self._wheels,
            requires=requires,
            fingerprint=self.requires_fingerprint(requires),
            python_tag=target.python_tag, platform_tag=target.platform_tag,
        )

    def load_manifest(self, path: Path) -> dict:
        try:
            return json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise GatewayError(str(exc)) from exc


@pytest.fixture
def tool() -> ToolSpec:
    return ToolSpec(tool_id="t1", requires=["torch==2.6.0"], yaml_path=Path("plugin.yaml"))


# ── decide_action（SPEC §8.1）──────────────────────────────────────────────────

def test_rebuild_when_no_pack(tmp_path: Path, tool: ToolSpec):
    gw = FakeGateway(tmp_path)
    entry = decide_action(tool, tmp_path / "packs", tmp_path / "big-deps", gw, Target())
    assert entry.action == REBUILD and "尚未產包" in entry.reason


def test_reuse_when_everything_matches(tmp_path: Path, tool: ToolSpec):
    gw = FakeGateway(tmp_path, wheels={"tiny-1.0.whl": SMALL})
    packs, big = tmp_path / "packs", tmp_path / "big-deps"
    gw.build_wheelhouse(tool.tool_id, tool.requires, packs, Target())

    entry = decide_action(tool, packs, big, gw, Target())
    assert entry.action == REUSE


def test_rebuild_when_requires_changed(tmp_path: Path, tool: ToolSpec):
    gw = FakeGateway(tmp_path, wheels={"tiny-1.0.whl": SMALL})
    packs, big = tmp_path / "packs", tmp_path / "big-deps"
    gw.build_wheelhouse(tool.tool_id, tool.requires, packs, Target())

    changed = ToolSpec(tool_id="t1", requires=["torch==2.7.0"], yaml_path=tool.yaml_path)
    entry = decide_action(changed, packs, big, gw, Target())
    assert entry.action == REBUILD and "requires 已變更" in entry.reason


def test_rebuild_when_python_tag_changed(tmp_path: Path, tool: ToolSpec):
    """cp314 事故的守門：目標 ABI 變了，舊包一律作廢。"""
    gw = FakeGateway(tmp_path, wheels={"tiny-1.0.whl": SMALL})
    packs, big = tmp_path / "packs", tmp_path / "big-deps"
    gw.build_wheelhouse(tool.tool_id, tool.requires, packs, Target())

    entry = decide_action(tool, packs, big, gw, Target(python_version="3.12", abi="cp312"))
    assert entry.action == REBUILD and "python 標籤不符" in entry.reason


def test_rebuild_when_platform_tag_changed(tmp_path: Path, tool: ToolSpec):
    gw = FakeGateway(tmp_path, wheels={"tiny-1.0.whl": SMALL})
    packs, big = tmp_path / "packs", tmp_path / "big-deps"
    gw.build_wheelhouse(tool.tool_id, tool.requires, packs, Target())
    entry = decide_action(tool, packs, big, gw, Target(platform_tag="manylinux2014_x86_64"))
    assert entry.action == REBUILD and "平台標籤不符" in entry.reason


def test_rebuild_when_wheel_corrupted(tmp_path: Path, tool: ToolSpec):
    gw = FakeGateway(tmp_path, wheels={"tiny-1.0.whl": SMALL})
    packs, big = tmp_path / "packs", tmp_path / "big-deps"
    gw.build_wheelhouse(tool.tool_id, tool.requires, packs, Target())
    target_wheel = packs / "t1" / "wheels" / "tiny-1.0.whl"
    data = bytearray(target_wheel.read_bytes())
    data[-1] ^= 0xFF
    target_wheel.write_bytes(bytes(data))

    entry = decide_action(tool, packs, big, gw, Target())
    assert entry.action == REBUILD and "驗證未過" in entry.reason


def test_reuse_when_big_wheel_lives_in_big_deps(tmp_path: Path, tool: ToolSpec):
    """隔離過的包（wheels/ 少了 torch）仍該被判為完整——這是搬運期的正常形狀。"""
    gw = FakeGateway(tmp_path)
    packs, big = tmp_path / "packs", tmp_path / "big-deps"
    gw.build_wheelhouse(tool.tool_id, tool.requires, packs, Target())
    bigdeps.isolate_pack(packs / "t1", big, 1000)

    entry = decide_action(tool, packs, big, gw, Target())
    assert entry.action == REUSE


def test_force_always_rebuilds(tmp_path: Path, tool: ToolSpec):
    gw = FakeGateway(tmp_path, wheels={"tiny-1.0.whl": SMALL})
    packs, big = tmp_path / "packs", tmp_path / "big-deps"
    gw.build_wheelhouse(tool.tool_id, tool.requires, packs, Target())
    entry = decide_action(tool, packs, big, gw, Target(), force=True)
    assert entry.action == REBUILD and entry.reason == "--force"


def test_shallow_check_skips_hashing(tmp_path: Path, tool: ToolSpec):
    """dry-run 用 deep=False：破壞內容也看不出來（但檔案在，所以判 reuse）。"""
    gw = FakeGateway(tmp_path, wheels={"tiny-1.0.whl": SMALL})
    packs, big = tmp_path / "packs", tmp_path / "big-deps"
    gw.build_wheelhouse(tool.tool_id, tool.requires, packs, Target())
    wheel = packs / "t1" / "wheels" / "tiny-1.0.whl"
    data = bytearray(wheel.read_bytes())
    data[-1] ^= 0xFF
    wheel.write_bytes(bytes(data))

    assert decide_action(tool, packs, big, gw, Target(), deep=False).action == REUSE
    assert decide_action(tool, packs, big, gw, Target(), deep=True).action == REBUILD


# ── build_one ─────────────────────────────────────────────────────────────────

def test_build_one_isolates_big_wheels(tmp_path: Path, tool: ToolSpec, monkeypatch):
    monkeypatch.setattr("provision_builder.build.offline_resolve", lambda *a, **k: (True, ""))
    gw = FakeGateway(tmp_path)
    packs, big = tmp_path / "packs", tmp_path / "big-deps"

    entry = build_one(tool, packs, big, gw, Target(), 1000, prev_big_deps=[])

    assert entry["tool_id"] == "t1"
    assert entry["wheel_count"] == 2
    assert entry["big_wheels"] == ["torch-2.6.0.whl"]
    assert (big / "torch-2.6.0.whl").is_file()
    assert not (packs / "t1" / "wheels" / "torch-2.6.0.whl").exists()
    # total_bytes 涵蓋兩個 wheel（不管它們住哪）
    assert entry["total_bytes"] > 5000


def test_build_one_selfcheck_gets_big_deps_in_find_links(tmp_path: Path, tool: ToolSpec, monkeypatch):
    captured: dict = {}

    def fake_resolve(python_cmd, requires, find_links, target, **kw):
        captured["find_links"] = [str(p) for p in find_links]
        return True, ""

    monkeypatch.setattr("provision_builder.build.offline_resolve", fake_resolve)
    gw = FakeGateway(tmp_path)
    build_one(tool, tmp_path / "packs", tmp_path / "big-deps", gw, Target(), 1000, prev_big_deps=[])

    assert len(captured["find_links"]) == 2
    assert captured["find_links"][1].endswith("big-deps")


def test_build_one_selfcheck_failure_leaves_no_half_pack(tmp_path: Path, tool: ToolSpec, monkeypatch):
    monkeypatch.setattr("provision_builder.build.offline_resolve",
                        lambda *a, **k: (False, "ERROR: no wheel for ghostpkg"))
    gw = FakeGateway(tmp_path)
    packs = tmp_path / "packs"

    with pytest.raises(GatewayError, match="自檢失敗"):
        build_one(tool, packs, tmp_path / "big-deps", gw, Target(), 1000, prev_big_deps=[])
    assert not (packs / "t1").exists()


def test_build_one_pip_failure_leaves_no_half_pack(tmp_path: Path, tool: ToolSpec, monkeypatch):
    monkeypatch.setattr("provision_builder.build.offline_resolve", lambda *a, **k: (True, ""))
    gw = FakeGateway(tmp_path)
    gw.fail_on = {"t1"}
    packs = tmp_path / "packs"
    with pytest.raises(GatewayError):
        build_one(tool, packs, tmp_path / "big-deps", gw, Target(), 1000, prev_big_deps=[])
    assert not (packs / "t1").exists()


def test_build_one_wipes_stale_wheels_before_download(tmp_path: Path, tool: ToolSpec, monkeypatch):
    """舊 wheel 留在目錄裡會被 compute_manifest 一起簽進 manifest。"""
    monkeypatch.setattr("provision_builder.build.offline_resolve", lambda *a, **k: (True, ""))
    gw = FakeGateway(tmp_path, wheels={"tiny-1.0.whl": SMALL})
    packs = tmp_path / "packs"
    (packs / "t1" / "wheels").mkdir(parents=True)
    (packs / "t1" / "wheels" / "stale-0.1.whl").write_bytes(b"old")

    build_one(tool, packs, tmp_path / "big-deps", gw, Target(), 0, prev_big_deps=[])
    assert not (packs / "t1" / "wheels" / "stale-0.1.whl").exists()


def test_build_one_releases_exclusive_big_wheel_but_not_shared(tmp_path: Path, tool: ToolSpec, monkeypatch):
    monkeypatch.setattr("provision_builder.build.offline_resolve", lambda *a, **k: (True, ""))
    gw = FakeGateway(tmp_path, wheels={"tiny-1.0.whl": SMALL})
    big = tmp_path / "big-deps"
    big.mkdir()
    (big / "exclusive.whl").write_bytes(b"e")
    (big / "shared.whl").write_bytes(b"s")

    prev = [
        {"name": "exclusive.whl", "used_by": ["t1"]},
        {"name": "shared.whl", "used_by": ["t1", "t2"]},
    ]
    build_one(tool, tmp_path / "packs", big, gw, Target(), 0, prev_big_deps=prev)

    assert not (big / "exclusive.whl").exists()
    assert (big / "shared.whl").is_file()


# ── run_build 端到端（假 gateway）─────────────────────────────────────────────

def _fake_project_with_tools(tmp_path: Path, tools: dict[str, list[str]]) -> Path:
    engine = tmp_path / "proj" / "sidecar" / "python-engine"
    (engine / "scripts").mkdir(parents=True)
    (engine / "plugins").mkdir(parents=True)
    (engine / "engine.py").write_text("#", encoding="utf-8")
    for tool_id, requires in tools.items():
        req = "\n".join(f"  - {r}" for r in requires)
        body = f"id: {tool_id}\n" + (f"requires:\n{req}\n" if requires else "")
        add_plugin_yaml(engine, f"scripts/{tool_id}/plugin.yaml", body)
    return tmp_path / "proj"


@pytest.fixture
def patched_build(monkeypatch, tmp_path):
    """把 run_build 內用到的 PlatformGateway / offline_resolve 換成假的。"""
    created: dict = {}

    def fake_gateway_factory(project_root, python_cmd=None):
        gw = FakeGateway(project_root)
        created["gateway"] = gw
        return gw

    monkeypatch.setattr("provision_builder.build.PlatformGateway", fake_gateway_factory)
    monkeypatch.setattr("provision_builder.build.offline_resolve", lambda *a, **k: (True, ""))
    return created


def _silent(*_args, **_kwargs):
    pass


def test_run_build_dry_run_writes_nothing(tmp_path: Path, patched_build):
    pytest.importorskip("yaml")
    project = _fake_project_with_tools(tmp_path, {"t1": ["numpy"], "t2": []})
    dest = tmp_path / "out"

    result = run_build(project, dest, target=Target(), threshold_mb=100,
                       dry_run=True, python_cmd=None, log=_silent)

    assert not dest.exists()
    assert [e.tool.tool_id for e in result.plan] == ["t1"]
    assert result.skipped == [{"tool_id": "t2", "reason": "no requires"}]


def test_run_build_full_output(tmp_path: Path, patched_build):
    pytest.importorskip("yaml")
    project = _fake_project_with_tools(tmp_path, {"t1": ["torch"], "t2": []})
    dest = tmp_path / "out"

    result = run_build(project, dest, target=Target(), threshold_mb=1,
                       python_cmd=None, log=_silent)

    assert result.ok
    assert (dest / "provision.json").is_file()
    assert (dest / "REPORT.md").is_file()
    assert (dest / "apply.py").is_file()
    assert (dest / "packs" / "t1" / "deppack.json").is_file()
    assert (dest / "big-deps" / "torch-2.6.0.whl").is_file()

    provision = json.loads((dest / "provision.json").read_text(encoding="utf-8"))
    assert provision["tools"][0]["tool_id"] == "t1"
    assert provision["big_deps"][0]["used_by"] == ["t1"]
    assert provision["skipped_tools"] == [{"tool_id": "t2", "reason": "no requires"}]
    assert provision["big_threshold_mb"] == 1


def test_run_build_writes_launcher_baking_mode_and_project(tmp_path: Path, patched_build):
    pytest.importorskip("yaml")
    project = _fake_project_with_tools(tmp_path, {"t1": ["torch"]})
    dest = tmp_path / "out"

    run_build(project, dest, target=Target(), threshold_mb=1,
              python_cmd=None, launch_mode="dev", log=_silent)

    bat = dest / "run-platform.bat"
    assert bat.is_file()
    assert (dest / "run-platform.README.txt").is_file()
    raw = bat.read_bytes()
    assert b"\r\n" in raw and raw.isascii()  # cmd.exe 讀得到:CRLF + ASCII
    text = raw.decode("ascii")
    assert 'set "MODE=dev"' in text
    assert f'set "DEV_PROJECT={project.resolve()}"' in text


def test_run_build_launcher_defaults_to_portable(tmp_path: Path, patched_build):
    pytest.importorskip("yaml")
    project = _fake_project_with_tools(tmp_path, {"t1": ["torch"]})
    dest = tmp_path / "out"

    run_build(project, dest, target=Target(), threshold_mb=1, python_cmd=None, log=_silent)

    assert 'set "MODE=portable"' in (dest / "run-platform.bat").read_text(encoding="ascii")


def test_run_build_continues_after_failure_and_reports(tmp_path: Path, patched_build, monkeypatch):
    pytest.importorskip("yaml")
    project = _fake_project_with_tools(tmp_path, {"t1": ["numpy"], "t2": ["ghostpkg"]})
    dest = tmp_path / "out"

    original = FakeGateway.build_wheelhouse

    def selective(self, tool_id, requires, dest_root, target):
        if tool_id == "t1":
            raise GatewayError("t1：pip download 失敗")
        return original(self, tool_id, requires, dest_root, target)

    monkeypatch.setattr(FakeGateway, "build_wheelhouse", selective)

    result = run_build(project, dest, target=Target(), threshold_mb=0,
                       python_cmd=None, log=_silent)

    assert not result.ok
    assert [f["tool_id"] for f in result.failed] == ["t1"]
    assert [t["tool_id"] for t in result.tools] == ["t2"]      # t2 仍然產出
    assert (dest / "packs" / "t2").is_dir()
    assert not (dest / "packs" / "t1").exists()
    assert "產包失敗" in (dest / "REPORT.md").read_text(encoding="utf-8")


def test_run_build_second_run_reuses(tmp_path: Path, patched_build):
    pytest.importorskip("yaml")
    project = _fake_project_with_tools(tmp_path, {"t1": ["numpy"]})
    dest = tmp_path / "out"

    run_build(project, dest, target=Target(), threshold_mb=1, python_cmd=None, log=_silent)
    gw1 = patched_build["gateway"]
    assert gw1.built == ["t1"]

    result = run_build(project, dest, target=Target(), threshold_mb=1, python_cmd=None, log=_silent)
    gw2 = patched_build["gateway"]
    assert gw2.built == []                                     # 秒過，沒有再 pip download
    assert [e.action for e in result.plan] == [REUSE]
    assert result.tools[0]["big_wheels"] == ["torch-2.6.0.whl"]  # 沿用時仍正確歸類


def test_run_build_prunes_orphan_big_deps_on_full_build(tmp_path: Path, patched_build):
    pytest.importorskip("yaml")
    project = _fake_project_with_tools(tmp_path, {"t1": ["numpy"]})
    dest = tmp_path / "out"
    run_build(project, dest, target=Target(), threshold_mb=1, python_cmd=None, log=_silent)

    (dest / "big-deps" / "orphan.whl").write_bytes(b"junk")
    result = run_build(project, dest, target=Target(), threshold_mb=1, python_cmd=None, log=_silent)

    assert result.pruned == ["orphan.whl"]
    assert not (dest / "big-deps" / "orphan.whl").exists()
    assert (dest / "big-deps" / "torch-2.6.0.whl").is_file()


def test_run_build_with_tools_filter_does_not_prune(tmp_path: Path, patched_build):
    """有 --tools 篩選時看不到全部引用關係，不敢刪（SPEC §8.1）。"""
    pytest.importorskip("yaml")
    project = _fake_project_with_tools(tmp_path, {"t1": ["numpy"], "t2": ["scipy"]})
    dest = tmp_path / "out"
    run_build(project, dest, target=Target(), threshold_mb=1, python_cmd=None, log=_silent)

    result = run_build(project, dest, target=Target(), threshold_mb=1,
                       only_tools=["t1"], python_cmd=None, log=_silent)
    assert result.pruned == []
    assert (dest / "big-deps" / "torch-2.6.0.whl").is_file()


def test_run_build_no_tools_with_requires(tmp_path: Path, patched_build):
    pytest.importorskip("yaml")
    project = _fake_project_with_tools(tmp_path, {"t1": []})
    dest = tmp_path / "out"
    result = run_build(project, dest, target=Target(), threshold_mb=100, python_cmd=None, log=_silent)

    assert result.ok and result.tools == []
    assert (dest / "provision.json").is_file()
    assert (dest / "apply.py").is_file()
