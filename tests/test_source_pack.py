"""source_pack.py 的專屬單元測試——鎖定 Source Package(原始碼獨立打包)行為。

不連網:YAML 解析走真實子程序 loader(sys.executable),但只讀本機臨時檔。
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest

from provision_builder.scan import ScanError
from provision_builder.source_pack import (
    discover_source_modules,
    package_source_module,
)

PY = [sys.executable]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _make_module(root: Path, tool_id: str, body: str) -> Path:
    folder = root / tool_id
    folder.mkdir(parents=True)
    (folder / "plugin.yaml").write_text(f"id: {tool_id}\n{body}", encoding="utf-8")
    return folder


def test_discovers_single_module_directly(tmp_path: Path):
    folder = _make_module(tmp_path, "module_042", "name: 甲\nversion: 1.2.3\ncategory: module\n")
    (folder / "run.py").write_text("print('ok')\n", encoding="utf-8")
    found = discover_source_modules(folder, PY)
    assert [m.tool_id for m in found] == ["module_042"]
    assert found[0].version == "1.2.3" and found[0].name == "甲"
    assert found[0].enabled is True and found[0].category == "module"


def test_discovers_multiple_modules_from_root_sorted(tmp_path: Path):
    _make_module(tmp_path, "module_b", "version: 1.0.0\n")
    _make_module(tmp_path, "module_a", "version: 1.0.0\n")
    found = discover_source_modules(tmp_path, PY)
    assert [m.tool_id for m in found] == ["module_a", "module_b"]


def test_duplicate_id_across_folders_raises(tmp_path: Path):
    (tmp_path / "one").mkdir()
    (tmp_path / "one" / "plugin.yaml").write_text("id: dup\n", encoding="utf-8")
    (tmp_path / "two").mkdir()
    (tmp_path / "two" / "plugin.yaml").write_text("id: dup\n", encoding="utf-8")
    with pytest.raises(ScanError, match="重複 id"):
        discover_source_modules(tmp_path, PY)


def test_missing_id_raises(tmp_path: Path):
    folder = tmp_path / "nameless"
    folder.mkdir()
    (folder / "plugin.yaml").write_text("name: 無 id\n", encoding="utf-8")
    with pytest.raises(ScanError, match="缺少 id"):
        discover_source_modules(folder, PY)


def test_no_plugin_yaml_raises(tmp_path: Path):
    with pytest.raises(ScanError, match="找不到 plugin.yaml"):
        discover_source_modules(tmp_path, PY)


def test_packaging_excludes_pycache_and_compiled(tmp_path: Path):
    folder = _make_module(tmp_path, "module_042", "version: 1.0.0\n")
    (folder / "run.py").write_text("print('ok')\n", encoding="utf-8")
    (folder / "__pycache__").mkdir()
    (folder / "__pycache__" / "run.cpython-311.pyc").write_bytes(b"\x00compiled")
    (folder / "stale.pyc").write_bytes(b"\x00stale")
    (folder / "sub").mkdir()
    (folder / "sub" / "helper.py").write_text("x = 1\n", encoding="utf-8")

    module = discover_source_modules(folder, PY)[0]
    manifest = package_source_module(module, tmp_path / "out")

    pack = tmp_path / "out" / "source-packages" / "module_042"
    paths = {e["path"] for e in manifest["files"]}
    assert paths == {"plugin.yaml", "run.py", "sub/helper.py"}
    assert not (pack / "source" / "__pycache__").exists()
    assert not (pack / "source" / "stale.pyc").exists()
    assert (pack / "source" / "sub" / "helper.py").is_file()


def test_manifest_signs_each_file_with_sha256(tmp_path: Path):
    folder = _make_module(tmp_path, "module_042", "version: 1.0.0\n")
    (folder / "run.py").write_text("print('signed')\n", encoding="utf-8")
    module = discover_source_modules(folder, PY)[0]
    manifest = package_source_module(module, tmp_path / "out")

    pack = tmp_path / "out" / "source-packages" / "module_042"
    for entry in manifest["files"]:
        copied = pack / "source" / entry["path"]
        assert copied.is_file()
        assert entry["sha256"] == _sha256(copied)
        assert entry["size"] == copied.stat().st_size
    assert manifest["format_version"] == 1
    assert manifest["tool_id"] == "module_042"


def test_repack_replaces_content_atomically(tmp_path: Path):
    folder = _make_module(tmp_path, "module_042", "version: 1.0.0\n")
    (folder / "run.py").write_text("OLD\n", encoding="utf-8")
    module = discover_source_modules(folder, PY)[0]
    out = tmp_path / "out"
    package_source_module(module, out)

    (folder / "run.py").write_text("NEW\n", encoding="utf-8")
    package_source_module(module, out)

    final = out / "source-packages" / "module_042"
    assert (final / "source" / "run.py").read_text(encoding="utf-8") == "NEW\n"
    # 沒有殘留的 staging 目錄(只留正式 module_042)
    leftovers = [p.name for p in (out / "source-packages").iterdir() if p.name != "module_042"]
    assert leftovers == []


def test_requires_and_defaults_captured(tmp_path: Path):
    folder = _make_module(tmp_path, "module_042", "requires:\n  - cowsay\n  - '  '\nenabled: false\n")
    module = discover_source_modules(folder, PY)[0]
    assert module.requires == ("cowsay",)  # 空白項被濾掉
    assert module.enabled is False
    assert module.category == "module"  # 未寫 category 時預設 module
    assert module.version == "0.0.0"    # 未寫 version 時預設
