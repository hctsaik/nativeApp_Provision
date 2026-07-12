"""provision.py — CLI 介面（SPEC §4）。用 subprocess 跑真的入口。"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from conftest import make_pack, run_python, write_provision_json

CLI = Path(__file__).resolve().parents[1] / "provision.py"
APPLY_PY = Path(__file__).resolve().parents[1] / "apply.py"


def _cli(*args: str) -> subprocess.CompletedProcess:
    return run_python([str(CLI), *args])


def test_version():
    result = _cli("--version")
    assert result.returncode == 0 and "native_Provision" in result.stdout


def test_help_lists_three_subcommands():
    out = _cli("--help").stdout
    for command in ("build", "verify", "apply"):
        assert command in out


def test_build_on_non_platform_folder_is_usage_error(tmp_path: Path):
    result = _cli("build", str(tmp_path))
    assert result.returncode == 2
    assert "不是 CIM 平台專案" in result.stdout


def test_build_defaults_are_the_platform_pin():
    out = _cli("build", "--help").stdout
    assert "win_amd64" in out and "3.11" in out and "cp311" in out
    assert "100" in out                      # big-threshold 預設


def _fake_provision(root: Path) -> Path:
    packs = root / "packs"
    manifest = make_pack(packs, "t1", {"tiny-1.0.whl": b"y" * 10})
    write_provision_json(
        root,
        tools=[{"tool_id": "t1", "requires": ["dummy"],
                "wheel_count": len(manifest["wheels"]),
                "total_bytes": sum(w["size"] for w in manifest["wheels"]),
                "big_wheels": []}],
        big_deps=[],
    )
    shutil.copy2(APPLY_PY, root / "apply.py")
    return root


def test_verify_ok(tmp_path: Path):
    root = _fake_provision(tmp_path / "provision")
    result = _cli("verify", str(root))
    assert result.returncode == 0
    assert "[OK]   t1" in result.stdout
    assert "全部通過" in result.stdout


def test_verify_detects_corruption(tmp_path: Path):
    root = _fake_provision(tmp_path / "provision")
    wheel = root / "packs" / "t1" / "wheels" / "tiny-1.0.whl"
    data = bytearray(wheel.read_bytes())
    data[-1] ^= 0xFF
    wheel.write_bytes(bytes(data))

    result = _cli("verify", str(root))
    assert result.returncode == 1
    assert "sha256 不符" in result.stdout


def test_verify_on_random_dir(tmp_path: Path):
    result = _cli("verify", str(tmp_path))
    assert result.returncode == 1
    assert "不是一個補給包" in result.stdout


def test_apply_delegates_to_bundled_script(tmp_path: Path):
    """provision apply 只是轉呼叫包內的 apply.py（D8：那支才是離線機真正跑的）。"""
    root = _fake_provision(tmp_path / "provision")
    cache = tmp_path / "cache"

    result = _cli("apply", str(root), "--deppack-cache", str(cache))

    assert result.returncode == 0
    assert (cache / "t1" / "deppack.json").is_file()
    assert "[OK]" in result.stdout


def test_apply_without_bundled_script_errors(tmp_path: Path):
    root = _fake_provision(tmp_path / "provision")
    (root / "apply.py").unlink()
    result = _cli("apply", str(root), "--deppack-cache", str(tmp_path / "cache"))
    assert result.returncode == 2
    assert "不是補給包" in result.stdout


def test_apply_dry_run_flag_passthrough(tmp_path: Path):
    root = _fake_provision(tmp_path / "provision")
    cache = tmp_path / "cache"
    result = _cli("apply", str(root), "--deppack-cache", str(cache), "--dry-run")
    assert result.returncode == 0 and not cache.exists()
