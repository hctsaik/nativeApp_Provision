"""apply.py — 離線機套用腳本（SPEC §9、D8）。

apply.py 會被逐字複製進每個補給包，在離線機獨立執行。所以這裡**一律用 subprocess
呼叫真檔案**（而不是 import 它的函式），這樣「它真的自足」是被測試持續證明的性質。
少數純函式（link_or_copy）例外，用 importlib 從檔案載入來測 hardlink 回退。
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import make_pack, make_wheel, run_python, write_provision_json
from provision_builder import (
    BIG_DEPS_DIRNAME,
    DEPPACK_MANIFEST,
    PACKS_DIRNAME,
    PROVISION_MANIFEST,
    WHEELS_DIRNAME,
)
from provision_builder.bigdeps import isolate_pack

APPLY_PY = Path(__file__).resolve().parents[1] / "apply.py"

BIG = b"x" * 5000
SMALL = b"y" * 10


# ── D8：自足性 ─────────────────────────────────────────────────────────────────

def test_apply_imports_nothing_from_this_project():
    source = APPLY_PY.read_text(encoding="utf-8")
    assert "import provision_builder" not in source
    assert "from provision_builder" not in source
    assert "sys.path" not in source          # 不靠路徑注入偷 import


def test_apply_never_invokes_pip_or_network():
    """apply 只搬檔案；安裝是 engine 的事（SPEC §9 禁止事項）。"""
    source = APPLY_PY.read_text(encoding="utf-8")
    code = source.split('"""', 2)[2]          # 去掉模組 docstring
    for banned in ("subprocess", "urllib", "socket", "http"):
        assert banned not in code, f"apply.py 不該用到 {banned}"


def test_constants_match_builder_package():
    """兩份常數（apply.py 自帶 vs provision_builder）必須一致，否則產出與套用會對不上。"""
    spec = importlib.util.spec_from_file_location("apply_mod", APPLY_PY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.PACKS_DIRNAME == PACKS_DIRNAME
    assert module.BIG_DEPS_DIRNAME == BIG_DEPS_DIRNAME
    assert module.PROVISION_MANIFEST == PROVISION_MANIFEST
    assert module.DEPPACK_MANIFEST == DEPPACK_MANIFEST
    assert module.WHEELS_DIRNAME == WHEELS_DIRNAME


def _apply_module():
    spec = importlib.util.spec_from_file_location("apply_mod", APPLY_PY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_link_or_copy_falls_back_to_copy(tmp_path: Path, monkeypatch):
    """跨磁碟區 / 檔案系統不支援 hardlink 時要退回 copy（Windows 常見）。"""
    module = _apply_module()
    src = tmp_path / "a.whl"
    src.write_bytes(b"content")
    dst = tmp_path / "b.whl"

    monkeypatch.setattr(module.os, "link", lambda a, b: (_ for _ in ()).throw(OSError("EXDEV")))
    module.link_or_copy(src, dst)

    assert dst.read_bytes() == b"content"


def test_link_or_copy_uses_hardlink_when_possible(tmp_path: Path):
    module = _apply_module()
    src = tmp_path / "a.whl"
    src.write_bytes(b"content")
    dst = tmp_path / "b.whl"
    module.link_or_copy(src, dst)
    assert dst.read_bytes() == b"content"


# ── 補給包 fixture ────────────────────────────────────────────────────────────

def _make_provision(root: Path, *, isolate: bool = True, tools: dict | None = None) -> Path:
    packs = root / PACKS_DIRNAME
    big_deps = root / BIG_DEPS_DIRNAME
    tools = tools or {"t1": {"torch-2.6.0.whl": BIG, "tiny-1.0.whl": SMALL}}

    tool_entries, big_names = [], []
    for tool_id, wheels in tools.items():
        make_pack(packs, tool_id, wheels)
        moved = isolate_pack(packs / tool_id, big_deps, 1000) if isolate else []
        big_names.extend(moved)
        manifest = json.loads((packs / tool_id / DEPPACK_MANIFEST).read_text(encoding="utf-8"))
        tool_entries.append({
            "tool_id": tool_id,
            "requires": ["dummy"],
            "wheel_count": len(manifest["wheels"]),
            "total_bytes": sum(w["size"] for w in manifest["wheels"]),
            "big_wheels": sorted(moved),
        })
    big_entries = [{"name": n, "sha256": "", "size": 0,
                    "used_by": sorted(t["tool_id"] for t in tool_entries if n in t["big_wheels"])}
                   for n in sorted(set(big_names))]
    write_provision_json(root, tool_entries, big_entries)
    shutil.copy2(APPLY_PY, root / "apply.py")
    return root


def _run_apply(root: Path, cache: Path, *extra: str) -> subprocess.CompletedProcess:
    return run_python([str(root / "apply.py"), "--deppack-cache", str(cache), *extra])


# ── 正常路徑 ───────────────────────────────────────────────────────────────────

def test_apply_assembles_pack_with_big_dep_restored(tmp_path: Path):
    root = _make_provision(tmp_path / "provision")
    cache = tmp_path / "cache"

    result = _run_apply(root, cache)

    assert result.returncode == 0, result.stdout + result.stderr
    wheels = cache / "t1" / WHEELS_DIRNAME
    assert (wheels / "torch-2.6.0.whl").is_file()      # 從 big-deps 補回來了
    assert (wheels / "tiny-1.0.whl").is_file()
    assert (cache / "t1" / DEPPACK_MANIFEST).is_file()
    assert "[OK]" in result.stdout


def test_applied_pack_passes_platform_style_verification(tmp_path: Path):
    """組裝後的形狀必須逐位元組等同 deppack.json（engine 的 fail-closed 驗章會查）。"""
    root = _make_provision(tmp_path / "provision")
    cache = tmp_path / "cache"
    _run_apply(root, cache)

    from provision_builder.verify import verify_pack
    verdict = verify_pack(cache / "t1", tmp_path / "nonexistent-big-deps")
    assert verdict.applicable, verdict.errors + verdict.missing_big + verdict.missing_other


def test_apply_leaves_no_temp_dirs(tmp_path: Path):
    root = _make_provision(tmp_path / "provision")
    cache = tmp_path / "cache"
    _run_apply(root, cache)
    assert [p.name for p in cache.iterdir()] == ["t1"]


def test_apply_dry_run_writes_nothing(tmp_path: Path):
    root = _make_provision(tmp_path / "provision")
    cache = tmp_path / "cache"

    result = _run_apply(root, cache, "--dry-run")

    assert result.returncode == 0
    assert not cache.exists()
    assert "可套用" in result.stdout


def test_apply_replaces_existing_version_atomically(tmp_path: Path):
    root = _make_provision(tmp_path / "provision")
    cache = tmp_path / "cache"
    (cache / "t1" / WHEELS_DIRNAME).mkdir(parents=True)
    (cache / "t1" / WHEELS_DIRNAME / "ancient-0.1.whl").write_bytes(b"old")
    (cache / "t1" / DEPPACK_MANIFEST).write_text("{}", encoding="utf-8")

    result = _run_apply(root, cache)

    assert result.returncode == 0
    assert not (cache / "t1" / WHEELS_DIRNAME / "ancient-0.1.whl").exists()  # 舊版整個換掉
    assert (cache / "t1" / WHEELS_DIRNAME / "torch-2.6.0.whl").is_file()
    assert not any(p.name.startswith("t1.old-") for p in cache.iterdir())    # 備份已清掉


def test_apply_cleans_stale_temp_from_previous_crash(tmp_path: Path):
    root = _make_provision(tmp_path / "provision")
    cache = tmp_path / "cache"
    (cache / ".applying-t1" / WHEELS_DIRNAME).mkdir(parents=True)
    (cache / "t1.old-0").mkdir(parents=True)

    _run_apply(root, cache)

    names = sorted(p.name for p in cache.iterdir())
    assert names == ["t1"]


def test_apply_selected_tools_only(tmp_path: Path):
    root = _make_provision(tmp_path / "provision", tools={
        "t1": {"tiny-1.0.whl": SMALL},
        "t2": {"other-1.0.whl": SMALL},
    }, isolate=False)
    cache = tmp_path / "cache"

    result = _run_apply(root, cache, "--tools", "t2")

    assert result.returncode == 0
    assert (cache / "t2").is_dir()
    assert not (cache / "t1").exists()


def test_apply_unknown_tool_is_usage_error(tmp_path: Path):
    root = _make_provision(tmp_path / "provision")
    result = _run_apply(root, tmp_path / "cache", "--tools", "ghost")
    assert result.returncode == 2
    assert "沒有的工具" in result.stdout


def test_apply_on_non_provision_dir(tmp_path: Path):
    (tmp_path / "empty").mkdir()
    shutil.copy2(APPLY_PY, tmp_path / "empty" / "apply.py")
    result = _run_apply(tmp_path / "empty", tmp_path / "cache")
    assert result.returncode == 2
    assert "不是補給包" in result.stdout


# ── 缺 big-deps（SPEC §6.3）────────────────────────────────────────────────────

def test_missing_big_dep_skips_tool_and_leaves_no_half_pack(tmp_path: Path):
    root = _make_provision(tmp_path / "provision")
    (root / BIG_DEPS_DIRNAME / "torch-2.6.0.whl").unlink()      # 使用者分開搬運，忘了放回來
    cache = tmp_path / "cache"

    result = _run_apply(root, cache)

    assert result.returncode == 1
    assert "跳過" in result.stdout
    assert "大型相依未就位" in result.stdout
    assert "torch-2.6.0.whl" in result.stdout
    assert "放回" in result.stdout                               # 訊息可行動
    assert "影響的工具：t1" in result.stdout
    # 關鍵：不可留下 wheels 不完整但 deppack.json 存在的目錄
    assert not (cache / "t1").exists()
    assert list(cache.iterdir()) == []      # 連暫存目錄都不留


def test_other_tools_still_applied_when_one_lacks_big_dep(tmp_path: Path):
    root = _make_provision(tmp_path / "provision", tools={
        "needs_big": {"torch-2.6.0.whl": BIG, "tiny-1.0.whl": SMALL},
        "small_only": {"tiny2-1.0.whl": SMALL},
    })
    (root / BIG_DEPS_DIRNAME / "torch-2.6.0.whl").unlink()
    cache = tmp_path / "cache"

    result = _run_apply(root, cache)

    assert result.returncode == 1                    # 有跳過 → 非零
    assert (cache / "small_only").is_dir()           # 其餘工具照常套用
    assert not (cache / "needs_big").exists()


def test_restoring_big_dep_makes_apply_succeed(tmp_path: Path):
    """把檔案放回去再跑一次就好——REPORT.md 承諾的行為。"""
    root = _make_provision(tmp_path / "provision")
    big = root / BIG_DEPS_DIRNAME / "torch-2.6.0.whl"
    stashed = tmp_path / "torch-2.6.0.whl"
    shutil.move(str(big), str(stashed))
    cache = tmp_path / "cache"

    assert _run_apply(root, cache).returncode == 1
    shutil.move(str(stashed), str(big))
    result = _run_apply(root, cache)

    assert result.returncode == 0
    assert (cache / "t1" / WHEELS_DIRNAME / "torch-2.6.0.whl").is_file()


# ── 損毀偵測 ───────────────────────────────────────────────────────────────────

def test_truncated_wheel_is_skipped_before_copying(tmp_path: Path):
    root = _make_provision(tmp_path / "provision")
    wheel = root / PACKS_DIRNAME / "t1" / WHEELS_DIRNAME / "tiny-1.0.whl"
    wheel.write_bytes(wheel.read_bytes()[:-3])
    cache = tmp_path / "cache"

    result = _run_apply(root, cache)

    assert result.returncode == 1
    assert "大小不符" in result.stdout
    assert not (cache / "t1").exists()


def test_silently_corrupted_wheel_caught_after_assembly(tmp_path: Path):
    """大小一樣、內容不同：只有組裝後的 sha256 全量驗證抓得到。"""
    root = _make_provision(tmp_path / "provision")
    wheel = root / PACKS_DIRNAME / "t1" / WHEELS_DIRNAME / "tiny-1.0.whl"
    data = bytearray(wheel.read_bytes())
    data[-1] ^= 0xFF
    wheel.write_bytes(bytes(data))
    cache = tmp_path / "cache"

    result = _run_apply(root, cache)

    assert result.returncode == 1
    assert "sha256 不符" in result.stdout
    assert not (cache / "t1").exists()               # 失敗 → 目標維持原樣
    assert "維持原樣" in result.stdout


def test_pack_without_manifest_fails_that_tool_only(tmp_path: Path):
    root = _make_provision(tmp_path / "provision", tools={
        "good": {"tiny-1.0.whl": SMALL},
        "bad": {"tiny2-1.0.whl": SMALL},
    }, isolate=False)
    (root / PACKS_DIRNAME / "bad" / DEPPACK_MANIFEST).unlink()
    cache = tmp_path / "cache"

    result = _run_apply(root, cache)

    assert result.returncode == 1
    assert (cache / "good").is_dir()
    assert not (cache / "bad").exists()
    assert "不是一個 dep-pack" in result.stdout


def test_extra_wheel_in_pack_is_rejected(tmp_path: Path):
    root = _make_provision(tmp_path / "provision", isolate=False)
    make_wheel(root / PACKS_DIRNAME / "t1" / WHEELS_DIRNAME / "stowaway-9.9.whl")
    cache = tmp_path / "cache"

    result = _run_apply(root, cache)
    assert result.returncode == 1
    assert "多餘" in result.stdout


def test_missing_ordinary_wheel_reports_as_broken_not_recoverable(tmp_path: Path):
    """provision.json 說它不是大 wheel → 訊息要說「補給包缺少」而不是「未就位」。"""
    root = _make_provision(tmp_path / "provision")
    (root / PACKS_DIRNAME / "t1" / WHEELS_DIRNAME / "tiny-1.0.whl").unlink()
    result = _run_apply(root, tmp_path / "cache")
    assert "補給包缺少 wheel" in result.stdout


@pytest.mark.parametrize("flag", ["--dry-run", ""])
def test_exit_code_zero_only_when_everything_ok(tmp_path: Path, flag: str):
    root = _make_provision(tmp_path / "provision")
    extra = [flag] if flag else []
    assert _run_apply(root, tmp_path / "cache", *extra).returncode == 0
