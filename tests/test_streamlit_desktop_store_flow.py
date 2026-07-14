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
from provision_builder.streamlit_desktop import models
from provision_builder.streamlit_desktop import pages as pages_mod
from provision_builder.streamlit_desktop.device import (
    bootstrap,
    gc as gc_mod,
    identifiers,
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


def test_runtime_publish_falls_back_to_verified_copy_when_defender_blocks_rename(
        tmp_path, monkeypatch):
    """A real CV Viewer build remained pinned beyond the 76-second rename window.

    The fallback may expose a directory name, but never a usable runtime: `.complete`
    is written only after the copied tree passes its own files.json verification.
    """
    staging = tmp_path / ".staging-runtime"
    target = tmp_path / "cp311-runtime"
    (staging / "Lib").mkdir(parents=True)
    (staging / "python.exe").write_bytes(b"python")
    (staging / "Lib" / "os.py").write_text("# stdlib", encoding="utf-8")
    (staging / runtime_store.RUNTIME_META).write_text(
        json.dumps({"fingerprint": target.name}), encoding="utf-8")
    integrity.write_files_json(
        staging, integrity.build_files_json(
            staging, extra_excluded={runtime_store.RUNTIME_META}))

    def defender_never_releases(*_args, **_kwargs):
        raise runtime_mod.RuntimeError_("WinError 5 after retries")

    monkeypatch.setattr(store_builder, "_rename_with_retry", defender_never_releases)
    store_builder._publish_completed_tree(
        staging, target, extra_excluded={runtime_store.RUNTIME_META})

    assert integrity.is_complete(target)
    assert integrity.verify_tree(
        target, extra_excluded={runtime_store.RUNTIME_META}) == []
    assert not staging.exists()


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
    """A provider that hands back another app's release is how App B's bytes get
    installed into App A's version slot. The check stays — but it is no longer a dead
    end: it names the app the payload IS for, and the way in."""
    source = tmp_path / "usb"
    (source / "other").mkdir(parents=True)
    (source / "other" / "release.json").write_text(json.dumps({
        "app_id": APP, "version": "v9", "revision": "r",
        "runtime_fingerprint": FP1}), encoding="utf-8")
    with pytest.raises(ProviderError) as caught:
        FolderUpdateProvider(source).get_latest_release("other", "v1")
    message = str(caught.value)
    assert APP in message and "other" in message      # both sides of the mismatch
    assert f"--app {APP}" in message                  # ...and what to type instead
    message.encode("cp950")


def test_a_payload_can_say_which_app_it_is_for_without_being_told_first(tmp_path):
    """S8, THE SECOND APP CAN NEVER ARRIVE. Every entry point into this provider starts
    from an app_id the CALLER already has — and bootstrap gets that id from the apps
    already installed in apps\\. So `--install <App B's payload>` on a machine that runs
    App A resolved to App A, asked for App A's release, got App B's release.json, and
    refused it. The machine, the operator and the payload were all correct.

    A payload knows perfectly well which app it is for. Asking it must not require
    knowing the answer in advance — that is the loop this closes.
    """
    payload = tmp_path / "v1.1.0更新包"          # the operator renamed it. They do.
    payload.mkdir()
    (payload / "release.json").write_text(json.dumps({
        "schema": 1, "app_id": "line-b-report", "version": "v2.0.0",
        "revision": "r7", "runtime_fingerprint": FP1}), encoding="utf-8")

    provider = FolderUpdateProvider.from_payload_dir(payload)
    assert provider.payload_app_id() == "line-b-report"
    assert provider.app_ids() == ["line-b-report"]
    # and knowing the id, the release itself reads back — for an app this machine has
    # never seen, which is the entire point
    release = provider.get_latest_release("line-b-report", "")
    assert release is not None and release.version == "v2.0.0"


def test_an_update_source_lists_every_app_it_offers_not_just_the_ones_we_have(tmp_path):
    """The same question asked of a SHARE rather than a folder: a machine polling
    \\\\server\\updates has to be able to see an app it does not have yet."""
    source = tmp_path / "share"
    for app_id, version in (("line-a-viewer", "v1"), ("line-b-report", "v2")):
        (source / app_id).mkdir(parents=True)
        (source / app_id / "release.json").write_text(json.dumps({
            "schema": 1, "app_id": app_id, "version": version, "revision": "r",
            "runtime_fingerprint": FP1}), encoding="utf-8")
    (source / "not-a-payload").mkdir()               # junk on a USB stick: skipped
    (source / "readme.txt").write_text("hi", encoding="utf-8")

    assert FolderUpdateProvider(source).app_ids() == ["line-a-viewer", "line-b-report"]
    assert FolderUpdateProvider(tmp_path / "nothing").app_ids() == []


def test_a_payload_whose_app_id_is_junk_is_not_a_payload(tmp_path):
    """An app_id off a USB stick is untrusted input, and it is about to become a path."""
    payload = tmp_path / "payload"
    payload.mkdir()
    (payload / "release.json").write_text(json.dumps({
        "app_id": "../../windows/system32", "version": "v1",
        "runtime_fingerprint": FP1}), encoding="utf-8")
    assert FolderUpdateProvider.from_payload_dir(payload).payload_app_id() is None

    (payload / "release.json").write_text("{ not json", encoding="utf-8")
    assert FolderUpdateProvider.from_payload_dir(payload).payload_app_id() is None


def test_staging_an_update_reuses_the_big_file_this_machine_already_has(tree, tmp_path):
    """S8/S5, THE FACTORY PC'S HALF. Deduplicating version slots on the BUILD machine
    saves a disk nobody is short of. The machine that actually matters is the factory PC,
    and it gets its versions from a payload: updater.stage_release() copies the whole
    version out of the USB stick — including the 84 MB model file sitting, byte for byte,
    in the version slot it is running RIGHT NOW, one directory over.

    `download_app(..., link_from=paths.versions_dir)` makes that file cost a directory
    entry instead of 84 MB, on every release, forever.

    It is safe precisely BECAUSE of where it sits: the staging dir is a sibling of the
    version slots (same volume), and stage_release() runs verify_tree() over the staged
    tree — hashing whatever the link actually points at — before it renames anything into
    place. A wrong link is a verification failure, not a promoted version.
    """
    weight = b"MODEL" * 20000                       # the file that never changes
    (tree / "apps" / APP / "versions" / "v1" / "application" / "model.bin").write_bytes(weight)
    integrity.write_files_json(tree / "apps" / APP / "versions" / "v1")
    integrity.write_complete(tree / "apps" / APP / "versions" / "v1")

    source = make_update_source(tmp_path, "v2", FP1, with_runtime=False)
    payload_v2 = source / APP / "versions" / "v2"
    (payload_v2 / "application" / "model.bin").write_bytes(weight)   # identical
    (payload_v2 / "application" / "app.py").write_text(              # this really changed
        "# v2: the new inference panel", encoding="utf-8")
    integrity.write_files_json(payload_v2)

    paths = app_paths(tree)
    provider = FolderUpdateProvider(source)
    staging = tree / "apps" / APP / "staging" / "probe"
    provider.download_app(
        ReleaseMetadata(app_id=APP, version="v2", revision="r1", runtime_fingerprint=FP1),
        staging, link_from=paths.versions_dir)

    staged = staging / "application" / "model.bin"
    live = tree / "apps" / APP / "versions" / "v1" / "application" / "model.bin"
    assert staged.stat().st_ino == live.stat().st_ino, "又從 USB 複製了一份一模一樣的大檔"
    assert staged.read_bytes() == weight
    # the file that really CHANGED is a real copy, carrying the payload's bytes — the
    # dedup keys on content, so it can never serve the old version's code as the new one
    app_py = staging / "application" / "app.py"
    assert app_py.stat().st_ino != (
        tree / "apps" / APP / "versions" / "v1" / "application" / "app.py").stat().st_ino
    assert app_py.read_text("utf-8") == "# v2: the new inference panel"
    # ...and the staged tree still verifies against its own files.json, which is what
    # stage_release() checks before it promotes anything
    assert integrity.verify_tree(staging) == []


def test_staging_without_link_from_behaves_exactly_as_before(tree, tmp_path):
    """The dedup is opt-in: a caller that does not ask for it gets today's behaviour,
    byte for byte. updater.py has to pass `link_from` before the factory PC benefits."""
    source = make_update_source(tmp_path, "v2", FP1, with_runtime=False)
    staging = tree / "apps" / APP / "staging" / "probe"
    FolderUpdateProvider(source).download_app(
        ReleaseMetadata(app_id=APP, version="v2", revision="r1", runtime_fingerprint=FP1),
        staging)
    assert (staging / "application" / "app.py").stat().st_nlink == 1
    assert integrity.verify_tree(staging) == []


def test_a_payload_file_that_only_LOOKS_like_the_one_we_have_is_never_linked(tree, tmp_path):
    """Same path, same size, different bytes — a retrained model. Linking on the path
    would install the OLD model under the NEW version's number, and every hash in
    files.json would agree with itself all the way to the factory floor."""
    old = b"A" * 4096
    new = b"B" * 4096                               # same size, different content
    (tree / "apps" / APP / "versions" / "v1" / "application" / "model.bin").write_bytes(old)
    integrity.write_files_json(tree / "apps" / APP / "versions" / "v1")
    integrity.write_complete(tree / "apps" / APP / "versions" / "v1")

    source = make_update_source(tmp_path, "v2", FP1, with_runtime=False)
    payload_v2 = source / APP / "versions" / "v2"
    (payload_v2 / "application" / "model.bin").write_bytes(new)
    integrity.write_files_json(payload_v2)

    staging = tree / "apps" / APP / "staging" / "probe"
    FolderUpdateProvider(source).download_app(
        ReleaseMetadata(app_id=APP, version="v2", revision="r1", runtime_fingerprint=FP1),
        staging, link_from=app_paths(tree).versions_dir)

    assert (staging / "application" / "model.bin").read_bytes() == new
    assert integrity.verify_tree(staging) == []
    # the version this machine is running still has ITS bytes
    assert (tree / "apps" / APP / "versions" / "v1" / "application"
            / "model.bin").read_bytes() == old


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

def make_store_project(tmp_path: Path, name: str = "Demo App",
                       app_id: str | None = None) -> BuildRequest:
    """A buildable project + the shared (fake) shell and runtime template.

    `app_id` is BuildRequest.app_id_override — the GUI's 「應用代號」 field.
    """
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
                        shell_exe=shell, runtime_template=template,
                        app_id_override=app_id)


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


# ── S8:一台機器、兩個 App —— 兩個 App 必須真的是兩個 App ─────────────────────
#
# 這一節全部在講同一件事:app_id 就是這個 App 在這台機器上的「身分」。身分撞在一起,
# 第二個 App 就會蓋掉第一個,而現場看不出來 —— 資料夾名、啟動檔名、管理主控台全都不變,
# 只有程式換了。

def test_two_chinese_named_apps_do_not_collide_into_one_app(stub_toolchain, tmp_path):
    """S8, THE BLOCKER。slugify() 把非英數字全部丟掉,所以「影像檢視器」和「報表分析」
    以前都得到同一個 app_id(app-streamlit-app):同一個資料夾、同一個 start bat、
    同一份 manifest。第二次建置看起來像「版本已經建過了」,而操作員照著那句話把版本號
    往上加 —— 產線上的 App A 就這樣被換成另一支程式,名字和入口都沒變。
    """
    root = tmp_path / "ROOT"
    viewer = make_store_project(tmp_path, "影像檢視器", app_id="image-viewer")
    report = make_store_project(tmp_path, "報表分析", app_id="report-analyzer")

    assert viewer.app_id != report.app_id                     # 兩個身分,不是一個
    first = store_builder.build_into_store(viewer, root, version="v1.0.0")
    second = store_builder.build_into_store(report, root, version="v1.0.0")
    assert first.ok, first.errors
    assert second.ok, second.errors                            # 不再是「版本衝突」
    assert (first.app_id, second.app_id) == ("app-image-viewer", "app-report-analyzer")

    # 兩棵版本樹、兩份 manifest、兩個顯示名稱,誰也沒有蓋掉誰
    assert sorted(paths_mod.list_app_ids(root)) == ["app-image-viewer",
                                                    "app-report-analyzer"]
    for app_id, request in ((first.app_id, viewer), (second.app_id, report)):
        vdir = root / "apps" / app_id / "versions" / "v1.0.0"
        assert integrity.is_complete(vdir)
        manifest = json.loads((vdir / "app-package.json").read_text("utf-8"))
        assert manifest["app_id"] == app_id
        assert manifest["display_name"] == request.display_name
        # 各自的入口與各自的主控台(共用的只有 runtime)
        assert (root / f"start-{app_id}.bat").is_file()
        assert (root / "tools" / f"admin-{app_id}.bat").is_file()

    # 500 MB 的 runtime 還是只有一份 —— 「共用 runtime」和「同一個 App」是兩件事
    assert second.runtime_reused
    assert len([p for p in (root / "deps" / "runtimes").iterdir() if p.is_dir()]) == 1
    assert first.fingerprint == second.fingerprint


def test_a_second_app_under_an_existing_app_id_is_refused_and_never_called_a_version_collision(
        stub_toolchain, tmp_path):
    """The sticky-field disaster: the operator gives App B the app id they typed for
    App A last time. That must STOP, and the message must never say 「改版本號」 — the
    old one did, and following it overwrites App A with App B."""
    root = tmp_path / "ROOT"
    viewer = make_store_project(tmp_path, "影像檢視器", app_id="image-viewer")
    report = make_store_project(tmp_path, "報表分析", app_id="image-viewer")  # 同一個代號
    assert store_builder.build_into_store(viewer, root, version="v1.0.0").ok

    result = store_builder.build_into_store(report, root, version="v1.0.0")
    assert not result.ok
    message = "\n".join(result.errors)
    assert "影像檢視器" in message and "報表分析" in message   # 兩個名字都要講出來
    assert "app-image-viewer" in message                       # 撞在一起的那個身分
    assert "這不是版本衝突" in message
    for bad_advice in ("改成 v1.0.1", "把版本號改成", "要發新版"):
        assert bad_advice not in message, "叫操作員改版本號 = 叫他覆蓋掉線上那支 App"

    # 而且什麼都沒有動:App A 的版本、manifest、狀態原封不動
    vdir = root / "apps" / "app-image-viewer" / "versions" / "v1.0.0"
    manifest = json.loads((vdir / "app-package.json").read_text("utf-8"))
    assert manifest["display_name"] == "影像檢視器"
    assert paths_mod.list_app_ids(root) == ["app-image-viewer"]


def test_a_name_with_no_latin_characters_is_refused_before_anything_is_written(
        stub_toolchain, tmp_path):
    """A store id is a folder name, a start-<id>.bat and a console name, forever. A
    name with nothing to slugify cannot produce one, so the build stops and asks —
    it does not invent `app-streamlit-app` (a collision) or a digest (a barcode)."""
    root = tmp_path / "ROOT"
    request = make_store_project(tmp_path, "影像檢視器")        # no explicit app id
    result = store_builder.build_into_store(request, root, version="v1.0.0")

    assert not result.ok
    message = "\n".join(result.errors)
    assert "影像檢視器" in message
    assert "應用代號" in message and "app id" in message
    assert not (root / "apps").exists()                        # 什麼都沒寫進去
    assert not (root / "deps").exists()


def test_an_explicit_app_id_does_not_need_the_app_prefix(stub_toolchain, tmp_path):
    """`app-` is a portal rendering contract (engine.py::_derive_category), not
    something a person should have to remember."""
    request = make_store_project(tmp_path, "影像檢視器", app_id="image-viewer")
    assert request.app_id == "app-image-viewer"
    assert make_store_project(tmp_path, "報表分析",
                              app_id="app-report").app_id == "app-report"
    root = tmp_path / "ROOT"
    result = store_builder.build_into_store(request, root, version="v1.0.0")
    assert result.ok and result.app_id == "app-image-viewer"
    assert (root / "apps" / "app-image-viewer").is_dir()


def test_an_illegal_app_id_is_a_message_not_a_traceback(stub_toolchain, tmp_path):
    request = make_store_project(tmp_path, "影像檢視器", app_id="../../etc")
    result = store_builder.build_into_store(request, tmp_path / "ROOT", version="v1.0.0")
    assert not result.ok
    assert "應用代號" in "\n".join(result.errors)


def test_rebuilding_the_same_app_with_the_same_name_is_not_a_collision(build_request,
                                                                       stub_toolchain,
                                                                       tmp_path):
    """The guard must not fire on the ordinary case it sits in front of: the same app,
    a new version."""
    root = tmp_path / "ROOT"
    assert store_builder.build_into_store(build_request, root, version="v1.0.0").ok
    assert store_builder.build_into_store(build_request, root, version="v1.1.0").ok


def test_slugify_never_hands_two_different_names_the_same_id():
    """The floor under all of the above, and it holds in FAT mode too, where there is
    no store to refuse anything: a name with no latin characters used to slug to the
    constant "streamlit-app", so EVERY pair of Chinese-named apps was one app."""
    names = ["影像檢視器", "報表分析", "產線 檢視器", "看板", "報表分析 "]
    ids = {models.app_id_for(n) for n in names}
    assert len(ids) == len({n.strip() for n in names})       # 每個名字一個身分
    assert "app-streamlit-app" not in ids                    # 共用的那個常數,不存在了

    # 決定性:同一個名字永遠是同一個 id(否則同一個 App 每次建置都變成新的 App)
    assert models.app_id_for("影像檢視器") == models.app_id_for("影像檢視器")
    assert models.slugify("影像檢視器").startswith("streamlit-app-")
    # 而有英數字可用時,slug 還是那個看得懂的 slug(既有行為不能動)
    assert models.slugify("Alpha Viewer") == "alpha-viewer"
    assert models.app_id_for("Report 2") == "app-report-2"
    # 這種 id 是合法的路徑元件 —— 撞不到別人,也逃不出 root
    identifiers.validate_identifier(models.slugify("影像檢視器"), "app_id")


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


def test_a_delivered_version_carries_the_shared_page_rules(build_request, stub_toolchain,
                                                           tmp_path):
    """launch.py loads `launcher/pages.py` BY PATH on the device (there is no
    provision_builder inside a delivered package) and refuses to start without it —
    LauncherIncomplete, exit 4, 「launcher 資料夾不完整」. It is one file; forgetting to
    ship it turns every new version into a version that cannot start.

    It has to survive the ROUND TRIP too: it lives inside the version directory, so
    both exports carry it — a 完整交付 the factory boots from, and an update payload
    that becomes the next `current`.
    """
    root = tmp_path / "ROOT"
    assert store_builder.build_into_store(build_request, root, version="v1.0.0").ok
    app = build_request.app_id

    delivered = pages_mod.DELIVERED_NAME
    built = root / "apps" / app / "versions" / "v1.0.0" / "launcher" / delivered
    assert built.is_file(), "版本裡沒有 pages.py — 這個版本在裝置上啟動不了"
    assert built.read_bytes() == pages_mod.SOURCE.read_bytes()
    # the device refuses a pages.py that is not THE pages.py, so the mark must travel
    assert pages_mod.MODULE_MARK in built.read_text("utf-8")
    # and it is covered by files.json, so a corrupted copy fails the integrity check
    # instead of being loaded
    assert integrity.verify_tree(built.parent.parent) == []

    out = tmp_path / "deliver"
    store_builder.export_full_tree(root, out)
    assert (out / "apps" / app / "versions" / "v1.0.0" / "launcher" / delivered).is_file()

    payload = tmp_path / "update"
    store_builder.export_update(root, app, "v1.0.0", payload)
    assert (payload / app / "versions" / "v1.0.0" / "launcher" / delivered).is_file()


def test_second_version_with_same_lock_reuses_the_runtime(build_request, stub_toolchain, tmp_path):
    root = tmp_path / "ROOT"
    first = store_builder.build_into_store(build_request, root, version="v1.0.0")
    second = store_builder.build_into_store(build_request, root, version="v1.1.0")
    assert second.ok and second.runtime_reused
    assert second.fingerprint == first.fingerprint
    runtimes = [p for p in (root / "deps" / "runtimes").iterdir() if p.is_dir()]
    assert len(runtimes) == 1                              # ONE runtime, two versions
    assert state_mod.StateStore(root / "apps" / build_request.app_id / "state").load().pending == "v1.1.0"


def test_a_delivered_tree_does_not_promote_the_build_machines_pending_on_first_boot(
        build_request, stub_toolchain, monkeypatch, tmp_path):
    """S4, proven on the target rather than argued on the build machine.

    The exporter used to copy state.json verbatim. A build machine NEVER launches
    what it builds, so its second build sits in `pending` forever — and `pending` is
    not a note, it is an INSTRUCTION: bootstrap promotes it before it launches
    anything. So the operator delivered v1.0.0, the factory machine booted, and it
    silently promoted and ran v1.1.0. Here the delivered tree is booted for real
    (only the launcher process is faked) and it must run what was delivered.
    """
    root = tmp_path / "ROOT"
    app = build_request.app_id
    assert store_builder.build_into_store(build_request, root, version="v1.0.0").ok
    assert store_builder.build_into_store(build_request, root, version="v1.1.0").ok
    build_state = state_mod.StateStore(root / "apps" / app / "state").load()
    assert build_state.current == "v1.0.0" and build_state.pending == "v1.1.0"

    out = tmp_path / "deliver"
    store_builder.export_full_tree(root, out)            # default: current = v1.0.0

    monkeypatch.setattr(bootstrap.time, "sleep", lambda _s: None)
    popen = popen_factory([dict(healthy=True)])
    code = bootstrap.start_app(paths_mod.AppPaths(out, app), [],
                               notify=lambda *a: None, popen=popen)

    assert code == 0
    launched = popen.calls[0][1]
    assert "v1.0.0" in launched and "v1.1.0" not in launched
    final = state_mod.StateStore(out / "apps" / app / "state").load()
    assert final.current == "v1.0.0" and final.pending is None
    # and the version it was never meant to have is not even on the disk
    assert not (out / "apps" / app / "versions" / "v1.1.0").exists()


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
    # way out #1 — "add it to the dependency declaration". In store mode the
    # source is a pinned lock, so the honest instruction is the lock file, not
    # "requirements": the 選用相依群組 field is inert when a lock is the source,
    # and recommending it produced a tool that contradicted itself in one breath.
    assert any("lock" in e or "requirements" in e for e in second.errors), second.errors
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
