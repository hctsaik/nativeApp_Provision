"""scan.py — 掃描規則（SPEC §7）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import add_plugin_yaml
from provision_builder.scan import (
    PLUGIN_GLOBS,
    ScanError,
    find_plugin_yamls,
    make_subprocess_loader,
    scan_project,
)


def dict_loader(data_by_path: dict[str, dict]):
    """假 loader：直接回預先準備好的 dict（不碰 subprocess / YAML）。"""

    def _load(paths):
        return {str(p): {"ok": True, "data": data_by_path.get(str(p), {})} for p in paths}

    return _load


def test_globs_mirror_engine():
    """守門：這兩條 glob 必須與 engine._scan_and_register_plugins 一致。"""
    assert PLUGIN_GLOBS == ("scripts/*/plugin.yaml", "plugins/*/modules/*/plugin.yaml")


def test_find_plugin_yamls_covers_both_roots(fake_project: Path):
    engine = fake_project / "sidecar" / "python-engine"
    a = add_plugin_yaml(engine, "scripts/tool_a/plugin.yaml", "id: tool_a\n")
    b = add_plugin_yaml(engine, "plugins/vendor/modules/mod_b/plugin.yaml", "id: mod_b\n")
    # 不該被掃到的深度
    add_plugin_yaml(engine, "plugins/vendor/plugin.yaml", "id: nope\n")
    add_plugin_yaml(engine, "scripts/deep/deeper/plugin.yaml", "id: nope2\n")

    found = find_plugin_yamls(engine)
    assert found == [a, b]


def test_scan_partitions_tools(fake_project: Path):
    engine = fake_project / "sidecar" / "python-engine"
    p1 = add_plugin_yaml(engine, "scripts/with_deps/plugin.yaml", "")
    p2 = add_plugin_yaml(engine, "scripts/no_deps/plugin.yaml", "")
    p3 = add_plugin_yaml(engine, "plugins/v/modules/disabled/plugin.yaml", "")
    p4 = add_plugin_yaml(engine, "plugins/v/modules/blank_req/plugin.yaml", "")

    loader = dict_loader({
        str(p1): {"id": "with_deps", "requires": ["torch==2.6.0", " numpy ", ""]},
        str(p2): {"id": "no_deps"},
        str(p3): {"id": "disabled_tool", "enabled": False, "requires": ["torch"]},
        str(p4): {"id": "blank_req", "requires": []},
    })
    result = scan_project(engine, loader)

    assert result.tool_ids == ["with_deps"]
    assert result.tools[0].requires == ["torch==2.6.0", "numpy"]  # 去空白、去空字串、保順序

    reasons = {s["tool_id"]: s["reason"] for s in result.skipped}
    assert reasons["no_deps"] == "no requires"
    assert reasons["blank_req"] == "no requires"
    assert "disabled" in reasons["disabled_tool"]


def test_scan_yaml_without_id_is_ignored(fake_project: Path):
    engine = fake_project / "sidecar" / "python-engine"
    p = add_plugin_yaml(engine, "scripts/x/plugin.yaml", "")
    result = scan_project(engine, dict_loader({str(p): {"name": "no id here"}}))
    assert result.tools == [] and result.skipped == []


def test_duplicate_tool_id_aborts(fake_project: Path):
    engine = fake_project / "sidecar" / "python-engine"
    p1 = add_plugin_yaml(engine, "scripts/a/plugin.yaml", "")
    p2 = add_plugin_yaml(engine, "plugins/v/modules/b/plugin.yaml", "")
    loader = dict_loader({
        str(p1): {"id": "module_006", "requires": ["x"]},
        str(p2): {"id": "module_006", "requires": ["y"]},
    })
    with pytest.raises(ScanError, match="工具 id 重複"):
        scan_project(engine, loader)


def test_yaml_parse_error_aborts(fake_project: Path):
    """engine 只 warning 跳過；補給包必須炸——靜默漏掉相依 = 工廠現場才發現。"""
    engine = fake_project / "sidecar" / "python-engine"
    p = add_plugin_yaml(engine, "scripts/broken/plugin.yaml", "id: [oops\n")

    def bad_loader(paths):
        return {str(x): {"ok": False, "error": "ScannerError: bad yaml"} for x in paths}

    with pytest.raises(ScanError, match="解析失敗"):
        scan_project(engine, bad_loader)


def test_no_plugin_yaml_at_all_aborts_with_submodule_hint(fake_project: Path):
    engine = fake_project / "sidecar" / "python-engine"
    with pytest.raises(ScanError, match="submodule"):
        scan_project(engine, dict_loader({}))


def test_only_tools_filters(fake_project: Path):
    engine = fake_project / "sidecar" / "python-engine"
    p1 = add_plugin_yaml(engine, "scripts/a/plugin.yaml", "")
    p2 = add_plugin_yaml(engine, "scripts/b/plugin.yaml", "")
    loader = dict_loader({
        str(p1): {"id": "a", "requires": ["x"]},
        str(p2): {"id": "b", "requires": ["y"]},
    })
    result = scan_project(engine, loader, only_tools=["a"])
    assert result.tool_ids == ["a"]


def test_only_tools_unknown_aborts(fake_project: Path):
    engine = fake_project / "sidecar" / "python-engine"
    p1 = add_plugin_yaml(engine, "scripts/a/plugin.yaml", "")
    loader = dict_loader({str(p1): {"id": "a", "requires": ["x"]}})
    with pytest.raises(ScanError, match="掃不到的工具"):
        scan_project(engine, loader, only_tools=["a", "ghost"])


def test_only_tools_pointing_at_skipped_tool_aborts(fake_project: Path):
    """明確指定一個「不需要補給包」的工具 → 使用者誤解了，要講清楚而不是靜默略過。"""
    engine = fake_project / "sidecar" / "python-engine"
    p1 = add_plugin_yaml(engine, "scripts/a/plugin.yaml", "")
    loader = dict_loader({str(p1): {"id": "a"}})
    with pytest.raises(ScanError, match="不需要補給包"):
        scan_project(engine, loader, only_tools=["a"])


def test_subprocess_loader_reads_real_yaml(fake_project: Path):
    """真的用當前直譯器 + PyYAML 讀檔（本機開發環境有 PyYAML）。"""
    pytest.importorskip("yaml")
    import sys

    engine = fake_project / "sidecar" / "python-engine"
    p = add_plugin_yaml(
        engine,
        "scripts/real/plugin.yaml",
        "id: real_tool\nname: 真實工具\nrequires:\n  - torch==2.6.0\n  - numpy\n",
    )
    loader = make_subprocess_loader([sys.executable])
    result = scan_project(engine, loader)
    assert result.tool_ids == ["real_tool"]
    assert result.tools[0].requires == ["torch==2.6.0", "numpy"]
    assert result.tools[0].yaml_path == p


def test_subprocess_loader_without_pyyaml_gives_hint(fake_project: Path, tmp_path: Path):
    """指到一個沒有 PyYAML 的直譯器 → 錯誤訊息要教人怎麼修。"""
    import sys
    import textwrap

    engine = fake_project / "sidecar" / "python-engine"
    add_plugin_yaml(engine, "scripts/real/plugin.yaml", "id: real_tool\n")

    # 造一個「假直譯器」：一個會噴 ModuleNotFoundError 的 python 腳本包裝
    shim = tmp_path / "shim.py"
    shim.write_text(
        textwrap.dedent("""
            import sys
            sys.stderr.write("ModuleNotFoundError: No module named 'yaml'\\n")
            sys.exit(1)
        """),
        encoding="utf-8",
    )
    loader = make_subprocess_loader([sys.executable, str(shim)])
    with pytest.raises(ScanError, match="PyYAML"):
        scan_project(engine, loader)
