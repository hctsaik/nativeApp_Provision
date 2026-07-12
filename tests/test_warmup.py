"""warmup.py — 離線機的相依暖機腳本。

它存在的理由是一個實測到的 GUI 行為：Tauri 殼的 HTTP bridge 對 engine 有 30 秒逾時，
而工具首次啟動時 engine 會**同步**安裝相依（torch 級要 76 秒）→ portal 顯示
「Failed to start tool」。先跑 warmup 把安裝成本移開，第一次按 Start 就會成功。

跟 apply.py 不同，warmup 需要平台專案（它借用 core.tool_deps 的驗章 + 離線安裝）。
所以測試用「迷你平台專案」：真平台的 core/ 三個檔 + 一個假的 engine.py。
"""

from __future__ import annotations

import json
import shutil
import textwrap
from pathlib import Path

import pytest

from conftest import make_pack, run_python, write_provision_json

WARMUP_PY = Path(__file__).resolve().parents[1] / "warmup.py"


def _provision(root: Path, tools: dict[str, list[str]]) -> Path:
    packs = root / "packs"
    entries = []
    for tool_id, requires in tools.items():
        manifest = make_pack(packs, tool_id, {"tiny-1.0.whl": b"y" * 10}, requires=requires)
        entries.append({
            "tool_id": tool_id, "requires": requires,
            "wheel_count": len(manifest["wheels"]),
            "total_bytes": sum(w["size"] for w in manifest["wheels"]),
            "big_wheels": [],
        })
    write_provision_json(root, entries, [])
    shutil.copy2(WARMUP_PY, root / "warmup.py")
    return root


def _stub_project(tmp_path: Path, ensure_body: str) -> Path:
    """迷你平台專案：engine.py + core/tool_deps.ensure_tool_deps（可注入行為）。"""
    project = tmp_path / "stub-platform"
    engine = project / "sidecar" / "python-engine"
    (engine / "core").mkdir(parents=True)
    (engine / "engine.py").write_text("# stub\n", encoding="utf-8")
    (engine / "core" / "__init__.py").write_text("", encoding="utf-8")
    (engine / "core" / "tool_deps.py").write_text(
        textwrap.dedent(f"""
            import os
            from dataclasses import dataclass, field

            @dataclass
            class DepResult:
                ok: bool = True
                installed: list = field(default_factory=list)
                message: str = ""

            def ensure_tool_deps(tool_id, requires):
            {textwrap.indent(textwrap.dedent(ensure_body), " " * 16)}
        """),
        encoding="utf-8",
    )
    return project


def _run(root: Path, *args: str):
    return run_python([str(root / "warmup.py"), *args])


# ── 前置檢查 ───────────────────────────────────────────────────────────────────

def test_non_provision_dir(tmp_path: Path):
    shutil.copy2(WARMUP_PY, tmp_path / "warmup.py")
    result = _run(tmp_path, "--project", str(tmp_path),
                  "--deppack-cache", str(tmp_path), "--tool-venvs", str(tmp_path))
    assert result.returncode != 0
    assert "不是補給包" in result.stdout + result.stderr


def test_empty_deppack_cache_tells_you_to_run_apply(tmp_path: Path):
    root = _provision(tmp_path / "provision", {"t1": ["numpy"]})
    project = _stub_project(tmp_path, "return DepResult()")
    empty_cache = tmp_path / "cache"
    empty_cache.mkdir()

    result = _run(root, "--project", str(project), "--deppack-cache", str(empty_cache),
                  "--tool-venvs", str(tmp_path / "venvs"))
    assert result.returncode != 0
    out = result.stdout + result.stderr
    assert "apply.py" in out and "是空的" in out


def test_non_platform_project(tmp_path: Path):
    root = _provision(tmp_path / "provision", {"t1": ["numpy"]})
    cache = tmp_path / "cache"
    (cache / "t1").mkdir(parents=True)

    result = _run(root, "--project", str(tmp_path / "not-a-platform"),
                  "--deppack-cache", str(cache), "--tool-venvs", str(tmp_path / "venvs"))
    assert result.returncode != 0
    assert "不是 CIM 平台專案" in result.stdout + result.stderr


def test_unknown_tool_is_usage_error(tmp_path: Path):
    root = _provision(tmp_path / "provision", {"t1": ["numpy"]})
    project = _stub_project(tmp_path, "return DepResult()")
    result = _run(root, "--project", str(project), "--deppack-cache", str(tmp_path),
                  "--tool-venvs", str(tmp_path / "venvs"), "--tools", "ghost")
    assert result.returncode != 0
    assert "沒有的工具" in result.stdout + result.stderr


# ── 主流程 ─────────────────────────────────────────────────────────────────────

def test_dry_run_lists_plan_without_touching_anything(tmp_path: Path):
    root = _provision(tmp_path / "provision", {"t1": ["numpy"], "t2": ["scipy"]})
    project = _stub_project(tmp_path, "raise AssertionError('dry-run 不該呼叫 ensure')")
    venvs = tmp_path / "venvs"

    result = _run(root, "--project", str(project), "--deppack-cache", str(tmp_path),
                  "--tool-venvs", str(venvs), "--dry-run")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "[計畫] t1" in result.stdout and "[計畫] t2" in result.stdout
    assert not venvs.exists()


def test_warms_every_tool_and_passes_env(tmp_path: Path):
    """核心契約：CIM_DEPPACK_CACHE / CIM_TOOL_VENVS_DIR 必須在呼叫前就設好。"""
    root = _provision(tmp_path / "provision", {"t1": ["numpy"], "t2": ["scipy>=1.0"]})
    cache = tmp_path / "cache"
    (cache / "t1").mkdir(parents=True)
    venvs = tmp_path / "venvs"
    record = tmp_path / "record.jsonl"

    project = _stub_project(tmp_path, f"""
        import json
        with open(r"{record}", "a", encoding="utf-8") as fh:
            fh.write(json.dumps({{
                "tool": tool_id, "requires": requires,
                "cache": os.environ.get("CIM_DEPPACK_CACHE"),
                "venvs": os.environ.get("CIM_TOOL_VENVS_DIR"),
            }}) + "\\n")
        return DepResult(ok=True, installed=list(requires))
    """)

    result = _run(root, "--project", str(project), "--deppack-cache", str(cache),
                  "--tool-venvs", str(venvs))

    assert result.returncode == 0, result.stdout + result.stderr
    assert "全部就緒（2 個工具）" in result.stdout
    assert "離線安裝完成" in result.stdout

    calls = [json.loads(line) for line in record.read_text(encoding="utf-8").splitlines()]
    assert [c["tool"] for c in calls] == ["t1", "t2"]
    assert calls[0]["requires"] == ["numpy"]
    assert all(Path(c["cache"]) == cache.resolve() for c in calls)
    assert all(Path(c["venvs"]) == venvs.resolve() for c in calls)


def test_cached_run_reports_fingerprint_hit(tmp_path: Path):
    root = _provision(tmp_path / "provision", {"t1": ["numpy"]})
    cache = tmp_path / "cache"
    (cache / "t1").mkdir(parents=True)
    project = _stub_project(tmp_path, "return DepResult(ok=True, installed=[])")

    result = _run(root, "--project", str(project), "--deppack-cache", str(cache),
                  "--tool-venvs", str(tmp_path / "venvs"))
    assert result.returncode == 0
    assert "指紋命中" in result.stdout


def test_failure_is_reported_and_exits_nonzero(tmp_path: Path):
    root = _provision(tmp_path / "provision", {"t1": ["numpy"]})
    cache = tmp_path / "cache"
    (cache / "t1").mkdir(parents=True)
    project = _stub_project(
        tmp_path, "return DepResult(ok=False, message='dep-pack 驗證失敗，拒絕安裝')")

    result = _run(root, "--project", str(project), "--deppack-cache", str(cache),
                  "--tool-venvs", str(tmp_path / "venvs"))

    assert result.returncode == 1
    assert "失敗 1 個：t1" in result.stdout
    assert "dep-pack 驗證失敗" in result.stdout
    assert "apply.py 沒跑過" in result.stdout      # 給可行動的原因


def test_tools_filter(tmp_path: Path):
    root = _provision(tmp_path / "provision", {"t1": ["numpy"], "t2": ["scipy"]})
    cache = tmp_path / "cache"
    (cache / "t1").mkdir(parents=True)
    record = tmp_path / "record.jsonl"
    project = _stub_project(tmp_path, f"""
        with open(r"{record}", "a", encoding="utf-8") as fh:
            fh.write(tool_id + "\\n")
        return DepResult()
    """)

    result = _run(root, "--project", str(project), "--deppack-cache", str(cache),
                  "--tool-venvs", str(tmp_path / "venvs"), "--tools", "t2")

    assert result.returncode == 0
    assert record.read_text(encoding="utf-8").split() == ["t2"]


# ── 產出裡要附帶 warmup.py ─────────────────────────────────────────────────────

def test_build_copies_warmup_into_provision(tmp_path: Path):
    """build 產出必須同時附帶 apply.py 與 warmup.py（離線機不會有本 repo）。"""
    from provision_builder.build import _copy_runtime_scripts

    dest = tmp_path / "out"
    dest.mkdir()
    _copy_runtime_scripts(dest)
    assert (dest / "apply.py").is_file()
    assert (dest / "warmup.py").is_file()


def test_warmup_docstring_names_the_root_cause():
    """這支腳本的存在理由不能只活在 commit message 裡。"""
    source = WARMUP_PY.read_text(encoding="utf-8")
    assert "30 秒" in source and "bridge.rs" in source
