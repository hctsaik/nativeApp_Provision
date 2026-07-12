"""打包 GUI 後端：不建立真實視窗也能驗證掃描與 CLI 接線。"""

from __future__ import annotations

import sys
import shutil
from pathlib import Path

from provision_builder.gui_backend import (
    BuildOptions, ValidationOptions, build_command, discover_tools, validation_command,
)
from provision_builder.source_pack import discover_source_modules, package_source_module

from conftest import add_plugin_yaml


def test_build_command_pins_platform_and_selected_tools(tmp_path: Path):
    options = BuildOptions(
        project_root=tmp_path / "platform",
        dest=tmp_path / "out",
        tool_ids=("app-lv", "module_016"),
        force=True,
    )
    cmd = build_command(options, repo_root=tmp_path / "builder")

    assert cmd[:2] == [sys.executable, "-u"]
    assert cmd[2].endswith("provision.py")
    assert [cmd[cmd.index(flag) + 1] for flag in ("--platform", "--python-version", "--abi")] == [
        "win_amd64", "3.11", "cp311"
    ]
    assert cmd[cmd.index("--tools") + 1] == "app-lv,module_016"
    assert "--force" in cmd


def test_build_command_omits_optional_switches(tmp_path: Path):
    cmd = build_command(BuildOptions(tmp_path / "p", tmp_path / "o", ()), repo_root=tmp_path)
    assert "--tools" not in cmd
    assert "--force" not in cmd
    assert "--python" not in cmd


def test_build_command_passes_launch_mode(tmp_path: Path):
    default = build_command(BuildOptions(tmp_path / "p", tmp_path / "o", ()), repo_root=tmp_path)
    assert default[default.index("--launch-mode") + 1] == "portable"
    dev = build_command(
        BuildOptions(tmp_path / "p", tmp_path / "o", (), launch_mode="dev"), repo_root=tmp_path,
    )
    assert dev[dev.index("--launch-mode") + 1] == "dev"


def test_discover_tools_reuses_platform_scanner(fake_project: Path):
    engine = fake_project / "sidecar" / "python-engine"
    add_plugin_yaml(engine, "scripts/module_042/plugin.yaml", "id: module_042\nrequires: [shapely]\n")
    add_plugin_yaml(engine, "scripts/module_043/plugin.yaml", "id: module_043\n")

    result = discover_tools(fake_project, python_cmd=[sys.executable])

    assert result.tool_ids == ["module_042"]
    assert result.skipped == [{"tool_id": "module_043", "reason": "no requires"}]


def test_validation_command_is_isolated_and_explicit(tmp_path: Path):
    options = ValidationOptions(tmp_path / "pack", tmp_path / "work", tmp_path / "platform", "app-lv")
    cmd = validation_command(options, repo_root=tmp_path / "builder")
    assert cmd[:2] == ["node", str(tmp_path / "builder" / "e2e" / "validate_package.mjs")]
    assert cmd[cmd.index("--tool") + 1] == "app-lv"
    assert cmd[cmd.index("--validation-dir") + 1] == str((tmp_path / "work").resolve())
    assert "--project" in cmd and "--provision" in cmd and "--python" in cmd


def test_source_module_can_be_selected_without_requires(tmp_path: Path):
    root = tmp_path / "modules"
    module = root / "module_042"
    module.mkdir(parents=True)
    (module / "plugin.yaml").write_text("id: module_042\nname: Test\nversion: 1.2.3\n", encoding="utf-8")
    (module / "run.py").write_text("print('ok')\n", encoding="utf-8")
    found = discover_source_modules(root, [sys.executable])
    assert len(found) == 1 and found[0].requires == ()
    manifest = package_source_module(found[0], tmp_path / "out")
    pack = tmp_path / "out" / "source-packages" / "module_042"
    assert manifest["version"] == "1.2.3"
    assert (pack / "source" / "run.py").is_file()
    assert (pack / "source-manifest.json").is_file()


def test_source_pack_succeeds_when_onedrive_locks_old_backup(tmp_path: Path, monkeypatch):
    root = tmp_path / "modules"
    module = root / "module_042"
    module.mkdir(parents=True)
    (module / "plugin.yaml").write_text("id: module_042\nversion: 1.0.0\n", encoding="utf-8")
    (module / "run.py").write_text("OLD\n", encoding="utf-8")
    selected = discover_source_modules(root, [sys.executable])[0]
    out = tmp_path / "onedrive"
    package_source_module(selected, out)
    (module / "run.py").write_text("NEW\n", encoding="utf-8")

    real_rmtree = shutil.rmtree

    def locked_rmtree(path, *args, **kwargs):
        if ".old-" in Path(path).name:
            raise PermissionError(5, "Access is denied", str(path / "source"))
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr("provision_builder.source_pack.shutil.rmtree", locked_rmtree)
    manifest = package_source_module(selected, out)

    final = out / "source-packages" / "module_042"
    assert manifest["tool_id"] == "module_042"
    assert (final / "source" / "run.py").read_text(encoding="utf-8") == "NEW\n"
    assert list((out / "source-packages").glob(".module_042.old-*"))
