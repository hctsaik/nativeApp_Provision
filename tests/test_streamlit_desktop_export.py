"""交付與更新來源:兩種匯出是兩件事。

export_full_tree() 產出的資料夾必須「在一台什麼都沒有的機器上雙擊就能跑」;
export_update() 產出的是自動更新來源,本來就跑不起來 —— 以前 GUI 的「匯出交付」
按鈕呼叫的是後者,交出去的資料夾連 bootstrap\\ 和 start.bat 都沒有。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from provision_builder.streamlit_desktop import imports as imports_mod
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
    # The import gate runs on EVERY build now (including the ones that reuse a
    # runtime), so it must answer with a real report, not a stand-in empty list.
    monkeypatch.setattr(store_builder.imports_mod, "missing_dependencies",
                        lambda *_a, **_k: imports_mod.MissingReport())


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


def test_update_needs_runtime_is_asked_before_the_export_can_refuse(build_request,
                                                                    stub_toolchain, tmp_path):
    """S2. The GUI must be able to DEFAULT the 「包含 runtime」 checkbox, not
    discover the answer as an exception after the operator has already picked a
    destination folder. The raise inside export_update() stays — it is the safety
    net — but nobody should have to hit it to find out."""
    root = tmp_path / "ROOT"
    app = build_request.app_id
    assert store_builder.build_into_store(build_request, root, version="v1.0.0").ok

    # same lock -> the target already has this runtime: 17 MB will do
    assert store_builder.build_into_store(build_request, root, version="v1.1.0").ok
    assert store_builder.update_needs_runtime(root, app, "v1.1.0") is False

    # the lock moved -> the target does NOT have the interpreter this version names
    (build_request.project_dir / "requirements.txt").write_text(
        "streamlit==1.40.0\npandas==2.2.0\n", encoding="utf-8")
    assert store_builder.build_into_store(build_request, root, version="v2.0.0").ok
    assert store_builder.update_needs_runtime(root, app, "v2.0.0") is True

    # ...and that is exactly the case export_update() refuses without the runtime
    with pytest.raises(store_builder.StoreBuildError, match="必須勾選「包含 runtime」"):
        store_builder.export_update(root, app, "v2.0.0", tmp_path / "usb",
                                    include_runtime=False)

    # it is a question, not a gate: an unreadable tree answers "include it",
    # which is the answer that always works, and it never raises.
    assert store_builder.update_needs_runtime(root, app, "v9.9.9") is True
    assert store_builder.update_needs_runtime(root, "app-nope", "v1.0.0") is True
    assert store_builder.update_needs_runtime(tmp_path / "nothing", app, "v1") is True


def test_the_first_update_package_of_an_app_carries_the_runtime(build_request,
                                                                stub_toolchain, tmp_path):
    """Nothing to compare against = the target has nothing to reuse. Defaulting to
    an incremental package here ships a version whose interpreter does not exist
    on the far side."""
    root = tmp_path / "ROOT"
    store_builder.build_into_store(build_request, root, version="v1.0.0")
    assert store_builder.update_needs_runtime(root, build_request.app_id, "v1.0.0") is True


def test_export_update_says_something_while_it_copies(build_request, stub_toolchain,
                                                      tmp_path):
    """A 500 MB export that prints one line and then goes quiet for 90 seconds is
    indistinguishable from a hang, and a killed export is a half-copied one."""
    root = tmp_path / "ROOT"
    store_builder.build_into_store(build_request, root, version="v1.0.0")
    lines: list[str] = []

    export = store_builder.export_update(root, build_request.app_id, "v1.0.0",
                                         tmp_path / "usb", include_runtime=True,
                                         progress=lines.append)

    assert export.kind == "update"
    assert any("複製版本" in line for line in lines), lines
    assert any("runtime" in line for line in lines), lines
    assert any("完成" in line for line in lines), lines
    for line in lines:
        line.encode("cp950")               # this text reaches a zh-TW console


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


def test_building_a_second_app_keeps_the_first_apps_console_and_entry(stub_toolchain,
                                                                      tmp_path):
    """tools\\ describes the MACHINE, not this build. Regenerating it for one app
    must never remove the console (or the start bat) of an app that is still
    installed and still running on that machine."""
    root = tmp_path / "ROOT"
    alpha = make_project(tmp_path, "Alpha Viewer")
    beta = make_project(tmp_path, "Beta Viewer")
    assert store_builder.build_into_store(alpha, root, version="v1").ok

    assert (root / "tools" / f"admin-{alpha.app_id}.bat").is_file()
    assert (root / "start.bat").is_file()

    assert store_builder.build_into_store(beta, root, version="v1").ok
    # alpha is still installed on this machine: it keeps its console and its entry
    assert (root / "tools" / f"admin-{alpha.app_id}.bat").is_file()
    assert (root / "tools" / f"admin-{beta.app_id}.bat").is_file()
    assert (root / f"start-{alpha.app_id}.bat").is_file()
    assert (root / f"start-{beta.app_id}.bat").is_file()

    # and the same call made with only ONE app's id still cannot orphan the other
    store_builder._write_tools(root, [beta.app_id])
    assert (root / "tools" / f"admin-{alpha.app_id}.bat").is_file()
    chooser = (root / "tools" / "admin.bat").read_text("utf-8")
    assert f"admin-{alpha.app_id}.bat" in chooser


def test_a_console_for_an_app_that_is_not_in_the_tree_is_removed(build_request,
                                                                 stub_toolchain, tmp_path):
    """The other side of the same rule: a stale console for an app that is NOT
    installed here (a leftover from an older tree) must go — it would roll back
    an app this machine does not have."""
    root = tmp_path / "ROOT"
    store_builder.build_into_store(build_request, root, version="v1.0.0")
    ghost = root / "tools" / "admin-app-ghost.bat"
    ghost.write_text("@echo off\n", encoding="utf-8")

    store_builder._write_tools(root)
    assert not ghost.exists()
    assert (root / "tools" / f"admin-{build_request.app_id}.bat").is_file()


def test_gc_bat_picks_the_runtime_a_current_version_actually_uses(build_request,
                                                                  stub_toolchain, tmp_path):
    """S9. gc.py will not delete the runtime its own interpreter runs from, so a
    gc.bat that picks its python by 'whichever the for-loop landed on last' can
    end up running GC under the very orphan runtime GC exists to reclaim — and
    then it reclaims nothing, every time, forever."""
    root = tmp_path / "ROOT"
    store_builder.build_into_store(build_request, root, version="v1.0.0")

    for name in ("gc.bat", f"admin-{build_request.app_id}.bat"):
        bat = (root / "tools" / name).read_text("utf-8")
        # 1. it resolves the CURRENT version's runtime out of the tree's own state
        assert 'findstr /i /c:"current" "%%~A\\state\\state.json"' in bat
        assert "runtime_fingerprint" in bat
        assert 'set "PY=deps\\runtimes\\!FP!\\python.exe"' in bat
        # 2. the fallback is the FIRST runtime, not the last: the loop stops
        assert 'if not defined PY if exist "%%~R\\python.exe"' in bat
        # 3. no python at all = a Chinese error, not a silent run with PY unset
        assert "找不到任何可用的 python.exe" in bat
        # 4. a failed GC must not look exactly like a successful one
        assert 'set "RC=%errorlevel%"' in bat
        assert "回收失敗" in bat and "回收完成" in bat
        # cp950: nothing here may be un-encodable on a zh-TW console
        bat.encode("cp950")


def test_gc_bat_has_no_last_wins_interpreter_loop(build_request, stub_toolchain, tmp_path):
    """The exact shape of the defect, spelled out so it cannot come back: a
    `for /d ... do ... set PY=` with no guard keeps the LAST match."""
    root = tmp_path / "ROOT"
    store_builder.build_into_store(build_request, root, version="v1.0.0")
    for name in ("gc.bat", f"admin-{build_request.app_id}.bat"):
        bat = (root / "tools" / name).read_text("utf-8")
        assert 'for /d %%R in ("deps\\runtimes\\*") do if exist' not in bat


def test_no_generated_bat_contains_an_em_dash(stub_toolchain, tmp_path):
    """cmd.exe mis-parses U+2014 (—) in a batch file under `chcp 65001`: the line
    it sits on is split and its tail is executed as a command, and a LATER line is
    mangled too. Proven by holding the file byte-size fixed and swapping — for a
    CJK character of identical length: with the em-dashes, two corrupted lines;
    same bytes without them, none. It is invisible in review (it reads as ordinary
    punctuation) and it survives in a rem line, so the rule is mechanical: no
    em-dash reaches a .bat, ever. 讀我-使用說明.txt is a text file and may keep it.
    """
    root = tmp_path / "ROOT"
    request = make_project(tmp_path, "產線 檢視器")
    assert store_builder.build_into_store(request, root, version="v1.0.0").ok

    bats = sorted(root.glob("*.bat")) + sorted((root / "tools").glob("*.bat"))
    assert len(bats) >= 4
    for bat in bats:
        text = bat.read_text("utf-8")
        assert "—" not in text, f"{bat.name} 帶了 em-dash,cmd.exe 會把那一行剖壞"
        text.encode("cp950")           # and every character must survive a zh-TW console


def run_bat(path: Path, *, stdin: str = "n\n") -> subprocess.CompletedProcess:
    return subprocess.run(["cmd", "/c", str(path)], input=stdin, capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=120)


def parse_damage(proc: subprocess.CompletedProcess) -> list[str]:
    """Lines cmd could not parse. A split multi-byte character shows up as a
    replacement char and/or as 'X is not recognized as an internal command' —
    which is what a corrupted .bat looks like from the outside, in any locale."""
    return [line for line in proc.stderr.splitlines()
            if "not recognized" in line or "不是內部或外部" in line or "�" in line]


@pytest.mark.skipif(os.name != "nt", reason="需要真的 cmd.exe 來剖析 .bat")
def test_the_picker_chooses_the_referenced_runtime_not_the_orphan(stub_toolchain, tmp_path):
    """S9, checked by running the real thing: the exact picker text both consoles
    ship, handed to a real cmd.exe, on a tree with a referenced runtime and an
    orphan that sorts LAST (which is precisely what the old last-wins `for /d`
    loop would have landed on, and precisely the runtime GC then refuses to
    delete because it is executing from inside it)."""
    root = tmp_path / "ROOT"
    request = make_project(tmp_path, "產線 檢視器")
    result = store_builder.build_into_store(request, root, version="v1.0.0")
    assert result.ok, result.errors

    orphan = root / "deps" / "runtimes" / "cp311-zzzorphan"
    orphan.mkdir(parents=True)
    (orphan / "python.exe").write_bytes(b"MZ orphan")

    probe = root / "tools" / "probe.bat"
    probe.write_text("@echo off\nsetlocal\nchcp 65001 >nul 2>&1\npushd \"%~dp0..\"\n"
                     + store_builder._pick_python("gc")
                     + "echo PICKED=%PY%\npopd\nexit /b 0\n", encoding="utf-8")
    picked = run_bat(probe).stdout
    assert f"PICKED=deps\\runtimes\\{result.fingerprint}\\python.exe" in picked, picked
    assert "zzzorphan" not in picked, picked

    # and when the tree cannot answer (state.json unreadable), it still picks
    # deterministically — the FIRST runtime — instead of silently running nothing
    (root / "apps" / request.app_id / "state" / "state.json").write_text(
        "{ not json", encoding="utf-8")
    again = run_bat(probe).stdout
    assert "PICKED=deps\\runtimes\\" in again, again


@pytest.mark.skipif(os.name != "nt", reason="需要真的 cmd.exe 來剖析 .bat")
def test_a_failed_gc_does_not_look_like_a_successful_one(stub_toolchain, tmp_path):
    """The generated gc.bat, run for real. Its python.exe cannot execute, so GC
    fails — and the operator must be told, instead of watching the window close on
    a disk that was never reclaimed. This also proves cmd could parse every line
    it executed (an em-dash would surface right here)."""
    root = tmp_path / "ROOT"
    request = make_project(tmp_path, "產線 檢視器")
    assert store_builder.build_into_store(request, root, version="v1.0.0").ok

    proc = run_bat(root / "tools" / "gc.bat")
    assert parse_damage(proc) == [], proc.stderr
    assert "回收失敗" in proc.stdout, proc.stdout
    assert proc.returncode != 0


@pytest.mark.skipif(os.name != "nt", reason="需要真的 cmd.exe 來剖析 .bat")
def test_a_tree_with_no_runtime_says_so_instead_of_running_nothing(stub_toolchain, tmp_path):
    """PY unset used to mean `"" "bootstrap\\gc.py"` — a console that flashes and
    closes, having done nothing, with no idea why."""
    root = tmp_path / "ROOT"
    request = make_project(tmp_path, "產線 檢視器")
    assert store_builder.build_into_store(request, root, version="v1.0.0").ok
    shutil.rmtree(root / "deps" / "runtimes")

    proc = run_bat(root / "tools" / "gc.bat")
    assert parse_damage(proc) == [], proc.stderr
    assert "找不到任何可用的 python.exe" in proc.stdout, proc.stdout
    assert proc.returncode == 1


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
