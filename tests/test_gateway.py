"""gateway.py — 與平台的唯一耦合點（SPEC D4 / §14）。"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from provision_builder.gateway import ENGINE_SUBPATH, GatewayError, PlatformGateway, Target


# ── Target（SPEC D7：目標標籤一律明示）─────────────────────────────────────────

def test_target_defaults_are_the_platform_pin():
    target = Target()
    assert (target.platform_tag, target.python_version, target.abi) == ("win_amd64", "3.11", "cp311")
    assert target.python_tag == "cp311"


def test_target_python_tag_derives_from_version():
    assert Target(python_version="3.12").python_tag == "cp312"


def test_target_rejects_malformed_python_version():
    with pytest.raises(GatewayError, match="python_version"):
        _ = Target(python_version="3").python_tag


def test_target_as_dict_round_trips():
    assert Target().as_dict() == {"platform_tag": "win_amd64", "python_version": "3.11", "abi": "cp311"}


# ── 專案偵測 ───────────────────────────────────────────────────────────────────

def test_rejects_non_platform_folder(tmp_path: Path):
    with pytest.raises(GatewayError, match="不是 CIM 平台專案"):
        PlatformGateway(tmp_path)


def test_accepts_folder_with_engine_py(fake_project: Path):
    gateway = PlatformGateway(fake_project)
    assert gateway.engine_root == fake_project.joinpath(*ENGINE_SUBPATH)
    assert gateway.python_cmd == [sys.executable]


def test_custom_python_cmd(fake_project: Path):
    assert PlatformGateway(fake_project, python_cmd=["py", "-3.11"]).python_cmd == ["py", "-3.11"]


# ── 契約守門 ───────────────────────────────────────────────────────────────────

def _complete_stub() -> types.SimpleNamespace:
    return types.SimpleNamespace(
        build_wheelhouse=lambda *a, **k: None,
        load_manifest=lambda *a, **k: None,
        verify_wheelhouse=lambda *a, **k: (True, []),
        verify_deppack_dir=lambda *a, **k: (True, []),
        requires_fingerprint=lambda r: "fp",
        MANIFEST_FILENAME="deppack.json",
        WHEELS_DIRNAME="wheels",
        DepPackError=Exception,
    )


def test_contract_passes_for_complete_module():
    PlatformGateway._assert_contract(_complete_stub())  # 不該拋


def test_contract_names_missing_apis():
    stub = _complete_stub()
    del stub.build_wheelhouse
    del stub.requires_fingerprint
    with pytest.raises(GatewayError) as exc:
        PlatformGateway._assert_contract(stub)
    assert "build_wheelhouse" in str(exc.value)
    assert "requires_fingerprint" in str(exc.value)


# ── API 包裝（用 stub 注入，不碰真平台）────────────────────────────────────────

class _FakeManifest:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def to_dict(self) -> dict:
        return dict(self._payload)


def test_build_wheelhouse_passes_target_tags_through(fake_project: Path):
    captured: dict = {}

    def fake_build(tool_id, requires, dest_root, **kwargs):
        captured.update({"tool_id": tool_id, "requires": requires, "dest": dest_root, **kwargs})
        return _FakeManifest({"tool_id": tool_id, "wheels": []})

    gateway = PlatformGateway(fake_project)
    stub = _complete_stub()
    stub.build_wheelhouse = fake_build
    gateway._deppack = stub

    target = Target()
    result = gateway.build_wheelhouse("t1", ["numpy"], Path("dest"), target)

    assert result == {"tool_id": "t1", "wheels": []}
    assert captured["platform_tag"] == "win_amd64"
    assert captured["python_version"] == "3.11"
    assert captured["abi"] == "cp311"
    assert captured["python_cmd"] == [sys.executable]


def test_build_wheelhouse_wraps_platform_errors(fake_project: Path):
    gateway = PlatformGateway(fake_project)
    stub = _complete_stub()

    def boom(*a, **k):
        raise RuntimeError("pip download 失敗：no matching distribution")

    stub.build_wheelhouse = boom
    gateway._deppack = stub

    with pytest.raises(GatewayError, match="no matching distribution"):
        gateway.build_wheelhouse("t1", ["ghost"], Path("dest"), Target())


def test_build_wheelhouse_empty_requires_is_gateway_error(fake_project: Path):
    gateway = PlatformGateway(fake_project)
    stub = _complete_stub()

    def raises_value_error(*a, **k):
        raise ValueError("沒有可下載的 requires")

    stub.build_wheelhouse = raises_value_error
    gateway._deppack = stub
    with pytest.raises(GatewayError, match="requires"):
        gateway.build_wheelhouse("t1", [], Path("dest"), Target())


def test_constants_come_from_platform_not_hardcoded(fake_project: Path):
    gateway = PlatformGateway(fake_project)
    stub = _complete_stub()
    stub.MANIFEST_FILENAME = "custom.json"
    stub.WHEELS_DIRNAME = "custom_wheels"
    gateway._deppack = stub
    assert gateway.manifest_filename == "custom.json"
    assert gateway.wheels_dirname == "custom_wheels"


# ── 對真實平台的契約檢查 ───────────────────────────────────────────────────────

@pytest.mark.platform_repo
def test_real_platform_satisfies_contract(project_root_opt: Path):
    """真的 import 真平台的 core.deppack，確認 SPEC §14 的 API 都還在。"""
    gateway = PlatformGateway(project_root_opt)
    deppack = gateway.deppack  # 觸發 import + _assert_contract
    assert deppack.MANIFEST_FILENAME == "deppack.json"
    assert deppack.WHEELS_DIRNAME == "wheels"
    # 指紋演算法必須穩定：本工具的增量判斷完全靠它
    assert gateway.requires_fingerprint(["b", "a"]) == gateway.requires_fingerprint(["a", "b"])
