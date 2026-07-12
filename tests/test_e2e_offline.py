"""M5 端到端：連網機產包 → 搬運 → 離線機 apply → 平台 engine 離線裝進 per-tool venv。

這是整個專案存在理由的驗證：**在沒有網路的電腦上，工具的相依真的裝得起來。**

預設不跑（需要網路 + 真實平台專案）：
    py -3.11 -m pytest tests/test_e2e_offline.py --network --project-root C:\\code\\claude\\nativeApp

「真的沒連網」怎麼證明：安裝那一步把 `PIP_INDEX_URL` 指向一個不存在的位址。
若相依鏈中任何一環漏掉 `--no-index`，pip 會去連那個位址並失敗——測試就會紅。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import run_python

pytestmark = [pytest.mark.network, pytest.mark.platform_repo]

CLI = Path(__file__).resolve().parents[1] / "provision.py"

TOOL_ID = "e2e_smoke"
# cowsay 是純 py wheel（~40 KB，留在 pack 內）；numpy 的 win/cp311 wheel ~12 MB
# （> 1 MB 門檻 → 進 big-deps）。一次驗證「一般相依」與「大型相依」兩條路。
REQUIRES = ["cowsay==6.1", "numpy==2.2.3"]
BIG_WHEEL_PREFIX = "numpy-2.2.3"


@pytest.fixture
def mini_project(tmp_path: Path, project_root_opt: Path) -> Path:
    """造一個迷你平台專案：真平台的 core/ + 一個宣告 requires 的工具。

    用真平台的 core/deppack.py（而非自己捏一份），這樣本測試同時守住 SPEC D4：
    產包格式永遠等於「將要吃這包的那個 engine」的格式。
    """
    real_engine = project_root_opt / "sidecar" / "python-engine"
    project = tmp_path / "mini-platform"
    engine = project / "sidecar" / "python-engine"
    (engine / "core").mkdir(parents=True)
    (engine / "engine.py").write_text("# mini engine for e2e\n", encoding="utf-8")
    for name in ("__init__.py", "deppack.py", "tool_deps.py"):
        shutil.copy2(real_engine / "core" / name, engine / "core" / name)

    tool_dir = engine / "plugins" / "e2e" / "modules" / TOOL_ID
    tool_dir.mkdir(parents=True)
    requires_yaml = "\n".join(f"  - {r}" for r in REQUIRES)
    (tool_dir / "plugin.yaml").write_text(
        f"id: {TOOL_ID}\nname: E2E 冒煙工具\nrunner: cv_framework\n"
        f"requires:\n{requires_yaml}\n",
        encoding="utf-8",
    )
    return project


def test_offline_provision_end_to_end(mini_project: Path, tmp_path: Path, project_root_opt: Path):
    provision_dir = tmp_path / "provision"

    # ── 1. 連網機：產包 ────────────────────────────────────────────────────────
    built = run_python([
        str(CLI), "build", str(mini_project),
        "--dest", str(provision_dir),
        "--big-threshold-mb", "1",
    ])
    assert built.returncode == 0, built.stdout + built.stderr

    packs = provision_dir / "packs" / TOOL_ID
    big_deps = provision_dir / "big-deps"
    assert (packs / "deppack.json").is_file()
    assert (provision_dir / "REPORT.md").is_file()
    assert (provision_dir / "apply.py").is_file()

    # 大 wheel 被隔離出去；一般 wheel 留在 pack 內
    big_wheels = sorted(p.name for p in big_deps.glob("*.whl"))
    assert any(n.startswith(BIG_WHEEL_PREFIX) for n in big_wheels), big_wheels
    pack_wheels = sorted(p.name for p in (packs / "wheels").glob("*.whl"))
    assert any(n.startswith("cowsay") for n in pack_wheels), pack_wheels
    assert not any(n.startswith(BIG_WHEEL_PREFIX) for n in pack_wheels)

    # deppack.json 仍然描述**全部** wheel（隔離只是搬運期暫態，SPEC §6.2）
    manifest = json.loads((packs / "deppack.json").read_text(encoding="utf-8"))
    listed = {w["name"] for w in manifest["wheels"]}
    assert listed == set(pack_wheels) | set(big_wheels)
    assert manifest["python_tag"] == "cp311" and manifest["platform_tag"] == "win_amd64"

    # REPORT.md 把大相依講清楚
    report = (provision_dir / "REPORT.md").read_text(encoding="utf-8")
    assert "分開搬運" in report and BIG_WHEEL_PREFIX in report

    # ── 2. 搬運後驗證 ─────────────────────────────────────────────────────────
    verified = run_python([str(CLI), "verify", str(provision_dir)])
    assert verified.returncode == 0, verified.stdout

    # ── 3. 大相依「另外處理」：抽走 → apply 應跳過且不留半套 ────────────────
    stash = tmp_path / "stash"
    stash.mkdir()
    for wheel in list(big_deps.glob("*.whl")):
        shutil.move(str(wheel), str(stash / wheel.name))

    cache = tmp_path / "deppack-cache"
    partial = run_python([str(provision_dir / "apply.py"), "--deppack-cache", str(cache)])
    assert partial.returncode == 1
    assert "大型相依未就位" in partial.stdout
    assert not (cache / TOOL_ID).exists()          # 不留半套

    # ── 4. 放回去 → apply 成功 ────────────────────────────────────────────────
    for wheel in stash.glob("*.whl"):
        shutil.move(str(wheel), str(big_deps / wheel.name))

    applied = run_python([str(provision_dir / "apply.py"), "--deppack-cache", str(cache)])
    assert applied.returncode == 0, applied.stdout
    assembled = sorted(p.name for p in (cache / TOOL_ID / "wheels").glob("*.whl"))
    assert assembled == sorted(listed)             # 大 wheel 已回填

    # ── 5. 平台 engine：驗章 + 離線裝進 per-tool venv ─────────────────────────
    _assert_engine_installs_offline(project_root_opt, cache, tmp_path)


def _assert_engine_installs_offline(project_root: Path, cache: Path, tmp_path: Path) -> None:
    """用**真平台**的 core.tool_deps 走一次工具首啟的相依安裝，全程斷網。

    在子程序裡跑，避免把平台的 core 套件載進 pytest 的 sys.modules。
    """
    venvs = tmp_path / "tool-venvs"
    script = tmp_path / "engine_side.py"
    script.write_text(
        "import json, os, sys\n"
        f"sys.path.insert(0, r'{project_root / 'sidecar' / 'python-engine'}')\n"
        "from core import tool_deps\n"
        f"result = tool_deps.ensure_tool_deps({TOOL_ID!r}, {REQUIRES!r})\n"
        "print(json.dumps({'ok': result.ok, 'message': result.message,\n"
        "                  'site_packages': result.site_packages}))\n",
        encoding="utf-8",
    )

    env = {
        **os.environ,
        "PYTHONIOENCODING": "utf-8",
        "CIM_DEPPACK_CACHE": str(cache),        # ← engine 從補給包找 wheelhouse
        "CIM_TOOL_VENVS_DIR": str(venvs),
        # 斷網證明：pip 若沒帶 --no-index 就會去連這個不存在的 index 並失敗
        "PIP_INDEX_URL": "http://127.0.0.1:1/simple",
        "PIP_NO_INPUT": "1",
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
    }
    proc = subprocess.run(
        [sys.executable, str(script)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", env=env, timeout=900,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    result = json.loads(proc.stdout.strip().splitlines()[-1])
    assert result["ok"], result["message"]
    assert result["site_packages"], result

    # 真的裝進去了嗎？從該 venv 的 site-packages import 一次
    site_packages = result["site_packages"][0]
    check = subprocess.run(
        [sys.executable, "-c", "import cowsay, numpy; print(numpy.__version__)"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env={**os.environ, "PYTHONPATH": site_packages}, timeout=120,
    )
    assert check.returncode == 0, check.stdout + check.stderr
    assert check.stdout.strip() == "2.2.3"


def test_second_build_is_incremental(mini_project: Path, tmp_path: Path):
    """同樣的 requires 重跑 build → 沿用快取，不再 pip download（SPEC §8.1）。"""
    dest = tmp_path / "provision"
    first = run_python([str(CLI), "build", str(mini_project), "--dest", str(dest),
                        "--big-threshold-mb", "1"])
    assert first.returncode == 0, first.stdout
    assert "[產包]" in first.stdout

    second = run_python([str(CLI), "build", str(mini_project), "--dest", str(dest),
                         "--big-threshold-mb", "1"])
    assert second.returncode == 0, second.stdout
    assert "[沿用]" in second.stdout
    assert "[產包]" not in second.stdout


def test_selfcheck_catches_unresolvable_requires(mini_project: Path, tmp_path: Path):
    """宣告一個沒有 win/cp311 wheel 的相依 → 在開發機就失敗，不會帶到工廠才發現。"""
    tool_dir = mini_project / "sidecar" / "python-engine" / "plugins" / "e2e" / "modules" / TOOL_ID
    (tool_dir / "plugin.yaml").write_text(
        f"id: {TOOL_ID}\nrequires:\n  - this-package-does-not-exist-xyzzy\n", encoding="utf-8",
    )
    result = run_python([str(CLI), "build", str(mini_project), "--dest", str(tmp_path / "p")])

    assert result.returncode == 1
    assert "[失敗]" in result.stdout
    assert not (tmp_path / "p" / "packs" / TOOL_ID).exists()      # 不留半套
    report = (tmp_path / "p" / "REPORT.md").read_text(encoding="utf-8")
    assert "產包失敗" in report
