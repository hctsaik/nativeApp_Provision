"""測試共用 fixtures。

原則（SPEC §12）：單元測試**一律不連網**。真正需要 pip download 的整合測試標
`@pytest.mark.network`，預設不跑。
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))
# Top-level sibling packages of the builder (control_plane / native_agent /
# build_worker / web_console) live at the repo root, not under src/. Put the
# root on the path so tests can import them without a src-layout move.
sys.path.insert(0, str(REPO_ROOT))


def run_python(args: list[str]) -> subprocess.CompletedProcess:
    """跑一支 CLI 並以 UTF-8 讀回 stdout。

    子程序的 stdout 預設用 locale 編碼（本機 CP950）。測試要斷言繁中訊息，就必須
    讓兩邊講同一種編碼——用 PYTHONIOENCODING 而不是改產品程式碼，因為真實使用者
    的 CP950 主控台需要的正是 locale 編碼（apply.py 的 guard_console_encoding 負責不炸）。
    """
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    return subprocess.run(
        [sys.executable, *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace", env=env,
    )


def pytest_addoption(parser):
    parser.addoption(
        "--project-root",
        action="store",
        default=None,
        help="真實 CIM 平台專案根（給 platform_repo 標記的測試用）",
    )
    parser.addoption(
        "--network",
        action="store_true",
        default=False,
        help="也跑需要網路的整合測試（真的 pip download）",
    )


def pytest_collection_modifyitems(config, items):
    """`network` 標記的測試預設跳過（SPEC §12：單元測試一律不連網）。"""
    if config.getoption("--network"):
        return
    skip = pytest.mark.skip(reason="需要網路；加 --network 才跑")
    for item in items:
        if "network" in item.keywords:
            item.add_marker(skip)


@pytest.fixture
def project_root_opt(request):
    value = request.config.getoption("--project-root")
    if not value:
        pytest.skip("需要 --project-root 指向真實平台專案")
    return Path(value)


# ── 假 wheel / 假 pack 建構器 ──────────────────────────────────────────────────

def make_wheel(path: Path, payload: bytes = b"fake wheel") -> tuple[str, int]:
    """造一個「長得像 wheel」的 zip 檔（內容不重要，我們只驗雜湊與檔名）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("payload.txt", payload)
    data = path.read_bytes()
    return hashlib.sha256(data).hexdigest(), len(data)


def make_pack(
    packs_dir: Path,
    tool_id: str,
    wheels: dict[str, bytes],
    *,
    requires: list[str] | None = None,
    python_tag: str = "cp311",
    platform_tag: str = "win_amd64",
    fingerprint: str = "fp-deadbeef",
) -> dict:
    """造一個完整的 dep-pack（wheels 全在 pack 內），回 manifest dict。"""
    pack_dir = packs_dir / tool_id
    wheels_dir = pack_dir / "wheels"
    wheels_dir.mkdir(parents=True, exist_ok=True)
    entries = []
    for name, payload in sorted(wheels.items()):
        digest, size = make_wheel(wheels_dir / name, payload)
        entries.append({"name": name, "sha256": digest, "size": size})
    manifest = {
        "schema": 1,
        "tool_id": tool_id,
        "requires": sorted(requires or ["dummy"]),
        "requires_fingerprint": fingerprint,
        "python_tag": python_tag,
        "platform_tag": platform_tag,
        "created_at": "2026-07-10T00:00:00Z",
        "wheels": entries,
    }
    (pack_dir / "deppack.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )
    return manifest


def write_provision_json(root: Path, tools: list[dict], big_deps: list[dict]) -> None:
    payload = {
        "format_version": 1,
        "builder_version": "test",
        "created_at": "2026-07-10T00:00:00Z",
        "source_project": "C:\\fake\\project",
        "git": {"platform_commit": None, "submodules": {}},
        "target": {"platform_tag": "win_amd64", "python_version": "3.11", "abi": "cp311"},
        "scanned_roots": ["scripts/*/plugin.yaml", "plugins/*/modules/*/plugin.yaml"],
        "big_threshold_mb": 100,
        "tools": tools,
        "big_deps": big_deps,
        "skipped_tools": [],
        "failed_tools": [],
    }
    (root / "provision.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


@pytest.fixture
def fake_project(tmp_path: Path):
    """造一個最小的「CIM 平台專案」骨架（含 engine.py 讓 gateway 認得）。"""
    engine = tmp_path / "sidecar" / "python-engine"
    (engine / "scripts").mkdir(parents=True)
    (engine / "plugins").mkdir(parents=True)
    (engine / "engine.py").write_text("# fake engine\n", encoding="utf-8")
    return tmp_path


def add_plugin_yaml(engine_root: Path, rel: str, body: str) -> Path:
    path = engine_root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path
