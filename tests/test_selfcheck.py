"""selfcheck.py — 離線可裝自檢的指令組裝（SPEC §8.2、D7）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from provision_builder.gateway import Target
from provision_builder.selfcheck import build_offline_resolve_command, offline_resolve


def test_command_is_offline_and_targeted():
    cmd = build_offline_resolve_command(
        ["python"], ["torch==2.6.0"], [Path("wheels")], Target(), Path("dest"),
    )
    # 離線：絕不連 PyPI
    assert "--no-index" in cmd
    assert "--find-links=wheels" in cmd
    # 目標標籤一律明示（D7：cp314 事故的解藥）
    assert cmd[cmd.index("--platform") + 1] == "win_amd64"
    assert cmd[cmd.index("--python-version") + 1] == "3.11"
    assert cmd[cmd.index("--abi") + 1] == "cp311"
    assert cmd[cmd.index("--implementation") + 1] == "cp"
    # 指定目標標籤時 pip 強制要求 only-binary
    assert "--only-binary=:all:" in cmd
    assert cmd[cmd.index("--dest") + 1] == "dest"
    assert cmd[:4] == ["python", "-m", "pip", "download"]


def test_multiple_find_links_for_big_deps():
    """大 wheel 被隔離走了，find-links 必須同時帶上 big-deps，否則 pip 一定解不開。"""
    cmd = build_offline_resolve_command(
        ["python"], ["torch"], [Path("packs/t1/wheels"), Path("big-deps")], Target(), Path("d"),
    )
    links = [c for c in cmd if c.startswith("--find-links=")]
    assert len(links) == 2
    assert links[1].endswith("big-deps")


def test_custom_target_tags_flow_through():
    target = Target(platform_tag="manylinux2014_x86_64", python_version="3.12", abi="cp312")
    cmd = build_offline_resolve_command(["python"], ["numpy"], [], target, Path("d"))
    assert cmd[cmd.index("--platform") + 1] == "manylinux2014_x86_64"
    assert cmd[cmd.index("--abi") + 1] == "cp312"


def test_offline_resolve_reports_failure(monkeypatch):
    """pip 解不開時要把 stderr 傳回來（訊息會點名缺哪個套件）。"""
    from provision_builder import selfcheck

    monkeypatch.setattr(
        selfcheck, "run",
        lambda cmd, timeout=None: (False, "ERROR: Could not find a version that satisfies ghostpkg"),
    )
    ok, msg = offline_resolve(["python"], ["ghostpkg"], [Path("w")], Target())
    assert not ok and "ghostpkg" in msg


def test_offline_resolve_success(monkeypatch):
    from provision_builder import selfcheck

    monkeypatch.setattr(selfcheck, "run", lambda cmd, timeout=None: (True, "Saved ..."))
    ok, _ = offline_resolve(["python"], ["numpy"], [Path("w")], Target())
    assert ok


@pytest.mark.network
def test_offline_resolve_against_real_wheelhouse(tmp_path: Path):
    """真的 pip download 一個小套件成 wheelhouse，再用 --no-index 重解一次。"""
    import subprocess
    import sys

    wheels = tmp_path / "wheels"
    subprocess.run(
        [sys.executable, "-m", "pip", "download", "cowsay", "-d", str(wheels),
         "--only-binary=:all:", "--platform", "win_amd64",
         "--python-version", "3.11", "--abi", "cp311", "--implementation", "cp", "-q"],
        check=True,
    )
    ok, msg = offline_resolve([sys.executable], ["cowsay"], [wheels], Target())
    assert ok, msg
