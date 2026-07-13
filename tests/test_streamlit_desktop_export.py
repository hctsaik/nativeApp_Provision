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
import sys
from pathlib import Path

import pytest

from provision_builder.streamlit_desktop import imports as imports_mod
from provision_builder.streamlit_desktop import runtime as runtime_mod
from provision_builder.streamlit_desktop import store_builder
from provision_builder.streamlit_desktop.device import gc as gc_mod
from provision_builder.streamlit_desktop.device import integrity
from provision_builder.streamlit_desktop.device import state as state_mod
from provision_builder.streamlit_desktop.models import BuildRequest

LOCK = "streamlit==1.40.0\n"
PANDAS_LOCK = "streamlit==1.40.0\npandas==2.2.0\n"


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


# ── S4:交付 = 目標機需要的東西,不是建置機的備份 ────────────────────────────

def state_of(root: Path, app_id: str) -> dict:
    return json.loads((root / "apps" / app_id / "state" / "state.json").read_text("utf-8"))


def versions_in(root: Path, app_id: str) -> set[str]:
    return {p.name for p in (root / "apps" / app_id / "versions").iterdir() if p.is_dir()}


def build_history(request: BuildRequest, root: Path, *versions: str) -> None:
    for version in versions:
        result = store_builder.build_into_store(request, root, version=version)
        assert result.ok, result.errors


def test_full_delivery_does_not_ship_the_build_machines_pending_update(build_request,
                                                                       stub_toolchain,
                                                                       tmp_path):
    """S4, the sharp end of it. A build machine NEVER launches what it builds, so
    every version after the first lands in `pending`. The exporter copied state.json
    verbatim, so the factory machine received a `pending` it was never meant to have
    — and bootstrap PROMOTES pending on the next start. The operator delivers v1.0.0,
    the line boots, and the machine is running v1.2.0. Nobody chose that.
    """
    root = tmp_path / "ROOT"
    app = build_request.app_id
    build_history(build_request, root, "v1.0.0", "v1.1.0", "v1.2.0")
    assert state_of(root, app)["pending"] == "v1.2.0"      # the build machine's truth

    out = tmp_path / "deliver"
    store_builder.export_full_tree(root, out)

    delivered = state_of(out, app)
    assert delivered["current"] == "v1.0.0"                # what the operator asked for
    assert delivered["pending"] is None                    # NOT a promotion time bomb
    assert delivered["pending_revision"] is None
    assert delivered["candidate"] is None
    assert delivered["candidate_revision"] is None
    assert delivered["last_known_good"] is None            # nothing ever started here
    assert delivered["failed_versions"] == []
    # and the version it never should have carried is not even on the disk
    assert versions_in(out, app) == {"v1.0.0"}


def test_full_delivery_does_not_ship_the_build_machines_failure_history(build_request,
                                                                        stub_toolchain,
                                                                        tmp_path):
    """failed_versions is a record of what went wrong ON THIS MACHINE. Shipped to a
    machine that has never met those versions, it silently forbids the updater from
    ever applying them again — a quarantine the target can neither see nor explain."""
    root = tmp_path / "ROOT"
    app = build_request.app_id
    build_history(build_request, root, "v1.0.0")

    store = state_mod.StateStore(root / "apps" / app / "state")
    store.mutate(lambda s: state_mod.clear_bad_pending(
        state_mod.set_pending(s, "v0.9.0"), revision="deadbeef"))
    store.mutate(lambda s: state_mod.commit_candidate(s))
    assert state_of(root, app)["failed_versions"]          # the build machine has one
    assert state_of(root, app)["last_known_good"] == "v1.0.0"

    out = tmp_path / "deliver"
    store_builder.export_full_tree(root, out)

    delivered = state_of(out, app)
    assert delivered["failed_versions"] == []
    assert delivered["last_known_good"] is None
    assert delivered["generation"] >= 1                    # a valid, loadable state
    state_mod.StateStore(out / "apps" / app / "state").load()   # it really parses


def test_full_delivery_ships_a_rollback_target_so_rollback_has_somewhere_to_go(
        build_request, stub_toolchain, tmp_path):
    """A delivery of exactly one version is a machine that can never roll back. Ship
    the version being delivered AND the one it rolls back to — and nothing else."""
    root = tmp_path / "ROOT"
    app = build_request.app_id
    build_history(build_request, root, "v1.0.0", "v1.1.0", "v1.2.0")

    out = tmp_path / "deliver"
    export = store_builder.export_full_tree(root, out, version="v1.2.0")

    # v1.2.0 is what we deliver; v1.0.0 (the build machine's `current`) is the
    # version the target can fall back to. v1.1.0 is nobody's business.
    assert versions_in(out, app) == {"v1.2.0", "v1.0.0"}
    delivered = state_of(out, app)
    assert delivered["current"] == "v1.2.0"
    assert delivered["previous"] == "v1.0.0"
    assert delivered["pending"] is None and delivered["failed_versions"] == []
    assert export.versions == ["v1.2.0", "v1.0.0"]
    # rollback_target() is what bootstrap --rollback asks: it must answer.
    loaded = state_mod.StateStore(out / "apps" / app / "state").load()
    assert loaded.rollback_target() == "v1.0.0"
    assert integrity.is_complete(out / "apps" / app / "versions" / "v1.0.0")


def test_full_delivery_ships_only_the_runtimes_its_versions_reference(build_request,
                                                                      stub_toolchain,
                                                                      tmp_path):
    """The union of the runtimes of EVERY version ever built is how a 500 MB delivery
    becomes a 2 GB one: each dependency change mints another runtime, and all of them
    rode along."""
    root = tmp_path / "ROOT"
    app = build_request.app_id
    first = store_builder.build_into_store(build_request, root, version="v1.0.0")
    assert first.ok, first.errors

    (build_request.project_dir / "requirements.txt").write_text(PANDAS_LOCK,
                                                                encoding="utf-8")
    second = store_builder.build_into_store(build_request, root, version="v2.0.0")
    assert second.ok and second.fingerprint != first.fingerprint

    out = tmp_path / "deliver"
    store_builder.export_full_tree(root, out, version="v2.0.0")

    shipped = {p.name for p in (out / "deps" / "runtimes").iterdir() if p.is_dir()}
    # v2.0.0's runtime, and v1.0.0's because v1.0.0 IS the rollback target we ship.
    assert shipped == {first.fingerprint, second.fingerprint}
    for fingerprint in shipped:
        assert (out / "deps" / "runtimes" / fingerprint).is_dir()

    # ...and a delivery with no rollback target carries exactly one runtime.
    lean = tmp_path / "deliver-lean"
    store_builder.export_full_tree(root, lean, version="v1.0.0")
    assert {p.name for p in (lean / "deps" / "runtimes").iterdir()
            if p.is_dir()} == {first.fingerprint}
    assert versions_in(lean, app) == {"v1.0.0"}     # v1.0.0 is `current`: no previous


def test_full_delivery_refuses_a_version_that_is_not_deliverable(build_request,
                                                                 stub_toolchain, tmp_path):
    root = tmp_path / "ROOT"
    build_history(build_request, root, "v1.0.0")
    with pytest.raises(store_builder.StoreBuildError, match="不完整或不存在"):
        store_builder.export_full_tree(root, tmp_path / "out", version="v9.9.9")


def test_full_delivery_refuses_a_version_whose_runtime_it_cannot_identify(build_request,
                                                                          stub_toolchain,
                                                                          tmp_path):
    """A version whose manifest will not parse names no runtime, so deps\\ would go out
    without the interpreter that version runs under: a folder that cannot start, and
    an export that said nothing."""
    root = tmp_path / "ROOT"
    app = build_request.app_id
    build_history(build_request, root, "v1.0.0")
    (root / "apps" / app / "versions" / "v1.0.0" / "app-package.json").write_text(
        "{ not json", encoding="utf-8")

    with pytest.raises(store_builder.StoreBuildError, match="runtime_fingerprint"):
        store_builder.export_full_tree(root, tmp_path / "out")


def test_a_version_cannot_be_named_without_naming_its_app(stub_toolchain, tmp_path):
    """Two apps, one version number: 「發 v1」 does not say whose v1."""
    root = tmp_path / "ROOT"
    alpha = make_project(tmp_path, "Alpha Viewer")
    beta = make_project(tmp_path, "Beta Viewer")
    build_history(alpha, root, "v1")
    build_history(beta, root, "v1")
    with pytest.raises(store_builder.StoreBuildError, match="必須同時指定是哪一個 app"):
        store_builder.export_full_tree(root, tmp_path / "out", version="v1")


# ── S8:兩個 App 的交付,要真的交付兩個 App ──────────────────────────────────

def test_export_reports_the_entry_bats_it_really_wrote(build_request, stub_toolchain,
                                                       tmp_path):
    """S8. With no entry_bats on ExportResult the GUI fell back to a hardcoded
    「雙擊 start.bat」. In a one-app tree that is true; in a two-app tree start.bat is
    DELETED (it would be ambiguous), so the operator handed over a folder and told the
    line worker to double-click a file that is not in it."""
    root = tmp_path / "ROOT"
    build_history(build_request, root, "v1.0.0")

    export = store_builder.export_full_tree(root, tmp_path / "one")
    assert export.entry_bats == ["start.bat"]
    assert (Path(export.out_dir) / "start.bat").is_file()
    assert export.entry_hint() == "雙擊 start.bat"
    assert "start.bat" in export.summary()

    # every name it reports is a file that is really there, and nothing else is
    on_disk = sorted(p.name for p in Path(export.out_dir).glob("start*.bat"))
    assert on_disk == export.entry_bats


def test_a_two_app_delivery_delivers_both_apps_and_names_both_entries(stub_toolchain,
                                                                      tmp_path):
    """S8, the whole scenario: build two apps into one tree, deliver the tree."""
    root = tmp_path / "ROOT"
    alpha = make_project(tmp_path, "Alpha Viewer")
    beta = make_project(tmp_path, "Beta Viewer")
    build_history(alpha, root, "v1")
    build_history(beta, root, "v1")

    out = tmp_path / "deliver"
    export = store_builder.export_full_tree(root, out)

    assert export.apps == sorted([alpha.app_id, beta.app_id])
    assert export.entry_bats == sorted([f"start-{alpha.app_id}.bat",
                                        f"start-{beta.app_id}.bat"])
    assert not (out / "start.bat").exists()          # ambiguous: it must NOT be there
    assert "start.bat" not in export.entry_hint()    # and we must not name it either
    for app in (alpha.app_id, beta.app_id):
        assert (out / f"start-{app}.bat").is_file()
        assert (out / "tools" / f"admin-{app}.bat").is_file()
        # each app gets its own coherent state: current set, nothing else
        delivered = state_of(out, app)
        assert delivered["current"] == "v1" and delivered["pending"] is None
        assert integrity.is_complete(out / "apps" / app / "versions" / "v1")
        assert f"--app {app}" in (out / f"start-{app}.bat").read_text("utf-8")
    # the chooser offers exactly these two apps
    chooser = (out / "tools" / "admin.bat").read_text("utf-8")
    assert f"admin-{alpha.app_id}.bat" in chooser and f"admin-{beta.app_id}.bat" in chooser
    # and the 讀我 tells the operator to double-click something that exists
    readme = (out / store_builder.README_NAME).read_text("utf-8")
    assert f"start-{alpha.app_id}.bat" in readme


def test_exporting_again_into_the_same_folder_leaves_no_stale_entry_bat(stub_toolchain,
                                                                        tmp_path):
    """The operator reuses the USB folder. A start bat left over from the previous
    delivery points at an app this delivery does not contain."""
    root = tmp_path / "ROOT"
    alpha = make_project(tmp_path, "Alpha Viewer")
    beta = make_project(tmp_path, "Beta Viewer")
    build_history(alpha, root, "v1")
    build_history(beta, root, "v1")

    out = tmp_path / "usb"
    both = store_builder.export_full_tree(root, out)
    assert len(both.entry_bats) == 2

    only_alpha = store_builder.export_full_tree(root, out, app_id=alpha.app_id)
    assert only_alpha.entry_bats == [f"start-{alpha.app_id}.bat"]
    assert not (out / f"start-{beta.app_id}.bat").exists()
    assert sorted(p.name for p in out.glob("start*.bat")) == only_alpha.entry_bats


# ── S2:「發最新的那一版」 ──────────────────────────────────────────────────

def test_newest_version_is_the_pending_build_not_the_current_one(build_request,
                                                                 stub_toolchain, tmp_path):
    """S2. On a build machine `current` is the version the FLEET already runs: the
    machine never launches what it builds, so a fresh build stops at `pending`. The
    GUI targeted state.current, so 「發最新的那一版」 exported the version the factory
    already had, and the trip was wasted."""
    root = tmp_path / "ROOT"
    app = build_request.app_id
    build_history(build_request, root, "v1.0.0", "v1.1.0", "v1.2.0")

    assert state_of(root, app)["current"] == "v1.0.0"      # the trap
    assert store_builder.newest_version(root, app) == "v1.2.0"


def test_list_versions_is_newest_first_and_tells_the_gui_each_versions_role(
        build_request, stub_toolchain, tmp_path):
    root = tmp_path / "ROOT"
    app = build_request.app_id
    build_history(build_request, root, "v1.0.0", "v1.9.0", "v1.10.0")

    infos = store_builder.list_versions(root, app)
    assert [i.version for i in infos] == ["v1.10.0", "v1.9.0", "v1.0.0"]  # not a str sort
    by_version = {i.version: i for i in infos}
    assert by_version["v1.10.0"].role == "pending"
    assert by_version["v1.0.0"].role == "current"
    assert by_version["v1.9.0"].role == ""                 # built, then orphaned
    for info in infos:
        assert info.is_complete and info.revision and len(info.revision) == 12
        assert info.built_at and info.runtime_fingerprint
        assert info.size_mb > 0
        assert info.version in info.label()
        info.label().encode("cp950")

    # an incomplete version is listed but must never be offered as deliverable
    half = root / "apps" / app / "versions" / "v2.0.0"
    (half / "application").mkdir(parents=True)
    assert not [i for i in store_builder.list_versions(root, app)
                if i.version == "v2.0.0" and i.is_complete]
    assert store_builder.newest_version(root, app) == "v1.10.0"


def test_list_versions_never_raises_on_a_tree_it_cannot_read(build_request,
                                                             stub_toolchain, tmp_path):
    """A picker that throws is a GUI that cannot open a store at all."""
    root = tmp_path / "ROOT"
    app = build_request.app_id
    build_history(build_request, root, "v1.0.0")
    (root / "apps" / app / "state" / "state.json").write_text("{ not json",
                                                              encoding="utf-8")
    infos = store_builder.list_versions(root, app)
    assert [i.version for i in infos] == ["v1.0.0"]
    assert infos[0].role == ""                             # no state = no roles, no crash
    assert store_builder.list_versions(tmp_path / "nothing", app) == []
    assert store_builder.newest_version(tmp_path / "nothing", app) is None


def test_export_warns_when_it_is_not_delivering_the_newest_version(build_request,
                                                                   stub_toolchain,
                                                                   tmp_path):
    """The backend half of S2: even when the caller does not ask, the export says so."""
    root = tmp_path / "ROOT"
    build_history(build_request, root, "v1.0.0", "v1.1.0")

    stale = store_builder.export_full_tree(root, tmp_path / "old")
    assert any("v1.1.0" in w and "最新" in w for w in stale.warnings), stale.warnings

    fresh = store_builder.export_full_tree(root, tmp_path / "new", version="v1.1.0")
    assert not any("最新" in w for w in fresh.warnings), fresh.warnings
    for warning in stale.warnings + fresh.warnings:
        warning.encode("cp950")


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


def test_no_bat_echoes_a_paren_inside_a_block(stub_toolchain, tmp_path):
    """cmd.exe cannot survive an unescaped half-width paren inside a ( ) block —
    not even a balanced pair. It does not print the line and carry on: it aborts the
    whole batch file with "was unexpected at this time" and exit 255.

    So `if not "%RC%"=="0" ( echo 啟動失敗(代碼 %RC%)。記錄在 ...\\logs\\ 裡 )` never
    printed one character of that message, never reached its `pause`, and the window
    closed instantly on the user. The exit code the caller saw was 255 — not the app's.
    It is invisible in review (it reads as ordinary Chinese punctuation) and it fires
    only on the failure path, which is exactly the path nobody exercises. Mechanical
    rule: no message inside a block may carry ( or ). 全形（）or a comma instead.
    """
    root = tmp_path / "ROOT"
    request = make_project(tmp_path, "產線 檢視器")
    assert store_builder.build_into_store(request, root, version="v1.0.0").ok

    bats = sorted(root.glob("*.bat")) + sorted((root / "tools").glob("*.bat"))
    assert len(bats) >= 4
    for bat in bats:
        depth = 0
        for number, line in enumerate(bat.read_text("utf-8").splitlines(), 1):
            stripped = line.strip()
            speaks = stripped.startswith(("echo ", "rem ", "set /p ", "title "))
            if speaks:
                # a message: it may only carry parens where cmd is not counting them
                if depth > 0:
                    assert "(" not in stripped and ")" not in stripped, \
                        f"{bat.name}:{number} 在 ( ) 區塊裡用了半形括號,cmd 會整個剖壞\n{line}"
                continue          # its text must never move the block depth
            depth += stripped.count("(") - stripped.count(")")
            assert depth >= 0, f"{bat.name}:{number} 括號收多了\n{line}"
        assert depth == 0, f"{bat.name} 有沒收掉的 ( )"


@pytest.mark.skipif(os.name != "nt", reason="需要真的 cmd.exe 來剖析 .bat")
def test_a_failed_launch_tells_the_user_where_the_log_is(build_request, stub_toolchain,
                                                         tmp_path):
    """The other half of the same defect, run for real: this tree's python.exe is a
    stub, so bootstrap cannot start — and the user must be TOLD that, and where to
    look, and the window must stay open long enough to read it."""
    root = tmp_path / "ROOT"
    assert store_builder.build_into_store(build_request, root, version="v1.0.0").ok
    env = shim_reg(tmp_path / "shim", "121.0.2277.128")   # WebView2 is fine; the app is not

    proc = subprocess.run(["cmd", "/c", str(root / "start.bat")], input="\n\n\n",
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", env=env, timeout=120)
    assert "was unexpected at this time" not in proc.stderr, proc.stderr
    assert "啟動失敗" in proc.stdout, proc.stdout
    assert f"apps\\{build_request.app_id}\\data\\logs\\" in proc.stdout, proc.stdout
    assert proc.returncode not in (0, 255)      # the app's code, not a cmd parse error


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


def test_gc_bat_does_not_blame_the_store_lock_for_every_failure(build_request,
                                                                stub_toolchain, tmp_path):
    """S9. Every non-zero exit printed 「回收失敗,沒有刪掉任何東西」 and then guessed
    「常見原因:store 鎖被佔用」. GC that deleted 400 MB and tripped on ONE folder
    Explorer had open reported the same thing as a GC that never started — and sent
    the operator off to wait for an update that was not running."""
    root = tmp_path / "ROOT"
    store_builder.build_into_store(build_request, root, version="v1.0.0")

    for name in ("gc.bat", f"admin-{build_request.app_id}.bat"):
        bat = (root / "tools" / name).read_text("utf-8")
        # one code, one outcome — and each outcome says only what its code proves
        assert f'"%RC%"=="{store_builder.GC_EXIT_PARTIAL}"' in bat
        assert f'"%RC%"=="{store_builder.GC_EXIT_NOTHING}"' in bat
        assert f'"%RC%"=="{store_builder.GC_EXIT_LOCKED}"' in bat
        assert "有一部分刪掉了" in bat            # partial: some space DID come back
        assert "一個項目都沒有刪掉" in bat        # nothing: and we say so, honestly
        # the lock is named to the OPERATOR only on a line that also says nothing was
        # deleted — i.e. only where the code proves it, never as a guess after any
        # failure (`rem` lines are for us, not for them)
        lock_lines = [line.strip() for line in bat.splitlines()
                      if "store 鎖被佔用" in line and line.strip().startswith("echo ")]
        assert lock_lines, bat
        for line in lock_lines:
            assert "沒有刪掉任何東西" in line, line
        assert "常見原因:目前正在下載或安裝更新" not in bat      # the old guess
        # an unknown code claims nothing about what was or was not deleted
        assert "不在預期之內" in bat or "上面那幾行是 GC 自己說的原因" in bat
        bat.encode("cp950")

    # the console's table IS gc.py's table. A bat that maps 4 to 「鎖被佔用」 while
    # gc.py maps 4 to something else is a worse lie than the one we just fixed.
    assert store_builder.GC_EXIT_OK == gc_mod.EXIT_OK
    assert store_builder.GC_EXIT_PARTIAL == gc_mod.EXIT_PARTIAL
    assert store_builder.GC_EXIT_NOTHING == gc_mod.EXIT_NOTHING_DELETED
    assert store_builder.GC_EXIT_LOCKED == gc_mod.EXIT_STORE_LOCKED


@pytest.mark.skipif(os.name != "nt", reason="需要真的 cmd.exe 來剖析 .bat")
@pytest.mark.parametrize("code,expected,forbidden", [
    (0, "回收完成", "回收失敗"),
    (store_builder.GC_EXIT_PARTIAL, "有一部分刪掉了", "store 鎖被佔用"),
    (store_builder.GC_EXIT_NOTHING, "一個項目都沒有刪掉", "store 鎖被佔用"),
    (store_builder.GC_EXIT_LOCKED, "store 鎖被佔用", "有一部分刪掉了"),
    (99, "回收沒有跑完", "沒有刪掉任何東西"),      # a code we do not know: claim nothing
])
def test_gc_bat_reports_what_gc_actually_did(build_request, stub_toolchain, tmp_path,
                                             code, expected, forbidden):
    """The generated gc.bat, driven by a REAL gc.py exit code through a REAL cmd.exe.
    The junction gives the tree a working python.exe (this tree's is a stub `MZ`),
    which is the only way to make gc.py exit with the code we want to test."""
    root = tmp_path / "ROOT"
    result = store_builder.build_into_store(build_request, root, version="v1.0.0")
    assert result.ok, result.errors

    runtime = root / "deps" / "runtimes" / result.fingerprint
    shutil.rmtree(runtime)
    link = subprocess.run(["cmd", "/c", "mklink", "/J", str(runtime),
                           str(Path(sys.executable).parent)],
                          capture_output=True, text=True)
    if link.returncode != 0:
        pytest.skip(f"這個檔案系統做不出 junction:{link.stdout}{link.stderr}")
    try:
        # a gc.py whose only job is to exit with `code` when it is asked to --apply
        (root / "bootstrap" / "gc.py").write_text(
            "import sys\n"
            "if '--apply' in sys.argv:\n"
            "    print('(stub) apply')\n"
            f"    sys.exit({code})\n"
            "print('(stub) dry-run')\n", encoding="utf-8")

        proc = run_bat(root / "tools" / "gc.bat", stdin="y\n\n")
        assert parse_damage(proc) == [], proc.stderr
        assert expected in proc.stdout, proc.stdout
        assert forbidden not in proc.stdout, proc.stdout
        assert proc.returncode == code
    finally:
        os.rmdir(runtime)          # remove the junction, NOT the real Python behind it


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


# ── A/S4:WebView2 供應鏈 ────────────────────────────────────────────────────

def test_start_bat_refuses_with_the_environment_code_not_the_app_broken_code(
        build_request, stub_toolchain, tmp_path):
    """A missing WebView2 is a fact about the MACHINE, not about this version.
    bootstrap spells that 5 (EXIT_SHELL_ENVIRONMENT); everything downstream keys off
    the number, and a version-specific code is what gets a good build quarantined."""
    root = tmp_path / "ROOT"
    store_builder.build_into_store(build_request, root, version="v1.0.0")
    start = (root / "start.bat").read_text("utf-8")

    block = start[start.index("set \"WV2=\""):start.index("Bootstrap chicken")]
    assert "exit /b 5" in block
    assert "exit /b 1" not in block            # 1 = 「這個版本壞了」, and it is not
    assert store_builder.EXIT_SHELL_ENVIRONMENT == 5
    # all three places WebView2 can be installed, and the uninstall husk rejected
    assert block.count("EdgeUpdate\\Clients") == 3
    assert "HKLM\\SOFTWARE\\Microsoft\\EdgeUpdate" in block   # native 64-bit view
    assert '"%%V"=="0.0.0.0"' in block


def test_build_bundles_the_webview2_installer_into_prereq(build_request, stub_toolchain,
                                                          tmp_path):
    """A/S4. NOTHING in this codebase ever created prereq\\ — the exporter copied it
    if it happened to exist, and it never did. So the one dependency the delivery
    cannot install itself was never actually shipped, and 安裝WebView2.bat printed a
    download URL at a factory machine with no internet."""
    source = tmp_path / "downloads" / "webview2-bootstrapper (1).exe"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"MZ webview2 setup")
    build_request.webview2_installer = source

    root = tmp_path / "ROOT"
    result = store_builder.build_into_store(build_request, root, version="v1.0.0")
    assert result.ok, result.errors

    # renamed to the canonical name: the .bat cannot guess what the operator's file
    # was called, and it is the .bat that has to find it.
    bundled = root / "prereq" / "MicrosoftEdgeWebview2Setup.exe"
    assert bundled.is_file() and bundled.read_bytes() == b"MZ webview2 setup"
    assert (root / store_builder.WEBVIEW2_INSTALLER).is_file()
    assert store_builder.WEBVIEW2_INSTALLER.replace("/", "\\") in \
        (root / "tools" / store_builder.WEBVIEW2_BAT_NAME).read_text("utf-8")

    out = tmp_path / "deliver"
    export = store_builder.export_full_tree(root, out)
    assert (out / "prereq" / "MicrosoftEdgeWebview2Setup.exe").is_file()
    assert not any("WebView2" in w for w in export.warnings), export.warnings


def test_a_delivery_that_cannot_install_webview2_offline_says_so(build_request,
                                                                 stub_toolchain, tmp_path):
    """No installer = a target machine with no WebView2 and no internet gets a blank
    window and no way out. That is worth saying while the operator is still standing
    next to the build machine that could have fixed it."""
    root = tmp_path / "ROOT"
    store_builder.build_into_store(build_request, root, version="v1.0.0")

    export = store_builder.export_full_tree(root, tmp_path / "deliver")
    assert not (Path(export.out_dir) / "prereq").exists()
    warning = [w for w in export.warnings if "WebView2" in w]
    assert warning, export.warnings
    assert store_builder.WEBVIEW2_DOWNLOAD in warning[0]
    assert "WebView2" in export.summary()
    for line in export.warnings:
        line.encode("cp950")


def test_a_webview2_installer_that_is_not_there_fails_before_the_runtime_install(
        build_request, stub_toolchain, tmp_path):
    build_request.webview2_installer = tmp_path / "nope" / "setup.exe"
    root = tmp_path / "ROOT"
    result = store_builder.build_into_store(build_request, root, version="v1.0.0")
    assert not result.ok
    assert any("找不到 WebView2 安裝檔" in e for e in result.errors), result.errors
    assert not (root / "deps").exists()          # it did not pay for a runtime first


def shim_reg(directory: Path, pv: str | None) -> dict:
    """A `reg` that answers what we tell it to, first on PATH. `pv=None` = the key is
    not there at all; "0.0.0.0" = the husk a WebView2 uninstall leaves behind."""
    directory.mkdir(parents=True, exist_ok=True)
    if pv is None:
        body = "@echo off\r\nexit /b 1\r\n"
    else:
        body = ("@echo off\r\necho.\r\necho HKEY_LOCAL_MACHINE\\SOFTWARE\r\n"
                f"echo     pv    REG_SZ    {pv}\r\nexit /b 0\r\n")
    (directory / "reg.bat").write_text(body, encoding="ascii", newline="")
    return dict(os.environ, PATH=str(directory) + os.pathsep + os.environ["PATH"])


@pytest.mark.skipif(os.name != "nt", reason="需要真的 cmd.exe 來剖析 .bat")
@pytest.mark.parametrize("pv,missing", [
    ("121.0.2277.128", False),      # a real install
    ("0.0.0.0", True),              # the husk an uninstall leaves behind
    (None, True),                   # nothing at all
])
def test_start_bat_treats_a_stale_webview2_key_as_not_installed(build_request,
                                                                stub_toolchain, tmp_path,
                                                                pv, missing):
    """Run the REAL generated start.bat against a `reg` we control. `reg query /v pv`
    SUCCEEDS on the empty husk an uninstall leaves behind, so the old
    `reg query >nul && set WV2=1` waved the start through and handed the user exactly
    the blank window this check exists to prevent."""
    root = tmp_path / "ROOT"
    assert store_builder.build_into_store(build_request, root, version="v1.0.0").ok
    env = shim_reg(tmp_path / "shim", pv)

    proc = subprocess.run(["cmd", "/c", str(root / "start.bat")], input="\n\n\n",
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", env=env, timeout=120)
    assert parse_damage(proc) == [], proc.stderr
    if missing:
        assert proc.returncode == 5, proc.stdout
        assert "沒有 Microsoft Edge WebView2 Runtime" in proc.stdout
        assert store_builder.WEBVIEW2_BAT_NAME in proc.stdout
    else:
        # it got past the check (and then died on this tree's fake python.exe —
        # which is the point: the WebView2 gate did not stop it)
        assert proc.returncode != 5
        assert "沒有 Microsoft Edge WebView2 Runtime" not in proc.stdout


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
