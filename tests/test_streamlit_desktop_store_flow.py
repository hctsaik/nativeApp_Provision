"""Phases 2–5: runtime store, update staging, bootstrap promote/rollback, GC,
and the store-layout builder. Every scenario runs on a real (tmp) tree — only
pip and the launcher process are stubbed."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from provision_builder.streamlit_desktop import imports as imports_mod
from provision_builder.streamlit_desktop import runtime as runtime_mod
from provision_builder.streamlit_desktop import store_builder
from provision_builder.streamlit_desktop.device import (
    bootstrap,
    gc as gc_mod,
    integrity,
    leases,
    paths as paths_mod,
    runtime_store,
    state as state_mod,
    updater,
)
from provision_builder.streamlit_desktop.device.provider import (
    FolderUpdateProvider,
    ProviderError,
    ReleaseMetadata,
)
from provision_builder.streamlit_desktop.models import BuildRequest

FP1 = "cp311-aaaaaaaaaaaa"
FP2 = "cp311-bbbbbbbbbbbb"
APP = "demo"


# ── tree fixtures ────────────────────────────────────────────────────────────

def make_runtime(root: Path, fingerprint: str, *, complete: bool = True) -> Path:
    rdir = root / "deps" / "runtimes" / fingerprint
    (rdir / "Lib").mkdir(parents=True)
    (rdir / "python.exe").write_bytes(b"MZ fake")
    (rdir / "Lib" / "os.py").write_text("# stdlib", encoding="utf-8")
    (rdir / runtime_store.RUNTIME_META).write_text(
        json.dumps({"schema": 1, "fingerprint": fingerprint}), encoding="utf-8")
    integrity.write_files_json(rdir, integrity.build_files_json(
        rdir, extra_excluded={runtime_store.RUNTIME_META}))
    if complete:
        integrity.write_complete(rdir)
    return rdir


def make_version(root: Path, version: str, fingerprint: str, *,
                 app: str = APP, complete: bool = True, body: str = "x") -> Path:
    vdir = root / "apps" / app / "versions" / version
    (vdir / "application").mkdir(parents=True)
    (vdir / "application" / "app.py").write_text(f"# {body}", encoding="utf-8")
    (vdir / "launcher").mkdir()
    (vdir / "launcher" / "launch.py").write_text("# fake launcher", encoding="utf-8")
    (vdir / "app-package.json").write_text(json.dumps({
        "schema_version": 2, "app_id": app, "display_name": "Demo",
        "version": version, "entrypoint": "application/app.py",
        "runtime_fingerprint": fingerprint,
        "engine_shim": "launcher/engine_shim.py",
        "shell_executable": "shell/cim-light.exe",
    }), encoding="utf-8")
    integrity.write_files_json(vdir)
    if complete:
        integrity.write_complete(vdir)
    return vdir


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    root = tmp_path / "ROOT"
    make_runtime(root, FP1)
    make_version(root, "v1", FP1)
    state_mod.StateStore(root / "apps" / APP / "state").initialize(APP, "v1")
    return root


def app_paths(root: Path) -> paths_mod.AppPaths:
    return paths_mod.AppPaths(root, APP)


def store_of(root: Path) -> state_mod.StateStore:
    return state_mod.StateStore(root / "apps" / APP / "state")


def rstore_of(root: Path) -> runtime_store.RuntimeStore:
    return runtime_store.RuntimeStore(root / "deps")


# ── runtime store ────────────────────────────────────────────────────────────

def test_lock_normalization_is_canonical():
    pins = runtime_store.normalize_lock("Streamlit==1.40.0\npandas==2.2.0\n# c\n")
    assert pins == ["pandas==2.2.0", "streamlit==1.40.0"]


def test_packaging_plumbing_is_ignored_not_rejected():
    """`pip freeze --all` on a python-build-standalone runtime emits pip as a
    local file URL. That is not an app dependency — and rejecting it would block
    every lock file produced the obvious way."""
    pins = runtime_store.normalize_lock(
        "pip @ file:///D:/a/python-build-standalone/build/pip-24.1.2-py3-none-any.whl\n"
        "setuptools==69.0.0\n"
        "streamlit==1.40.0\n")
    assert pins == ["streamlit==1.40.0"]


@pytest.mark.parametrize("bad", [
    "streamlit>=1.0",                      # not pinned
    "-e .",                                # editable
    "-r other.txt",                        # nested requirements
    "pkg @ https://x/y.whl",               # URL
    "pkg==1 ; python_version<'3.12'",      # unfrozen marker
    "",                                    # empty
    "a==1\nA==2",                          # conflicting duplicates
])
def test_non_lock_requirements_are_rejected(bad):
    with pytest.raises(runtime_store.LockfileError):
        runtime_store.normalize_lock(bad)


def test_fingerprint_depends_on_pins_and_python():
    base = dict(python_version="3.11.9", platform="win_amd64", abi="cp311",
                pins=["a==1", "b==2"])
    fp = runtime_store.compute_fingerprint(**base)
    assert fp == runtime_store.compute_fingerprint(**{**base, "pins": ["b==2", "a==1"]})
    assert fp != runtime_store.compute_fingerprint(**{**base, "pins": ["a==1", "b==3"]})
    assert fp != runtime_store.compute_fingerprint(**{**base, "python_version": "3.11.10"})
    assert fp.startswith("cp311-")


def test_first_use_verifies_deeply_then_writes_sentinel(tree):
    rdir = make_runtime(tree, FP2, complete=False)
    rstore = rstore_of(tree)
    assert not rstore.is_complete(FP2)
    rstore.ensure_verified(FP2)
    assert integrity.is_complete(rdir)


def test_half_copied_runtime_fails_closed(tree):
    rdir = make_runtime(tree, FP2, complete=False)
    (rdir / "Lib" / "os.py").unlink()          # the USB was yanked mid-copy
    with pytest.raises(runtime_store.RuntimeStoreError, match="驗證失敗"):
        rstore_of(tree).ensure_verified(FP2)
    assert not integrity.is_complete(rdir)     # still invisible


def test_fingerprint_identity_mismatch_is_fatal(tree):
    rdir = make_runtime(tree, FP2)
    (rdir / runtime_store.RUNTIME_META).write_text(
        json.dumps({"fingerprint": "cp311-other"}), encoding="utf-8")
    with pytest.raises(runtime_store.RuntimeStoreError, match="指紋不一致"):
        rstore_of(tree).quick_check(FP2)


# ── update staging (spec §9) ─────────────────────────────────────────────────

def make_update_source(tmp_path: Path, version: str, fingerprint: str, *,
                       revision: str = "r1", with_runtime: bool = True) -> Path:
    source = tmp_path / "usb"
    app_root = source / APP
    make_version(source_root_hack(app_root), version, fingerprint, complete=False)
    if with_runtime and fingerprint:
        make_runtime(source_root_hack(app_root), fingerprint, complete=False)
    (app_root / "release.json").write_text(json.dumps({
        "schema": 1, "app_id": APP, "version": version,
        "revision": revision, "runtime_fingerprint": fingerprint,
    }), encoding="utf-8")
    return source


def source_root_hack(app_root: Path) -> Path:
    """make_version/make_runtime build ROOT-shaped trees; the update source uses
    <src>/<app>/versions/... directly, so give them a fake root that maps there."""
    class _Fake:
        def __truediv__(self, part):
            if part == "apps":
                return _Apps()
            if part == "deps":
                return _Deps()
            raise AssertionError(part)

    class _Apps:
        def __truediv__(self, _app):
            return _Sub()

    class _Sub:
        def __truediv__(self, part):
            assert part == "versions"
            return app_root / "versions"

    class _Deps:
        def __truediv__(self, part):
            assert part == "runtimes"
            return app_root / "runtimes"

    return _Fake()


def test_update_with_same_fingerprint_stages_only_the_version(tree, tmp_path):
    source = make_update_source(tmp_path, "v2", FP1, with_runtime=False)
    outcome = updater.check_once(app_paths(tree), store_of(tree), rstore_of(tree),
                                 FolderUpdateProvider(source), notify=lambda *a: None)
    assert outcome == "staged"
    assert integrity.is_complete(tree / "apps" / APP / "versions" / "v2")
    assert store_of(tree).load().pending == "v2"
    runtimes = {p.name for p in (tree / "deps" / "runtimes").iterdir() if p.is_dir()}
    assert runtimes == {FP1}                   # nothing new downloaded


def test_update_with_new_fingerprint_stages_runtime_too(tree, tmp_path):
    source = make_update_source(tmp_path, "v2", FP2)
    outcome = updater.check_once(app_paths(tree), store_of(tree), rstore_of(tree),
                                 FolderUpdateProvider(source), notify=lambda *a: None)
    assert outcome == "staged"
    assert rstore_of(tree).is_complete(FP2)


def test_corrupt_update_leaves_no_pending_and_no_debris(tree, tmp_path):
    source = make_update_source(tmp_path, "v2", FP1, with_runtime=False)
    (source / APP / "versions" / "v2" / "application" / "app.py").write_text(
        "# TAMPERED", encoding="utf-8")
    with pytest.raises(updater.UpdateError, match="驗證失敗"):
        updater.check_once(app_paths(tree), store_of(tree), rstore_of(tree),
                           FolderUpdateProvider(source), notify=lambda *a: None)
    state = store_of(tree).load()
    assert state.pending is None
    assert not any((tree / "apps" / APP / "staging").iterdir())


def test_failed_version_is_not_restaged_until_revision_changes(tree, tmp_path):
    store_of(tree).mutate(lambda s: state_mod.clear_bad_pending(
        state_mod.set_pending(s, "v2"), revision="r1"))
    source = make_update_source(tmp_path, "v2", FP1, with_runtime=False, revision="r1")
    provider = FolderUpdateProvider(source)
    assert updater.check_once(app_paths(tree), store_of(tree), rstore_of(tree),
                              provider, notify=lambda *a: None) == "skipped-failed"

    (source / APP / "release.json").write_text(json.dumps({
        "schema": 1, "app_id": APP, "version": "v2", "revision": "r2",
        "runtime_fingerprint": FP1}), encoding="utf-8")
    assert updater.check_once(app_paths(tree), store_of(tree), rstore_of(tree),
                              provider, notify=lambda *a: None) == "staged"


def test_release_for_another_app_is_rejected(tmp_path):
    source = tmp_path / "usb"
    (source / "other").mkdir(parents=True)
    (source / "other" / "release.json").write_text(json.dumps({
        "app_id": APP, "version": "v9", "revision": "r",
        "runtime_fingerprint": FP1}), encoding="utf-8")
    with pytest.raises(ProviderError, match="別的 app"):
        FolderUpdateProvider(source).get_latest_release("other", "v1")


def test_notification_happens_only_after_pending_is_written(tree, tmp_path):
    events = []
    source = make_update_source(tmp_path, "v2", FP1, with_runtime=False)

    def notify(title, message):
        events.append(store_of(tree).load().pending)

    updater.check_once(app_paths(tree), store_of(tree), rstore_of(tree),
                       FolderUpdateProvider(source), notify=notify)
    assert events == ["v2"]                    # pending already durable when told


# ── bootstrap: promote / health / rollback ───────────────────────────────────

class FakeLauncher:
    """Stands in for the spawned launch.py process."""

    def __init__(self, env, *, healthy: bool, exit_code: int = 0, polls: int = 2):
        self.pid = 4242
        self.returncode = None
        self._exit_code = exit_code
        self._polls_left = polls
        if healthy:
            marker = Path(env["CIM_HEALTHY_MARKER"])
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text("http://127.0.0.1:9999", encoding="utf-8")

    def poll(self):
        if self._polls_left > 0:
            self._polls_left -= 1
            return None
        self.returncode = self._exit_code
        return self.returncode


def popen_factory(script: list[dict]):
    """Each spawn consumes the next behaviour from `script`; records the cmd."""
    calls: list[list[str]] = []

    def popen(cmd, cwd=None, env=None):
        calls.append([str(c) for c in cmd])
        spec = script.pop(0)
        return FakeLauncher(env, **spec)

    popen.calls = calls
    return popen


def run_bootstrap(tree, script, monkeypatch):
    monkeypatch.setattr(bootstrap.time, "sleep", lambda _s: None)
    popen = popen_factory(script)
    code = bootstrap.start_app(app_paths(tree), [], notify=lambda *a: None, popen=popen)
    return code, popen.calls


def test_plain_start_runs_current_under_its_runtime(tree, monkeypatch):
    code, calls = run_bootstrap(tree, [dict(healthy=True)], monkeypatch)
    assert code == 0 and len(calls) == 1
    assert calls[0][0].endswith(f"{FP1}\\python.exe")
    assert "v1" in calls[0][1] and calls[0][1].endswith("launch.py")


def test_pending_is_promoted_before_launch(tree, monkeypatch):
    make_version(tree, "v2", FP1, body="two")
    store_of(tree).mutate(lambda s: state_mod.set_pending(s, "v2"))

    code, calls = run_bootstrap(tree, [dict(healthy=True)], monkeypatch)
    assert code == 0
    assert "v2" in calls[0][1]                 # launched the NEW version
    final = store_of(tree).load()
    assert (final.current, final.previous, final.pending) == ("v2", "v1", None)
    assert final.candidate is None and final.last_known_good == "v2"  # health committed


def test_broken_pending_is_quarantined_and_current_still_runs(tree, monkeypatch):
    vdir = make_version(tree, "v2", FP1, complete=False)   # no sentinel = half copy
    store_of(tree).mutate(lambda s: state_mod.set_pending(s, "v2"))

    code, calls = run_bootstrap(tree, [dict(healthy=True)], monkeypatch)
    assert code == 0
    assert "v1" in calls[0][1]                 # old version kept running
    final = store_of(tree).load()
    assert final.current == "v1" and final.pending is None
    assert final.is_failed("v2")


def test_candidate_that_never_gets_healthy_rolls_back_and_relaunches(tree, monkeypatch):
    make_version(tree, "v2", FP1, body="two")
    store_of(tree).mutate(lambda s: state_mod.set_pending(s, "v2"))
    # also give v1 LKG status so rollback has a proven target
    store_of(tree).mutate(lambda s: state_mod.commit_candidate(s))

    code, calls = run_bootstrap(
        tree, [dict(healthy=False, exit_code=1, polls=1), dict(healthy=True)], monkeypatch)
    assert code == 0
    assert "v2" in calls[0][1] and "v1" in calls[1][1]   # v2 tried, v1 relaunched
    final = store_of(tree).load()
    assert final.current == "v1" and final.is_failed("v2")


def test_stable_version_failure_does_not_roll_back(tree, monkeypatch):
    store_of(tree).mutate(state_mod.commit_candidate)     # v1 is LKG, no candidate
    code, calls = run_bootstrap(tree, [dict(healthy=False, exit_code=1, polls=1)],
                                monkeypatch)
    assert code == 1 and len(calls) == 1                  # fail loud, no relaunch
    assert store_of(tree).load().current == "v1"


def test_launcher_gets_data_dir_outside_the_version(tree, monkeypatch):
    monkeypatch.setattr(bootstrap.time, "sleep", lambda _s: None)
    seen = {}

    def popen(cmd, cwd=None, env=None):
        seen["env"] = env
        return FakeLauncher(env, healthy=True)

    bootstrap.start_app(app_paths(tree), [], notify=lambda *a: None, popen=popen)
    data = Path(seen["env"]["CIM_APP_DATA"])
    assert data == tree / "apps" / APP / "data"
    assert "versions" not in data.parts


# ── GC ───────────────────────────────────────────────────────────────────────

def test_gc_keeps_all_slots_and_leased_items(tree):
    make_version(tree, "v0", FP1)              # orphan version
    make_runtime(tree, FP2)                    # orphan runtime
    make_version(tree, "v3", FP1)              # will be leased
    lease = leases.create_lease(tree / "apps" / APP / "data" / "leases",
                                app_id=APP, version="v3", runtime_fingerprint=FP2)
    try:
        plan = gc_mod.run_gc(tree, apply=False, log=lambda *_a: None)
        doomed_versions = {v for _a, v, _p in plan.delete_versions}
        doomed_runtimes = {fp for fp, _p in plan.delete_runtimes}
        assert doomed_versions == {"v0"}       # v1=current, v3=leased
        assert doomed_runtimes == set()        # FP1 referenced, FP2 leased
    finally:
        lease.release()

    plan = gc_mod.run_gc(tree, apply=True, log=lambda *_a: None)
    assert {fp for fp, _p in plan.delete_runtimes} == {FP2}
    assert not (tree / "deps" / "runtimes" / FP2).exists()
    assert not (tree / "apps" / APP / "versions" / "v0").exists()
    assert (tree / "apps" / APP / "versions" / "v1").exists()


def test_gc_dry_run_deletes_nothing(tree):
    make_runtime(tree, FP2)
    gc_mod.run_gc(tree, apply=False, log=lambda *_a: None)
    assert (tree / "deps" / "runtimes" / FP2).exists()


# ── store builder ────────────────────────────────────────────────────────────

def make_store_project(tmp_path: Path, name: str = "Demo App") -> BuildRequest:
    """A buildable project + the shared (fake) shell and runtime template."""
    project = tmp_path / f"proj-{store_builder.slugify(name)}"
    project.mkdir(exist_ok=True)
    (project / "app.py").write_text("import streamlit as st\nst.write('READY')\n",
                                    encoding="utf-8")
    (project / "requirements.txt").write_text("streamlit==1.40.0\n", encoding="utf-8")
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
    return make_store_project(tmp_path)


@pytest.fixture
def stub_toolchain(monkeypatch):
    """No real pip / no real interpreter probing inside unit tests.

    The import probe answers "nothing is missing" — but it answers with a real
    MissingReport, because that is what the gate consumes.
    """
    monkeypatch.setattr(store_builder, "_python_version_of", lambda _p: "3.11.9")
    monkeypatch.setattr(store_builder, "_freeze",
                        lambda _p: ["streamlit==1.40.0", "setuptools==69.0"])
    monkeypatch.setattr(runtime_mod, "install_requirements",
                        lambda *_a, **_k: None)
    monkeypatch.setattr(runtime_mod, "verify_imports", lambda *_a, **_k: None)
    monkeypatch.setattr(store_builder.imports_mod, "missing_dependencies",
                        lambda *_a, **_k: imports_mod.MissingReport())


def test_shell_lives_in_the_store_not_in_every_version(build_request, stub_toolchain, tmp_path):
    """The shell is byte-identical in every version and was 60% of a CV_Viewer
    slot (16.6 MB of 28 MB). It belongs in deps/, like the runtime."""
    root = tmp_path / "ROOT"
    first = store_builder.build_into_store(build_request, root, version="v1.0.0")
    second = store_builder.build_into_store(build_request, root, version="v1.1.0")
    assert first.ok and second.ok

    slots = root / "apps" / build_request.app_id / "versions"
    for version in ("v1.0.0", "v1.1.0"):
        assert not (slots / version / "shell").exists()      # not copied per version
        manifest = json.loads((slots / version / "app-package.json").read_text("utf-8"))
        assert manifest["shell_fingerprint"].startswith("shell-")
        assert manifest["shell_name"] == "cim-light.exe"

    shells = [p for p in (root / "deps" / "shells").iterdir() if p.is_dir()]
    assert len(shells) == 1                                   # ONE shell, two versions
    assert (shells[0] / "cim-light.exe").is_file()
    assert integrity.is_complete(shells[0])


def test_bootstrap_hands_the_shared_shell_to_the_launcher(tree, monkeypatch):
    """The shell is outside the version dir, so the launcher cannot resolve it
    itself — bootstrap must validate it and pass the path."""
    shell_dir = tree / "deps" / "shells" / "shell-abc123"
    shell_dir.mkdir(parents=True)
    (shell_dir / "cim-light.exe").write_bytes(b"MZ")
    integrity.write_files_json(shell_dir)
    integrity.write_complete(shell_dir)

    vdir = tree / "apps" / APP / "versions" / "v1"
    manifest = json.loads((vdir / "app-package.json").read_text("utf-8"))
    manifest["shell_fingerprint"] = "shell-abc123"
    manifest["shell_name"] = "cim-light.exe"
    manifest.pop("shell_executable")
    (vdir / "app-package.json").write_text(json.dumps(manifest), encoding="utf-8")
    integrity.write_files_json(vdir)
    integrity.write_complete(vdir)

    monkeypatch.setattr(bootstrap.time, "sleep", lambda _s: None)
    seen = {}

    def popen(cmd, cwd=None, env=None):
        seen["env"] = env
        return FakeLauncher(env, healthy=True)

    code = bootstrap.start_app(app_paths(tree), [], notify=lambda *a: None, popen=popen)
    assert code == 0
    assert Path(seen["env"]["CIM_SHELL_EXE"]) == shell_dir / "cim-light.exe"


def test_missing_shared_shell_fails_loudly(tree, monkeypatch):
    """A half-copied delivery (version came, shell did not) must say exactly what
    is missing — not open a window onto nothing."""
    store_of(tree).mutate(state_mod.commit_candidate)   # v1 is proven, not a candidate
    vdir = tree / "apps" / APP / "versions" / "v1"
    manifest = json.loads((vdir / "app-package.json").read_text("utf-8"))
    manifest["shell_fingerprint"] = "shell-gone"
    (vdir / "app-package.json").write_text(json.dumps(manifest), encoding="utf-8")
    integrity.write_files_json(vdir)
    integrity.write_complete(vdir)

    monkeypatch.setattr(bootstrap.time, "sleep", lambda _s: None)
    with pytest.raises(runtime_store.RuntimeStoreError, match="缺共用 Tauri 殼"):
        bootstrap.start_app(app_paths(tree), [], notify=lambda *a: None,
                            popen=lambda *a, **k: FakeLauncher({}, healthy=True))


def test_gc_keeps_the_shell_a_version_still_needs(tree):
    for fingerprint in ("shell-keepme", "shell-orphan"):
        path = tree / "deps" / "shells" / fingerprint
        path.mkdir(parents=True)
        (path / "cim-light.exe").write_bytes(b"MZ")
        integrity.write_files_json(path)
        integrity.write_complete(path)

    vdir = tree / "apps" / APP / "versions" / "v1"
    manifest = json.loads((vdir / "app-package.json").read_text("utf-8"))
    manifest["shell_fingerprint"] = "shell-keepme"
    (vdir / "app-package.json").write_text(json.dumps(manifest), encoding="utf-8")
    integrity.write_files_json(vdir)
    integrity.write_complete(vdir)

    plan = gc_mod.run_gc(tree, apply=True, log=lambda *_a: None)
    assert {fp for fp, _p in plan.delete_shells} == {"shell-orphan"}
    assert (tree / "deps" / "shells" / "shell-keepme").exists()


def test_store_build_creates_the_whole_tree(build_request, stub_toolchain, tmp_path):
    root = tmp_path / "ROOT"
    result = store_builder.build_into_store(build_request, root, version="v1.0.0")
    assert result.ok, result.errors
    fp = result.fingerprint

    assert (root / "deps" / "runtimes" / fp / "python.exe").is_file()
    assert integrity.is_complete(root / "deps" / "runtimes" / fp)
    vdir = root / "apps" / build_request.app_id / "versions" / "v1.0.0"
    assert integrity.is_complete(vdir)
    manifest = json.loads((vdir / "app-package.json").read_text("utf-8"))
    assert manifest["runtime_fingerprint"] == fp and "python" not in manifest
    assert (root / "bootstrap" / "bootstrap.py").is_file()
    assert (root / "start.bat").is_file()
    assert state_mod.StateStore(root / "apps" / build_request.app_id / "state").load().current == "v1.0.0"


def test_second_version_with_same_lock_reuses_the_runtime(build_request, stub_toolchain, tmp_path):
    root = tmp_path / "ROOT"
    first = store_builder.build_into_store(build_request, root, version="v1.0.0")
    second = store_builder.build_into_store(build_request, root, version="v1.1.0")
    assert second.ok and second.runtime_reused
    assert second.fingerprint == first.fingerprint
    runtimes = [p for p in (root / "deps" / "runtimes").iterdir() if p.is_dir()]
    assert len(runtimes) == 1                              # ONE runtime, two versions
    assert state_mod.StateStore(root / "apps" / build_request.app_id / "state").load().pending == "v1.1.0"


def test_version_slot_excludes_build_artifacts(build_request, stub_toolchain, tmp_path):
    """The slot is what travels on every update — a project's wheelhouse must not
    ride along (CV_Viewer's added 124 MB to every single version)."""
    wheels = build_request.project_dir / "wheels"
    wheels.mkdir()
    (wheels / "numpy-2.0.whl").write_bytes(b"PK")

    root = tmp_path / "ROOT"
    result = store_builder.build_into_store(build_request, root, version="v1.0.0")
    assert result.ok
    slot = root / "apps" / build_request.app_id / "versions" / "v1.0.0"
    assert not list((slot / "application" / "wheels").glob("*.whl"))


# ── the missing-import gate (it must survive a REUSED runtime) ───────────────

def missing_report(*required: str) -> "imports_mod.MissingReport":
    report = imports_mod.MissingReport(required=list(required))
    report.sites = {name: ["application/app.py:1(模組層級 import)"] for name in required}
    return report


def gate_reports(monkeypatch, report, *, seen: list | None = None):
    """Make the import probe answer `report`, recording which python it was asked."""
    def probe(entrypoint, project_dir, python):
        if seen is not None:
            seen.append(Path(python))
        return report
    monkeypatch.setattr(store_builder.imports_mod, "missing_dependencies", probe)


def test_reused_runtime_still_gets_the_missing_import_gate(build_request, stub_toolchain,
                                                           monkeypatch, tmp_path):
    """S8, the one the store layout is FOR. The gate used to sit after the
    `return fingerprint, True` of ensure_runtime(), so it ran only on the build
    that installed the runtime. Every version after that — and every 2nd..Nth app,
    which is the entire reason the store exists — got no gate at all: a version
    importing a package nobody installed was built, marked .complete, and shipped.
    """
    root = tmp_path / "ROOT"
    first = store_builder.build_into_store(build_request, root, version="v1.0.0")
    assert first.ok, first.errors

    # v2 grows an import of something the (unchanged) lock never installed.
    seen: list[Path] = []
    gate_reports(monkeypatch, missing_report("duckdb"), seen=seen)
    (build_request.project_dir / "app.py").write_text(
        "import streamlit as st\nimport duckdb\n", encoding="utf-8")

    second = store_builder.build_into_store(build_request, root, version="v2.0.0")
    assert not second.ok
    assert any("duckdb" in e for e in second.errors), second.errors
    assert any("app.py:1" in e for e in second.errors), second.errors      # where
    assert any("requirements" in e for e in second.errors), second.errors  # way out 1
    assert any("try/except ImportError" in e for e in second.errors), second.errors  # way 2

    # asked the runtime that will really run the app — the shared, reused one
    assert seen == [root / "deps" / "runtimes" / first.fingerprint / "python.exe"]

    # and NOTHING was left behind: no version dir, so certainly no .complete on one
    slot = root / "apps" / build_request.app_id / "versions" / "v2.0.0"
    assert not slot.exists()
    assert not integrity.is_complete(slot)
    state = state_mod.StateStore(root / "apps" / build_request.app_id / "state").load()
    assert state.pending is None and state.current == "v1.0.0"


def test_a_second_app_reusing_a_runtime_cannot_ship_a_missing_import(stub_toolchain,
                                                                     monkeypatch, tmp_path):
    """Same hole, the multi-app shape: app B reuses the runtime app A's lock built.
    B's imports were never checked against it, because B's build never installed
    anything."""
    root = tmp_path / "ROOT"
    alpha = make_store_project(tmp_path, "Alpha")
    assert store_builder.build_into_store(alpha, root, version="v1").ok

    beta = make_store_project(tmp_path, "Beta")
    gate_reports(monkeypatch, missing_report("pandas"))
    result = store_builder.build_into_store(beta, root, version="v1")
    assert not result.ok and any("pandas" in e for e in result.errors)
    assert result.runtime_reused is False        # never got that far
    assert not (root / "apps" / beta.app_id / "versions" / "v1").exists()


def test_a_probe_that_cannot_run_is_not_blamed_on_the_project(build_request, stub_toolchain,
                                                              monkeypatch, tmp_path):
    """Not knowing is not the same as knowing the worst: if we cannot ASK the
    runtime what it can import, say so — and still refuse to ship."""
    def explode(*_a, **_k):
        raise imports_mod.ImportProbeError("python.exe 沒有回應")
    monkeypatch.setattr(store_builder.imports_mod, "missing_dependencies", explode)

    result = store_builder.build_into_store(build_request, tmp_path / "ROOT",
                                            version="v1.0.0")
    assert not result.ok
    assert any("沒辦法用交付包裡的 Python 檢查" in e for e in result.errors), result.errors
    assert not (tmp_path / "ROOT" / "apps" / build_request.app_id
                / "versions" / "v1.0.0").exists()


def test_optional_imports_are_a_warning_not_a_failed_build(build_request, stub_toolchain,
                                                           monkeypatch, tmp_path):
    """A lazy `import anthropic` inside a function cannot crash a first render, so
    it can never be a reason to refuse a build — but the operator still hears it."""
    report = imports_mod.MissingReport(optional=["anthropic"])
    report.sites = {"anthropic": ["application/app.py:9(函式內延遲 import)"]}
    gate_reports(monkeypatch, report)

    result = store_builder.build_into_store(build_request, tmp_path / "ROOT",
                                            version="v1.0.0")
    assert result.ok, result.errors
    assert any("anthropic" in w for w in result.warnings), result.warnings


def test_store_build_requires_a_real_lockfile(build_request, stub_toolchain, tmp_path):
    (build_request.project_dir / "requirements.txt").write_text("streamlit>=1.0\n",
                                                                encoding="utf-8")
    result = store_builder.build_into_store(build_request, tmp_path / "ROOT", version="v1")
    assert not result.ok and any("釘死" in e for e in result.errors)


def test_versions_are_immutable_once_complete(build_request, stub_toolchain, tmp_path):
    root = tmp_path / "ROOT"
    store_builder.build_into_store(build_request, root, version="v1.0.0")
    again = store_builder.build_into_store(build_request, root, version="v1.0.0")
    assert not again.ok and any("不可變" in e for e in again.errors)


def test_exported_runtime_still_verifies_on_the_target(build_request, stub_toolchain, tmp_path):
    """The invariant that broke every export: whatever files.json declares must
    survive the copy. The old exporter filtered *.pyc while files.json declared
    4,039 of them, so the target machine failed integrity on a package that was
    perfectly fine — and the error told the operator to copy it again."""
    root = tmp_path / "ROOT"
    result = store_builder.build_into_store(build_request, root, version="v1.0.0")
    assert result.ok

    # A wheel that shipped its own bytecode: it must not be in the store at all.
    site = root / "deps" / "runtimes" / result.fingerprint / "Lib" / "site-packages"
    assert not list(site.rglob("*.pyc")) if site.is_dir() else True

    out = store_builder.export_update(root, build_request.app_id, "v1.0.0", tmp_path / "usb")
    runtime_out = out / "runtimes" / result.fingerprint
    assert integrity.verify_tree(
        runtime_out, extra_excluded={runtime_store.RUNTIME_META}) == []
    assert integrity.verify_tree(out / "versions" / "v1.0.0") == []


def test_export_update_strips_sentinels_and_writes_release(build_request, stub_toolchain, tmp_path):
    root = tmp_path / "ROOT"
    result = store_builder.build_into_store(build_request, root, version="v1.0.0")
    out = store_builder.export_update(root, build_request.app_id, "v1.0.0",
                                      tmp_path / "usb")
    release = json.loads((out / "release.json").read_text("utf-8"))
    assert release["version"] == "v1.0.0"
    assert release["runtime_fingerprint"] == result.fingerprint
    assert not (out / "versions" / "v1.0.0" / integrity.SENTINEL).exists()
    assert not (out / "runtimes" / result.fingerprint / integrity.SENTINEL).exists()
    # and the payload is exactly installable: verify as the device would
    assert integrity.verify_tree(out / "versions" / "v1.0.0") == []
