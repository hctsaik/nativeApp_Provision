"""交付與更新來源:兩種匯出是兩件事。

export_full_tree() 產出的資料夾必須「在一台什麼都沒有的機器上雙擊就能跑」;
export_update() 產出的是自動更新來源,本來就跑不起來 —— 以前 GUI 的「匯出交付」
按鈕呼叫的是後者,交出去的資料夾連 bootstrap\\ 和 start.bat 都沒有。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from provision_builder.streamlit_desktop import runtime as runtime_mod
from provision_builder.streamlit_desktop import store_builder
from provision_builder.streamlit_desktop.device import integrity
from provision_builder.streamlit_desktop.models import BuildRequest

LOCK = "streamlit==1.40.0\n"


def make_project(tmp_path: Path, name: str, *, lock: str = LOCK) -> BuildRequest:
    project = tmp_path / f"proj-{store_builder.slugify(name)}"
    (project).mkdir(parents=True)
    (project / "app.py").write_text("import streamlit as st\nst.write('READY')\n",
                                    encoding="utf-8")
    (project / "requirements.txt").write_text(lock, encoding="utf-8")
    shell = tmp_path / "cim-light.exe"
    if not shell.exists():
        shell.write_bytes(b"MZ shell")
    template = tmp_path / "rt-template"
    if not template.exists():
        (template / "Lib" / "site-packages").mkdir(parents=True)
        (template / "python.exe").write_bytes(b"MZ python")
        (template / "Scripts").mkdir()
    return BuildRequest(project_dir=project, entrypoint=project / "app.py",
                        display_name=name, output_dir=tmp_path / "unused",
                        shell_exe=shell, runtime_template=template)


@pytest.fixture
def build_request(tmp_path: Path) -> BuildRequest:
    return make_project(tmp_path, "Demo App")


@pytest.fixture
def stub_toolchain(monkeypatch):
    """No real pip / no real interpreter probing. `pip freeze` answers with
    whatever the project's lock actually pins, so a lock change really does move
    the fingerprint (which is what the incremental-package guard turns on)."""
    monkeypatch.setattr(store_builder, "_python_version_of", lambda _p: "3.11.9")

    def fake_freeze(python: Path) -> list[str]:
        # The staged runtime carries the lock the builder just wrote into it.
        lock = Path(python).parent / "lock.txt"
        pins = lock.read_text("utf-8").splitlines() if lock.is_file() else []
        return [p for p in pins if p.strip()] + ["setuptools==69.0"]

    monkeypatch.setattr(store_builder, "_freeze", fake_freeze)
    monkeypatch.setattr(runtime_mod, "install_requirements", lambda *_a, **_k: None)
    monkeypatch.setattr(runtime_mod, "verify_imports", lambda *_a, **_k: None)
    monkeypatch.setattr(store_builder.imports_mod, "missing_dependencies",
                        lambda *_a, **_k: [])


# ── 完整交付 ─────────────────────────────────────────────────────────────────

def test_full_export_is_runnable_on_a_bare_machine(build_request, stub_toolchain, tmp_path):
    root = tmp_path / "ROOT"
    result = store_builder.build_into_store(build_request, root, version="v1.0.0")
    assert result.ok, result.errors
    app = build_request.app_id

    # Build-machine noise that must NEVER be delivered.
    logs = root / "apps" / app / "data" / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    (logs / "bootstrap-old.log").write_text("build machine", encoding="utf-8")
    (root / "apps" / app / "data" / "leases").mkdir(parents=True, exist_ok=True)
    (root / "apps" / app / "data" / "leases" / "lease-1.json").write_text("{}",
                                                                          encoding="utf-8")
    debris = root / "apps" / app / "versions" / ".staging-deadbeef"
    debris.mkdir(parents=True, exist_ok=True)
    (debris / "half.txt").write_text("x", encoding="utf-8")

    out = tmp_path / "deliver"
    export = store_builder.export_full_tree(root, out)

    assert export.kind == "full"
    assert export.apps == [app] and export.versions == ["v1.0.0"]
    assert export.includes_runtime is True
    assert export.total_mb >= 0
    assert Path(export.out_dir) == out

    # 1. it can boot: bootstrap + an entry point + tools
    assert (out / "bootstrap" / "bootstrap.py").is_file()
    assert (out / "bootstrap" / "gc.py").is_file()
    assert (out / "start.bat").is_file()
    assert (out / "tools" / "gc.bat").is_file()
    assert (out / "tools" / "admin.bat").is_file()
    assert (out / "tools" / f"admin-{app}.bat").is_file()
    assert (out / "tools" / store_builder.WEBVIEW2_BAT_NAME).is_file()
    assert (out / store_builder.README_NAME).is_file()

    # 2. it knows which version is current
    state = json.loads((out / "apps" / app / "state" / "state.json").read_text("utf-8"))
    assert state["current"] == "v1.0.0"

    # 3. the version is immediately runnable (nothing re-verifies it at first boot)
    vdir = out / "apps" / app / "versions" / "v1.0.0"
    assert integrity.is_complete(vdir)
    assert integrity.verify_tree(vdir) == []

    # 4. the shared deps re-earn their sentinel on the target
    runtime_out = out / "deps" / "runtimes" / result.fingerprint
    assert runtime_out.is_dir() and not integrity.is_complete(runtime_out)
    shells = [p for p in (out / "deps" / "shells").iterdir() if p.is_dir()]
    assert len(shells) == 1 and not integrity.is_complete(shells[0])

    # 5. no build-machine state and no debris travelled
    assert not (out / "apps" / app / "data").exists()
    assert not list((out / "apps" / app / "versions").glob(".staging-*"))


def test_full_export_of_one_app_leaves_the_other_behind(stub_toolchain, tmp_path):
    root = tmp_path / "ROOT"
    first = make_project(tmp_path, "Alpha Viewer")
    second = make_project(tmp_path, "Beta Viewer")
    assert store_builder.build_into_store(first, root, version="v1").ok
    assert store_builder.build_into_store(second, root, version="v1").ok

    out = tmp_path / "deliver-alpha"
    export = store_builder.export_full_tree(root, out, app_id=first.app_id)
    assert export.apps == [first.app_id]
    assert (out / "apps" / first.app_id).is_dir()
    assert not (out / "apps" / second.app_id).exists()
    assert (out / f"start-{first.app_id}.bat").is_file()
    assert not (out / f"start-{second.app_id}.bat").exists()
    # the console of an app that is not in this delivery must not be offered
    assert (out / "tools" / f"admin-{first.app_id}.bat").is_file()
    assert not (out / "tools" / f"admin-{second.app_id}.bat").exists()


def test_full_export_rejects_an_unknown_app(build_request, stub_toolchain, tmp_path):
    root = tmp_path / "ROOT"
    store_builder.build_into_store(build_request, root, version="v1.0.0")
    with pytest.raises(store_builder.StoreBuildError, match="沒有 app"):
        store_builder.export_full_tree(root, tmp_path / "out", app_id="app-nope")


# ── 自動更新來源 ─────────────────────────────────────────────────────────────

def test_update_export_refuses_to_drop_a_changed_runtime(build_request, stub_toolchain,
                                                         tmp_path):
    """v2 moved to a new dependency lock, so its interpreter does not exist on the
    target. An incremental package would install, promote, fail to start and roll
    back — on every machine, forever."""
    root = tmp_path / "ROOT"
    first = store_builder.build_into_store(build_request, root, version="v1.0.0")
    assert first.ok, first.errors

    (build_request.project_dir / "requirements.txt").write_text(
        "streamlit==1.40.0\npandas==2.2.0\n", encoding="utf-8")
    second = store_builder.build_into_store(build_request, root, version="v2.0.0")
    assert second.ok, second.errors
    assert second.fingerprint != first.fingerprint

    with pytest.raises(store_builder.StoreBuildError, match="必須勾選「包含 runtime」"):
        store_builder.export_update(root, build_request.app_id, "v2.0.0",
                                    tmp_path / "usb", include_runtime=False)

    # and it is exportable the moment the runtime rides along
    export = store_builder.export_update(root, build_request.app_id, "v2.0.0",
                                         tmp_path / "usb-full", include_runtime=True)
    assert export.kind == "update" and export.includes_runtime
    assert (export.out_dir / "runtimes" / second.fingerprint).is_dir()
    assert (export.out_dir / "release.json").is_file()


def test_update_export_is_incremental_when_the_lock_did_not_move(build_request,
                                                                 stub_toolchain, tmp_path):
    root = tmp_path / "ROOT"
    store_builder.build_into_store(build_request, root, version="v1.0.0")
    store_builder.build_into_store(build_request, root, version="v1.1.0")

    export = store_builder.export_update(root, build_request.app_id, "v1.1.0",
                                         tmp_path / "usb", include_runtime=False)
    assert export.kind == "update" and export.includes_runtime is False
    assert not (export.out_dir / "runtimes").exists()
    payload = export.out_dir / "versions" / "v1.1.0"
    assert not integrity.is_complete(payload)      # the target must earn the sentinel
    assert integrity.verify_tree(payload) == []


# ── .bat 產生 ────────────────────────────────────────────────────────────────

def test_admin_console_is_emitted_per_app(stub_toolchain, tmp_path):
    """The old admin.bat hardcoded apps[0] while wearing THIS build's display name:
    in a two-app store, 「退回上一版」 silently rolled back the wrong app."""
    root = tmp_path / "ROOT"
    first = make_project(tmp_path, "Alpha Viewer")
    second = make_project(tmp_path, "Beta Viewer")
    store_builder.build_into_store(first, root, version="v1")
    store_builder.build_into_store(second, root, version="v1")

    tools = root / "tools"
    alpha = (tools / f"admin-{first.app_id}.bat").read_text("utf-8")
    beta = (tools / f"admin-{second.app_id}.bat").read_text("utf-8")
    assert f"--app {first.app_id}" in alpha and second.app_id not in alpha
    assert f"--app {second.app_id}" in beta and first.app_id not in beta
    assert "Alpha Viewer" in alpha and "Beta Viewer" in beta

    chooser = (tools / "admin.bat").read_text("utf-8")
    assert f"admin-{first.app_id}.bat" in chooser
    assert f"admin-{second.app_id}.bat" in chooser

    # every menu item the docs name, and a gc that can actually free anything
    for item in ("--status", "--rollback", "--rollback-to", "--install",
                 "--set-update-source", "--clear-failed", 'gc.py" --apply'):
        assert item in alpha, item
    assert "cd /d" not in alpha and 'pushd "%~dp0.."' in alpha


def test_a_chinese_display_name_does_not_blow_up_the_tools_write(stub_toolchain, tmp_path):
    """`encoding="ascii"` + a Chinese name = UnicodeEncodeError, which is NOT an
    OSError — it sailed past `except OSError` and killed a build whose version dir
    was already complete (and therefore immutable: unrecoverable)."""
    root = tmp_path / "ROOT"
    request = make_project(tmp_path, "產線 檢視器")
    result = store_builder.build_into_store(request, root, version="v1.0.0")

    assert result.ok, result.errors
    assert not any("tools" in w for w in result.warnings), result.warnings
    admin = (root / "tools" / f"admin-{request.app_id}.bat").read_text("utf-8")
    assert "產線 檢視器" in admin
    assert "chcp 65001 >nul 2>&1" in admin.splitlines()[:5]   # codepage BEFORE any echo
    assert 'set "PYTHONUTF8=1"' in admin
    (root / "tools" / "gc.bat").read_text("utf-8")          # decodes as utf-8
    assert "產線 檢視器" in (root / "start.bat").read_text("utf-8")


def test_start_bat_checks_webview2_before_starting_anything(build_request, stub_toolchain,
                                                            tmp_path):
    root = tmp_path / "ROOT"
    store_builder.build_into_store(build_request, root, version="v1.0.0")
    start = (root / "start.bat").read_text("utf-8")
    assert store_builder.WEBVIEW2_CLIENT in start
    assert "HKLM\\SOFTWARE\\WOW6432Node\\Microsoft\\EdgeUpdate\\Clients" in start
    assert "HKCU\\SOFTWARE\\Microsoft\\EdgeUpdate\\Clients" in start
    assert store_builder.WEBVIEW2_BAT_NAME in start
    # the check must come BEFORE we hand over to bootstrap.py
    assert start.index("reg query") < start.index("bootstrap.py")

    installer = (root / "tools" / store_builder.WEBVIEW2_BAT_NAME).read_text("utf-8")
    assert "MicrosoftEdgeWebview2Setup.exe" in installer
    assert "/silent /install" in installer
    assert store_builder.WEBVIEW2_DOWNLOAD in installer


def test_readme_tells_the_truth_about_the_port_and_the_button(build_request,
                                                              stub_toolchain, tmp_path):
    root = tmp_path / "ROOT"
    store_builder.build_into_store(build_request, root, version="v1.0.0")
    readme = (root / store_builder.README_NAME).read_text("utf-8")

    assert "若 0 埠" not in readme                    # preferred_port=0 = pick a free one
    assert "自動挑一個沒被占用的埠" in readme
    assert "工作流程" in readme and "Start" in readme
    assert "「啟動」" not in readme                    # no such button exists on screen
    assert "WebView2" in readme
    assert "不需要安裝任何東西" not in readme
    assert "SmartScreen" in readme and "仍要執行" in readme


# ── 取消 ─────────────────────────────────────────────────────────────────────

def test_cancel_leaves_no_half_built_version(build_request, stub_toolchain, tmp_path):
    root = tmp_path / "ROOT"
    result = store_builder.build_into_store(build_request, root, version="v1.0.0",
                                            should_cancel=lambda: True)
    assert not result.ok and result.cancelled
    assert not (root / "apps" / build_request.app_id / "versions" / "v1.0.0").exists()
    assert "取消" in result.summary()


def test_cancel_after_the_runtime_still_leaves_a_clean_tree(build_request, stub_toolchain,
                                                            tmp_path):
    root = tmp_path / "ROOT"
    calls = {"n": 0}

    def should_cancel() -> bool:
        calls["n"] += 1
        return calls["n"] > 2          # let the runtime + shell through, then cancel

    result = store_builder.build_into_store(build_request, root, version="v1.0.0",
                                            should_cancel=should_cancel)
    assert not result.ok and result.cancelled
    versions = root / "apps" / build_request.app_id / "versions"
    assert not versions.exists() or not any(versions.iterdir())


# ── warnings ─────────────────────────────────────────────────────────────────

def test_store_build_reports_the_big_file_warnings(build_request, stub_toolchain, tmp_path):
    """StoreBuildResult.warnings was declared, rendered by the GUI, and never
    filled: in Store mode the 85 MB screen recording went out silently."""
    fat = build_request.project_dir / "demo.mp4"
    fat.write_bytes(b"\0" * (11 * 1024 * 1024))

    result = store_builder.build_into_store(build_request, tmp_path / "ROOT",
                                            version="v1.0.0")
    assert result.ok, result.errors
    assert any("demo.mp4" in w for w in result.warnings), result.warnings
