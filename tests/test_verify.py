"""verify.py — 補給包完整性驗證（SPEC §4.2）。刻意不依賴平台（可在離線機獨跑）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import make_pack, make_wheel, write_provision_json
from provision_builder.bigdeps import isolate_pack
from provision_builder.verify import format_verdict, verify_pack, verify_provision

BIG = b"x" * 5000
SMALL = b"y" * 10


def _make_provision(tmp_path: Path, *, isolate: bool = True) -> Path:
    """造一個「一般 wheel 留在 pack、大 wheel 隔離到 big-deps」的補給包。"""
    root = tmp_path / "provision"
    packs = root / "packs"
    big_deps = root / "big-deps"
    make_pack(packs, "t1", {"torch-2.6.0.whl": BIG, "tiny-1.0.whl": SMALL})
    if isolate:
        isolate_pack(packs / "t1", big_deps, 1000)
    manifest = json.loads((packs / "t1" / "deppack.json").read_text(encoding="utf-8"))
    big_names = ["torch-2.6.0.whl"] if isolate else []
    write_provision_json(
        root,
        tools=[{
            "tool_id": "t1",
            "requires": ["dummy"],
            "wheel_count": len(manifest["wheels"]),
            "total_bytes": sum(w["size"] for w in manifest["wheels"]),
            "big_wheels": big_names,
        }],
        big_deps=[{"name": n, "sha256": "", "size": 0, "used_by": ["t1"]} for n in big_names],
    )
    return root


def test_verify_pack_ok_when_split_across_big_deps(tmp_path: Path):
    root = _make_provision(tmp_path)
    verdict = verify_pack(root / "packs" / "t1", root / "big-deps")
    assert verdict.applicable
    assert verdict.errors == []


def test_verify_pack_ok_when_fully_assembled(tmp_path: Path):
    root = _make_provision(tmp_path, isolate=False)
    verdict = verify_pack(root / "packs" / "t1", root / "big-deps")
    assert verdict.applicable


def test_missing_big_dep_is_recoverable_not_corruption(tmp_path: Path):
    root = _make_provision(tmp_path)
    (root / "big-deps" / "torch-2.6.0.whl").unlink()

    verdict = verify_pack(root / "packs" / "t1", root / "big-deps",
                          known_big={"torch-2.6.0.whl"})
    assert not verdict.applicable
    assert verdict.missing_big == ["torch-2.6.0.whl"]
    assert verdict.missing_other == []
    assert verdict.errors == []           # 不是「壞掉」，是「沒放回去」


def test_missing_ordinary_wheel_is_corruption(tmp_path: Path):
    root = _make_provision(tmp_path)
    (root / "packs" / "t1" / "wheels" / "tiny-1.0.whl").unlink()

    verdict = verify_pack(root / "packs" / "t1", root / "big-deps",
                          known_big={"torch-2.6.0.whl"})
    assert verdict.missing_other == ["tiny-1.0.whl"]
    assert verdict.missing_big == []


def test_single_byte_corruption_detected(tmp_path: Path):
    root = _make_provision(tmp_path)
    target = root / "packs" / "t1" / "wheels" / "tiny-1.0.whl"
    data = bytearray(target.read_bytes())
    data[-1] ^= 0xFF                       # 同樣大小，內容不同 → 只有 sha256 抓得到
    target.write_bytes(bytes(data))

    verdict = verify_pack(root / "packs" / "t1", root / "big-deps")
    assert not verdict.ok
    assert any("sha256 不符" in e for e in verdict.errors)


def test_truncated_wheel_detected_by_size(tmp_path: Path):
    root = _make_provision(tmp_path)
    target = root / "packs" / "t1" / "wheels" / "tiny-1.0.whl"
    target.write_bytes(target.read_bytes()[:-3])

    verdict = verify_pack(root / "packs" / "t1", root / "big-deps")
    assert any("大小不符" in e for e in verdict.errors)


def test_extra_wheel_detected(tmp_path: Path):
    root = _make_provision(tmp_path)
    make_wheel(root / "packs" / "t1" / "wheels" / "stowaway-9.9.whl")

    verdict = verify_pack(root / "packs" / "t1", root / "big-deps")
    assert any("多餘 wheel" in e for e in verdict.errors)


def test_pack_without_manifest(tmp_path: Path):
    pack = tmp_path / "packs" / "ghost"
    (pack / "wheels").mkdir(parents=True)
    verdict = verify_pack(pack, tmp_path / "big-deps")
    assert not verdict.ok and "不是一個 dep-pack" in verdict.errors[0]


def test_pack_with_broken_manifest(tmp_path: Path):
    pack = tmp_path / "packs" / "broken"
    (pack / "wheels").mkdir(parents=True)
    (pack / "deppack.json").write_text("{not json", encoding="utf-8")
    verdict = verify_pack(pack, tmp_path / "big-deps")
    assert not verdict.ok and "無法解析" in verdict.errors[0]


# ── 整包驗證 ───────────────────────────────────────────────────────────────────

def test_verify_provision_all_ok(tmp_path: Path):
    verdict = verify_provision(_make_provision(tmp_path))
    assert verdict.ok
    assert [p.tool_id for p in verdict.packs] == ["t1"]


def test_verify_provision_detects_pack_missing_from_disk(tmp_path: Path):
    import shutil

    root = _make_provision(tmp_path)
    shutil.rmtree(root / "packs" / "t1")
    verdict = verify_provision(root)
    assert not verdict.ok
    assert any("沒有對應的 pack" in e for e in verdict.errors)


def test_verify_provision_detects_unknown_pack_dir(tmp_path: Path):
    root = _make_provision(tmp_path)
    (root / "packs" / "surprise").mkdir()
    verdict = verify_provision(root)
    assert any("未列的目錄" in e for e in verdict.errors)


def test_verify_provision_detects_stray_big_dep(tmp_path: Path):
    root = _make_provision(tmp_path)
    make_wheel(root / "big-deps" / "unexpected.whl")
    verdict = verify_provision(root)
    assert any("未列的檔案" in e for e in verdict.errors)


def test_verify_provision_without_manifest_still_checks_packs(tmp_path: Path):
    root = _make_provision(tmp_path)
    (root / "provision.json").unlink()
    verdict = verify_provision(root)
    assert any("缺少 provision.json" in e for e in verdict.errors)
    assert len(verdict.packs) == 1


def test_verify_provision_on_non_provision_dir(tmp_path: Path):
    verdict = verify_provision(tmp_path)
    assert not verdict.ok and "不是一個補給包" in verdict.errors[0]


def test_format_verdict_gives_actionable_big_dep_message(tmp_path: Path):
    root = _make_provision(tmp_path)
    (root / "big-deps" / "torch-2.6.0.whl").unlink()
    text = format_verdict(verify_provision(root))
    assert "大型相依未就位" in text
    assert "torch-2.6.0.whl" in text
    assert "放回" in text                  # 要告訴使用者怎麼修
    assert "影響的工具：t1" in text


# ── 與平台驗章語意一致 ─────────────────────────────────────────────────────────

@pytest.mark.platform_repo
def test_agrees_with_platform_verify_deppack_dir(tmp_path: Path, project_root_opt: Path):
    """組裝完成（無 big-deps 分離）的 pack，我們的判斷必須與平台 verify_deppack_dir 相同。"""
    from provision_builder.gateway import PlatformGateway

    gateway = PlatformGateway(project_root_opt)
    packs = tmp_path / "packs"
    make_pack(packs, "t1", {"a-1.0.whl": SMALL, "b-1.0.whl": BIG})

    ours = verify_pack(packs / "t1", tmp_path / "big-deps")
    theirs_ok, theirs_errors = gateway.verify_deppack_dir(packs / "t1")
    assert ours.applicable is theirs_ok is True
    assert theirs_errors == []

    # 破壞一個 wheel → 兩邊都要抓到
    target = packs / "t1" / "wheels" / "a-1.0.whl"
    data = bytearray(target.read_bytes())
    data[-1] ^= 0xFF
    target.write_bytes(bytes(data))

    ours2 = verify_pack(packs / "t1", tmp_path / "big-deps")
    theirs2_ok, _ = gateway.verify_deppack_dir(packs / "t1")
    assert ours2.applicable is False and theirs2_ok is False
