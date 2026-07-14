"""交付與更新來源:兩種匯出是兩件事。

export_full_tree() 產出的資料夾必須「在一台什麼都沒有的機器上雙擊就能跑」;
export_update() 產出的是自動更新來源,本來就跑不起來 —— 以前 GUI 的「匯出交付」
按鈕呼叫的是後者,交出去的資料夾連 bootstrap\\ 和 start.bat 都沒有。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from provision_builder.streamlit_desktop import imports as imports_mod
from provision_builder.streamlit_desktop import runtime as runtime_mod
from provision_builder.streamlit_desktop import store_builder
from provision_builder.streamlit_desktop.device import bootstrap
from provision_builder.streamlit_desktop.device import gc as gc_mod
from provision_builder.streamlit_desktop.device import integrity
from provision_builder.streamlit_desktop.device import state as state_mod
from provision_builder.streamlit_desktop.models import BuildRequest

LOCK = "streamlit==1.40.0\n"
PANDAS_LOCK = "streamlit==1.40.0\npandas==2.2.0\n"


def make_project(tmp_path: Path, name: str, *, lock: str = LOCK,
                 app_id: str | None = None) -> BuildRequest:
    """`app_id` = BuildRequest.app_id_override, i.e. the GUI's 「應用代號」 field.

    A name with no latin characters (「產線 檢視器」) has no slug to derive an id
    from, and a store REFUSES a derived id — so these tests pass one, exactly as the
    operator now has to. The display name stays Chinese: that is the half that has to
    keep reaching the user, and it does (messages\\*.txt), which is what most of the
    tests below are about.
    """
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
                        shell_exe=shell, runtime_template=template,
                        app_id_override=app_id)


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
    """The entry bat's NAME is decided by the folder that receives it, not by how many
    apps the build store happened to hold. This folder gets ONE app, so it gets
    start.bat — the same file the 讀我 in it tells the operator to double-click, and
    the same file every other one-app delivery has. It used to inherit the source's
    start-<app>.bat purely because the source store had a second, unrelated app in it:
    two machines running only App A ended up with differently-named entry points, and
    support had to ask which build machine had cut the folder."""
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
    assert export.entry_bats == ["start.bat"]
    assert (out / "start.bat").is_file()
    assert f"--app {first.app_id}" in (out / "start.bat").read_text("utf-8")
    # nothing in this folder starts an app this folder does not have
    assert sorted(p.name for p in out.glob("start*.bat")) == ["start.bat"]
    assert store_builder.README_NAME and "start.bat" in export.entry_hint()
    # the console of an app that is not in this delivery must not be offered
    assert (out / "tools" / f"admin-{first.app_id}.bat").is_file()
    assert not (out / "tools" / f"admin-{second.app_id}.bat").exists()


def test_the_delivery_carries_the_messages_its_bats_print(build_request, stub_toolchain,
                                                          tmp_path):
    """The bats are ASCII and `type` their Chinese out of messages\\*.txt. Deliver the
    bats without the messages and every `type ... 2>nul` prints NOTHING: the WebView2
    gate turns the user away in silence, the failed launch says nothing, and the
    window closes. The messages are not documentation, they are the program's voice."""
    root = tmp_path / "ROOT"
    assert store_builder.build_into_store(build_request, root, version="v1.0.0").ok
    out = tmp_path / "deliver"
    export = store_builder.export_full_tree(root, out)

    typed = set()
    for bat in generated_bats(out):
        raw = bat.read_bytes()
        assert raw.isascii(), bat.name          # the delivered bats keep the rule
        typed |= set(re.findall(r'messages\\([\w.-]+\.txt)', raw.decode("ascii")))
        typed |= set(re.findall(r'set /p TITLE=<"messages\\([\w.-]+\.txt)"',
                                raw.decode("ascii")))
    assert typed, "沒有任何 bat 會印訊息?"
    for name in sorted(typed):
        path = out / store_builder.MESSAGES_DIR / name
        assert path.is_file(), f"交付樹缺 messages\\{name}(bat 會 type 它,卻不存在)"
        path.read_bytes().decode("utf-8").encode("cp950")
    assert Path(export.out_dir) == out


@pytest.mark.skipif(os.name != "nt", reason="需要真的 cmd.exe 來剖析 .bat")
def test_the_delivered_start_bat_speaks_on_every_single_run(build_request, stub_toolchain,
                                                            tmp_path):
    """The delivered tree, driven by a real cmd.exe, {CMD_RUNS} times. This is the
    artifact the factory actually receives: it has to work every time, not 19 times
    out of 20."""
    root = tmp_path / "ROOT"
    assert store_builder.build_into_store(build_request, root, version="v1.0.0").ok
    out = tmp_path / "deliver"
    store_builder.export_full_tree(root, out)
    env = shim_reg(tmp_path / "shim", None)              # this machine has no WebView2

    for attempt in range(CMD_RUNS):
        proc = run_bat(out / "start.bat", stdin="\n\n", env=env)
        assert parse_damage(proc) == [], f"第 {attempt + 1} 次:{proc.stderr}"
        assert proc.returncode == 5, proc.stdout        # the environment code
        assert "沒有 Microsoft Edge WebView2 Runtime" in proc.stdout, proc.stdout
        assert store_builder.WEBVIEW2_BAT_NAME in proc.stdout


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
    # The BUILD MACHINE's candidate does not travel — but the delivered version is
    # itself on trial on a machine that has never run it (see the test below).
    assert delivered["candidate"] == "v1.0.0"
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


def test_a_freshly_delivered_version_boots_on_trial_so_rollback_can_fire(
        build_request, stub_toolchain, tmp_path, monkeypatch):
    """BLOCKER, and the other half of the healthy-marker fix. The exporter wrote
    candidate=None, so a delivered version was NOT ON TRIAL on its first boot:
    bootstrap only auto-rolls-back the candidate (`is_candidate`), so the single most
    dangerous moment in the product's life — a version's very first launch on a
    machine that has never run it — was the one moment the safety net was switched
    off. Meanwhile the 讀我 in the same folder promises 「萬一新版啟動失敗,系統會自動
    退回上一個能用的版本」.

    state.py has always agreed with us: StateStore.initialize() sets candidate=current
    「a fresh install is itself an unproven candidate」. Only the exporter disagreed.
    """
    root = tmp_path / "ROOT"
    app = build_request.app_id
    build_history(build_request, root, "v1.0.0", "v1.2.0")

    out = tmp_path / "deliver"
    store_builder.export_full_tree(root, out, version="v1.2.0")

    delivered = state_of(out, app)
    assert delivered["current"] == "v1.2.0"
    assert delivered["candidate"] == "v1.2.0", "交付出去的版本第一次開機沒有被當成試用版"
    assert delivered["candidate_revision"], "沒有 revision 的失敗記錄會擋掉這個版號的每一版"
    assert delivered["candidate_revision"] == store_builder.version_revision(
        store_builder.AppPaths(root, app), "v1.2.0")     # the same id release.json uses
    assert delivered["previous"] == "v1.0.0"             # somewhere to roll back TO
    assert delivered["last_known_good"] is None          # nothing has started here yet

    # ...and the net really catches it: a first boot that fails rolls back to v1.0.0
    # rather than leaving the machine dead on a version that cannot start.
    paths = store_builder.AppPaths(out, app)
    state = state_mod.StateStore(paths.state_dir).load()
    assert bootstrap.resolve_rollback_target(paths, state) == "v1.0.0"

    store = state_mod.StateStore(paths.state_dir)
    store.mutate(lambda s: state_mod.fail_candidate(s, target="v1.0.0"))
    after = state_of(out, app)
    assert after["current"] == "v1.0.0"                  # the machine is alive again
    assert after["candidate"] is None
    assert [e["version"] for e in after["failed_versions"]] == ["v1.2.0"]
    assert after["failed_versions"][0]["revision"] == delivered["candidate_revision"]


def test_a_one_version_delivery_on_trial_has_nowhere_to_roll_back_and_says_so(
        build_request, stub_toolchain, tmp_path):
    """The other side of putting the delivered version on trial: with ONE version
    there is no rollback target, and the machine must not pretend otherwise. Nothing
    may claim a recovery that cannot happen — bootstrap resolves None and says so, and
    the export warned about it before the folder ever left the building."""
    root = tmp_path / "ROOT"
    app = build_request.app_id
    build_history(build_request, root, "v1.0.0")

    out = tmp_path / "deliver"
    export = store_builder.export_full_tree(root, out)

    delivered = state_of(out, app)
    assert delivered["candidate"] == "v1.0.0" and delivered["previous"] is None
    paths = store_builder.AppPaths(out, app)
    state = state_mod.StateStore(paths.state_dir).load()
    assert state.rollback_target() is None
    assert bootstrap.resolve_rollback_target(paths, state) is None   # honestly nothing
    # fail_candidate REFUSES to invent one, so no false 「已恢復前一版本」 is possible
    with pytest.raises(state_mod.StateError):
        state_mod.fail_candidate(state, target=None)
    # and the operator was told at export time, while they could still fix it
    assert any("沒有可以退回的版本" in w for w in export.warnings), export.warnings


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


def test_exporting_one_app_does_not_delete_the_other_apps_only_entry_point(stub_toolchain,
                                                                           tmp_path):
    """S8, BLOCKER. The operator reuses the USB folder: it already holds a delivery of
    App A. They export App B into it — and the exporter unlinked every start bat that
    was not part of THIS export, so App A's start bat was deleted while App A's entire
    500 MB tree stayed exactly where it was. The folder still contains App A. There is
    just no longer any way for a human being to start it.

    tools\\ had the right rule all along (the union of「this export」and「what is
    installed in the destination」). The entry bats and the 讀我 now use the same one.
    """
    root = tmp_path / "ROOT"
    alpha = make_project(tmp_path, "Alpha Viewer")
    beta = make_project(tmp_path, "Beta Viewer")
    build_history(alpha, root, "v1")
    build_history(beta, root, "v1")

    usb = tmp_path / "usb"
    store_builder.export_full_tree(root, usb, app_id=alpha.app_id)
    assert (usb / "start.bat").is_file()          # one app in the folder: one entry

    second = store_builder.export_full_tree(root, usb, app_id=beta.app_id)

    # Alpha is STILL INSTALLED in this folder, so alpha still has its entry point.
    assert (usb / "apps" / alpha.app_id / "versions" / "v1").is_dir()
    assert (usb / f"start-{alpha.app_id}.bat").is_file(), "把還裝在這裡的 App 的唯一入口刪掉了"
    assert (usb / f"start-{beta.app_id}.bat").is_file()
    # ...and it is still administrable, and the report names both entries
    assert (usb / "tools" / f"admin-{alpha.app_id}.bat").is_file()
    assert sorted(second.entry_bats) == sorted([f"start-{alpha.app_id}.bat",
                                                f"start-{beta.app_id}.bat"])
    # the operator is TOLD the folder now holds an app this export did not deliver
    assert any(alpha.app_id in w for w in second.warnings), second.warnings
    # and the 讀我 in that folder names both apps and both bats
    readme = (usb / store_builder.README_NAME).read_text("utf-8")
    for app, request in ((alpha.app_id, alpha), (beta.app_id, beta)):
        assert f"start-{app}.bat" in readme
        assert request.display_name in readme


def test_exporting_app_b_does_not_hijack_app_as_admin_console(stub_toolchain, tmp_path):
    """S8. The machine on the line already runs App A. The operator exports App B onto
    it (a second delivery into the same folder, from a DIFFERENT store — which is how
    two apps built by two teams actually meet).

    If tools\\ were written from 「what this export delivered」 rather than 「what is
    installed in this folder」, tools\\admin.bat would take the one-app path and forward
    to App B: the machine's admin console would silently administer the wrong app —
    「退回上一版」 rolls back B while the operator is standing in front of A — and the
    讀我 would deny that A is even here.

    The union rule (see _write_tools / export_full_tree) is what prevents it, and this
    is the test that keeps it honest for two SEPARATE stores, not just two apps in one.
    """
    store_a, store_b = tmp_path / "STORE_A", tmp_path / "STORE_B"
    alpha = make_project(tmp_path, "產線 A 檢視器", app_id="line-a-viewer")
    beta = make_project(tmp_path, "產線 B 報表", app_id="line-b-report")
    build_history(alpha, store_a, "v1.0.0")
    build_history(beta, store_b, "v1.0.0")

    machine = tmp_path / "machine"
    store_builder.export_full_tree(store_a, machine)
    assert (machine / "tools" / "admin.bat").read_text("ascii").count("admin-") >= 1

    second = store_builder.export_full_tree(store_b, machine)

    # 1. the console is a CHOOSER over both apps, not a forwarder to the newcomer
    admin = (machine / "tools" / "admin.bat").read_text("ascii")
    assert f"admin-{alpha.app_id}.bat" in admin, "App A 的主控台被 App B 劫走了"
    assert f"admin-{beta.app_id}.bat" in admin
    assert admin.count("call ") == 2                     # one dispatch line per app
    for app in (alpha.app_id, beta.app_id):
        assert (machine / "tools" / f"admin-{app}.bat").is_file()
        # ...and each console really drives ITS OWN app
        own = (machine / "tools" / f"admin-{app}.bat").read_text("ascii")
        assert f"--app {app}" in own
        other = beta.app_id if app == alpha.app_id else alpha.app_id
        assert f"--app {other}" not in own

    # 2. the chooser's menu names both apps, in the operator's language
    chooser = message(machine, "admin-chooser.txt")
    assert alpha.display_name in chooser and beta.display_name in chooser

    # 3. the 讀我 does not deny that App A is here
    readme = (machine / store_builder.README_NAME).read_text("utf-8")
    assert alpha.display_name in readme and beta.display_name in readme
    assert f"start-{alpha.app_id}.bat" in readme

    # 4. and the export SAYS the folder already held an app it did not deliver
    assert second.apps == [beta.app_id]                  # what THIS export delivered
    assert any(alpha.app_id in w for w in second.warnings), second.warnings
    for line in second.warnings:
        line.encode("cp950")


def test_exporting_the_whole_tree_delivers_every_app_coherently(stub_toolchain, tmp_path):
    """S8, the 「整棵樹(全部 N 個 App)」 path — export_full_tree(app_id=None). It has
    always worked and nothing could reach it; now that the GUI can, every part of the
    delivered folder has to be about ALL the apps, not the first one:

      * one state.json per app, each pointing at its own version;
      * one entry bat per app, each starting ITS app;
      * tools\\ listing every app, and a chooser to pick between them;
      * a 讀我 that says WHICH bat starts WHICH app (「雙擊 a.bat、b.bat」 tells a line
        worker to double-click both files to start one program);
      * and only the runtimes/shells those apps really reference.
    """
    root = tmp_path / "ROOT"
    # different locks = different runtime fingerprints: the delivery must carry BOTH,
    # and nothing else.
    alpha = make_project(tmp_path, "Alpha Viewer")
    beta = make_project(tmp_path, "報表分析", lock=PANDAS_LOCK, app_id="report-analyzer")
    build_history(alpha, root, "v1.0.0")
    build_history(beta, root, "v2.0.0")

    out = tmp_path / "deliver-all"
    export = store_builder.export_full_tree(root, out)          # app_id=None: 整棵樹

    assert export.apps == sorted([alpha.app_id, beta.app_id])
    assert sorted(export.versions) == sorted([f"{alpha.app_id}/v1.0.0",
                                              f"{beta.app_id}/v2.0.0"])
    for app, version, request in ((alpha.app_id, "v1.0.0", alpha),
                                  (beta.app_id, "v2.0.0", beta)):
        assert state_of(out, app)["current"] == version         # per-app state
        assert state_of(out, app)["pending"] is None
        assert integrity.is_complete(out / "apps" / app / "versions" / version)
        bat = out / f"start-{app}.bat"
        assert bat.is_file() and f"--app {app}" in bat.read_text("utf-8")
        assert (out / "tools" / f"admin-{app}.bat").is_file()
        # each app's own Chinese/plain name reaches its own console, and no other's
        assert request.display_name in message(out, f"admin-menu-{app}.txt")
        assert request.display_name in message(out, f"starting-{app}.txt")
    assert not (out / "start.bat").exists()      # ambiguous with two apps: it must go

    # the chooser knows both apps
    chooser = (out / "tools" / "admin.bat").read_text("utf-8")
    assert all(f"admin-{a}.bat" in chooser for a in export.apps)
    assert alpha.display_name in message(out, "admin-chooser.txt")
    assert beta.display_name in message(out, "admin-chooser.txt")

    # 讀我: which bat starts which app, by name
    readme = (out / store_builder.README_NAME).read_text("utf-8")
    for app, request in ((alpha.app_id, alpha), (beta.app_id, beta)):
        assert re.search(rf"{re.escape(request.display_name)}.*start-{re.escape(app)}\.bat",
                         readme), readme
    readme.encode("cp950")

    # exactly the runtimes/shells those two versions name — a store that has built
    # other things must not ship them.
    manifests = [json.loads((out / "apps" / a / "versions" / v / "app-package.json")
                            .read_text("utf-8"))
                 for a, v in ((alpha.app_id, "v1.0.0"), (beta.app_id, "v2.0.0"))]
    wanted_runtimes = {m["runtime_fingerprint"] for m in manifests}
    wanted_shells = {m["shell_fingerprint"] for m in manifests}
    assert len(wanted_runtimes) == 2                    # the locks really do differ
    assert {p.name for p in (out / "deps" / "runtimes").iterdir()} == wanted_runtimes
    assert {p.name for p in (out / "deps" / "shells").iterdir()} == wanted_shells
    assert export.includes_runtime is True


def test_a_start_bat_of_an_app_that_is_not_in_the_folder_is_removed(build_request,
                                                                    stub_toolchain,
                                                                    tmp_path):
    """The other side of the union rule. A leftover bat for an app that is NOT in the
    destination starts nothing: it must go."""
    root = tmp_path / "ROOT"
    build_history(build_request, root, "v1.0.0")
    out = tmp_path / "usb"
    out.mkdir()
    ghost = out / "start-app-ghost.bat"
    ghost.write_text('@echo off\r\n"%PY%" "bootstrap\\bootstrap.py" --app app-ghost %*\r\n',
                     encoding="ascii")

    export = store_builder.export_full_tree(root, out)
    assert not ghost.exists()
    assert export.entry_bats == ["start.bat"]
    assert sorted(p.name for p in out.glob("start*.bat")) == ["start.bat"]


def test_a_second_delivery_never_writes_over_another_installed_apps_entry_bat(
        stub_toolchain, tmp_path):
    """S8, THE DESTRUCTIVE STEP. App A was delivered from a one-app store, so its entry
    is `start.bat`. App B is delivered from ANOTHER one-app store, whose entry is also
    called `start.bat` — and the exporter copied it straight over A's file before
    anything had read who owned it.

    A survived only by accident: _start_bat_text() happens to be byte-identical for
    every app, so the regeneration pass could reconstruct what had just been destroyed.
    The day a bat carries one app-specific line, App A's entry point silently becomes a
    launcher for App B — on a machine where both are installed, so nothing looks wrong
    until the wrong program opens.

    The bytes of A's own bat must survive the delivery of B, whatever they are.
    """
    store_a, store_b = tmp_path / "STORE_A", tmp_path / "STORE_B"
    alpha = make_project(tmp_path, "Alpha Viewer")
    beta = make_project(tmp_path, "Beta Viewer")
    build_history(alpha, store_a, "v1.0.0")
    build_history(beta, store_b, "v1.0.0")

    machine = tmp_path / "machine"
    store_builder.export_full_tree(store_a, machine)
    # Mark A's own entry bat, so a file that merely LOOKS right cannot pass.
    a_bat = machine / "start.bat"
    original = a_bat.read_text("ascii")
    a_bat.write_text(original + "rem OWNED-BY-APP-A\r\n", encoding="ascii")
    a_bytes = a_bat.read_bytes()

    store_builder.export_full_tree(store_b, machine)

    kept = machine / f"start-{alpha.app_id}.bat"
    assert kept.is_file(), "App A 還裝在這個資料夾裡,卻沒有任何啟動檔"
    assert kept.read_bytes() == a_bytes, "App A 的啟動檔被 App B 的覆蓋掉了"
    assert f"--app {alpha.app_id}" in kept.read_text("ascii")
    assert f"--app {beta.app_id}" not in kept.read_text("ascii")
    # ...and B got its own, which starts B and only B
    b_bat = machine / f"start-{beta.app_id}.bat"
    assert f"--app {beta.app_id}" in b_bat.read_text("ascii")
    assert not (machine / "start.bat").exists()      # ambiguous with two apps


def test_a_rename_of_the_only_file_a_user_double_clicks_is_never_reported_as_no_change(
        stub_toolchain, tmp_path):
    """S8. Delivering App B into App A's folder DOES remove `start.bat` — it has to, it
    no longer says which app — and the export then told the operator, in as many words,
    that App A's 「版本、啟動檔與管理主控台都原封不動留著」. The one file the line worker
    had been taught to double-click was gone, and the warning about it denied it.

    A warning that contradicts what the code just did is worse than no warning: it sends
    the operator looking for a different explanation for a problem we created.
    """
    store_a, store_b = tmp_path / "STORE_A", tmp_path / "STORE_B"
    alpha = make_project(tmp_path, "產線 A 檢視器", app_id="line-a-viewer")
    beta = make_project(tmp_path, "產線 B 報表", app_id="line-b-report")
    build_history(alpha, store_a, "v1.0.0")
    build_history(beta, store_b, "v1.0.0")

    machine = tmp_path / "machine"
    store_builder.export_full_tree(store_a, machine)
    assert (machine / "start.bat").is_file()

    second = store_builder.export_full_tree(store_b, machine)

    assert not (machine / "start.bat").exists()
    told = [w for w in second.warnings if "start.bat" in w]
    assert told, f"start.bat 被刪掉了,卻沒有半個字告訴操作員:{second.warnings}"
    said = told[0]
    # it names the app, the old file, and the new file — all three, or it is not a fix
    assert alpha.app_id in said and alpha.display_name in said
    assert f"start-{alpha.app_id}.bat" in said
    assert "→" in said or "->" in said
    # and NOTHING anywhere in this export may still claim the entry files were untouched
    assert not any("啟動檔" in w and "原封不動" in w for w in second.warnings)
    for line in second.warnings:
        line.encode("cp950")


def test_one_factory_pc_two_apps_app_a_still_starts_administers_and_remembers(
        stub_toolchain, tmp_path):
    """S8, THE WHOLE SCENARIO, END TO END.

    One factory PC. App A has been running on it for months: it has a version, a
    runtime, an admin console, a state.json that knows which version is good and which
    one died. App B is built by another team, in another store, and delivered onto the
    same machine — which is the entire reason the store layout exists (one 500 MB
    runtime, two apps).

    Everything App A has must survive that delivery: it must still START, its console
    must still administer A (and not B), its versions and its state must be untouched,
    and the folder must still tell the truth about both apps.
    """
    store_a, store_b = tmp_path / "STORE_A", tmp_path / "STORE_B"
    alpha = make_project(tmp_path, "產線 A 檢視器", app_id="line-a-viewer")
    beta = make_project(tmp_path, "產線 B 報表", app_id="line-b-report")
    build_history(alpha, store_a, "v1.0.0")
    build_history(beta, store_b, "v2.0.0")

    machine = tmp_path / "machine"
    store_builder.export_full_tree(store_a, machine)

    # Months on the line: A proved v1.0.0 good, and watched v1.5.0 die.
    a_state = state_mod.StateStore(machine / "apps" / alpha.app_id / "state")
    a_state.mutate(lambda s: state_mod.dataclasses.replace(
        s, candidate=None, last_known_good="v1.0.0",
        failed_versions=[{"version": "v1.5.0", "revision": "diedhere"}]))
    before = a_state.load()
    a_runtime = json.loads(
        (machine / "apps" / alpha.app_id / "versions" / "v1.0.0" / "app-package.json")
        .read_text("utf-8"))["runtime_fingerprint"]

    export = store_builder.export_full_tree(store_b, machine)   # ← App B lands

    # 1. APP A STILL STARTS. It has exactly one entry bat, that bat starts A, it is
    #    pure ASCII, and every message it prints is really in this folder.
    a_bats = [b for b in machine.glob("start*.bat")
              if store_builder._entry_bat_app(b) == alpha.app_id]
    assert len(a_bats) == 1, f"App A 的啟動檔剩下 {len(a_bats)} 個"
    a_bat = a_bats[0]
    raw = a_bat.read_bytes()
    assert raw.isascii()
    assert f"--app {alpha.app_id}" in raw.decode("ascii")
    assert f"--app {beta.app_id}" not in raw.decode("ascii")
    for name in re.findall(r'messages\\([\w.-]+\.txt)', raw.decode("ascii")):
        assert (machine / store_builder.MESSAGES_DIR / name).is_file(), name
    assert a_bat.name in export.entry_bats

    # 2. A's OWN NAME still reaches the person in front of it (the source store B has
    #    never heard of App A, so this has to be read from the destination).
    assert alpha.display_name in message(machine, f"starting-{alpha.app_id}.txt")
    assert alpha.display_name in message(machine, f"admin-menu-{alpha.app_id}.txt")

    # 3. A'S CONSOLE STILL ADMINISTERS A. The chooser offers both; A's console drives A.
    admin = (machine / "tools" / f"admin-{alpha.app_id}.bat").read_text("ascii")
    assert f"--app {alpha.app_id}" in admin and f"--app {beta.app_id}" not in admin
    chooser = (machine / "tools" / "admin.bat").read_text("ascii")
    assert f"admin-{alpha.app_id}.bat" in chooser and f"admin-{beta.app_id}.bat" in chooser
    assert alpha.display_name in message(machine, "admin-chooser.txt")

    # 4. A'S STATE IS INTACT — every field, byte for byte. This export never touched it.
    assert a_state.load().to_dict() == before.to_dict()
    assert a_state.load().is_failed("v1.5.0", "diedhere")
    assert integrity.is_complete(machine / "apps" / alpha.app_id / "versions" / "v1.0.0")
    assert integrity.verify_tree(
        machine / "apps" / alpha.app_id / "versions" / "v1.0.0") == []

    # 5. A's RUNTIME is still there — and B, built from the same lock, SHARES it. That
    #    is the whole reason this machine has a store on it instead of two fat packages.
    b_runtime = json.loads(
        (machine / "apps" / beta.app_id / "versions" / "v2.0.0" / "app-package.json")
        .read_text("utf-8"))["runtime_fingerprint"]
    assert b_runtime == a_runtime
    assert {p.name for p in (machine / "deps" / "runtimes").iterdir()} == {a_runtime}

    # 6. B works too, and the folder tells the truth about both.
    assert state_of(machine, beta.app_id)["current"] == "v2.0.0"
    readme = (machine / store_builder.README_NAME).read_text("utf-8")
    assert alpha.display_name in readme and beta.display_name in readme
    assert a_bat.name in readme
    readme.encode("cp950")
    for line in export.warnings:
        line.encode("cp950")


# ── S8:重新交付 = 更新,更新不可以抹掉這台機器學到的東西 ────────────────────

def test_redelivering_an_app_does_not_erase_what_the_machine_learned_about_bad_versions(
        stub_toolchain, tmp_path):
    """S8/S4, BLOCKER. The factory PC ran App B, watched v2.0.0 die on startup, and
    rolled back. That failure is written in ITS state.json, and it is the only reason
    the background updater does not re-stage v2.0.0 off the share tomorrow morning.

    The operator then hand-delivers v3.0.0 — and the exporter wrote a FRESH state over
    the top: failed_versions emptied, last_known_good erased, generation reset from 2
    to 1. The machine forgot, in one copy, everything it had learned about itself the
    hard way. Next updater pass, v2.0.0 comes straight back.

    A delivery decides which version RUNS. It does not get to decide what the machine
    remembers.
    """
    store = tmp_path / "STORE"
    beta = make_project(tmp_path, "Beta Viewer")
    build_history(beta, store, "v1.0.0")

    machine = tmp_path / "machine"
    store_builder.export_full_tree(store, machine)

    # What this MACHINE learned, on the factory floor, that no build machine can know.
    paths = state_mod.StateStore(machine / "apps" / beta.app_id / "state")
    learned = state_mod.dataclasses.replace(
        paths.load(), candidate=None, last_known_good="v1.0.0",
        failed_versions=[{"version": "v2.0.0", "revision": "cafebabe"}])
    before = paths.write_locked(learned)

    build_history(beta, store, "v3.0.0")
    export = store_builder.export_full_tree(store, machine, app_id=beta.app_id,
                                            version="v3.0.0")
    after = paths.load()

    # 1. the delivery decides what RUNS, and puts it on trial
    assert after.current == "v3.0.0"
    assert after.candidate == "v3.0.0" and after.candidate_revision
    assert after.pending is None

    # 2. ...and it forgets NOTHING the machine learned
    assert after.failed_versions == [{"version": "v2.0.0", "revision": "cafebabe"}], \
        "這台機器親眼看著 v2.0.0 起不來,交付把那個記錄抹掉了 —— 自動更新會再裝一次"
    assert after.is_failed("v2.0.0", "cafebabe")
    assert after.last_known_good == "v1.0.0", "抹掉了這台機器唯一證明過能跑的版本"
    assert after.generation > before.generation, "generation 倒退了"

    # 3. the rollback floor is the version this machine was really running
    assert after.previous == "v1.0.0"
    assert integrity.is_complete(machine / "apps" / beta.app_id / "versions" / "v1.0.0")
    assert export.apps == [beta.app_id]


def test_a_first_delivery_still_gets_a_fresh_state_and_not_the_build_machines(
        build_request, stub_toolchain, tmp_path):
    """The other half of the same decision. A destination that has never seen this app
    has learned NOTHING, so there is nothing to preserve — and the build machine's own
    pending/candidate/failure history must still not travel (see S4)."""
    root = tmp_path / "ROOT"
    build_history(build_request, root, "v1.0.0", "v1.1.0")
    app = build_request.app_id
    store_mod = state_mod.StateStore(root / "apps" / app / "state")
    store_mod.mutate(lambda s: state_mod.dataclasses.replace(
        s, last_known_good="v1.0.0",
        failed_versions=[{"version": "v9.9.9", "revision": "buildmachine"}]))

    out = tmp_path / "deliver"
    store_builder.export_full_tree(root, out, app_id=app, version="v1.1.0")

    fresh = state_of(out, app)
    assert fresh["current"] == "v1.1.0"
    assert fresh["candidate"] == "v1.1.0"          # never started HERE: on trial
    assert fresh["pending"] is None
    assert fresh["last_known_good"] is None        # nothing has started here yet
    assert fresh["failed_versions"] == []          # the BUILD machine's history stays home


def test_delivering_over_a_machines_own_staged_update_cancels_it_and_says_so(
        build_request, stub_toolchain, tmp_path):
    """The machine's updater staged v2.0.0 and is waiting for a restart to promote it.
    The operator walks over and hand-delivers v3.0.0. If `pending` survived the
    delivery, the next boot would promote v2.0.0 straight over the version the operator
    just carried across the factory — and nobody would ever know why."""
    root = tmp_path / "ROOT"
    build_history(build_request, root, "v1.0.0")
    app = build_request.app_id

    machine = tmp_path / "machine"
    store_builder.export_full_tree(root, machine)
    paths = state_mod.StateStore(machine / "apps" / app / "state")
    paths.mutate(lambda s: state_mod.dataclasses.replace(
        s, candidate=None, last_known_good="v1.0.0",
        pending="v2.0.0", pending_revision="staged"))

    build_history(build_request, root, "v3.0.0")
    export = store_builder.export_full_tree(root, machine, app_id=app, version="v3.0.0")

    after = paths.load()
    assert after.current == "v3.0.0"
    assert after.pending is None, "目標機自己排好的更新會在下次啟動蓋掉這次交付的版本"
    warned = [w for w in export.warnings if "v2.0.0" in w]
    assert warned, f"取消了目標機已經裝好的更新,卻沒說:{export.warnings}"
    for line in export.warnings:
        line.encode("cp950")


def test_delivering_a_version_this_machine_already_watched_die_says_so(
        build_request, stub_toolchain, tmp_path):
    """We keep the machine's failure record (above) — so we must also say out loud when
    the version being delivered is IN it. Silently making a known-bad version `current`
    is how an operator ends up watching the same rollback twice."""
    root = tmp_path / "ROOT"
    build_history(build_request, root, "v1.0.0", "v2.0.0")
    app = build_request.app_id

    machine = tmp_path / "machine"
    store_builder.export_full_tree(root, machine, app_id=app, version="v1.0.0")
    revision = store_builder.version_revision(
        store_builder.AppPaths(root, app), "v2.0.0")
    paths = state_mod.StateStore(machine / "apps" / app / "state")
    paths.mutate(lambda s: state_mod.dataclasses.replace(
        s, candidate=None, last_known_good="v1.0.0",
        failed_versions=[{"version": "v2.0.0", "revision": revision}]))

    export = store_builder.export_full_tree(root, machine, app_id=app, version="v2.0.0")

    warned = [w for w in export.warnings if "v2.0.0" in w and "失敗" in w]
    assert warned, f"交付了一個這台機器已經證明起不來的版本,卻沒說:{export.warnings}"
    assert "清除失敗記錄" in warned[0]                # ...and how to get past it
    assert paths.load().is_failed("v2.0.0", revision)  # the record itself still stands
    for line in export.warnings:
        line.encode("cp950")


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


# ── S8/S5:版本槽之間去重 —— 「一次改版只搬十幾 MB」必須是真的 ──────────────

def apparent_size(path: Path) -> int:
    """What `dir` and Explorer report: every directory entry's size, summed."""
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


def real_size(path: Path) -> int:
    """What the VOLUME actually lost: each inode counted once, however many names it has.

    This is the number the factory PC's disk cares about, and the number nobody was
    measuring."""
    seen, total = set(), 0
    for item in path.rglob("*"):
        if not item.is_file():
            continue
        stat = item.stat()
        key = (stat.st_dev, stat.st_ino)
        if key in seen:
            continue
        seen.add(key)
        total += stat.st_size
    return total


def cv_viewer(tmp_path: Path, weight_mb: int = 8) -> BuildRequest:
    """CV_Viewer's shape: one big model file that never changes, and some Python."""
    request = make_project(tmp_path, "CV Viewer", app_id="cv-viewer")
    models = request.project_dir / "models"
    models.mkdir()
    (models / "dinov2.pth").write_bytes(os.urandom(weight_mb * 1024 ** 2))
    return request


def test_a_second_version_costs_what_changed_not_another_copy_of_the_model_file(
        stub_toolchain, tmp_path):
    """S8/S5, THE PROMISE. The store layout is sold on 「一次改版只搬十幾 MB」 — that is
    the entire reason a factory PC gets a store instead of two fat packages. For
    CV_Viewer it was false: a version directory is a WHOLE copy of the app, so its 84 MB
    DINOv2 weight (unchanged in a year) was written again into every single slot. Five
    releases cost 481 MB to store five copies of one identical file.

    A byte-identical file in an existing slot is now a hardlink, not a second copy: two
    names, one inode, one lot of bytes. The slot still LOOKS full size (that is what a
    version is), but the disk only pays once.
    """
    root = tmp_path / "ROOT"
    request = cv_viewer(tmp_path)
    weight = 8 * 1024 ** 2

    first = store_builder.build_into_store(request, root, version="v1.0.0")
    assert first.ok, first.errors
    assert first.deduped_mb == 0                     # nothing to share with yet
    assert first.added_mb >= 8                       # the model really did cost 8 MB

    # A real incremental release: the Python moved, the model did not.
    request.entrypoint.write_text("import streamlit as st\nst.write('v1.1')\n",
                                  encoding="utf-8")
    second = store_builder.build_into_store(request, root, version="v1.1.0")
    assert second.ok, second.errors

    app = request.app_id
    versions = root / "apps" / app / "versions"
    w1 = versions / "v1.0.0" / "application" / "models" / "dinov2.pth"
    w2 = versions / "v1.1.0" / "application" / "models" / "dinov2.pth"

    # 1. ONE inode, two names. The second version's model cost the disk nothing.
    assert w1.stat().st_ino == w2.stat().st_ino, "第二版又複製了一份一模一樣的權重檔"
    assert w2.stat().st_nlink == 2
    assert second.deduped_mb >= 8
    assert second.added_mb < 1, f"這一版只改了幾行 Python,卻付了 {second.added_mb:.0f} MB"
    assert second.runtime_reused                     # ...and the runtime was shared too

    # 2. the slot still looks like a whole version (it IS one), but the disk paid once
    assert apparent_size(versions) >= 2 * weight
    assert real_size(versions) < 1.5 * weight, "磁碟還是被扣了兩份"

    # 3. BOTH versions still verify byte-for-byte against their own files.json — the
    #    device checks exactly this before it will run anything.
    for version in ("v1.0.0", "v1.1.0"):
        assert integrity.is_complete(versions / version)
        assert integrity.verify_tree(versions / version) == [], version


def test_deleting_one_version_never_takes_a_byte_another_version_still_points_at(
        stub_toolchain, tmp_path):
    """The question a hardlink scheme lives or dies on. GC deletes an old version slot;
    the file it shares with the version the factory is RUNNING must not go with it.

    It cannot: a name goes, and the bytes go only when the LAST name goes. This test is
    here because "cannot" is not a thing to take on faith about a factory PC's only copy
    of an 84 MB model file."""
    root = tmp_path / "ROOT"
    request = cv_viewer(tmp_path, weight_mb=4)
    assert store_builder.build_into_store(request, root, version="v1.0.0").ok
    request.entrypoint.write_text("import streamlit as st\nst.write('v2')\n",
                                  encoding="utf-8")
    assert store_builder.build_into_store(request, root, version="v1.1.0").ok

    versions = root / "apps" / request.app_id / "versions"
    live = versions / "v1.1.0"
    weight = live / "application" / "models" / "dinov2.pth"
    before = weight.read_bytes()
    assert weight.stat().st_nlink == 2

    shutil.rmtree(versions / "v1.0.0")               # exactly what gc.py does

    assert weight.is_file()
    assert weight.read_bytes() == before, "刪掉舊版本時,把還在跑的版本的權重檔一起帶走了"
    assert weight.stat().st_nlink == 1
    assert integrity.verify_tree(live) == [], "刪掉舊版本之後,還在跑的版本驗不過了"
    assert integrity.is_complete(live)


def test_a_delivered_folder_is_self_contained_and_never_a_link_back_to_the_build_machine(
        stub_toolchain, tmp_path):
    """The delivered USB has to work on a machine that has never seen this store. The
    slots inside it must be real files — a delivery whose files are directory entries
    pointing at a build machine's inodes is not a delivery."""
    root = tmp_path / "ROOT"
    request = cv_viewer(tmp_path, weight_mb=4)
    assert store_builder.build_into_store(request, root, version="v1.0.0").ok
    request.entrypoint.write_text("import streamlit as st\nst.write('v2')\n",
                                  encoding="utf-8")
    assert store_builder.build_into_store(request, root, version="v1.1.0").ok
    app = request.app_id

    out = tmp_path / "deliver"
    store_builder.export_full_tree(root, out, app_id=app, version="v1.1.0")

    delivered = out / "apps" / app / "versions" / "v1.1.0"
    weight = delivered / "application" / "models" / "dinov2.pth"
    assert weight.is_file() and weight.stat().st_size == 4 * 1024 ** 2
    assert weight.stat().st_nlink == 1, "交付出去的檔案還連著建置機的 inode"
    assert integrity.verify_tree(delivered) == []
    # ...and the source's own slots are untouched by having been exported
    assert integrity.verify_tree(root / "apps" / app / "versions" / "v1.1.0") == []


def test_a_filesystem_that_cannot_hardlink_still_builds_it_just_copies(
        stub_toolchain, tmp_path, monkeypatch):
    """FAT/exFAT — most USB sticks, and spec §9.3 promises they work — has no hard links
    at all. `os.link` raises there, and a build that treated that as a failure would
    refuse to run on the media this product is delivered on. It degrades to a copy, and
    the version is byte-for-byte correct either way."""
    root = tmp_path / "ROOT"
    request = cv_viewer(tmp_path, weight_mb=2)
    assert store_builder.build_into_store(request, root, version="v1.0.0").ok

    def no_links_here(_src, _dst):
        raise OSError(1, "ERROR_INVALID_FUNCTION")   # what exFAT actually gives

    monkeypatch.setattr(store_builder.os, "link", no_links_here)
    request.entrypoint.write_text("import streamlit as st\nst.write('v2')\n",
                                  encoding="utf-8")
    second = store_builder.build_into_store(request, root, version="v1.1.0")

    assert second.ok, second.errors
    assert second.deduped_mb == 0                    # nothing shared: it could not be
    versions = root / "apps" / request.app_id / "versions"
    w1 = versions / "v1.0.0" / "application" / "models" / "dinov2.pth"
    w2 = versions / "v1.1.0" / "application" / "models" / "dinov2.pth"
    assert w1.stat().st_ino != w2.stat().st_ino      # two real copies
    assert w1.read_bytes() == w2.read_bytes()
    for version in ("v1.0.0", "v1.1.0"):             # and both are still correct
        assert integrity.verify_tree(versions / version) == []


def test_a_file_that_really_changed_is_never_linked_to_the_old_versions_copy(
        stub_toolchain, tmp_path):
    """The dedup must key on CONTENT, not on the path. A model file that was retrained
    between releases has the same name and (plausibly) the same size — linking it to the
    old version's bytes would ship the OLD model under the new version's number, and
    files.json would agree with itself all the way to the factory floor."""
    root = tmp_path / "ROOT"
    request = cv_viewer(tmp_path, weight_mb=2)
    assert store_builder.build_into_store(request, root, version="v1.0.0").ok

    retrained = os.urandom(2 * 1024 ** 2)            # same path, same size, new bytes
    (request.project_dir / "models" / "dinov2.pth").write_bytes(retrained)
    second = store_builder.build_into_store(request, root, version="v1.1.0")
    assert second.ok, second.errors

    versions = root / "apps" / request.app_id / "versions"
    w1 = versions / "v1.0.0" / "application" / "models" / "dinov2.pth"
    w2 = versions / "v1.1.0" / "application" / "models" / "dinov2.pth"
    assert w1.stat().st_ino != w2.stat().st_ino, "改過的權重檔被連到舊版本的位元組上了"
    assert w2.read_bytes() == retrained
    assert w1.read_bytes() != retrained             # the old version still has the old one
    assert second.deduped_mb < 2                    # the model was NOT shared
    for version in ("v1.0.0", "v1.1.0"):
        assert integrity.verify_tree(versions / version) == []


# ── S8:「10 MB 的增量更新」到底有多大 ───────────────────────────────────────

def test_an_incremental_update_package_says_how_much_of_it_is_the_same_bytes_again(
        stub_toolchain, tmp_path, monkeypatch):
    """S8/S5. A version directory is a WHOLE COPY of the app. CV_Viewer's 84 MB DINOv2
    weight has not changed in a year, and it is copied into every version slot, into
    every delivery, and into every "incremental" update package — so a release that
    changed 10 MB of Python still costs 94 MB on the wire, every single time.

    We are NOT deduplicating it (see the hardlink note in store_builder: links do not
    survive the copytree that every export goes through, they would make gc.py's
    reclaimed-bytes report a lie, and they would let one corrupted file corrupt every
    version that shares it). What was actually costing the operator is that nobody ever
    TOLD them: they sat in front of a checkbox choosing between a 「10 MB 增量包」 and a
    「457 MB 完整包」, and the 10 MB one did not exist.

    So the size is made honest, with the file named, while they are still choosing.
    """
    monkeypatch.setattr(store_builder, "_REDUNDANT_WARNING_BYTES", 1024 ** 2)
    root = tmp_path / "ROOT"
    request = make_project(tmp_path, "CV Viewer")
    weights = request.project_dir / "weights"
    weights.mkdir()
    (weights / "dinov2.pth").write_bytes(b"W" * (3 * 1024 ** 2))     # never changes
    build_history(request, root, "v1.0.0")

    # A real 10 MB-of-nothing release: one line of Python moved, the model untouched.
    request.entrypoint.write_text("import streamlit as st\nst.write('v2')\n",
                                  encoding="utf-8")
    build_history(request, root, "v1.1.0")
    app = request.app_id
    assert (root / "apps" / app / "versions" / "v1.1.0" / "application" / "weights"
            / "dinov2.pth").is_file()

    out = tmp_path / "update"
    export = store_builder.export_update(root, app, "v1.1.0", out, include_runtime=False)

    assert export.kind == "update"
    assert export.redundant_mb >= 3, "沒算出「跟上一版一模一樣、還是又送一次」的位元組"
    said = [w for w in export.warnings if "dinov2.pth" in w]
    assert said, f"3 MB 的權重檔又送了一次,卻沒說:{export.warnings}"
    assert "v1.0.0" in said[0]                       # what it is identical TO
    # ...and the honest split, now that version slots on BOTH machines share their
    # unchanged files by hardlink: the disk does not pay twice, the STICK does.
    assert "硬連結" in said[0] and "FAT/exFAT" in said[0]
    assert ".provisionignore" in said[0]             # ...and the one thing that helps
    said[0].encode("cp950")


def test_a_package_whose_bytes_really_did_all_change_says_nothing_about_redundancy(
        build_request, stub_toolchain, tmp_path, monkeypatch):
    """The other side: a warning that fires on every export is a warning nobody reads.
    A small app whose files really did change has nothing redundant to report."""
    monkeypatch.setattr(store_builder, "_REDUNDANT_WARNING_BYTES", 1024 ** 2)
    root = tmp_path / "ROOT"
    build_history(build_request, root, "v1.0.0", "v1.1.0")

    export = store_builder.export_update(root, build_request.app_id, "v1.1.0",
                                         tmp_path / "update", include_runtime=False)
    assert export.redundant_mb < 1
    assert not [w for w in export.warnings if "不是差異更新" in w], export.warnings


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

def message(root: Path, name: str) -> str:
    return (root / store_builder.MESSAGES_DIR / name).read_text("utf-8")


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
    # The display name is DATA, never bytes in a .bat (see the ASCII rule) — but it
    # must still reach the operator, on the right console.
    assert "Alpha Viewer" in message(root, f"admin-menu-{first.app_id}.txt")
    assert "Beta Viewer" in message(root, f"admin-menu-{second.app_id}.txt")
    assert "Beta Viewer" not in message(root, f"admin-menu-{first.app_id}.txt")
    assert f"admin-menu-{first.app_id}.txt" in alpha        # and the console types it

    chooser = (tools / "admin.bat").read_text("utf-8")
    assert f"admin-{first.app_id}.bat" in chooser
    assert f"admin-{second.app_id}.bat" in chooser
    both = message(root, "admin-chooser.txt")
    assert "Alpha Viewer" in both and "Beta Viewer" in both

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
        # 3. no python at all = a Chinese error, not a silent run with PY unset.
        #    The bat is ASCII, so it `type`s the Chinese out of messages\.
        assert 'type "messages\\nopython.txt"' in bat
        assert "找不到任何可用的 python.exe" in message(root, "nopython.txt")
        # 4. a failed GC must not look exactly like a successful one
        assert 'set "RC=%errorlevel%"' in bat
        assert "回收失敗" in message(root, "gc-nothing.txt")
        assert "回收完成" in message(root, "gc-done.txt")
        # cp950: every message must be renderable on a zh-TW console, and the bat
        # itself must be pure ASCII (see test_every_generated_bat_is_pure_ascii)
        bat.encode("ascii")


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
    request = make_project(tmp_path, "產線 檢視器", app_id="line-viewer")
    assert store_builder.build_into_store(request, root, version="v1.0.0").ok

    bats = sorted(root.glob("*.bat")) + sorted((root / "tools").glob("*.bat"))
    assert len(bats) >= 4
    for bat in bats:
        text = bat.read_text("utf-8")
        assert "—" not in text, f"{bat.name} 帶了 em-dash,cmd.exe 會把那一行剖壞"
        text.encode("cp950")           # and every character must survive a zh-TW console


def generated_bats(root: Path) -> list[Path]:
    return sorted(root.glob("*.bat")) + sorted((root / "tools").glob("*.bat"))


def test_every_generated_bat_is_pure_ascii(stub_toolchain, tmp_path):
    """THE rule. Under `chcp 65001` cmd.exe tracks its position in a .bat as a BYTE
    offset but computes it by counting CHARACTERS. Every re-read — after a `for /f`,
    a pipe, an external command, a `goto`, all of which these bats do — seeks to an
    offset wrong by however many multi-byte characters came before it, lands in the
    MIDDLE of a line, and executes whatever text is sitting there. We measured it on
    this module's own start.bat: 1 corrupted run in 30, cmd executing the tail of a
    Chinese `rem`. In an ASCII-only file byte offset == character offset, so the seek
    cannot miss. The old 「no em-dash」 rule was this same bug seen through a keyhole.

    Checked with builder.bat_problems() — the same gate builder.py uses, not a second
    copy of the rule — plus the on-disk bytes, because that is what cmd reads.
    """
    root = tmp_path / "ROOT"
    request = make_project(tmp_path, "產線 檢視器", app_id="line-viewer")           # a name full of landmines
    assert store_builder.build_into_store(request, root, version="v1.0.0").ok

    bats = generated_bats(root)
    assert len(bats) >= 4
    for bat in bats:
        raw = bat.read_bytes()
        assert store_builder.bat_problems(raw.decode("utf-8")) == [], bat.name
        assert raw.isascii(), f"{bat.name} 帶了非 ASCII 位元組:cmd.exe 會 seek 到行中間"
        assert not raw.startswith(b"\xef\xbb\xbf"), f"{bat.name} 有 BOM"
        assert b"\r\n" in raw and b"\n" not in raw.replace(b"\r\n", b""), \
            f"{bat.name} 不是 CRLF"

    # and the Chinese still exists — as DATA the bats `type` and cmd never parses
    messages = sorted((root / store_builder.MESSAGES_DIR).glob("*.txt"))
    assert messages
    for path in messages:
        text = path.read_bytes().decode("utf-8")
        text.encode("cp950")            # a zh-TW console has to be able to render it
    assert any("產線 檢視器" in p.read_bytes().decode("utf-8") for p in messages)


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
    request = make_project(tmp_path, "產線 檢視器", app_id="line-viewer")
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


def run_bat(path: Path, *, stdin: str = "n\n",
            env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(["cmd", "/c", str(path)], input=stdin, capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=120,
                          env=env)


def parse_damage(proc: subprocess.CompletedProcess) -> list[str]:
    """Lines cmd could not parse. A mis-seek lands in the middle of a line and cmd
    executes the tail of it, which surfaces as 'X is not recognized as an internal
    command', as 'X was unexpected at this time' (a fragment of a ( ) block), and/or
    as replacement characters — what a corrupted .bat looks like from outside."""
    return [line for line in proc.stderr.splitlines()
            if "not recognized" in line or "不是內部或外部" in line or "�" in line
            or "unexpected at this time" in line]


# A 1-in-20 failure does not show up in a single run. Measured on the pre-fix bats:
# start.bat corrupted 1 run in 30. So one run catches it 3% of the time — which is
# how it passed every real-cmd test in this file until now. 20 runs of one bat catch
# it ~49% of the time; 20 runs of each of the five bats below, all of which carried
# the same Chinese, catch it ~97% of the time. That is a backstop, not a proof: the
# PROOF is test_every_generated_bat_is_pure_ascii, which is deterministic. Both stay.
CMD_RUNS = 20


@pytest.mark.skipif(os.name != "nt", reason="需要真的 cmd.exe 來剖析 .bat")
def test_every_bat_survives_being_run_over_and_over_by_a_real_cmd(stub_toolchain,
                                                                  tmp_path):
    """The test that would have caught it — and the reason the old ones did not.

    The seek bug fires on roughly 1 run in 20, so a real-cmd test that runs a bat
    ONCE proves nothing at all: it passes nineteen times out of twenty on a .bat that
    is definitely broken. Measured on the pre-fix bats, start.bat corrupted 1 run in
    30 (cmd executed the mojibake'd tail of a Chinese `rem`). Every bat here is run
    {CMD_RUNS} times and every single run must come back with clean stderr.
    """
    root = tmp_path / "ROOT"
    alpha = make_project(tmp_path, "產線 檢視器", app_id="line-viewer")             # Chinese display names:
    beta = make_project(tmp_path, "Beta 檢視器")             # the old landmine source
    assert store_builder.build_into_store(alpha, root, version="v1.0.0").ok
    assert store_builder.build_into_store(beta, root, version="v1.0.0").ok

    absent = shim_reg(tmp_path / "shim-absent", None)          # no WebView2
    present = shim_reg(tmp_path / "shim-present", "121.0.2277.128")

    cases = [
        # bat,                              stdin,   env,      a marker proving it ran
        (root / f"start-{alpha.app_id}.bat", "\n\n", absent,  "[start][ERROR]"),
        (root / "tools" / "gc.bat",          "n\n",  absent,  "[gc][ERROR]"),
        (root / "tools" / f"admin-{alpha.app_id}.bat", "0\n", absent, "產線 檢視器"),
        (root / "tools" / "admin.bat",       "0\n",  absent,  "Beta 檢視器"),   # chooser
        (root / "tools" / store_builder.WEBVIEW2_BAT_NAME, "\n", present,
         "WebView2 Runtime"),
    ]
    for bat, stdin, env, marker in cases:
        assert bat.is_file(), bat
        for attempt in range(CMD_RUNS):
            proc = run_bat(bat, stdin=stdin, env=env)
            assert parse_damage(proc) == [], \
                f"{bat.name} 第 {attempt + 1} 次執行被 cmd 剖壞了:{proc.stderr}"
            # not just "no error": it must have reached the line it was supposed to.
            # A mis-seek can also SKIP a line silently, and silence is what the
            # operator was left with.
            assert marker in proc.stdout, \
                f"{bat.name} 第 {attempt + 1} 次執行沒印出該印的東西:{proc.stdout!r}"


@pytest.mark.skipif(os.name != "nt", reason="需要真的 cmd.exe 來剖析 .bat")
def test_the_picker_chooses_the_referenced_runtime_not_the_orphan(stub_toolchain, tmp_path):
    """S9, checked by running the real thing: the exact picker text both consoles
    ship, handed to a real cmd.exe, on a tree with a referenced runtime and an
    orphan that sorts LAST (which is precisely what the old last-wins `for /d`
    loop would have landed on, and precisely the runtime GC then refuses to
    delete because it is executing from inside it)."""
    root = tmp_path / "ROOT"
    request = make_project(tmp_path, "產線 檢視器", app_id="line-viewer")
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
    request = make_project(tmp_path, "產線 檢視器", app_id="line-viewer")
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
        # one code, one message file — and each outcome says only what its code proves
        assert f'"%RC%"=="{store_builder.GC_EXIT_PARTIAL}"' in bat
        assert f'"%RC%"=="{store_builder.GC_EXIT_NOTHING}"' in bat
        assert f'"%RC%"=="{store_builder.GC_EXIT_LOCKED}"' in bat
        for txt in ("gc-partial.txt", "gc-nothing.txt", "gc-locked.txt",
                    "gc-unknown.txt", "gc-done.txt"):
            assert f'type "messages\\{txt}"' in bat, (name, txt)

    assert "有一部分刪掉了" in message(root, "gc-partial.txt")     # some space DID return
    assert "一個項目都沒有刪掉" in message(root, "gc-nothing.txt")  # and we say so, honestly
    # the store lock is named in exactly ONE message: the one the LOCKED code selects.
    # It used to be the guess printed after every failure, including the ones that had
    # just reclaimed 400 MB.
    named = [name for name in sorted((root / store_builder.MESSAGES_DIR).glob("*.txt"))
             if "store 鎖被佔用" in name.read_text("utf-8")]
    assert [p.name for p in named] == ["gc-locked.txt"], named
    assert "沒有刪掉任何東西" in message(root, "gc-locked.txt")
    assert "常見原因" not in message(root, "gc-nothing.txt")       # no more guessing
    # an unknown code claims nothing about what was or was not deleted
    assert "不在預期之內" in message(root, "gc-unknown.txt")

    # the console's table IS gc.py's table. A bat that maps 4 to 「鎖被佔用」 while
    # gc.py maps 4 to something else is a worse lie than the one we just fixed.
    assert store_builder.GC_EXIT_OK == gc_mod.EXIT_OK
    assert store_builder.GC_EXIT_PARTIAL == gc_mod.EXIT_PARTIAL
    assert store_builder.GC_EXIT_NOTHING == gc_mod.EXIT_NOTHING_DELETED
    assert store_builder.GC_EXIT_LOCKED == gc_mod.EXIT_STORE_LOCKED


def test_gc_bat_does_not_announce_success_for_a_plan_that_deletes_nothing(
        build_request, stub_toolchain, tmp_path):
    """An EMPTY plan. The dry run listed nothing, and the console then asked
    「以上列出的項目要真的刪除嗎? [y/N]」 over that blank list and finished with
    「回收完成。上面列出的項目都已經刪掉了。」 — a success message for a run that
    deleted nothing, printed at an operator who came looking for disk space and now
    believes they found some.

    The empty plan gets its own exit code and its own branch: no prompt, no 「完成」.
    """
    root = tmp_path / "ROOT"
    assert store_builder.build_into_store(build_request, root, version="v1.0.0").ok
    empty = store_builder.GC_EXIT_EMPTY
    assert empty not in (store_builder.GC_EXIT_OK, store_builder.GC_EXIT_PARTIAL,
                         store_builder.GC_EXIT_NOTHING, store_builder.GC_EXIT_LOCKED)

    for name in ("gc.bat", f"admin-{build_request.app_id}.bat"):
        bat = (root / "tools" / name).read_text("utf-8")
        assert bat.isascii(), name
        head, _, tail = bat.partition('"bootstrap\\gc.py" --apply')
        # BEFORE the confirmation prompt: an empty plan never reaches the [y/N]
        prompt = head.index("set /p YES=")
        assert head.index(f'"%RC%"=="{empty}"') < prompt, "空計畫還是被問了 y/N"
        assert f'"%RC%"=="{empty}"' in tail                # and after --apply too
        assert 'type "messages\\gc-empty.txt"' in bat
        # and the branch must not fall into the one that claims the listed items are gone
        empty_branch = bat[bat.index(":empty" if name == "gc.bat" else ":rempty"):]
        assert "gc-done.txt" not in empty_branch.split("goto menu")[0].split("exit /b")[0]

    body = message(root, "gc-empty.txt")
    assert "沒有可回收的項目" in body
    assert "刪掉了" not in body                            # nothing was deleted, so say so
    body.encode("cp950")


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
    request = make_project(tmp_path, "產線 檢視器", app_id="line-viewer")
    assert store_builder.build_into_store(request, root, version="v1.0.0").ok
    shutil.rmtree(root / "deps" / "runtimes")

    proc = run_bat(root / "tools" / "gc.bat")
    assert parse_damage(proc) == [], proc.stderr
    assert "找不到任何可用的 python.exe" in proc.stdout, proc.stdout
    assert proc.returncode == 1


def test_a_chinese_display_name_reaches_the_operator_without_entering_the_bat(
        stub_toolchain, tmp_path):
    """A Chinese display name must reach the user — and must NOT be a byte in a .bat.

    (This test used to assert the opposite: 「產線 檢視器」 in start.bat. That is the
    defect. cmd.exe seeks through a .bat by byte offset while counting characters, so
    a name in the file is a landmine that goes off about 1 run in 20 — the bat is
    re-read after every for/f, pipe, goto and external command, lands mid-line, and
    executes whatever is there. The name lives in messages\\*.txt now, which cmd
    `type`s and never parses. The original point of the test still stands and is
    still checked: writing it must not blow the build up.)
    """
    root = tmp_path / "ROOT"
    request = make_project(tmp_path, "產線 檢視器", app_id="line-viewer")
    result = store_builder.build_into_store(request, root, version="v1.0.0")

    assert result.ok, result.errors
    # 「tools\ 與說明檔沒有全部寫成功」 is what a Chinese name used to cause (an
    # ascii-only .bat + a UnicodeEncodeError). Match the failure, not the word
    # "tools" — the WebView2 warning legitimately names tools\安裝WebView2.bat.
    assert not any("沒有全部寫成功" in w for w in result.warnings), result.warnings

    app = request.app_id
    assert "產線 檢視器" in message(root, f"title-{app}.txt")
    assert "產線 檢視器" in message(root, f"starting-{app}.txt")
    assert "產線 檢視器" in message(root, f"admin-menu-{app}.txt")

    admin = (root / "tools" / f"admin-{app}.bat").read_text("utf-8")
    start = (root / "start.bat").read_text("utf-8")
    for bat in (admin, start, (root / "tools" / "gc.bat").read_text("utf-8")):
        assert bat.isascii()
        assert "產線" not in bat
    assert "chcp 65001 >nul 2>&1" in admin.splitlines()[:8]   # codepage BEFORE any type
    assert 'set "PYTHONUTF8=1"' in admin
    # and each bat really does print it: title from a file, body from a file
    assert f'set /p TITLE=<"messages\\title-{app}.txt"' in start
    assert f'type "messages\\starting-{app}.txt"' in start
    assert f'type "messages\\admin-menu-{app}.txt"' in admin


def test_start_bat_checks_webview2_before_starting_anything(build_request, stub_toolchain,
                                                            tmp_path):
    root = tmp_path / "ROOT"
    store_builder.build_into_store(build_request, root, version="v1.0.0")
    start = (root / "start.bat").read_text("utf-8")
    assert store_builder.WEBVIEW2_CLIENT in start
    assert "HKLM\\SOFTWARE\\WOW6432Node\\Microsoft\\EdgeUpdate\\Clients" in start
    assert "HKCU\\SOFTWARE\\Microsoft\\EdgeUpdate\\Clients" in start
    # the check must come BEFORE we hand over to bootstrap.py
    assert start.index("reg query") < start.index("bootstrap.py")
    # what the user is told to do about it is Chinese, so it is a message, not code
    assert 'type "messages\\start-webview2.txt"' in start
    assert store_builder.WEBVIEW2_BAT_NAME in message(root, "start-webview2.txt")

    installer = (root / "tools" / store_builder.WEBVIEW2_BAT_NAME).read_text("utf-8")
    assert "MicrosoftEdgeWebView2RuntimeInstallerX64.exe" in installer
    assert "/silent /install" in installer
    assert store_builder.WEBVIEW2_DOWNLOAD in message(root, "webview2-none.txt")


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


def offline_installer(tmp_path: Path, name: str | None = None) -> Path:
    """A file big enough to BE the Evergreen Standalone Installer (~130 MB in the
    field; over 10 MB is enough to prove we do not mistake it for a bootstrapper)."""
    source = tmp_path / "downloads" / (name or store_builder.builder.WEBVIEW2_INSTALLER_NAME)
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"MZ" + b"\0" * (11 * 1024 * 1024))
    return source


def test_build_bundles_the_webview2_installer_into_prereq(build_request, stub_toolchain,
                                                          tmp_path):
    """A/S4. NOTHING in this codebase ever created prereq\\ — the exporter copied it
    if it happened to exist, and it never did. So the one dependency the delivery
    cannot install itself was never actually shipped, and 安裝WebView2.bat printed a
    download URL at a factory machine with no internet."""
    source = offline_installer(tmp_path)
    build_request.webview2_installer = source

    root = tmp_path / "ROOT"
    result = store_builder.build_into_store(build_request, root, version="v1.0.0")
    assert result.ok, result.errors

    bundled = root / store_builder.WEBVIEW2_INSTALLER      # prereq/<standalone name>
    assert bundled.is_file() and bundled.read_bytes() == source.read_bytes()
    assert store_builder.WEBVIEW2_INSTALLER.replace("/", "\\") in \
        (root / "tools" / store_builder.WEBVIEW2_BAT_NAME).read_text("utf-8")
    assert not [w for w in result.warnings if "WebView2" in w], result.warnings

    out = tmp_path / "deliver"
    export = store_builder.export_full_tree(root, out)
    assert (out / store_builder.WEBVIEW2_INSTALLER).is_file()
    assert not any("WebView2" in w for w in export.warnings), export.warnings


def test_the_admins_chosen_installer_is_never_renamed(build_request, stub_toolchain,
                                                      tmp_path):
    """BLOCKER. The store copied the admin's file to a hard-coded 「canonical」 name:
    MicrosoftEdgeWebview2Setup.exe — the ~2 MB Evergreen BOOTSTRAPPER, which contains
    no WebView2 and downloads it at install time. An operator who correctly fetched
    the 130 MB Standalone Installer had it silently relabelled as the one file that
    CANNOT install on an air-gapped machine, which is the only machine prereq\\ exists
    for. Nothing was ever gained by renaming: 安裝WebView2.bat runs any .exe there."""
    source = offline_installer(tmp_path, "WebView2 離線版 (IT提供).exe")
    build_request.webview2_installer = source

    root = tmp_path / "ROOT"
    assert store_builder.build_into_store(build_request, root, version="v1.0.0").ok

    shipped = sorted(p.name for p in (root / "prereq").glob("*.exe"))
    assert shipped == ["WebView2 離線版 (IT提供).exe"], shipped
    assert not (root / "prereq" / "MicrosoftEdgeWebview2Setup.exe").exists()

    # and the delivery carries THAT file, under THAT name, and does not complain
    export = store_builder.export_full_tree(root, tmp_path / "deliver")
    assert (Path(export.out_dir) / "prereq" / "WebView2 離線版 (IT提供).exe").is_file()
    assert not [w for w in export.warnings if "WebView2" in w], export.warnings


def test_a_two_megabyte_bootstrapper_is_called_out_at_build_time(build_request,
                                                                 stub_toolchain, tmp_path):
    """The bootstrapper cannot install anything without a network. Say so on the build
    machine, where the right file is a 30-second download — not on the factory floor,
    where the version directory is immutable and there is nothing left to do."""
    boot = tmp_path / "MicrosoftEdgeWebview2Setup.exe"
    boot.write_bytes(b"MZ" + b"\0" * (2 * 1024 * 1024))        # the real one is ~2 MB
    build_request.webview2_installer = boot

    root = tmp_path / "ROOT"
    result = store_builder.build_into_store(build_request, root, version="v1.0.0")
    assert result.ok, result.errors                            # a warning, not a wall

    warned = [w for w in result.warnings if "bootstrap" in w.lower()]
    assert warned, result.warnings
    assert "需要連網" in warned[0]
    assert store_builder.builder.WEBVIEW2_INSTALLER_NAME in warned[0]   # get THIS instead
    warned[0].encode("cp950")
    assert (root / "prereq" / boot.name).is_file()             # copied anyway, own name


def test_the_whole_offline_webview2_chain_points_at_the_standalone_installer(
        build_request, stub_toolchain, tmp_path):
    """BLOCKER, and the one that destroys the product's reason for existing. Every
    message, URL and constant used to name the Evergreen Bootstrapper
    (MicrosoftEdgeWebview2Setup.exe, ~2 MB, LinkId=2124703) — a DOWNLOADER. An
    air-gapped factory PC that followed our instructions to the letter still could not
    install WebView2, and still could not open a window."""
    assert store_builder.WEBVIEW2_INSTALLER == \
        "prereq/MicrosoftEdgeWebView2RuntimeInstallerX64.exe"
    assert store_builder.WEBVIEW2_DOWNLOAD == "https://go.microsoft.com/fwlink/?LinkId=2124701"

    root = tmp_path / "ROOT"
    result = store_builder.build_into_store(build_request, root, version="v1.0.0")
    assert result.ok, result.errors

    # every operator-facing sentence about WebView2, in the tree and in the result
    texts = {name: message(root, name)
             for name in ("webview2-none.txt", "start-webview2.txt",
                          "webview2-bootstrapper.txt")}
    texts["讀我"] = (root / store_builder.README_NAME).read_text("utf-8")
    texts["warning"] = [w for w in result.warnings if "WebView2" in w][0]
    texts["export"] = [w for w in store_builder.export_full_tree(
        root, tmp_path / "deliver").warnings if "WebView2" in w][0]
    for where, body in texts.items():
        assert "2124703" not in body, where                    # the bootstrapper link
        assert store_builder.builder.WEBVIEW2_INSTALLER_NAME in body, where
        assert "需要連網" in body, where       # WHY the 2 MB file cannot be used offline
        body.encode("cp950")

    # the helper bat diagnoses it too: a failed install + a sub-10 MB file in prereq\
    bat = (root / "tools" / store_builder.WEBVIEW2_BAT_NAME).read_text("ascii")
    assert 'for %%A in ("%WV2SETUP%") do set "SZ=%%~zA"' in bat
    assert f"if %SZ% LSS {store_builder.WEBVIEW2_MIN_OFFLINE_BYTES} " \
           'type "messages\\webview2-bootstrapper.txt"' in bat
    # the size is read BEFORE the install runs: %SZ% inside the failure branch is
    # expanded when that block is parsed, not when it is reached
    assert bat.index('set "SZ=') < bat.index('"%WV2SETUP%" /silent /install')


def test_a_second_runtime_names_the_pins_that_differ_instead_of_silently_costing_450mb(
        stub_toolchain, tmp_path):
    """S8. compute_fingerprint() hashes the ENTIRE pin set, so two apps whose locks
    differ by ONE unrelated pin get two ~450 MB runtimes and share nothing — which is
    the only reason anyone chose the store layout. It happened silently: the operator
    saw 「runtime 新建」, had no idea it was avoidable, and the second 450 MB sat on the
    factory PC forever. Name the pins, and the fix is a one-line lock edit."""
    root = tmp_path / "ROOT"
    alpha = make_project(tmp_path, "Alpha Viewer", lock="streamlit==1.40.0\npandas==2.2.0\n")
    beta = make_project(tmp_path, "Beta Viewer", lock="streamlit==1.40.0\npandas==2.2.1\n")
    first = store_builder.build_into_store(alpha, root, version="v1.0.0")
    assert first.ok and not first.runtime_reused
    # the FIRST runtime in a tree has nothing to share with: no divergence warning
    assert not [w for w in first.warnings if "450 MB" in w], first.warnings

    second = store_builder.build_into_store(beta, root, version="v1.0.0")
    assert second.ok, second.errors
    assert second.runtime_reused is False                      # the fact being warned about
    assert second.fingerprint != first.fingerprint

    warned = [w for w in second.warnings if first.fingerprint in w]
    assert warned, second.warnings
    assert "pandas:這次是 2.2.1,那一份是 2.2.0" in warned[0]   # the exact pin, both sides
    assert "streamlit" not in warned[0]                        # the pins that AGREE are noise
    assert "450 MB" in warned[0]
    warned[0].encode("cp950")

    # a lock that MATCHES gets the sharing, and no warning about it
    gamma = make_project(tmp_path, "Gamma Viewer", lock="streamlit==1.40.0\npandas==2.2.0\n")
    third = store_builder.build_into_store(gamma, root, version="v1.0.0")
    assert third.ok and third.runtime_reused
    assert third.fingerprint == first.fingerprint
    assert not [w for w in third.warnings if "450 MB" in w], third.warnings


def test_a_pin_the_app_never_imports_is_named_as_the_450mb_you_are_paying_for_nothing(
        stub_toolchain, tmp_path):
    """S8. Naming the pins is not enough on its own: 「pandas 2.2.0 vs 2.2.1」 leaves the
    operator to work out whether they DARE align them, and the safe answer to a question
    you cannot answer is always 「leave it」 — so the second 450 MB runtime lands anyway.

    We can answer it. The app's own imports are right there, and the import gate already
    resolves an import name to the distributions that provide it. When every differing
    pin is one this app never imports, that is a 450 MB runtime bought to satisfy a
    version number nothing in the code asks for, and it can be said in one sentence.
    """
    root = tmp_path / "ROOT"
    # Neither app imports pandas — it is somebody's transitive dependency, pinned by
    # `pip freeze`, and it is the ONLY thing keeping these two apps apart.
    alpha = make_project(tmp_path, "Alpha Viewer", lock="streamlit==1.40.0\npandas==2.2.0\n")
    beta = make_project(tmp_path, "Beta Viewer", lock="streamlit==1.40.0\npandas==2.2.1\n")
    assert store_builder.build_into_store(alpha, root, version="v1.0.0").ok

    second = store_builder.build_into_store(beta, root, version="v1.0.0")
    assert second.ok and second.runtime_reused is False
    said = [w for w in second.warnings if "450 MB" in w][0]
    assert "pandas" in said
    assert "沒有 import 過" in said                      # the fact that unlocks the fix
    assert "可以共用同一份 runtime" in said               # ...and what it is worth
    assert "pip freeze" in said                          # ...and the honest caveat
    assert "streamlit" not in said                       # the pins that AGREE are noise
    said.encode("cp950")

    # And the other direction: a differing pin the app REALLY imports must never be
    # described as one it does not. A wrong 「你沒用到它」 is how a build gets aligned
    # onto a pandas its own code cannot run against.
    gamma = make_project(tmp_path, "Gamma Viewer", lock="streamlit==1.40.0\npandas==2.2.9\n")
    gamma.entrypoint.write_text("import streamlit as st\nimport pandas as pd\n",
                                encoding="utf-8")
    third = store_builder.build_into_store(gamma, root, version="v1.0.0")
    assert third.ok
    told = [w for w in third.warnings if "450 MB" in w][0]
    assert "pandas" in told
    assert "沒有 import 過" not in told
    assert "真的會 import" in told and "確認相容" in told
    told.encode("cp950")


def test_a_store_build_with_no_webview2_installer_says_so_at_build_time(build_request,
                                                                        stub_toolchain,
                                                                        tmp_path):
    """The fat path has warned about this since the beginning (builder's
    WEBVIEW2_MISSING_WARNING). The store path declared a `warnings` field and then put
    NOTHING in it — same offline factory machine, same blank window, same dead end,
    and the store operator was the only one not told.

    And the remedy has to be one that WORKS. 「請在建置時指定」 means "rebuild", and a
    completed version directory is immutable, so the operator who follows that advice
    is refused by _build_version_dir and has nowhere left to go. They do not need a
    rebuild: 安裝WebView2.bat takes any .exe in prereq\\.
    """
    root = tmp_path / "ROOT"
    result = store_builder.build_into_store(build_request, root, version="v1.0.0")
    assert result.ok, result.errors

    warning = [w for w in result.warnings if "WebView2" in w]
    assert warning, result.warnings
    assert "prereq" in warning[0]
    assert "不必重建" in warning[0]                       # the remedy that works
    assert "請在建置時指定" not in warning[0]              # the remedy that is refused
    assert store_builder.WEBVIEW2_DOWNLOAD in warning[0]
    warning[0].encode("cp950")                            # a zh-TW console must print it

    # ...and the operator can act on it WITHOUT rebuilding: drop the .exe in prereq\
    # under any name, and both the warning and the delivery's warning stop.
    (root / "prereq").mkdir(parents=True, exist_ok=True)
    (root / "prereq" / "webview2 (offline).exe").write_bytes(b"MZ setup")
    again = store_builder.build_into_store(build_request, root, version="v1.1.0")
    assert again.ok, again.errors
    assert not [w for w in again.warnings if "WebView2" in w], again.warnings

    export = store_builder.export_full_tree(root, tmp_path / "deliver", version="v1.1.0")
    assert (Path(export.out_dir) / "prereq" / "webview2 (offline).exe").is_file()
    assert not [w for w in export.warnings if "WebView2" in w], export.warnings


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
    # the remedy names THIS folder and does not ask for a rebuild (which the
    # immutable-version rule then refuses)
    assert str(Path(export.out_dir) / "prereq") in warning[0]
    assert "不必重新建置" in warning[0]
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
