"""bigdeps.py — 大型相依隔離 / 去重 / 引用計數（SPEC §6、§8.1）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import make_pack, make_wheel
from provision_builder.bigdeps import (
    BigDepConflict,
    classify_pack_wheels,
    exclusive_wheels,
    isolate_pack,
    prune_orphans,
)

BIG = b"x" * 5000    # 5000 bytes（壓縮後仍 > 門檻）
SMALL = b"y" * 10


def _sizes(pack_dir: Path) -> dict[str, int]:
    return {p.name: p.stat().st_size for p in (pack_dir / "wheels").glob("*.whl")}


def test_isolate_moves_only_wheels_above_threshold(tmp_path: Path):
    packs = tmp_path / "packs"
    make_pack(packs, "t1", {"big-1.0.whl": BIG, "small-1.0.whl": SMALL})
    pack_dir = packs / "t1"
    big_deps = tmp_path / "big-deps"

    sizes = _sizes(pack_dir)
    threshold = (sizes["small-1.0.whl"] + sizes["big-1.0.whl"]) // 2

    moved = isolate_pack(pack_dir, big_deps, threshold)

    assert moved == ["big-1.0.whl"]
    assert not (pack_dir / "wheels" / "big-1.0.whl").exists()
    assert (big_deps / "big-1.0.whl").is_file()
    assert (pack_dir / "wheels" / "small-1.0.whl").is_file()


def test_isolate_disabled_when_threshold_zero(tmp_path: Path):
    packs = tmp_path / "packs"
    make_pack(packs, "t1", {"big-1.0.whl": BIG})
    assert isolate_pack(packs / "t1", tmp_path / "big-deps", 0) == []
    assert (packs / "t1" / "wheels" / "big-1.0.whl").is_file()


def test_manifest_untouched_by_isolation(tmp_path: Path):
    """deppack.json 描述的是 apply **之後**的形狀，隔離不能動它（SPEC §6.2）。"""
    packs = tmp_path / "packs"
    manifest = make_pack(packs, "t1", {"big-1.0.whl": BIG})
    before = (packs / "t1" / "deppack.json").read_text(encoding="utf-8")

    isolate_pack(packs / "t1", tmp_path / "big-deps", 1)

    after = (packs / "t1" / "deppack.json").read_text(encoding="utf-8")
    assert before == after
    assert [w["name"] for w in json.loads(after)["wheels"]] == ["big-1.0.whl"]
    assert manifest["wheels"][0]["name"] == "big-1.0.whl"


def test_isolate_dedupes_identical_wheel_across_tools(tmp_path: Path):
    """兩個工具都要 torch → big-deps 只留一份。"""
    packs = tmp_path / "packs"
    big_deps = tmp_path / "big-deps"
    make_pack(packs, "t1", {"torch-2.6.0.whl": BIG})
    make_pack(packs, "t2", {"torch-2.6.0.whl": BIG})

    assert isolate_pack(packs / "t1", big_deps, 1) == ["torch-2.6.0.whl"]
    assert isolate_pack(packs / "t2", big_deps, 1) == ["torch-2.6.0.whl"]

    assert list(big_deps.glob("*.whl")) == [big_deps / "torch-2.6.0.whl"]
    assert not (packs / "t2" / "wheels" / "torch-2.6.0.whl").exists()


def test_isolate_aborts_on_same_name_different_content(tmp_path: Path):
    """同名不同 sha256 = 有一份損毀。靜默挑一個會變成難查的執行期錯誤，所以中止。"""
    packs = tmp_path / "packs"
    big_deps = tmp_path / "big-deps"
    make_pack(packs, "t1", {"torch-2.6.0.whl": BIG})
    make_pack(packs, "t2", {"torch-2.6.0.whl": b"z" * 5000})

    isolate_pack(packs / "t1", big_deps, 1)
    with pytest.raises(BigDepConflict, match="同名但內容不同"):
        isolate_pack(packs / "t2", big_deps, 1)


def test_isolate_no_wheels_dir_is_noop(tmp_path: Path):
    assert isolate_pack(tmp_path / "ghost", tmp_path / "big-deps", 1) == []


def test_classify_pack_wheels_finds_isolated_ones(tmp_path: Path):
    packs = tmp_path / "packs"
    big_deps = tmp_path / "big-deps"
    make_pack(packs, "t1", {"big-1.0.whl": BIG, "small-1.0.whl": SMALL})
    sizes = _sizes(packs / "t1")
    isolate_pack(packs / "t1", big_deps, (sizes["small-1.0.whl"] + sizes["big-1.0.whl"]) // 2)

    result = classify_pack_wheels(packs / "t1", big_deps, ["big-1.0.whl", "small-1.0.whl"])
    assert result == ["big-1.0.whl"]


def test_exclusive_wheels_ignores_shared(tmp_path: Path):
    prev = [
        {"name": "torch.whl", "used_by": ["t1", "t2"]},
        {"name": "cuda.whl", "used_by": ["t1"]},
        {"name": "other.whl", "used_by": ["t2"]},
    ]
    assert exclusive_wheels(prev, "t1") == ["cuda.whl"]
    assert exclusive_wheels(prev, "t2") == ["other.whl"]
    assert exclusive_wheels([], "t1") == []


def test_prune_orphans_removes_unreferenced_only(tmp_path: Path):
    big_deps = tmp_path / "big-deps"
    make_wheel(big_deps / "keep.whl")
    make_wheel(big_deps / "orphan.whl")

    removed = prune_orphans(big_deps, referenced={"keep.whl"})

    assert removed == ["orphan.whl"]
    assert (big_deps / "keep.whl").is_file()
    assert not (big_deps / "orphan.whl").exists()


def test_prune_orphans_on_missing_dir(tmp_path: Path):
    assert prune_orphans(tmp_path / "nope", referenced=set()) == []
