"""manifest.py — provision.json 讀寫與 big-deps 引用計數（SPEC §5.1）。"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from conftest import make_wheel
from provision_builder import PROVISION_FORMAT_VERSION
from provision_builder.manifest import (
    build_provision_manifest,
    collect_big_deps,
    git_info,
    read_provision_manifest,
    write_provision_manifest,
)


def _minimal(project_root: Path) -> dict:
    return build_provision_manifest(
        project_root=project_root,
        target={"platform_tag": "win_amd64", "python_version": "3.11", "abi": "cp311"},
        scanned_roots=["scripts/*/plugin.yaml"],
        big_threshold_mb=100,
        tools=[{"tool_id": "t1", "requires": ["numpy"], "wheel_count": 2,
                "total_bytes": 300, "big_wheels": []}],
        big_deps=[],
        skipped_tools=[{"tool_id": "t2", "reason": "no requires"}],
        failed_tools=[],
        created_at="2026-07-10T00:00:00Z",
    )


def test_manifest_has_spec_fields(tmp_path: Path):
    manifest = _minimal(tmp_path)
    expected = {
        "format_version", "builder_version", "created_at", "source_project", "git",
        "target", "scanned_roots", "big_threshold_mb", "tools", "big_deps",
        "skipped_tools", "failed_tools",
    }
    assert set(manifest) == expected
    assert manifest["format_version"] == PROVISION_FORMAT_VERSION
    assert manifest["source_project"] == str(tmp_path.resolve())


def test_round_trip(tmp_path: Path):
    manifest = _minimal(tmp_path)
    write_provision_manifest(manifest, tmp_path)
    assert read_provision_manifest(tmp_path) == manifest


def test_read_missing_or_broken_returns_none(tmp_path: Path):
    assert read_provision_manifest(tmp_path) is None
    (tmp_path / "provision.json").write_text("{ not json", encoding="utf-8")
    assert read_provision_manifest(tmp_path) is None


def test_written_json_is_utf8_and_readable(tmp_path: Path):
    manifest = _minimal(tmp_path)
    manifest["failed_tools"] = [{"tool_id": "t9", "reason": "找不到相依套件"}]
    path = write_provision_manifest(manifest, tmp_path)
    loaded = json.loads(path.read_text(encoding="utf-8"))
    assert loaded["failed_tools"][0]["reason"] == "找不到相依套件"


def test_git_info_on_non_git_dir(tmp_path: Path):
    info = git_info(tmp_path)
    assert info["platform_commit"] is None
    assert info["submodules"] == {}


def test_git_info_on_real_repo(tmp_path: Path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.t"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    (tmp_path / "f.txt").write_text("x", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=tmp_path, check=True)

    info = git_info(tmp_path)
    assert info["platform_commit"] and len(info["platform_commit"]) == 40
    assert info["submodules"] == {}


# ── big-deps 引用計數 ──────────────────────────────────────────────────────────

def test_collect_big_deps_counts_shared_users(tmp_path: Path):
    big_deps = tmp_path / "big-deps"
    make_wheel(big_deps / "torch.whl")
    make_wheel(big_deps / "cuda.whl")

    tools = [
        {"tool_id": "t1", "big_wheels": ["torch.whl", "cuda.whl"]},
        {"tool_id": "t2", "big_wheels": ["torch.whl"]},
    ]
    entries = collect_big_deps(tools, big_deps)

    by_name = {e["name"]: e for e in entries}
    assert by_name["torch.whl"]["used_by"] == ["t1", "t2"]
    assert by_name["cuda.whl"]["used_by"] == ["t1"]
    assert all(e["sha256"] and e["size"] > 0 for e in entries)


def test_collect_big_deps_records_absent_file_without_crashing(tmp_path: Path):
    """使用者把 big-deps 搬走後重跑 build（--tools 模式）也不能爆。"""
    entries = collect_big_deps([{"tool_id": "t1", "big_wheels": ["gone.whl"]}], tmp_path / "big-deps")
    assert entries == [{"name": "gone.whl", "sha256": "", "size": 0, "used_by": ["t1"]}]


def test_collect_big_deps_empty(tmp_path: Path):
    assert collect_big_deps([{"tool_id": "t1", "big_wheels": []}], tmp_path) == []
