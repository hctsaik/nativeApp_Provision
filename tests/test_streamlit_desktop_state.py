"""Phase 1 foundations: identifiers, state machine, atomic StateStore, locks,
leases, integrity manifests. These are the invariants everything above rests on.

Plus the operator-facing surface built on them: bootstrap's --install /
--set-update-source / --rollback-to, the launcher exit-code contract, and GC's
promise that it neither crashes on a zh-TW console nor lies about what it freed.
"""

from __future__ import annotations

import errno
import io
import json
import os
import re
import shutil
import threading
from pathlib import Path

import pytest

from provision_builder.streamlit_desktop.device import (
    bootstrap,
    gc as gc_mod,
    integrity,
    leases,
    locks,
    paths as paths_mod,
    runtime_store,
    state,
)
from provision_builder.streamlit_desktop.device.identifiers import (
    IdentifierError,
    is_safe_relpath,
    validate_identifier,
)


# ── identifiers ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("value", ["v1.2.0", "cp311-a1b2c3", "my-app_2", "A"])
def test_safe_identifiers_pass(value):
    assert validate_identifier(value, "x") == value


@pytest.mark.parametrize("value", [
    "..", "../x", "a/b", "a\\b", "C:evil", ".hidden", "-flag", "", None,
    "v1.", "v1 ", "a" * 101,
])
def test_dangerous_identifiers_are_rejected(value):
    with pytest.raises(IdentifierError):
        validate_identifier(value, "x")


def test_safe_relpath_rules():
    assert is_safe_relpath("application/app.py")
    assert is_safe_relpath("Lib/site-packages/x.py")
    for bad in ("/abs", "C:/x", "a\\b", "a/../b", "./a", "", "a/b/"):
        assert not is_safe_relpath(bad), bad


# ── state transitions (pure) ─────────────────────────────────────────────────

def make_state(**kw) -> state.AppState:
    kw.setdefault("current", "v1")
    return state.AppState(app_id="demo", **kw)


def test_promote_rotates_all_slots_in_one_step():
    s = state.promote_pending(make_state(pending="v2", last_known_good="v1"))
    assert (s.previous, s.current, s.pending, s.candidate) == ("v1", "v2", None, "v2")
    assert s.last_known_good == "v1"          # untouched until health check


def test_promote_without_pending_refuses():
    with pytest.raises(state.StateError):
        state.promote_pending(make_state())


def test_commit_candidate_sets_lkg():
    s = state.commit_candidate(make_state(current="v2", candidate="v2"))
    assert s.candidate is None and s.last_known_good == "v2"


def test_fail_candidate_rolls_back_and_remembers():
    s = state.fail_candidate(
        make_state(current="v2", candidate="v2", previous="v1", last_known_good="v1"),
        revision="r1",
    )
    assert s.current == "v1" and s.candidate is None
    assert s.is_failed("v2") and s.is_failed("v2", "r1")
    assert not s.is_failed("v2", "r2")        # a new revision may retry


def test_fail_candidate_without_rollback_target_refuses():
    with pytest.raises(state.StateError, match="cannot roll back"):
        state.fail_candidate(make_state(current="v2", candidate="v2"))


def test_clear_bad_pending_keeps_current_running():
    s = state.clear_bad_pending(make_state(pending="v9"), revision="r1")
    assert s.pending is None and s.current == "v1" and s.is_failed("v9", "r1")


def test_set_pending_rejects_current_version():
    with pytest.raises(state.StateError):
        state.set_pending(make_state(), "v1")


# ── StateStore atomicity ─────────────────────────────────────────────────────

@pytest.fixture
def store(tmp_path) -> state.StateStore:
    return state.StateStore(tmp_path / "state")


def test_initialize_then_load_roundtrip(store):
    created = store.initialize("demo", "v1")
    loaded = store.load()
    assert loaded.current == "v1" and loaded.generation == created.generation == 1


def test_every_write_bumps_generation_and_verifies(store):
    store.initialize("demo", "v1")
    s2 = store.mutate(lambda s: state.set_pending(s, "v2"))
    assert s2.generation == 2 and s2.pending == "v2"


def test_corrupt_state_is_a_loud_error(store):
    store.initialize("demo", "v1")
    store.path.write_text("{ half json", encoding="utf-8")
    # The message is Traditional Chinese now, and names the file — see
    # test_a_broken_state_file_names_the_file_and_speaks_the_operators_language.
    with pytest.raises(state.StateError, match="毀損"):
        store.load()


def test_state_with_path_escape_version_is_rejected(store):
    store.initialize("demo", "v1")
    data = json.loads(store.path.read_text("utf-8"))
    data["pending"] = "..\\..\\evil"
    store.path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(state.StateError):
        store.load()


def test_interrupted_write_leaves_old_state_intact(store, monkeypatch):
    """Crash between tmp-write and replace: state.json must still be the old,
    complete document — no torn JSON, no missing file."""
    store.initialize("demo", "v1")

    def boom(src, dst):
        raise OSError(6, "simulated crash before replace")

    monkeypatch.setattr(state.os, "replace", boom)
    with pytest.raises(OSError):
        store.mutate(lambda s: state.set_pending(s, "v2"))
    monkeypatch.undo()

    survivor = store.load()
    assert survivor.current == "v1" and survivor.pending is None
    assert not list(store.state_dir.glob(".state-*.tmp"))   # tmp cleaned up


def test_concurrent_mutations_serialize_without_loss(store):
    store.initialize("demo", "v1")
    errors = []

    def add_failed(tag):
        def fn(s):
            return state.clear_bad_pending(
                state.set_pending(s, f"x{tag}"), revision=str(tag))
        try:
            store.mutate(fn)
        except Exception as exc:  # noqa: BLE001 - collect for assertion
            errors.append(exc)

    threads = [threading.Thread(target=add_failed, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    final = store.load()
    assert len(final.failed_versions) == 8      # no lost update
    assert final.generation == 9                # 1 init + 8 writes


# ── locks ────────────────────────────────────────────────────────────────────

def test_lock_blocks_second_acquirer(tmp_path):
    first = locks.FileLock(tmp_path / "l.lock").acquire()
    with pytest.raises(locks.LockTimeout):
        locks.FileLock(tmp_path / "l.lock").acquire(timeout=0.5, poll=0.05)
    first.release()
    locks.FileLock(tmp_path / "l.lock").acquire(timeout=1).release()


def test_stale_lock_from_dead_process_is_taken_over(tmp_path):
    path = tmp_path / "l.lock"
    path.write_text(json.dumps({"pid": 999999999, "process_start_time": 12345,
                                "operation_id": "dead"}), encoding="utf-8")
    lock = locks.FileLock(path).acquire(timeout=2)
    assert json.loads(path.read_text("utf-8"))["pid"] == os.getpid()
    lock.release()


def test_live_lock_with_same_pid_reuse_is_not_stolen(tmp_path):
    """A recorded owner whose PID is alive but with a DIFFERENT start time is
    stale; same start time (this very process) is not."""
    me = locks.my_identity()
    path = tmp_path / "l.lock"
    path.write_text(json.dumps({**me, "operation_id": "x"}), encoding="utf-8")
    with pytest.raises(locks.LockTimeout):
        locks.FileLock(path).acquire(timeout=0.5, poll=0.05)

    if me["process_start_time"] not in (None, -1):
        path.write_text(json.dumps({"pid": me["pid"],
                                    "process_start_time": me["process_start_time"] - 7,
                                    "operation_id": "ghost"}), encoding="utf-8")
        locks.FileLock(path).acquire(timeout=2).release()   # PID reused → stale


def test_release_does_not_remove_someone_elses_lock(tmp_path):
    path = tmp_path / "l.lock"
    mine = locks.FileLock(path).acquire()
    path.write_text(json.dumps({**locks.my_identity(), "operation_id": "other"}),
                    encoding="utf-8")
    mine.release()
    assert path.exists()                        # not ours anymore → left alone


# ── leases ───────────────────────────────────────────────────────────────────

def test_lease_lifecycle(tmp_path):
    lease = leases.create_lease(tmp_path, app_id="demo", version="v1",
                                runtime_fingerprint="cp311-abc")
    live = leases.valid_leases(tmp_path)
    assert len(live) == 1 and live[0]["version"] == "v1"
    lease.release()
    assert leases.valid_leases(tmp_path) == []


def test_stale_lease_is_cleaned_up(tmp_path):
    (tmp_path / "ghost.json").write_text(
        json.dumps({"pid": 999999999, "process_start_time": 1, "version": "v9"}),
        encoding="utf-8")
    assert leases.valid_leases(tmp_path) == []
    assert not (tmp_path / "ghost.json").exists()


# ── integrity ────────────────────────────────────────────────────────────────

@pytest.fixture
def payload(tmp_path) -> Path:
    root = tmp_path / "payload"
    (root / "application").mkdir(parents=True)
    (root / "application" / "app.py").write_text("print('hi')", encoding="utf-8")
    (root / "manifest.json").write_text("{}", encoding="utf-8")
    integrity.write_files_json(root)
    return root


def test_verify_passes_on_untouched_tree(payload):
    assert integrity.verify_tree(payload) == []


def test_verify_catches_tamper_missing_and_extra(payload):
    # Same length as the original so it is the HASH that trips, not the size.
    (payload / "application" / "app.py").write_text("print('ho')", encoding="utf-8")
    assert any("hash mismatch" in p for p in integrity.verify_tree(payload))

    (payload / "application" / "app.py").unlink()
    assert any("missing file" in p for p in integrity.verify_tree(payload))

    (payload / "application" / "app.py").write_text("print('hi')", encoding="utf-8")
    (payload / "smuggled.dll").write_bytes(b"MZ")
    assert any("undeclared file" in p for p in integrity.verify_tree(payload))


def test_manifest_with_unsafe_path_is_flagged(payload):
    manifest = integrity.load_files_json(payload)
    manifest["files"].append({"path": "../escape.py", "size": 1, "sha256": "0" * 64})
    problems = integrity.verify_tree(payload, manifest=manifest)
    assert any("unsafe path" in p for p in problems)


def test_sentinel_is_last_and_removal_is_first(payload):
    assert not integrity.is_complete(payload)
    integrity.write_complete(payload)
    assert integrity.is_complete(payload)
    assert integrity.verify_tree(payload) == []   # sentinel excluded from hashing
    integrity.remove_complete(payload)
    assert not integrity.is_complete(payload)


# ═════════════════════════════════════════════════════════════════════════════
# The operator-facing surface: install, update source, rollback, exit codes, GC
# ═════════════════════════════════════════════════════════════════════════════

APP = "demo"
FP1 = "cp311-aaaaaaaaaaaa"
FP2 = "cp311-bbbbbbbbbbbb"
FP3 = "cp311-cccccccccccc"


def build_runtime(root: Path, fingerprint: str, *, complete: bool = True) -> Path:
    rdir = Path(root) / "deps" / "runtimes" / fingerprint
    (rdir / "Lib").mkdir(parents=True, exist_ok=True)
    (rdir / "python.exe").write_bytes(b"MZ fake")
    (rdir / "Lib" / "os.py").write_text("# stdlib", encoding="utf-8")
    (rdir / runtime_store.RUNTIME_META).write_text(
        json.dumps({"schema": 1, "fingerprint": fingerprint}), encoding="utf-8")
    integrity.write_files_json(rdir, integrity.build_files_json(
        rdir, extra_excluded={runtime_store.RUNTIME_META}))
    if complete:
        integrity.write_complete(rdir)
    return rdir


def build_version(vdir: Path, version: str, fingerprint: str, *,
                  app: str = APP, complete: bool = True, body: str = "x",
                  shell_fp: str | None = None) -> Path:
    vdir = Path(vdir)
    (vdir / "application").mkdir(parents=True, exist_ok=True)
    (vdir / "application" / "app.py").write_text(f"# {body}", encoding="utf-8")
    (vdir / "launcher").mkdir(exist_ok=True)
    (vdir / "launcher" / "launch.py").write_text("# fake launcher", encoding="utf-8")
    manifest = {
        "schema_version": 2, "app_id": app, "display_name": "Demo",
        "version": version, "entrypoint": "application/app.py",
        "runtime_fingerprint": fingerprint,
        "shell_executable": "shell/cim-light.exe",
    }
    if shell_fp:                      # the shell lives in the SHARED store
        manifest["shell_fingerprint"] = shell_fp
    (vdir / "app-package.json").write_text(json.dumps(manifest), encoding="utf-8")
    integrity.write_files_json(vdir)
    if complete:
        integrity.write_complete(vdir)
    return vdir


def make_version(root: Path, version: str, fingerprint: str = FP1, **kw) -> Path:
    return build_version(Path(root) / "apps" / APP / "versions" / version,
                         version, fingerprint, **kw)


@pytest.fixture
def tree(tmp_path) -> Path:
    """A deployed store: runtime FP1, version v1, state initialized."""
    root = tmp_path / "ROOT"
    build_runtime(root, FP1)
    make_version(root, "v1")
    state.StateStore(root / "apps" / APP / "state").initialize(APP, "v1")
    return root


def paths_of(root: Path) -> paths_mod.AppPaths:
    return paths_mod.AppPaths(root, APP)


def store_of(root: Path) -> state.StateStore:
    return state.StateStore(Path(root) / "apps" / APP / "state")


def make_payload(tmp_path: Path, version: str, fingerprint: str = FP1, *,
                 revision: str = "r1", with_runtime: bool = False,
                 body: str = "two") -> Path:
    """What store_builder.export_update() writes: <out>/<app>/release.json +
    versions/<ver>/ + runtimes/<fp>/, with EVERY .complete sentinel stripped."""
    out = tmp_path / "usb"
    payload = out / APP
    payload.mkdir(parents=True, exist_ok=True)
    build_version(payload / "versions" / version, version, fingerprint,
                  complete=False, body=body)
    if with_runtime:
        rdir = payload / "runtimes" / fingerprint
        (rdir / "Lib").mkdir(parents=True)
        (rdir / "python.exe").write_bytes(b"MZ fake")
        (rdir / "Lib" / "os.py").write_text("# stdlib", encoding="utf-8")
        (rdir / runtime_store.RUNTIME_META).write_text(
            json.dumps({"schema": 1, "fingerprint": fingerprint}), encoding="utf-8")
        integrity.write_files_json(rdir, integrity.build_files_json(
            rdir, extra_excluded={runtime_store.RUNTIME_META}))
    (payload / "release.json").write_text(json.dumps({
        "schema": 1, "app_id": APP, "version": version, "revision": revision,
        "runtime_fingerprint": fingerprint,
    }), encoding="utf-8")
    return payload


# ── --install ────────────────────────────────────────────────────────────────

def test_install_verifies_then_earns_the_sentinel_and_sets_pending(tree, tmp_path, capsys):
    """The payload carries NO .complete (the exporter strips it on purpose). The
    sentinel must be written by THIS machine, after it has hashed every byte."""
    payload = make_payload(tmp_path, "v2")
    assert not (payload / "versions" / "v2" / integrity.SENTINEL).exists()

    code = bootstrap.install_payload(paths_of(tree), payload)
    assert code == 0

    installed = tree / "apps" / APP / "versions" / "v2"
    assert integrity.is_complete(installed)           # earned here, not trusted
    assert integrity.verify_tree(installed) == []
    final = store_of(tree).load()
    assert final.pending == "v2" and final.pending_revision == "r1"
    assert final.current == "v1"                      # nothing promoted yet
    out = capsys.readouterr().out
    assert "已安裝 v2" in out and "v1" in out          # names the fallback version


def test_install_accepts_the_folder_the_operator_actually_copied(tree, tmp_path):
    """--install <usb>\\demo and --install <usb> are both unambiguous."""
    payload = make_payload(tmp_path, "v2")
    assert bootstrap.install_payload(paths_of(tree), payload.parent) == 0
    assert store_of(tree).load().pending == "v2"


def test_install_of_a_corrupt_payload_changes_absolutely_nothing(tree, tmp_path, capsys):
    payload = make_payload(tmp_path, "v2")
    # Same length, different bytes: it is the HASH that must trip.
    (payload / "versions" / "v2" / "application" / "app.py").write_text(
        "# TWO", encoding="utf-8")

    code = bootstrap.install_payload(paths_of(tree), payload)
    assert code == 2

    err = capsys.readouterr().err
    assert "application/app.py" in err                # WHICH file was wrong
    assert "沒有任何變更" in err                        # and that we broke nothing
    final = store_of(tree).load()
    assert final.pending is None and final.current == "v1"
    installed = tree / "apps" / APP / "versions" / "v2"
    assert not integrity.is_complete(installed)       # no sentinel for a bad tree
    assert not list((tree / "apps" / APP / "staging").iterdir())


def test_install_stages_a_missing_runtime_from_the_payload(tree, tmp_path):
    payload = make_payload(tmp_path, "v2", FP2, with_runtime=True)
    assert bootstrap.install_payload(paths_of(tree), payload) == 0
    assert runtime_store.RuntimeStore(tree / "deps").is_complete(FP2)


def test_install_refuses_the_same_revision_that_already_failed(tree, tmp_path, capsys):
    """Reinstalling the identical bytes that just crashed is a loop, not a fix."""
    store_of(tree).mutate(lambda s: state.clear_bad_pending(
        state.set_pending(s, "v2"), revision="r1"))
    payload = make_payload(tmp_path, "v2", revision="r1")

    assert bootstrap.install_payload(paths_of(tree), payload) == 2
    assert "失敗清單" in capsys.readouterr().err
    assert store_of(tree).load().pending is None

    assert bootstrap.install_payload(paths_of(tree), payload, force=True) == 0
    assert store_of(tree).load().pending == "v2"


def test_install_of_a_rebuilt_version_is_allowed_by_the_new_revision(tree, tmp_path):
    store_of(tree).mutate(lambda s: state.clear_bad_pending(
        state.set_pending(s, "v2"), revision="r1"))
    payload = make_payload(tmp_path, "v2", revision="r2-fixed", body="fixed")
    assert bootstrap.install_payload(paths_of(tree), payload) == 0
    assert store_of(tree).load().pending == "v2"


def test_install_rejects_a_folder_that_is_not_a_payload(tree, tmp_path):
    (tmp_path / "junk").mkdir()
    with pytest.raises(bootstrap.BootstrapError, match="release.json"):
        bootstrap.install_payload(paths_of(tree), tmp_path / "junk")


def test_install_works_from_a_payload_folder_the_operator_renamed(tree, tmp_path):
    """--install rebuilt the payload path as <given>.parent / <app_id>, so it only
    worked while the exported folder was still literally named <app_id>. Renaming
    it to 「v1.1.0更新包」 — an entirely human thing to do before copying it to a
    stick — sent the provider looking for <parent>\\demo\\release.json, a path that
    has never existed on that machine, and the install failed with it."""
    payload = make_payload(tmp_path, "v2", FP2, with_runtime=True)
    renamed = payload.parent / "v1.1.0更新包"
    payload.rename(renamed)

    assert bootstrap.install_payload(paths_of(tree), renamed) == 0
    assert store_of(tree).load().pending == "v2"
    # versions/ AND runtimes/ must resolve against the folder we were handed.
    assert integrity.is_complete(tree / "apps" / APP / "versions" / "v2")
    assert runtime_store.RuntimeStore(tree / "deps").is_complete(FP2)


def test_install_works_from_a_copy_of_the_payload_left_at_the_drive_root(tree, tmp_path):
    """The other half of the same bug: the operator copies the payload folder to
    D:\\ (or a second stick), so its parent is a drive root with no <app_id> in it."""
    payload = make_payload(tmp_path, "v2")
    import shutil
    copied = tmp_path / "elsewhere" / "更新包"
    copied.parent.mkdir()
    shutil.copytree(payload, copied)

    assert bootstrap.install_payload(paths_of(tree), copied) == 0
    assert store_of(tree).load().pending == "v2"


# ── --set-update-source ──────────────────────────────────────────────────────

def test_set_update_source_writes_config_and_is_read_back(tree, tmp_path, capsys):
    """The ONLY way an already-deployed machine can be pointed at a share:
    config.json used to be written on the build machine and never again, so
    background_check() found no update_source and auto-update never fired."""
    share = tmp_path / "share"
    (share / APP).mkdir(parents=True)
    (share / APP / "release.json").write_text("{}", encoding="utf-8")

    assert bootstrap.set_update_source(paths_of(tree), str(share)) == 0
    assert paths_of(tree).config()["update_source"] == str(share)
    assert "更新來源已設定" in capsys.readouterr().out


def test_set_update_source_preserves_other_config_keys(tree, tmp_path):
    config = tree / "apps" / APP / "config.json"
    config.write_text(json.dumps({"telemetry": False}), encoding="utf-8")
    bootstrap.set_update_source(paths_of(tree), str(tmp_path / "share"))
    data = json.loads(config.read_text("utf-8"))
    assert data["telemetry"] is False and data["update_source"]


def test_set_update_source_warns_but_accepts_an_offline_share(tree, capsys):
    """A UNC path that is not reachable right now (VPN down) is normal."""
    assert bootstrap.set_update_source(paths_of(tree), r"\\fileserver\updates") == 0
    out = capsys.readouterr().out
    assert "[注意]" in out and "連不上" in out
    assert paths_of(tree).config()["update_source"] == r"\\fileserver\updates"


def test_set_update_source_accepts_a_unc_share_when_windows_stat_raises(
        tree, capsys, monkeypatch):
    """Disconnected UNC roots may raise WinError 53/64/67 instead of returning False.

    The share is still a useful update source: the VPN/server can come back later.
    Persist it and warn; never turn a routine network outage into a traceback.
    """
    original_stat = Path.stat

    def offline_unc(path, *args, **kwargs):
        if str(path).startswith(r"\\server\share"):
            raise OSError(64, "The specified network name is no longer available")
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", offline_unc)
    assert bootstrap.set_update_source(paths_of(tree), r"\\server\share") == 0
    out = capsys.readouterr().out
    assert "[注意]" in out and "連不上" in out
    assert paths_of(tree).config()["update_source"] == r"\\server\share"


def test_set_update_source_rejects_a_file(tree, tmp_path):
    target = tmp_path / "notadir.txt"
    target.write_text("x", encoding="utf-8")
    with pytest.raises(bootstrap.BootstrapError, match="資料夾"):
        bootstrap.set_update_source(paths_of(tree), str(target))


# ── rollback ─────────────────────────────────────────────────────────────────

def test_rollback_target_is_never_the_version_we_are_fleeing():
    """A version became last_known_good the moment it printed one healthy marker.
    A build that starts and THEN misbehaves was therefore its own rollback target,
    and 'rolling back' relaunched exactly the same thing."""
    broken = state.AppState(app_id=APP, current="v2", previous="v1",
                            last_known_good="v2")
    assert broken.rollback_target() == "v1"


def test_rollback_target_skips_versions_already_known_to_fail():
    s = state.AppState(app_id=APP, current="v3", previous="v2",
                       last_known_good="v2",
                       failed_versions=[{"version": "v2", "revision": None}])
    assert s.rollback_target() is None


def test_manual_rollback_marks_the_version_it_left_as_failed(tree, capsys):
    """Otherwise the background updater sees the same release on the share, finds
    nothing in failed_versions, and re-stages the build we just fled — same day."""
    make_version(tree, "v2", body="two")
    store_of(tree).mutate(state.commit_candidate)                 # v1 = LKG
    store_of(tree).mutate(lambda s: state.set_pending(s, "v2"))
    store_of(tree).mutate(state.promote_pending)                  # current=v2
    store_of(tree).mutate(state.commit_candidate)                 # v2 = LKG too

    assert bootstrap.rollback_now(paths_of(tree)) == 0
    final = store_of(tree).load()
    assert final.current == "v1"
    assert final.is_failed("v2")                  # <- the whole point
    assert final.last_known_good != "v2"
    assert "已從 v2 退回到 v1" in capsys.readouterr().out


def test_rollback_falls_back_to_any_intact_version_left_on_disk(tree):
    """LKG and previous both unusable, but v0 is sitting right there, complete."""
    import shutil
    make_version(tree, "v0", body="zero")
    make_version(tree, "v2", body="two")
    store_of(tree).mutate(lambda s: state.set_pending(s, "v2"))
    store_of(tree).mutate(state.promote_pending)      # current=v2, previous=v1
    # v1 (the only recorded fallback) is gone, and LKG was never set.
    shutil.rmtree(tree / "apps" / APP / "versions" / "v1")

    current = store_of(tree).load()
    assert bootstrap.resolve_rollback_target(paths_of(tree), current) == "v0"
    assert bootstrap.rollback_now(paths_of(tree)) == 0
    assert store_of(tree).load().current == "v0"


def test_rollback_with_nothing_left_fails_loudly_instead_of_succeeding(tree, capsys):
    """It used to return 0 and print '已退回' when there was nothing to go back to."""
    code = bootstrap.rollback_now(paths_of(tree))     # only v1 exists, it IS current
    assert code != 0
    assert "沒有任何" in capsys.readouterr().err
    assert store_of(tree).load().current == "v1"


def test_rollback_never_rolls_forward_onto_a_version_that_never_booted(tree, capsys):
    """The bug this exists to keep dead: with no LKG and no previous, the disk scan
    took "newest intact version" — so `--rollback` on a machine running a working
    v1.0.0 with v1.1.0 merely STAGED would jump forward onto the unproven build and
    mark the working one failed. Refusing is strictly better than that."""
    make_version(tree, "v2", body="two")
    store_of(tree).mutate(lambda s: state.set_pending(s, "v2"))   # staged, never booted

    assert bootstrap.resolve_rollback_target(paths_of(tree), store_of(tree).load()) is None
    assert bootstrap.rollback_now(paths_of(tree)) != 0
    after = store_of(tree).load()
    assert after.current == "v1"                    # still on the version that works
    assert not after.is_failed("v1")                # and it was NOT blamed
    assert "退到那裡不叫退回" in capsys.readouterr().err


def test_rollback_to_an_explicit_version(tree, capsys):
    make_version(tree, "v0", body="zero")
    make_version(tree, "v2", body="two")
    store_of(tree).mutate(lambda s: state.set_pending(s, "v2"))
    store_of(tree).mutate(state.promote_pending)      # current=v2

    assert bootstrap.rollback_to_version(paths_of(tree), "v0") == 0
    final = store_of(tree).load()
    assert final.current == "v0" and final.previous == "v2"
    assert final.is_failed("v2")
    assert "已從 v2 退回到 v0" in capsys.readouterr().out


def test_rollback_to_a_failed_version_needs_force(tree, capsys):
    make_version(tree, "v2", body="two")
    store_of(tree).mutate(lambda s: state.set_pending(s, "v2"))
    store_of(tree).mutate(state.promote_pending)
    store_of(tree).mutate(lambda s: state.rollback_to(s, "v1", revision="r1"))
    assert store_of(tree).load().is_failed("v2")

    assert bootstrap.rollback_to_version(paths_of(tree), "v2") == 2
    assert "失敗清單" in capsys.readouterr().err
    assert store_of(tree).load().current == "v1"

    assert bootstrap.rollback_to_version(paths_of(tree), "v2", force=True) == 0
    assert store_of(tree).load().current == "v2"


def test_rollback_to_a_missing_or_incomplete_version_is_refused(tree, capsys):
    make_version(tree, "v9", complete=False)          # half-copied: no sentinel
    assert bootstrap.rollback_to_version(paths_of(tree), "v9") == 2
    assert bootstrap.rollback_to_version(paths_of(tree), "v404") == 2
    assert store_of(tree).load().current == "v1"


def test_rollback_to_current_is_a_no_op(tree):
    assert bootstrap.rollback_to_version(paths_of(tree), "v1") == 1


# ── what the machine SAYS about a rollback it already did ────────────────────
#
# Everything below is read down a phone by somebody trying to work out what happened
# to a machine overnight. The mechanism was right and the story was not.

def test_fail_candidate_moves_previous_to_the_version_it_rolled_away_from():
    """`previous` is 'the version we were running before this one'. fail_candidate()
    was the only transition that changed `current` without moving it, so after an
    automatic rollback current and previous were THE SAME VERSION."""
    s = state.fail_candidate(
        make_state(current="v2", candidate="v2", previous="v1", last_known_good="v1"),
        revision="r1")

    assert s.current == "v1"
    assert s.previous == "v2"          # the failed candidate: where we came FROM
    assert s.previous != s.current     # …and never the version we are standing on


def auto_rollback(tree, monkeypatch):
    """The factory-floor event: v1 is proven, v2 is promoted, v2 dies on its first
    start, the machine rolls itself back to v1 without anyone touching it."""
    arm_candidate(tree)                                  # v1 = LKG, v2 staged
    monkeypatch.setattr(bootstrap.time, "sleep", lambda _s: None)
    popen = popen_factory([dict(healthy=False, exit_code=bootstrap.EXIT_APP_FAILURE),
                           dict(healthy=True)])
    assert bootstrap.start_app(paths_of(tree), [], notify=lambda *a: None,
                               popen=popen) == 0
    final = store_of(tree).load()
    assert final.current == "v1" and final.is_failed("v2")     # the rollback happened
    return final


def test_status_never_prints_the_same_version_as_both_current_and_previous(
        tree, monkeypatch, capsys):
    """「目前版本 v1.0.0 / 上一版 v1.0.0」 — the same version in two fields, read down a
    phone to an admin trying to understand what the machine did last night. The two
    fields have to describe two different things or neither of them means anything."""
    auto_rollback(tree, monkeypatch)
    capsys.readouterr()                                  # drop the launch chatter

    assert bootstrap.print_status(paths_of(tree)) == 0
    out = capsys.readouterr().out
    lines = {line.split(":", 1)[0].strip(): line.split(":", 1)[1].strip()
             for line in out.splitlines() if ":" in line}

    assert lines["目前版本"].startswith("v1")
    assert lines["上一版"].startswith("v2")              # where we came FROM
    assert lines["目前版本"].split()[0] != lines["上一版"].split()[0]
    out.encode("cp950")


def test_status_says_what_happened_to_this_machine_last_night(tree, monkeypatch, capsys):
    """state.json has recorded last_operation since the first commit — and --status
    read every other field and skipped the only one that answers the question the
    operator is actually phoning about."""
    auto_rollback(tree, monkeypatch)
    capsys.readouterr()

    bootstrap.print_status(paths_of(tree))
    out = capsys.readouterr().out

    assert "最後動作" in out
    assert "自動退版" in out                              # not 「rollback」, and not silence
    assert "v2 → v1" in out                              # WHICH versions, in which direction
    # …and when, on the wall clock of the room the machine is in.
    assert re.search(r"自動退版\(v2 → v1\),\d{4}-\d{2}-\d{2} \d{2}:\d{2}", out), out
    out.encode("cp950")


def test_a_second_rollback_never_lands_on_the_version_that_just_failed(
        tree, monkeypatch, capsys):
    """`previous` now points AT the failed candidate, so both rollback resolvers are
    one is_failed() check away from marching the machine straight back onto the build
    it just fled. They must not."""
    auto_rollback(tree, monkeypatch)                     # current=v1, previous=v2(failed)
    capsys.readouterr()
    after = store_of(tree).load()
    assert after.previous == "v2" and after.is_failed("v2")

    # Neither resolver may offer it — state-only or checked against the disk.
    assert after.rollback_target() is None
    assert bootstrap.resolve_rollback_target(paths_of(tree), after) is None

    assert bootstrap.rollback_now(paths_of(tree)) == 0   # nothing to do, and that is fine
    final = store_of(tree).load()
    assert final.current == "v1"                         # NOT back onto the broken v2
    assert final.generation == after.generation          # and nothing was written at all


def test_being_on_the_last_known_good_is_not_an_error_and_never_recommends_the_failed_build(
        tree, monkeypatch, capsys):
    """--rollback after an automatic rollback. The machine is not broken — it is on the
    last version that worked, which is what the earlier rollback was FOR. It used to
    print [ERROR], three guessed reasons, and 「--rollback-to <版本> --force」 aimed at
    the list of other versions on the disk, which consists of exactly the build that
    just failed. Forcing the machine back onto that is the one action guaranteed to
    stop the line again."""
    auto_rollback(tree, monkeypatch)
    capsys.readouterr()

    code = bootstrap.rollback_now(paths_of(tree))
    captured = capsys.readouterr()

    assert code == 0                                     # not a failure: it worked
    assert "[ERROR]" not in captured.out + captured.err
    assert "不需要、也不能再退" in captured.out
    assert "v1 就是最後一個確認可用的版本" in captured.out
    assert "v2 啟動失敗" in captured.out                  # and WHY we are here
    assert "--force" not in captured.out + captured.err   # never aim it at a failed build
    (captured.out + captured.err).encode("cp950")


def test_a_genuine_dead_end_is_still_an_error(tree, capsys):
    """The trap in the fix above: 「你已經在最好的版本上了」 must not swallow 「無路可退」.
    A machine whose current version has never once started successfully has no LKG, and
    that is a real failure with a real next step."""
    code = bootstrap.rollback_now(paths_of(tree))        # v1 is current, LKG is None
    err = capsys.readouterr().err
    assert code != 0
    assert "沒有任何" in err and "--install" in err


# ── launcher exit-code contract ──────────────────────────────────────────────

class FakeLauncher:
    """`healthy` writes the marker the way launch.py does once its WINDOW is up.

    The marker's BODY is the whole contract (see bootstrap's module docstring):

      session=True   the user pressed Start, so the app was really asked to run and
                     the marker carries the app's URL. This — and only this — is
                     what may promote a candidate to last-known-good.
      session=False  the user opened the window, looked at the portal, and closed it
                     without pressing Start. The marker says "no-session": Streamlit's
                     server ran, the app's own script did not. Nothing to promote,
                     nothing to blame.

    `revoke` deletes it again on the way out — exactly what the real launcher's
    _revoke_marker() does when the app it was hosting dies.
    """

    def __init__(self, env, *, healthy: bool, exit_code: int = 0, polls: int = 1,
                 revoke: bool = False, session: bool = True):
        self.pid = 4242
        self.returncode = None
        self._exit_code = exit_code
        self._polls_left = polls
        self._revoke = revoke
        self.marker = Path(env["CIM_HEALTHY_MARKER"])
        if healthy:
            self.marker.parent.mkdir(parents=True, exist_ok=True)
            self.marker.write_text(
                "http://127.0.0.1:9999" if session else bootstrap.MARKER_NO_SESSION,
                encoding="utf-8")

    def poll(self):
        if self._polls_left > 0:
            self._polls_left -= 1
            return None
        if self._revoke:
            self.marker.unlink(missing_ok=True)
        self.returncode = self._exit_code
        return self.returncode


def popen_factory(script):
    calls = []

    def popen(cmd, cwd=None, env=None):
        calls.append([str(c) for c in cmd])
        return FakeLauncher(env, **script.pop(0))

    popen.calls = calls
    return popen


def arm_candidate(tree):
    """v1 proven (LKG), v2 promoted and unproven — the classic update moment."""
    make_version(tree, "v2", body="two")
    store_of(tree).mutate(state.commit_candidate)
    store_of(tree).mutate(lambda s: state.set_pending(s, "v2"))


@pytest.mark.parametrize("code", [bootstrap.EXIT_APP_FAILURE,
                                  bootstrap.EXIT_VERSION_INTEGRITY])
def test_version_specific_exits_roll_back_and_blame_the_version(tree, monkeypatch, code):
    arm_candidate(tree)
    monkeypatch.setattr(bootstrap.time, "sleep", lambda _s: None)
    popen = popen_factory([dict(healthy=False, exit_code=code), dict(healthy=True)])

    result = bootstrap.start_app(paths_of(tree), [], notify=lambda *a: None, popen=popen)
    assert result == 0
    assert len(popen.calls) == 2                       # v2 tried, v1 relaunched
    final = store_of(tree).load()
    assert final.current == "v1" and final.is_failed("v2")


def test_shell_environment_exit_does_not_touch_state_or_claim_a_rollback(
        tree, monkeypatch, capsys):
    """Exit 5 = the shared Tauri shell / WebView2 could not start. Rolling back
    runs the SAME shell against the SAME missing WebView2 and fails identically —
    while telling the user we 'restored the previous version'. Do neither."""
    arm_candidate(tree)
    monkeypatch.setattr(bootstrap.time, "sleep", lambda _s: None)
    notified = []
    popen = popen_factory([dict(healthy=False, exit_code=bootstrap.EXIT_SHELL_ENVIRONMENT)])

    result = bootstrap.start_app(paths_of(tree), [], notify=lambda *a: notified.append(a),
                                 popen=popen)
    assert result == bootstrap.EXIT_SHELL_ENVIRONMENT
    assert len(popen.calls) == 1                       # no relaunch of an older version
    assert notified == []                              # nobody was told "已恢復前一版本"

    final = store_of(tree).load()
    assert final.current == "v2"                       # state untouched
    assert final.failed_versions == []                 # v2 is NOT blamed
    err = capsys.readouterr().err
    assert "WebView2" in err and "防毒" in err
    assert "退回舊版也不會好" in err


# ── what actually COMMITS a candidate to last-known-good ─────────────────────
#
# The healthy marker appearing is NOT it. launch.py writes the marker once its
# window has survived ~12 seconds, but the Streamlit app script only runs when the
# user presses Start — minutes later. commit_candidate() fired on the marker, so:
#
#   * a build that came up and then died was latched as last_known_good BEFORE it
#     died, and stayed the machine's idea of "the version to fall back to"; and
#   * commit_candidate() had cleared `candidate`, so start_app's post-exit guard
#     (`refreshed.candidate != version`) was always true and the ENTIRE rollback
#     block was skipped — the version that just failed was neither marked failed
#     nor rolled back.
#
# The commit signal is the process EXITING CLEANLY with the marker still present.

def test_a_marker_seen_mid_session_does_not_latch_a_broken_version_as_last_known_good(
        tree, monkeypatch):
    """S3 blocker. Window up (marker written), user presses Start minutes later, the
    app dies, launch.py revokes the marker and exits 3. The broken build must not be
    last_known_good, and the rollback must actually happen."""
    arm_candidate(tree)                                # v1 = LKG, v2 = pending
    monkeypatch.setattr(bootstrap.time, "sleep", lambda _s: None)
    notified = []
    popen = popen_factory([
        dict(healthy=True, exit_code=bootstrap.EXIT_APP_FAILURE, revoke=True),  # v2
        dict(healthy=True),                                                     # v1 again
    ])

    code = bootstrap.start_app(paths_of(tree), [], notify=lambda *a: notified.append(a),
                               popen=popen)
    assert code == 0
    assert len(popen.calls) == 2                       # v2 died, v1 was relaunched

    final = store_of(tree).load()
    assert final.is_failed("v2")                       # the broken build IS blamed
    assert final.last_known_good == "v1"               # and is NOT the fallback
    assert final.current == "v1" and final.candidate is None
    assert notified                                    # the user was told about it


def test_a_version_that_dies_after_going_healthy_rolls_back_even_with_the_marker_left(
        tree, monkeypatch):
    """The harder half: the app crashes so hard the launcher never gets to delete the
    marker, so it is STILL on disk at exit. A leftover marker must not save a version
    that exited 3 — the exit code decides, the marker only corroborates a clean one."""
    arm_candidate(tree)
    monkeypatch.setattr(bootstrap.time, "sleep", lambda _s: None)
    popen = popen_factory([
        dict(healthy=True, exit_code=bootstrap.EXIT_VERSION_INTEGRITY, revoke=False),
        dict(healthy=True),
    ])

    assert bootstrap.start_app(paths_of(tree), [], notify=lambda *a: None,
                               popen=popen) == 0
    final = store_of(tree).load()
    assert final.current == "v1" and final.is_failed("v2")
    assert final.last_known_good == "v1"


def test_the_marker_alone_writes_no_state_while_the_app_is_still_running(tree, monkeypatch):
    """The marker is EVIDENCE that a window appeared, nothing more. Nothing may be
    committed while the app is up — the session is not over, and most versions die
    after the marker, not before it."""
    arm_candidate(tree)
    monkeypatch.setattr(bootstrap.time, "sleep", lambda _s: None)
    snapshots = []

    class Watcher(FakeLauncher):
        def poll(self):
            snapshots.append(store_of(tree).load())    # what state looks like mid-session
            return super().poll()

    calls = []

    def popen(cmd, cwd=None, env=None):
        calls.append([str(c) for c in cmd])
        if len(calls) == 1:                            # the candidate's own session
            return Watcher(env, healthy=True, polls=3,
                           exit_code=bootstrap.EXIT_APP_FAILURE, revoke=True)
        return FakeLauncher(env, healthy=True)         # the v1 relaunch afterwards

    bootstrap.start_app(paths_of(tree), [], notify=lambda *a: None, popen=popen)

    assert len(snapshots) >= 3                         # we really did watch it run
    assert all(s.current == "v2" and s.candidate == "v2" and s.last_known_good == "v1"
               for s in snapshots), "state was mutated while the app was still up"


def test_a_clean_exit_with_the_marker_still_there_commits_the_candidate(tree, monkeypatch):
    """The other side of the line: the user used the app and closed the window. THAT
    is what proves a version, and it must still commit it as last-known-good."""
    arm_candidate(tree)
    monkeypatch.setattr(bootstrap.time, "sleep", lambda _s: None)
    popen = popen_factory([dict(healthy=True, exit_code=bootstrap.EXIT_OK)])

    assert bootstrap.start_app(paths_of(tree), [], notify=lambda *a: None,
                               popen=popen) == 0
    assert len(popen.calls) == 1                       # nothing was rolled back

    final = store_of(tree).load()
    assert final.last_known_good == "v2" and final.candidate is None
    assert final.current == "v2" and final.failed_versions == []


# ── THE BLOCKER: "I opened it, looked at it, and closed it" is not a proven version ──
#
# Streamlit does not execute the app script until a session opens, and a session opens
# only when the user presses Start in the portal. So the most ordinary thing a user can
# do — open the app, glance at the portal, close the window — produced exit 0 + a marker,
# and bootstrap committed a version that had NEVER EXECUTED A LINE as last-known-good.
# If that build was broken, the next launch died and the version we "rolled back" to was
# the same broken build. The automatic-rollback promise was dead on the daily path.
#
# The fix: the marker has a BODY, and only "the app was actually asked to run" promotes.

def test_a_version_the_user_never_pressed_start_on_is_not_committed_as_last_known_good(
        tree, monkeypatch):
    """THE reproduction, at the bootstrap end. Window opens, the user never presses
    Start, they close it: exit 0 with a marker that says "no-session".

    It must NOT become last_known_good — it has proved nothing.
    It must NOT be failed either — the user simply did not use it. It stays the
    candidate and is tried again next launch."""
    arm_candidate(tree)                                # v1 = LKG, v2 = the candidate
    monkeypatch.setattr(bootstrap.time, "sleep", lambda _s: None)
    notified = []
    popen = popen_factory([dict(healthy=True, session=False, exit_code=bootstrap.EXIT_OK)])

    code = bootstrap.start_app(paths_of(tree), [], notify=lambda *a: notified.append(a),
                               popen=popen)

    assert code == 0
    assert len(popen.calls) == 1                       # nothing was rolled back or relaunched

    final = store_of(tree).load()
    assert final.last_known_good == "v1", \
        "a version whose app never ran was stamped last-known-good — the BLOCKER"
    assert final.candidate == "v2"                     # still on trial…
    assert final.current == "v2"
    assert final.failed_versions == []                 # …but NOT blamed: they just did not use it
    assert notified == []                              # and no false 「已恢復前一版本」


def test_never_pressing_start_is_not_an_app_failure_and_never_rolls_back(tree, monkeypatch):
    """The trap next door: having stopped committing on "no-session", it would be very
    easy to let it fall through to the exit-0-with-no-marker branch, which is treated as
    EXIT_APP_FAILURE. That would roll the machine back because the user did not feel like
    using the app today. Neither promote nor blame: touch nothing."""
    arm_candidate(tree)
    monkeypatch.setattr(bootstrap.time, "sleep", lambda _s: None)
    popen = popen_factory([dict(healthy=True, session=False, exit_code=bootstrap.EXIT_OK)])

    outcome = bootstrap.run_version(
        paths_of(tree), store_of(tree), runtime_store.RuntimeStore(tree / "deps"),
        "v2", [], is_candidate=True, notify=lambda *a: None, popen=popen)

    assert outcome.code == bootstrap.EXIT_OK           # NOT EXIT_APP_FAILURE
    assert outcome.marker_at_exit is True              # a window did come up…
    assert outcome.app_ran is False                    # …but the app was never asked to run
    assert store_of(tree).load().last_known_good == "v1"


def test_a_version_the_user_did_press_start_on_is_still_committed(tree, monkeypatch):
    """The other side of the line, or the safety net has simply been disabled: the user
    pressed Start, the app ran, they closed the window. THAT proves a version."""
    arm_candidate(tree)
    monkeypatch.setattr(bootstrap.time, "sleep", lambda _s: None)
    popen = popen_factory([dict(healthy=True, session=True, exit_code=bootstrap.EXIT_OK)])

    assert bootstrap.start_app(paths_of(tree), [], notify=lambda *a: None,
                               popen=popen) == 0

    final = store_of(tree).load()
    assert final.last_known_good == "v2" and final.candidate is None


def test_the_marker_body_bootstrap_reads_is_the_one_the_launcher_writes(tree):
    """Two files, delivered together, that must never disagree about one string. If the
    launcher writes "no-session" and bootstrap compares against "nosession", every
    version that was merely looked at gets committed again and the BLOCKER is back."""
    launch_py = (Path(__file__).resolve().parents[1] / "src" / "provision_builder" /
                 "streamlit_desktop" / "templates" / "launch.py").read_text(encoding="utf-8")
    assert f'MARKER_NO_SESSION = "{bootstrap.MARKER_NO_SESSION}"' in launch_py


def test_a_clean_exit_that_never_showed_a_marker_is_still_a_failure(tree, monkeypatch):
    """Exit 0 with no marker: nothing ever came up. A 0 here is a lie, and committing
    on it would make a version that cannot even open a window last-known-good."""
    arm_candidate(tree)
    monkeypatch.setattr(bootstrap.time, "sleep", lambda _s: None)
    popen = popen_factory([dict(healthy=False, exit_code=bootstrap.EXIT_OK),
                           dict(healthy=True)])

    assert bootstrap.start_app(paths_of(tree), [], notify=lambda *a: None,
                               popen=popen) == 0
    final = store_of(tree).load()
    assert final.current == "v1" and final.is_failed("v2")
    assert final.last_known_good == "v1"


def test_killing_a_working_app_from_task_manager_does_not_blame_the_version(
        tree, monkeypatch, capsys):
    """Our launcher only ever chooses 3, 4 or 5. An UNKNOWN non-zero code (1 is what
    Task Manager's End Task leaves behind) after a healthy window means something
    OUTSIDE ended the process — a kill, a power event, a hard crash of the shell.

    failed_versions is a destructive, sticky verdict: the background updater refuses
    to re-stage anything in it, and only --clear-failed takes it back out. 'The user
    killed a window that had been up and working' is not evidence against the build,
    so we do not spend it. Nor do we commit: it was not a clean exit, so the LKG
    promotion has not been earned — the version stays ON TRIAL."""
    arm_candidate(tree)                                # v1 = LKG, v2 = pending
    monkeypatch.setattr(bootstrap.time, "sleep", lambda _s: None)
    notified = []
    popen = popen_factory([dict(healthy=True, exit_code=1, revoke=False)])

    code = bootstrap.start_app(paths_of(tree), [], notify=lambda *a: notified.append(a),
                               popen=popen)
    assert code == 1
    assert len(popen.calls) == 1                       # nothing was rolled back

    final = store_of(tree).load()
    assert not final.is_failed("v2")                   # NOT blamed…
    assert final.failed_versions == []
    assert final.current == "v2"                       # …not rolled back…
    assert final.candidate == "v2"                     # …still on trial…
    assert final.last_known_good == "v1"               # …and NOT promoted either
    assert notified == []                              # no false 「已恢復前一版本」

    out = capsys.readouterr().out
    assert "非正常結束" in out and "工作管理員" in out   # one honest line about it
    assert "沒有把它標記為失敗" in out
    out.encode("cp950")


def test_an_unknown_exit_from_a_window_that_never_came_up_is_still_the_versions_fault(
        tree, monkeypatch):
    """The other half of the same rule: an unknown code with NO marker means the app
    died before it could even open a window. We cannot prove that was environmental,
    and it is exactly what a bad build looks like — so it is still a 3."""
    arm_candidate(tree)
    monkeypatch.setattr(bootstrap.time, "sleep", lambda _s: None)
    popen = popen_factory([dict(healthy=False, exit_code=1), dict(healthy=True)])

    assert bootstrap.start_app(paths_of(tree), [], notify=lambda *a: None,
                               popen=popen) == 0
    assert len(popen.calls) == 2                       # rolled back and relaunched
    final = store_of(tree).load()
    assert final.current == "v1" and final.is_failed("v2")


# ── a version nobody will roll back for you must at least SAY so ─────────────

def test_a_broken_non_candidate_tells_the_operator_which_button_to_press(
        tree, monkeypatch, capsys):
    """Automatic rollback only fires for a CANDIDATE. A version that is not on trial —
    most commonly the FIRST BOOT of a fresh delivery, whose state.json has
    candidate=None — fails and nobody comes. We used to return the code in silence, so
    the operator sat in front of a window that would not open while 讀我 promised them
    an automatic recovery that was never going to happen."""
    monkeypatch.setattr(bootstrap.time, "sleep", lambda _s: None)
    make_version(tree, "v2", body="two")
    # exactly what store_builder._write_target_state() hands a factory machine:
    # current=v2, previous=v1, candidate=None, last_known_good=None.
    store_of(tree).mutate(lambda s: state.dataclasses.replace(
        s, current="v2", previous="v1", candidate=None, last_known_good=None))
    popen = popen_factory([dict(healthy=False, exit_code=bootstrap.EXIT_APP_FAILURE)])

    code = bootstrap.start_app(paths_of(tree), [], notify=lambda *a: None, popen=popen)

    assert code == bootstrap.EXIT_APP_FAILURE
    assert len(popen.calls) == 1                   # no automatic rollback happened…
    err = capsys.readouterr().err
    assert "這一版壞了" in err                      # …so SAY so, and say what to do
    assert "tools\\admin.bat" in err and "[2] 退回上一版" in err
    assert "--rollback" in err                     # the CLI route, for a remote operator
    assert "v1" in err                             # and name the version it would land on
    err.encode("cp950")                            # a zh-TW console must be able to print it


def test_a_first_boot_with_nowhere_to_roll_back_to_says_how_to_get_the_line_running(
        tree, monkeypatch, capsys):
    """THE dead end, and it is now reachable: store_builder ships state.json with
    candidate=current, so a one-version delivery IS on trial at its first boot — on a
    machine that has no previous version by definition. When that boot fails there is
    nothing to roll back to.

    「啟動失敗,而且沒有任何可以退回的版本」 is honest and useless: it leaves a line
    operator in front of a dead machine with no next step. There IS one — put a working
    version on from the USB stick — and it is the only thing that gets the line running
    again."""
    monkeypatch.setattr(bootstrap.time, "sleep", lambda _s: None)
    # exactly a fresh single-version delivery: on trial, and no previous.
    store_of(tree).mutate(lambda s: state.dataclasses.replace(
        s, candidate="v1", previous=None, last_known_good=None))
    popen = popen_factory([dict(healthy=False, exit_code=bootstrap.EXIT_APP_FAILURE)])

    code = bootstrap.start_app(paths_of(tree), [], notify=lambda *a: None, popen=popen)

    assert code == bootstrap.EXIT_APP_FAILURE
    assert len(popen.calls) == 1                    # there was nothing to relaunch
    err = capsys.readouterr().err
    assert "沒有任何可以退回的版本" in err            # what failed…
    assert "tools\\admin.bat" in err and "[3] 套用已複製進來的更新包" in err   # …and what to DO
    assert "--install" in err                      # the CLI route
    err.encode("cp950")


def test_a_non_candidate_killed_from_task_manager_is_not_called_a_broken_version(
        tree, monkeypatch, capsys):
    """The trap in the fix above. 'Not a candidate' and 'exit code 1' are both true when
    a user End-Tasks a perfectly good, already-proven version — and telling them
    「這一版壞了。請退回上一版」 for that would talk them into downgrading a working
    machine. An unknown code after a window that came up is an abnormal session, whoever
    the version is."""
    monkeypatch.setattr(bootstrap.time, "sleep", lambda _s: None)
    store_of(tree).mutate(state.commit_candidate)     # v1 is proven: NOT a candidate
    popen = popen_factory([dict(healthy=True, exit_code=1, revoke=False)])

    code = bootstrap.start_app(paths_of(tree), [], notify=lambda *a: None, popen=popen)

    assert code == 1
    out, err = capsys.readouterr()
    assert "這一版壞了" not in err                    # not blamed…
    assert "非正常結束" in out                        # …reported honestly instead
    assert store_of(tree).load().failed_versions == []


# ── one app, one instance; and logs that do not grow forever ─────────────────

def test_a_second_start_does_not_launch_a_second_instance(tree, monkeypatch, capsys):
    """Double-clicking start.bat twice started a second EVERYTHING: a second launcher,
    a second Streamlit on a second port, two processes writing one state.json. The
    store lock did not stop it — locks.acquire() WAITS 30s and then proceeds anyway.

    The second copy must say「已經在執行中」and leave, WITHOUT spawning a launcher."""
    monkeypatch.setattr(bootstrap.time, "sleep", lambda _s: None)
    paths = paths_of(tree)
    paths.ensure_data_dirs()
    held = locks.acquire_single_instance(paths.data_dir)   # a live first instance
    try:
        popen = popen_factory([])                          # …must never be called
        code = bootstrap.start_app(paths, [], notify=lambda *a: None, popen=popen)
    finally:
        held.release()

    assert code == bootstrap.EXIT_OK                       # NOT a failure
    assert popen.calls == [], "a second launcher was spawned"
    out = capsys.readouterr().out
    assert "已經在執行中" in out
    out.encode("cp950")


def test_a_second_start_never_marks_the_version_failed_or_applies_the_update(
        tree, monkeypatch):
    """Two traps next door.

    Exit 0 is not decoration: any non-zero code here would be read by the rest of this
    module as a failed launch — blame the version, mark it failed, roll the machine
    back — because a user double-clicked an icon.

    And the second copy must bail BEFORE promote_if_pending: applying a staged update
    behind the back of the instance that is already running would swap the version out
    from under a live session."""
    arm_candidate(tree)                                    # v1 running, v2 staged (pending)
    monkeypatch.setattr(bootstrap.time, "sleep", lambda _s: None)
    paths = paths_of(tree)
    paths.ensure_data_dirs()
    held = locks.acquire_single_instance(paths.data_dir)
    try:
        code = bootstrap.start_app(paths, [], notify=lambda *a: None,
                                   popen=popen_factory([]))
    finally:
        held.release()

    assert code == bootstrap.EXIT_OK
    final = store_of(tree).load()
    assert final.failed_versions == []                     # nothing blamed
    assert final.current == "v1"                           # nothing rolled back…
    assert final.pending == "v2"                           # …and the update NOT applied
    assert final.last_known_good == "v1"                   # nothing promoted either


def test_the_instance_lock_is_released_so_the_next_launch_can_start(tree, monkeypatch):
    """Held past the session, the app could never be started again — a far worse bug
    than the one we are fixing. The lock must go back on the way out, including when
    the launch raised."""
    monkeypatch.setattr(bootstrap.time, "sleep", lambda _s: None)
    paths = paths_of(tree)
    bootstrap.start_app(paths, [], notify=lambda *a: None,
                        popen=popen_factory([dict(healthy=True)]))

    second = locks.acquire_single_instance(paths.data_dir)  # would raise if still held
    second.release()


def test_the_live_sessions_log_is_never_rotated_away(tree, monkeypatch):
    """Retention must not delete the log of the session that is starting RIGHT NOW (nor
    one still running). gc keeps anything under an hour old, whatever the count."""
    logs = tree / "apps" / APP / "data" / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    for i in range(gc_mod.LOG_KEEP_RECENT + 8):            # plenty of old launcher logs
        write_log(logs, f"launcher-old-{i:02d}.log", kb=8, age_seconds=90 * 86400 + i)
    live = logs / "launcher-live.log"                      # …and one from right now
    live.write_bytes(b"x" * 1024)

    bootstrap._rotate_logs(paths_of(tree))

    assert live.exists(), "the running session's log was deleted underneath it"
    survivors = sorted(p.name for p in logs.glob("launcher-*.log"))
    assert len(survivors) == gc_mod.LOG_KEEP_RECENT + 1    # the newest 10 + the live one
    assert "launcher-live.log" in survivors


def test_every_launch_rotates_the_logs_that_quietly_filled_the_disk(tree):
    """Nothing ever deleted a log: launcher-*, streamlit-* and bootstrap-* piled up for
    months, and on a machine that has been running since spring that is a real part of
    what fills the disk. Each family keeps its newest few; the rest go."""
    logs = tree / "apps" / APP / "data" / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    for family in ("launcher", "streamlit", "bootstrap"):
        for i in range(gc_mod.LOG_KEEP_RECENT + 5):
            write_log(logs, f"{family}-{i:02d}.log", kb=64, age_seconds=90 * 86400 + i)
    before = len(list(logs.glob("*.log")))

    bootstrap._rotate_logs(paths_of(tree))

    remaining = list(logs.glob("*.log"))
    assert len(remaining) < before, "logs were never rotated"
    for family in ("launcher", "streamlit", "bootstrap"):
        kept = [p for p in remaining if p.name.startswith(f"{family}-")]
        assert len(kept) == gc_mod.LOG_KEEP_RECENT, family


def test_rotation_is_wired_into_the_path_every_single_launch_takes(tree, monkeypatch):
    """A rotate_logs nobody calls is a fix nobody gets. It belongs where the logs are
    CREATED — _setup_logging, which runs on every launch — not only in a GC the
    operator has to remember to run."""
    called = []
    monkeypatch.setattr(bootstrap, "_rotate_logs", lambda p: called.append(p))
    monkeypatch.setattr(bootstrap.logging, "basicConfig", lambda **_kw: None)

    bootstrap._setup_logging(paths_of(tree))

    assert called, "_setup_logging does not rotate: the logs grow forever"


def test_log_rotation_never_costs_the_user_their_app(tree, monkeypatch):
    """Housekeeping is not worth a launch. An old deployment whose gc.py predates
    rotate_logs, a locked file, a permission error — none of them may stop the app
    from starting."""
    monkeypatch.setattr(bootstrap.gc_mod, "rotate_logs",
                        lambda *_a, **_k: (_ for _ in ()).throw(OSError("access denied")))
    bootstrap._rotate_logs(paths_of(tree))                 # must not raise

    monkeypatch.setattr(bootstrap.time, "sleep", lambda _s: None)
    assert bootstrap.start_app(paths_of(tree), [], notify=lambda *a: None,
                               popen=popen_factory([dict(healthy=True)])) == 0


def test_bootstrap_imports_the_stores_gc_not_pythons_garbage_collector():
    """`gc` is a BUILT-IN module name and BuiltinImporter beats sys.path, so a plain
    `import gc` in the shipped loose-file layout (start.bat runs `python bootstrap\\
    bootstrap.py`, so __package__ is empty) hands back Python's garbage collector —
    while every test, which imports bootstrap as a package, sees the real gc.py. That
    is green in CI and dead in the factory. Pin the module we actually got."""
    assert hasattr(bootstrap.gc_mod, "rotate_logs")
    assert hasattr(bootstrap.gc_mod, "LOG_KEEP_RECENT")
    assert not hasattr(bootstrap.gc_mod, "collect"),  \
        "this is Python's built-in gc, not the store's gc.py"


def test_unknown_failure_classification():
    """3/4/5 are OUR launcher's verdicts; anything else came from outside it."""
    assert bootstrap.is_unknown_failure(1)             # Task Manager / hard crash
    assert bootstrap.is_unknown_failure(-1073741819)   # access violation
    for known in (bootstrap.EXIT_OK, bootstrap.EXIT_APP_FAILURE,
                  bootstrap.EXIT_VERSION_INTEGRITY, bootstrap.EXIT_SHELL_ENVIRONMENT):
        assert not bootstrap.is_unknown_failure(known)


def test_an_environment_exit_after_a_marker_still_blames_nobody(tree, monkeypatch):
    """Exit 5 is the machine, not the version — and that stays true whether or not a
    window managed to come up first."""
    arm_candidate(tree)
    monkeypatch.setattr(bootstrap.time, "sleep", lambda _s: None)
    popen = popen_factory([dict(healthy=True, revoke=True,
                                exit_code=bootstrap.EXIT_SHELL_ENVIRONMENT)])

    code = bootstrap.start_app(paths_of(tree), [], notify=lambda *a: None, popen=popen)
    assert code == bootstrap.EXIT_SHELL_ENVIRONMENT
    assert len(popen.calls) == 1                       # no rollback, no relaunch
    final = store_of(tree).load()
    assert final.current == "v2" and final.candidate == "v2"   # still unproven
    assert final.failed_versions == [] and final.last_known_good == "v1"


# ── first-boot verification is not a hang (S4) ───────────────────────────────

def test_first_boot_prints_progress_instead_of_hashing_500_mb_in_silence(
        tree, monkeypatch, capsys):
    """ensure_verified() takes a progress callback and bootstrap passed None, so the
    first start on a factory machine deep-verified the whole shared runtime with ZERO
    output — minutes of a black window, indistinguishable from a hang, on the one
    occasion the user has never seen the product before."""
    integrity.remove_complete(tree / "deps" / "runtimes" / FP1)   # never deep-verified
    monkeypatch.setattr(bootstrap.time, "sleep", lambda _s: None)
    popen = popen_factory([dict(healthy=True)])

    assert bootstrap.start_app(paths_of(tree), [], notify=lambda *a: None,
                               popen=popen) == 0

    out = capsys.readouterr().out
    assert "正在驗證共用元件" in out
    assert "(2/2)" in out                       # x of y (python.exe + Lib/os.py), not a spinner
    assert runtime_store.RuntimeStore(tree / "deps").is_complete(FP1)
    out.encode("cp950")                         # a zh-TW console can print it


def test_verify_progress_proves_life_without_scrolling_the_console():
    """A real runtime is ~12 000 files. One line per file is not progress, it is a
    denial of service on the console — and the LAST line must always land on y/y."""
    ticks = iter(0.1 * i for i in range(100000))
    sink = io.StringIO()
    progress = bootstrap.VerifyProgress(1000, out=sink, clock=lambda: next(ticks))
    for i in range(1000):
        progress(f"Lib/site-packages/mod{i}.py")

    lines = [line for line in sink.getvalue().splitlines() if line.strip()]
    assert 3 <= len(lines) <= 120               # alive, not a waterfall
    assert "(1000/1000)" in lines[-2]           # the final count is exact
    sink.getvalue().encode("cp950")


def test_verify_progress_is_not_built_for_an_already_verified_runtime(tree):
    """Every start after the first must not even read files.json to build a callback
    it will never call."""
    rstore = runtime_store.RuntimeStore(tree / "deps")
    assert bootstrap._verify_progress(rstore, FP1) is None      # .complete already
    integrity.remove_complete(tree / "deps" / "runtimes" / FP1)
    assert bootstrap._verify_progress(rstore, FP1).total == 2


def test_exit_code_classification():
    assert bootstrap.is_version_failure(bootstrap.EXIT_APP_FAILURE)
    assert bootstrap.is_version_failure(bootstrap.EXIT_VERSION_INTEGRITY)
    assert bootstrap.is_version_failure(1)             # unknown crash: blame the version
    assert not bootstrap.is_version_failure(bootstrap.EXIT_OK)
    assert not bootstrap.is_version_failure(bootstrap.EXIT_SHELL_ENVIRONMENT)
    assert bootstrap.is_environment_failure(bootstrap.EXIT_SHELL_ENVIRONMENT)
    assert not bootstrap.is_environment_failure(bootstrap.EXIT_APP_FAILURE)


# ── a BROKEN MACHINE is not a broken version (exit 5, never 4) ───────────────
#
# deps/runtimes/<fp> and deps/shells/<fp> are SHARED by every version installed on
# the machine. When antivirus quarantines one, or a dying disk corrupts one, the
# version that trips over it is not the suspect — but RuntimeStoreError used to map
# straight to EXIT_VERSION_INTEGRITY, so bootstrap marked a good version failed,
# rolled back, told the operator 「已恢復前一版本」, and the previous version then
# failed in exactly the same way. Two versions in failed_versions, and a false story.

def run_main(tree, monkeypatch, argv, *, notified=None):
    """bootstrap.main() against a temp store — the real operator entry point.

    Popen is booby-trapped: every failure below must be caught BEFORE a launcher
    is ever started, and notify is redirected so a regression cannot pop a real
    MessageBox and hang the suite.
    """
    monkeypatch.setattr(bootstrap, "_store_root", lambda: Path(tree))
    monkeypatch.setattr(bootstrap.subprocess, "Popen",
                        lambda *a, **k: pytest.fail("launcher must not be started"))
    monkeypatch.setattr(bootstrap.notifications, "notify",
                        lambda *a: (notified if notified is not None else []).append(a))
    return bootstrap.main(["--app", APP, *argv])


def eat_shared_runtime(tree, fingerprint: str = FP1) -> None:
    """What an antivirus quarantine looks like from here: the shared runtime that
    EVERY version points at is simply gone."""
    import shutil
    shutil.rmtree(Path(tree) / "deps" / "runtimes" / fingerprint)


def test_a_missing_shared_runtime_never_marks_a_version_failed(tree, monkeypatch, capsys):
    """The S10 blocker: an environment failure blamed on the version. The pending
    build verified byte for byte; the RUNTIME under it is what went missing."""
    arm_candidate(tree)                      # v1 = LKG, v2 = a good, staged build
    eat_shared_runtime(tree)
    notified = []

    code = run_main(tree, monkeypatch, [], notified=notified)
    assert code == bootstrap.EXIT_SHELL_ENVIRONMENT     # 5 — not 4, not 2

    final = store_of(tree).load()
    assert final.failed_versions == []       # nobody is blamed for the machine
    assert final.pending == "v2"             # the good build is still armed
    assert final.current == "v1"             # nothing was promoted, nothing rolled back
    assert notified == []                    # nobody was told 「已恢復前一版本」

    err = capsys.readouterr().err
    assert "防毒" in err and "排除清單" in err          # what to DO
    assert "安裝WebView2.bat" in err
    assert "退版救不了" in err
    assert "沒有任何版本被標記為失敗" in err
    err.encode("cp950")                                # a zh-TW console can print it


def test_a_quarantined_shared_shell_does_not_fail_the_candidate(tree, monkeypatch, capsys):
    """The same defect through the other shared component. The candidate is live
    here, which is exactly the path that used to call fail_candidate() and notify()."""
    make_version(tree, "v2", body="two", shell_fp="shell-eaten")   # not in deps/shells
    store_of(tree).mutate(state.commit_candidate)                  # v1 proven
    store_of(tree).mutate(lambda s: state.set_pending(s, "v2"))
    notified = []

    code = run_main(tree, monkeypatch, [], notified=notified)
    assert code == bootstrap.EXIT_SHELL_ENVIRONMENT

    final = store_of(tree).load()
    assert final.current == "v2" and final.candidate == "v2"   # promoted, still unproven
    assert final.failed_versions == []                         # and NOT blamed
    assert notified == []                                      # no false recovery story

    err = capsys.readouterr().err
    assert "缺共用 Tauri 殼" in err
    assert "安裝WebView2.bat" in err and "排除清單" in err
    assert "沒有退回任何版本" in err


def test_a_corrupt_shared_runtime_is_the_machine_not_the_version(tree, monkeypatch, capsys):
    """A dying disk flips a byte in the SHARED runtime. Deep verification fails —
    for every version on the machine, so rolling back cannot help and must not be
    claimed. The advice has to be about the disk, not about the release."""
    arm_candidate(tree)
    rdir = tree / "deps" / "runtimes" / FP1
    integrity.remove_complete(rdir)                    # not yet deep-verified
    (rdir / "Lib" / "os.py").write_text("# CORRUPT", encoding="utf-8")
    notified = []

    code = run_main(tree, monkeypatch, [], notified=notified)
    assert code == bootstrap.EXIT_SHELL_ENVIRONMENT
    final = store_of(tree).load()
    assert final.failed_versions == [] and final.pending == "v2" and notified == []

    err = capsys.readouterr().err
    assert "驗證失敗" in err and "chkdsk" in err
    assert "退版救不了" in err


def test_a_broken_version_tree_is_still_the_version_s_fault(tree, monkeypatch):
    """The other side of the line: a files.json mismatch INSIDE
    apps/<app>/versions/<ver>/ is version-specific. It must still roll back —
    fixing exit 5 must not turn every version failure into 'blame the machine'."""
    arm_candidate(tree)
    store_of(tree).mutate(state.promote_pending)       # current=v2 (candidate)
    # THIS version's tree, not a shared one: half-installed, no sentinel.
    integrity.remove_complete(tree / "apps" / APP / "versions" / "v2")
    monkeypatch.setattr(bootstrap.time, "sleep", lambda _s: None)
    popen = popen_factory([dict(healthy=True)])        # only v1 ever gets launched

    code = bootstrap.start_app(paths_of(tree), [], notify=lambda *a: None, popen=popen)
    assert code == 0
    final = store_of(tree).load()
    assert final.current == "v1" and final.is_failed("v2")


def test_an_unwritable_install_location_says_what_to_do_instead_of_a_traceback(
        tree, monkeypatch, capsys):
    """_setup_logging() was called OUTSIDE main()'s try block, so on a read-only USB
    stick or a locked-down production PC the very first thing the product ever showed
    a line operator was an English Python traceback out of logging.FileHandler."""
    def denied(_self):
        raise PermissionError(13, "Access is denied")

    monkeypatch.setattr(paths_mod.AppPaths, "ensure_data_dirs", denied)

    code = run_main(tree, monkeypatch, ["--status"])
    assert code == bootstrap.EXIT_SHELL_ENVIRONMENT    # the machine, not a version

    err = capsys.readouterr().err
    assert "Traceback" not in err
    assert "唯讀" in err and "權限" in err              # what to DO
    assert "沒有任何版本被標記為失敗" in err
    err.encode("cp950")                                # a zh-TW console can print it


# ── the CLI surface itself ───────────────────────────────────────────────────

def test_every_new_flag_is_documented_in_help(capsys):
    with pytest.raises(SystemExit):
        bootstrap.main(["--help"])
    help_text = capsys.readouterr().out
    for flag in ("--install", "--set-update-source", "--rollback-to", "--force",
                 "--rollback", "--status", "--clear-failed"):
        assert flag in help_text, flag


def test_all_operator_output_survives_a_cp950_console(tree, tmp_path, capsys):
    """zh-TW consoles are cp950. A single U+26A0 in a message makes print() raise
    UnicodeEncodeError — and the operator gets a traceback instead of an answer."""
    payload = make_payload(tmp_path, "v2")
    bootstrap.install_payload(paths_of(tree), payload)
    bootstrap.print_status(paths_of(tree))
    bootstrap.set_update_source(paths_of(tree), r"\\server\share")
    bootstrap.rollback_now(paths_of(tree))
    gc_mod.run_gc(tree, apply=False)

    captured = capsys.readouterr()
    for stream in (captured.out, captured.err):
        stream.encode("cp950")     # raises UnicodeEncodeError if we regressed


# ── GC ───────────────────────────────────────────────────────────────────────

def test_gc_summary_is_cp950_safe(tree):
    plan = gc_mod.collect_plan(tree)
    plan.self_hosted = FP1                     # the branch that carried the U+26A0
    plan.summary().encode("cp950")


def test_gc_deletes_even_when_the_console_cannot_print(tree):
    """summary() used to be logged BEFORE the delete loop, so one UnicodeEncodeError
    meant the operator reclaimed exactly zero bytes."""
    orphan = build_runtime(tree, FP2)
    assert orphan.is_dir()

    def hostile_log(_message):
        raise UnicodeEncodeError("cp950", "x", 0, 1, "illegal multibyte sequence")

    plan = gc_mod.run_gc(tree, apply=True, log=hostile_log)
    assert {fp for fp, _p in plan.delete_runtimes} == {FP2}
    assert not orphan.exists()                 # the work happened anyway


def test_gc_reclaims_build_and_download_leftovers(tree):
    """Hundreds of MB of .staging-* trees sat under dot-names that every other
    scan skips, so GC could not even see the thing it was run to reclaim."""
    leftovers = [
        tree / "deps" / "runtimes" / ".staging-abc123",
        tree / "deps" / "shells" / ".staging-def456",
        tree / "apps" / APP / "versions" / ".staging-789xyz",
        tree / "apps" / APP / "staging" / "0123456789abcdef",
    ]
    for path in leftovers:
        path.mkdir(parents=True)
        (path / "big.bin").write_bytes(b"0" * 4096)

    plan = gc_mod.collect_plan(tree)
    assert {p for _w, p in plan.delete_staging} == set(leftovers)
    # and they are NOT mistaken for a deletable version
    assert not any(v.startswith(".") for _a, v, _p in plan.delete_versions)
    assert plan.reclaimable_mb() > 0

    gc_mod.run_gc(tree, apply=True, log=lambda *_a: None)
    for path in leftovers:
        assert not path.exists()
    assert (tree / "apps" / APP / "versions" / "v1").is_dir()   # current, untouched


def test_gc_does_not_say_nothing_to_reclaim_when_the_orphan_is_what_it_runs_from(
        tree, monkeypatch):
    """S9. tools\\gc.bat takes python.exe from whichever runtime folder the FOR loop
    sees last, so GC can easily be executing from the very orphan it should delete.
    It then finds nothing else, prints 「沒有可回收的項目。」 — and the operator
    believes it. The 450 MB they came to reclaim stays on the disk forever."""
    orphan = build_runtime(tree, FP2)               # nothing references it
    monkeypatch.setattr(gc_mod.sys, "prefix", str(orphan))

    plan = gc_mod.collect_plan(tree)
    assert plan.self_hosted == FP2 and plan.is_empty()

    text = plan.summary()
    assert "沒有可回收的項目" not in text            # the lie
    assert FP2 in text and "回收不掉" in text        # the truth
    # …and the exact command that reclaims it: another runtime's python.exe.
    assert f"deps\\runtimes\\{FP1}\\python.exe bootstrap\\gc.py --apply" in text
    text.encode("cp950")                            # a zh-TW console can print it


def test_gc_reports_what_it_deleted_not_what_it_planned_to(tree):
    """The plan text was built before the delete loop and printed after it, so an
    --apply run signed off with 「可刪 runtime …」 and 「可回收合計 N MB」 about
    trees it had just deleted. Past tense, or it is not a report."""
    build_runtime(tree, FP2)
    lines: list[str] = []

    plan = gc_mod.run_gc(tree, apply=True, log=lines.append)
    text = "\n".join(lines)

    assert plan.applied and [label for label, _mb in plan.deleted]
    assert f"已刪除 runtime {FP2}" in text
    assert "實際回收合計" in text
    assert "可回收合計" not in text and "可刪" not in text
    text.encode("cp950")


def test_gc_reports_trees_it_could_not_delete_instead_of_claiming_the_space(
        tree, monkeypatch):
    """shutil.rmtree(ignore_errors=True) turned 'the App still has this file open'
    into silence, and GC signed off with 「可回收 480 MB」 having reclaimed nothing."""
    orphan = build_runtime(tree, FP2)

    def in_use(_path, *_a, **_kw):
        raise PermissionError(32, "the file is in use by another process")

    monkeypatch.setattr(gc_mod.shutil, "rmtree", in_use)
    lines: list[str] = []

    plan = gc_mod.run_gc(tree, apply=True, log=lines.append)
    text = "\n".join(lines)

    assert plan.failures and plan.deleted == []
    assert plan.reclaimed_mb() == 0
    assert orphan.is_dir()                          # the truth on disk
    assert "刪不掉" in text and str(orphan) in text
    assert "實際回收合計" not in text                # nothing was reclaimed
    assert "App 完全關掉" in text                    # what to DO
    text.encode("cp950")


# ── GC: four different failures used to exit 2 (S9) ──────────────────────────
#
# "I deleted 3 of the 5 trees, 2 are still open in the App" and "I could not take
# the store lock and did nothing at all" exited identically, so tools\gc.bat had
# exactly one story to tell — 「沒有刪掉任何東西」 — and it was false for every
# partial run, and it blamed the store lock for a problem that was not the lock.

def gc_main(tree, monkeypatch, argv):
    """gc.main() against a temp store — the real operator entry point."""
    monkeypatch.setattr(gc_mod, "_store_root", lambda: Path(tree))
    return gc_mod.main(argv)


def make_app(root: Path, app_id: str, versions: dict, *, current: str) -> None:
    """A second app in the same store: {version: runtime_fingerprint}."""
    for version, fingerprint in versions.items():
        build_version(Path(root) / "apps" / app_id / "versions" / version,
                      version, fingerprint, app=app_id, body=version)
    state.StateStore(Path(root) / "apps" / app_id / "state").initialize(app_id, current)


def test_gc_partial_delete_is_not_reported_as_nothing_deleted(tree, monkeypatch, capsys):
    """Three trees went, one would not. Reporting that as 「沒有刪掉任何東西」 (and
    exiting the same code as a lock failure) is false twice over."""
    gone = build_runtime(tree, FP2)
    stuck = build_runtime(tree, FP3)
    real_rmtree = gc_mod.shutil.rmtree

    def rmtree(path, *a, **kw):
        if Path(path) == stuck:
            raise PermissionError(32, "the file is in use by another process")
        return real_rmtree(path, *a, **kw)

    monkeypatch.setattr(gc_mod.shutil, "rmtree", rmtree)

    code = gc_main(tree, monkeypatch, ["--apply"])
    assert code == gc_mod.EXIT_PARTIAL
    assert code not in (gc_mod.EXIT_OK, gc_mod.EXIT_NOTHING_DELETED,
                        gc_mod.EXIT_STORE_LOCKED, gc_mod.EXIT_ABORTED)
    assert not gone.exists() and stuck.is_dir()        # the truth on disk

    captured = capsys.readouterr()
    text = captured.out + captured.err
    assert f"已刪除 runtime {FP2}" in text             # what DID happen
    assert FP3 in text and "檔案使用中" in text        # which tree survived, and why
    assert "App 完全關掉" in text                      # what to do about it
    text.encode("cp950")


def test_gc_that_could_not_delete_a_single_tree_has_its_own_exit_code(
        tree, monkeypatch, capsys):
    orphan = build_runtime(tree, FP2)

    def in_use(_path, *_a, **_kw):
        raise PermissionError(32, "the file is in use by another process")

    monkeypatch.setattr(gc_mod.shutil, "rmtree", in_use)

    code = gc_main(tree, monkeypatch, ["--apply"])
    assert code == gc_mod.EXIT_NOTHING_DELETED
    assert code != gc_mod.EXIT_PARTIAL
    assert orphan.is_dir()
    err = capsys.readouterr().err
    assert "一項都沒有刪掉" in err
    err.encode("cp950")


def test_applying_an_empty_gc_plan_never_claims_it_reclaimed_anything(
        tree, monkeypatch, capsys):
    """S9. The store is already clean, so --apply deletes nothing and frees 0 bytes.

    That used to exit EXIT_OK — the very same code as 「deleted all five trees, freed
    480 MB」 — and tools\\gc.bat's exit-0 branch prints 「回收完成。上面列出的項目都
    已經刪掉了。」 The list was empty. Nothing was listed, nothing was deleted, and
    nothing was reclaimed, and the operator was congratulated for it.
    """
    before = sorted(p.name for p in (tree / "deps" / "runtimes").iterdir())

    code = gc_main(tree, monkeypatch, ["--apply"])

    assert code == gc_mod.EXIT_EMPTY_PLAN
    assert code != gc_mod.EXIT_OK          # …which is the code the bat calls 「回收完成」
    assert code != gc_mod.EXIT_NOTHING_DELETED     # nor is it a failure: nothing broke
    assert sorted(p.name for p in (tree / "deps" / "runtimes").iterdir()) == before

    out = capsys.readouterr().out
    assert "沒有可回收的項目" in out and "沒有刪除任何東西" in out
    assert "已刪除" not in out              # because nothing was
    assert "實際回收合計" not in out         # 0 MB came back: do not print a total
    assert "都已經刪掉了" not in out         # the sentence this test exists to prevent
    out.encode("cp950")


def test_an_apply_that_reclaimed_everything_and_one_that_reclaimed_nothing_differ(
        tree):
    """The two opposite outcomes of --apply must be distinguishable by the one thing
    a .bat can branch on. They both exited 0."""
    build_runtime(tree, FP2)                              # something to reclaim

    reclaimed = gc_mod.run_gc(tree, apply=True, log=lambda *_a: None)
    assert reclaimed.deleted and reclaimed.exit_code() == gc_mod.EXIT_OK
    assert not reclaimed.nothing_to_reclaim()

    empty = gc_mod.run_gc(tree, apply=True, log=lambda *_a: None)   # now clean
    assert empty.applied and empty.is_empty() and empty.nothing_to_reclaim()
    assert empty.deleted == [] and empty.reclaimed_mb() == 0
    assert empty.exit_code() == gc_mod.EXIT_EMPTY_PLAN
    assert empty.exit_code() != reclaimed.exit_code()

    assert "沒有可回收的項目" in empty.headline()
    assert "回收完成" in reclaimed.headline()
    empty.headline().encode("cp950")


def test_the_headline_after_apply_is_measured_never_the_plans_forecast(tree, monkeypatch):
    """reclaimable_mb() is a promise; reclaimed_mb() is the outcome. Printing the
    promise in the past tense is exactly how operators came to believe they had
    freed space that rmtree never managed to take."""
    orphan = build_runtime(tree, FP2)
    assert gc_mod.collect_plan(tree).reclaimable_mb() > 0        # the forecast

    def in_use(_path, *_a, **_kw):
        raise PermissionError(32, "the file is in use by another process")

    monkeypatch.setattr(gc_mod.shutil, "rmtree", in_use)
    plan = gc_mod.run_gc(tree, apply=True, log=lambda *_a: None)

    assert plan.reclaimed_mb() == 0 and orphan.is_dir()
    headline = plan.headline()
    assert "一項都沒有刪掉" in headline and "完全沒有回收" in headline
    assert "實際回收" not in headline           # there was none to report
    headline.encode("cp950")


def test_an_applied_plan_tells_a_gui_what_survived_and_why(tree, monkeypatch):
    """「刪不掉」 has to be actionable. A pre-rendered sentence in a list of strings
    is neither queryable nor clickable: a GUI needs WHAT stayed, WHERE it is, WHY,
    and whether closing the App is the thing that fixes it."""
    orphan = build_runtime(tree, FP2)

    def in_use(_path, *_a, **_kw):
        raise PermissionError(32, "the file is in use by another process")

    monkeypatch.setattr(gc_mod.shutil, "rmtree", in_use)
    plan = gc_mod.run_gc(tree, apply=True, log=lambda *_a: None)

    assert plan.applied and plan.deleted == []
    [survivor] = plan.survivors
    assert survivor.label == f"runtime {FP2}"          # WHAT
    assert Path(survivor.path) == orphan               # WHERE
    assert "檔案使用中" in survivor.reason             # WHY
    assert survivor.in_use is True                     # …and it is the fixable why
    assert "關掉" in survivor.hint()                   # 關掉 App 再跑一次
    assert plan.failures == [survivor.line()]          # the console view of one fact
    survivor.hint().encode("cp950")


def test_a_dry_run_with_nothing_to_reclaim_does_not_send_the_operator_to_a_y_n_prompt(
        tree, monkeypatch, capsys):
    """The empty plan has to be visible on the DRY RUN too, not just after --apply.

    tools\\gc.bat asks 「以上列出的項目要真的刪除嗎? [y/N]」 whenever the dry run
    exits 0 — so on a clean store it asked that about a blank list, the operator
    typed y, and the bat then congratulated them: 「回收完成。上面列出的項目都已經
    刪掉了。」 EXIT_EMPTY_PLAN is what lets the bat skip straight to 「沒有可回收的
    項目」. It is not an error (:empty exits 0), so it must not be EXIT_OK and must
    not be a failure code either."""
    code = gc_main(tree, monkeypatch, [])

    assert code == gc_mod.EXIT_EMPTY_PLAN
    assert code != gc_mod.EXIT_OK              # …which is what opens the y/N prompt
    assert code not in (gc_mod.EXIT_PARTIAL, gc_mod.EXIT_NOTHING_DELETED,
                        gc_mod.EXIT_STORE_LOCKED, gc_mod.EXIT_ABORTED)

    out = capsys.readouterr().out
    assert "沒有可回收的項目" in out and "dry-run" in out
    assert "已刪除" not in out                 # a dry run deletes nothing, and says so


def test_a_dry_run_that_has_something_to_reclaim_still_exits_zero(tree, monkeypatch):
    """…and the y/N prompt must still be reached when there IS something to delete.
    EXIT_EMPTY_PLAN is about the plan being empty, not about it being a dry run."""
    build_runtime(tree, FP2)
    assert gc_main(tree, monkeypatch, []) == gc_mod.EXIT_OK
    assert (tree / "deps" / "runtimes" / FP2).is_dir()      # and it deleted nothing


def test_a_gc_that_never_took_the_store_lock_is_not_a_failed_delete(
        tree, monkeypatch, capsys):
    """An update is downloading: GC did not scan, did not try, did not fail to
    delete anything. It is not the same event as 'the App is still open', and it
    must not report — or exit — as if it were."""
    build_runtime(tree, FP2)
    real_acquire = locks.FileLock.acquire
    monkeypatch.setattr(  # 30s of default timeout is not worth a green test
        locks.FileLock, "acquire",
        lambda self, timeout=0.4, poll=0.05: real_acquire(self, timeout, poll))

    held = locks.store_gc_lock(tree / "deps").acquire(timeout=5)
    try:
        code = gc_main(tree, monkeypatch, ["--apply"])
    finally:
        held.release()

    assert code == gc_mod.EXIT_STORE_LOCKED
    assert code not in (gc_mod.EXIT_PARTIAL, gc_mod.EXIT_NOTHING_DELETED,
                        gc_mod.EXIT_ABORTED)
    assert (tree / "deps" / "runtimes" / FP2).is_dir()     # untouched, unscanned
    err = capsys.readouterr().err
    assert "連掃描都沒有做" in err and "沒有刪除任何東西" in err
    err.encode("cp950")


def test_gc_refusing_up_front_is_reported_as_nothing_deleted_never_as_partial(
        tree, monkeypatch, capsys):
    """GC aborted before touching a single tree. Zero bytes came back — so it must
    NOT exit the code that means 「有些刪掉了,有些還在用」."""
    build_runtime(tree, FP2)
    code = gc_main(tree, monkeypatch, ["--apply", "--app", "nosuchapp"])

    assert code == gc_mod.EXIT_ABORTED == gc_mod.EXIT_NOTHING_DELETED
    assert code not in (gc_mod.EXIT_OK, gc_mod.EXIT_PARTIAL, gc_mod.EXIT_STORE_LOCKED)
    assert (tree / "deps" / "runtimes" / FP2).is_dir()     # nothing was deleted
    err = capsys.readouterr().err
    assert "找不到 app" in err and "一項都沒有刪" in err
    err.encode("cp950")


def test_gc_exit_codes_are_the_ones_the_generated_bat_actually_branches_on():
    """A CONTRACT across two modules: gc.py produces these numbers, store_builder
    bakes them into tools\\gc.bat. Renumber one side and the bat cheerfully prints
    「部分回收…已經刪掉的不會再刪一次」 for a run that deleted nothing at all — the
    exact class of lie the separate codes were introduced to kill."""
    from provision_builder.streamlit_desktop import store_builder

    assert gc_mod.EXIT_OK == store_builder.GC_EXIT_OK
    assert gc_mod.EXIT_PARTIAL == store_builder.GC_EXIT_PARTIAL
    assert gc_mod.EXIT_NOTHING_DELETED == store_builder.GC_EXIT_NOTHING
    assert gc_mod.EXIT_STORE_LOCKED == store_builder.GC_EXIT_LOCKED
    assert gc_mod.EXIT_EMPTY_PLAN == store_builder.GC_EXIT_EMPTY
    # …and they must stay distinguishable from each other, which was the whole point.
    assert len({gc_mod.EXIT_OK, gc_mod.EXIT_PARTIAL, gc_mod.EXIT_NOTHING_DELETED,
                gc_mod.EXIT_STORE_LOCKED, gc_mod.EXIT_EMPTY_PLAN}) == 5


def test_the_empty_plan_code_is_the_one_the_generated_bat_branches_on():
    """A CONTRACT ACROSS TWO MODULES, and the fragile kind: store_builder reads this
    code by NAME (getattr(gc_mod, "EXIT_EMPTY_PLAN", 6)) and bakes the NUMBER into
    every tools\\gc.bat it writes. Rename the constant here and the bat silently
    falls back to 6 while gc.py returns something else — the :empty branch is then
    never taken, gc.bat drops through to `goto failed`, and a perfectly healthy GC
    of an already-clean store is reported as 「回收沒有跑完」."""
    from provision_builder.streamlit_desktop import store_builder

    assert gc_mod.EXIT_EMPTY_PLAN == store_builder.GC_EXIT_EMPTY
    assert gc_mod.EXIT_NOTHING_TO_RECLAIM == gc_mod.EXIT_EMPTY_PLAN   # one code, two names
    # It is not any of the four the bat already knew: 0 would make it print
    # 「都已經刪掉了」 about an empty list, and 3 would make it print 「回收失敗」
    # about a store that is simply already clean.
    assert gc_mod.EXIT_EMPTY_PLAN not in (
        gc_mod.EXIT_OK, gc_mod.EXIT_PARTIAL, gc_mod.EXIT_NOTHING_DELETED,
        gc_mod.EXIT_STORE_LOCKED, gc_mod.EXIT_ABORTED)


def test_gc_can_scope_a_reclaim_to_one_app_and_says_which_apps_it_looked_at(
        tree, monkeypatch, capsys):
    """S9. gc.py had no --app at all, so on a two-app store an operator who wanted
    to reclaim one app's old versions could only reclaim BOTH apps' — or neither."""
    make_version(tree, "v0", body="zero")                      # demo's garbage
    build_runtime(tree, FP2)
    make_app(tree, "other", {"v1": FP2, "vold": FP2}, current="v1")

    code = gc_main(tree, monkeypatch, ["--apply", "--app", APP])
    assert code == gc_mod.EXIT_OK
    assert not (tree / "apps" / APP / "versions" / "v0").exists()     # in scope
    assert (tree / "apps" / "other" / "versions" / "vold").is_dir()   # out of scope

    out = capsys.readouterr().out
    assert "掃描的 app:demo、other" in out      # both were considered…
    assert "不動的 app:other" in out            # …and it says which one it left alone
    out.encode("cp950")


def test_a_scoped_reclaim_never_deletes_the_runtime_another_apps_tree_still_needs(
        tree, monkeypatch):
    """The trap under --app: no SLOT of any app names FP3, but the other app's
    leftover version tree does — and a scoped run is not deleting that tree. Freeing
    the shared runtime under it would leave an intact-looking version that cannot
    start, and that --rollback-to would happily offer."""
    build_runtime(tree, FP2)
    build_runtime(tree, FP3)
    make_app(tree, "other", {"v1": FP2, "vold": FP3}, current="v1")

    plan = gc_mod.run_gc(tree, apps=[APP], apply=True, log=lambda *_a: None)
    assert not plan.delete_runtimes
    assert (tree / "deps" / "runtimes" / FP3).is_dir()      # other/vold still points at it

    # …and a FULL run still reclaims both together: the version tree AND its runtime.
    gc_mod.run_gc(tree, apply=True, log=lambda *_a: None)
    assert not (tree / "apps" / "other" / "versions" / "vold").exists()
    assert not (tree / "deps" / "runtimes" / FP3).exists()


def test_gc_prints_why_each_version_is_kept(tree, capsys):
    """plan.keep_versions was computed on every run and printed on none of them. The
    operator's first question when GC frees less than they expected is 「為什麼那個
    版本還在?」 — and the answer was sitting in memory, unsaid."""
    make_version(tree, "v2", body="two")
    store_of(tree).mutate(state.commit_candidate)                  # v1 = LKG
    store_of(tree).mutate(lambda s: state.set_pending(s, "v2"))
    lease = leases.create_lease(paths_of(tree).data_dir / "leases", app_id=APP,
                                version="v1", runtime_fingerprint=FP1)
    lines: list[str] = []
    try:
        gc_mod.run_gc(tree, apply=False, log=lines.append)
    finally:
        lease.release()

    text = "\n".join(lines)
    assert f"{APP}/v1:目前版本" in text          # …and every other reason it is pinned
    assert "LKG" in text and "正在執行中" in text
    assert f"{APP}/v2:待套用的更新" in text
    text.encode("cp950")


def test_gc_and_the_updater_take_the_same_lock(tree):
    """GC must not be able to rmtree a runtime the updater is mid-way through
    staging. Same lock file on both sides, or the staging dir is fair game."""
    from provision_builder.streamlit_desktop.device import updater as updater_mod

    gc_lock = locks.store_gc_lock(tree / "deps").acquire()
    try:
        # The updater's staging lock is the same file: it must now be contended.
        with pytest.raises(locks.LockTimeout):
            locks.store_gc_lock(tree / "deps").acquire(timeout=0.4, poll=0.05)
    finally:
        gc_lock.release()
    assert updater_mod.store_gc_lock is locks.store_gc_lock
    assert updater_mod._STAGE_LOCK_TIMEOUT > 0


# ── locks: FAT/exFAT (spec §9.3 promises "FAT/exFAT USB 皆可") ────────────────

def no_hardlinks(monkeypatch):
    """os.link on FAT/exFAT: OSError, not FileExistsError."""
    def boom(_src, _dst):
        raise OSError(errno.EPERM, "hard links not supported on this filesystem")
    monkeypatch.setattr(locks.os, "link", boom)


def test_lock_falls_back_to_o_excl_when_hardlinks_are_unsupported(tmp_path, monkeypatch):
    """This used to hand the operator a raw traceback from os.link."""
    no_hardlinks(monkeypatch)
    path = tmp_path / "l.lock"

    first = locks.FileLock(path).acquire(timeout=1)
    assert path.is_file()
    assert json.loads(path.read_text("utf-8"))["pid"] == os.getpid()

    with pytest.raises(locks.LockTimeout):          # still exclusive
        locks.FileLock(path).acquire(timeout=0.4, poll=0.05)

    first.release()
    assert not path.exists()
    locks.FileLock(path).acquire(timeout=1).release()   # and reusable


def test_fallback_lock_still_takes_over_a_dead_owner(tmp_path, monkeypatch):
    no_hardlinks(monkeypatch)
    path = tmp_path / "l.lock"
    path.write_text(json.dumps({"pid": 999999999, "process_start_time": 12345,
                                "operation_id": "dead"}), encoding="utf-8")
    lock = locks.FileLock(path).acquire(timeout=2)
    assert json.loads(path.read_text("utf-8"))["pid"] == os.getpid()
    lock.release()


def test_an_empty_lock_file_is_a_claim_in_progress_not_garbage(tmp_path, monkeypatch):
    """O_EXCL exposes a zero-length lock for the instant between create and write.
    A waiter that reads it must WAIT, not decide the owner is dead and steal —
    that hands the same lock to two processes."""
    no_hardlinks(monkeypatch)
    path = tmp_path / "l.lock"
    path.touch()                                    # the create-then-write window

    with pytest.raises(locks.LockTimeout):
        locks.FileLock(path).acquire(timeout=0.5, poll=0.05)
    assert path.stat().st_size == 0                 # not stolen, not clobbered


def test_an_abandoned_empty_lock_is_eventually_reclaimed(tmp_path, monkeypatch):
    """...but a process killed inside that window must not deadlock the store
    forever, so an OLD empty lock is takeable."""
    no_hardlinks(monkeypatch)
    path = tmp_path / "l.lock"
    path.touch()
    monkeypatch.setattr(locks.FileLock, "_age", lambda _self: 3600.0)

    lock = locks.FileLock(path).acquire(timeout=2)
    assert json.loads(path.read_text("utf-8"))["pid"] == os.getpid()
    lock.release()


def test_hardlink_unsupported_detection():
    assert locks.hardlinks_unsupported(OSError(errno.EPERM, "no"))
    assert locks.hardlinks_unsupported(OSError(errno.EACCES, "no"))
    assert not locks.hardlinks_unsupported(OSError(errno.ENOENT, "missing"))


def test_held_helper_does_not_deadlock_on_itself(tmp_path):
    """`with some_lock().acquire(timeout=X)` self-deadlocks: __enter__ acquires a
    SECOND time. locks.held() is the form that works."""
    lock = locks.FileLock(tmp_path / "l.lock")
    with locks.held(lock, timeout=1):
        assert (tmp_path / "l.lock").is_file()
    assert not (tmp_path / "l.lock").exists()


# ═════════════════════════════════════════════════════════════════════════════
# S9, round 6: 現場 IT,工廠機 C 槽快滿,要清出空間
#
# Four things made GC useless to that person:
#   1. it could not SEE what actually fills a long-running machine — apps/<app>/
#      data/logs (never rotated: one launcher-*.log + one streamlit-*.log +
#      one bootstrap-*.log per launch, forever) and data/cache (pycache)
#   2. an already-clean store answered 「沒有可回收的項目」 and stopped — true, and
#      useless to somebody whose C: drive is full: it never said where the space
#      went, and never once called shutil.disk_usage()
#   3. one unreadable file made an entire 450 MB runtime report as 「0 MB」
#   4. double-clicking start.bat started a SECOND app: the lock waited 30 seconds
#      and then proceeded anyway
# ═════════════════════════════════════════════════════════════════════════════

def write_log(log_dir: Path, name: str, *, kb: int = 100, age_seconds: float = 0.0) -> Path:
    """A log file with a real age — retention is decided on mtime, not on the name."""
    import time as _time
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / name
    path.write_bytes(b"x" * (kb * 1024))
    stamp = _time.time() - age_seconds
    os.utime(path, (stamp, stamp))
    return path


def fake_disk(monkeypatch, *, total_gb: float, free_mb: float) -> None:
    """A nearly-full C:. shutil.disk_usage() had ZERO uses in the whole repo."""
    import types
    total = int(total_gb * 1024 ** 3)
    free = int(free_mb * 1024 ** 2)
    monkeypatch.setattr(gc_mod.shutil, "disk_usage",
                        lambda _p: types.SimpleNamespace(
                            total=total, used=total - free, free=free))


# ── (3) an unreadable file does not make a 450 MB runtime report as 0 MB ─────

def test_an_unreadable_file_does_not_make_a_450_mb_runtime_report_as_zero_mb(
        tree, monkeypatch):
    """GcPlan._mb() wrapped the WHOLE directory walk in one try/except and returned
    0.0 on any OSError. One file held open by a running App (or antivirus, or
    Explorer) and a 450 MB runtime was reported as 「0 MB」 — so the operator, who
    came to reclaim exactly that 450 MB, was told there was nothing to gain."""
    orphan = build_runtime(tree, FP2)
    (orphan / "big.bin").write_bytes(b"0" * (2 * 1024 * 1024))   # the 450 MB, in miniature
    (orphan / "locked.bin").write_bytes(b"0" * (1024 * 1024))    # the file the App has open

    real_size = gc_mod._entry_size

    def in_use(entry):
        if entry.name == "locked.bin":
            raise PermissionError(32, "the file is in use by another process")
        return real_size(entry)

    monkeypatch.setattr(gc_mod, "_entry_size", in_use)

    plan = gc_mod.collect_plan(tree)
    measured = plan.measure(orphan)

    assert measured.mb > 1.5               # NOT 0.0 — the whole point
    assert measured.unreadable == 1        # …and we know exactly what we missed
    assert measured.partial and "至少" in measured.text()
    assert plan.reclaimable_mb() > 1.5     # the forecast the operator acts on
    assert plan.measurement_is_partial() and plan.unmeasured_count == 1
    [(path, why)] = plan.unmeasured
    assert path.endswith("locked.bin") and "使用中" in why

    text = plan.summary()
    assert "至少" in text and "量不到大小" in text      # said out loud, not swallowed
    assert f"可刪 runtime:{FP2}(0 MB)" not in text     # the lie this test kills
    text.encode("cp950")


# ── (1) GC can finally see what actually fills the disk ──────────────────────

def test_gc_offers_the_unrotated_logs_and_cache_that_actually_filled_the_disk(tree):
    """Every launch writes launcher-*.log + streamlit-*.log + bootstrap-*.log and
    NOTHING has ever rotated one; PYTHONPYCACHEPREFIX points every .pyc the app
    compiles at data/cache/pycache. After months, THAT is the disk — and
    collect_plan() looked only at versions, runtimes, shells and leases."""
    logs = tree / "apps" / APP / "data" / "logs"
    month = 30 * 86400
    for i in range(25):                                     # 25 launches' worth
        write_log(logs, f"launcher-2026-{i:02d}.log", kb=100,
                  age_seconds=month + (25 - i) * 3600)
        write_log(logs, f"streamlit-2026-{i:02d}-8501.log", kb=200,
                  age_seconds=month + (25 - i) * 3600)
    cache = tree / "apps" / APP / "data" / "cache" / "pycache"
    cache.mkdir(parents=True)
    (cache / "app.cpython-311.pyc").write_bytes(b"0" * (512 * 1024))

    plan = gc_mod.collect_plan(tree)

    assert not plan.is_empty()                              # …it used to be
    [log_group] = plan.delete_logs
    assert log_group.count == 2 * (25 - gc_mod.LOG_KEEP_RECENT)   # newest 10 per family stay
    assert plan.logs_mb() > 1 and plan.cache_mb() > 0.4
    # …and they are reported SEPARATELY from versions/runtimes, which is the point:
    # the operator has to see WHERE the disk went, not just a lump sum.
    kinds = {consumer.kind for consumer in plan.consumers}
    assert {"logs", "cache", "versions", "runtime"} <= kinds
    text = plan.summary()
    assert "可刪舊記錄檔" in text and "可刪快取" in text
    text.encode("cp950")

    before = plan.store_mb()
    done = gc_mod.run_gc(tree, apply=True, log=lambda *_a: None)

    survivors = sorted(path.name for path in logs.iterdir())
    assert len(survivors) == 2 * gc_mod.LOG_KEEP_RECENT     # the newest of each family
    assert "launcher-2026-24.log" in survivors              # the newest launch
    assert "launcher-2026-00.log" not in survivors          # …and the oldest is gone
    assert not any(cache.parent.iterdir())                  # pycache is regenerable: gone
    assert (tree / "apps" / APP / "versions" / "v1").is_dir()   # current: untouched

    # The REPORT is measured from the tree that exists NOW. Printing the scan's
    # breakdown (「記錄檔 33 MB,其中 31 MB 可以回收」) after the reclaim would be the
    # same past-tense forecast that once had operators looking for space that had
    # never been freed.
    assert done.store_mb() < before
    assert all(c.reclaimable_mb == 0 for c in done.consumers)
    report = done.report()
    assert "實際回收合計" in report
    assert "可回收合計" not in report and "可刪" not in report
    report.encode("cp950")


def test_the_log_of_a_session_that_is_running_right_now_is_never_reclaimed(tree):
    """A machine can be launched a hundred times in one day. 「keep the newest N」
    cannot be the only rule, or the log of the session that is being written right
    now — the one the operator is on the phone about — becomes reclaimable."""
    logs = tree / "apps" / APP / "data" / "logs"
    for i in range(20):
        write_log(logs, f"launcher-old-{i:02d}.log", age_seconds=30 * 86400 + i)
    live = write_log(logs, "launcher-now.log", age_seconds=0.0)     # this session

    doomed = gc_mod.stale_logs(logs)
    assert live not in doomed
    assert doomed and all(path.name.startswith("launcher-old-") for path in doomed)

    gc_mod.rotate_logs(logs)
    assert live.is_file()


def test_rotate_logs_is_the_retention_the_launcher_never_had(tree):
    """The GC-side reclaim is a mop. THIS is the tap: one call on every launch,
    wherever the logs are created (bootstrap.py, right after ensure_data_dirs())."""
    logs = tree / "apps" / APP / "data" / "logs"
    for i in range(30):
        write_log(logs, f"bootstrap-2026-{i:02d}.log", kb=50,
                  age_seconds=30 * 86400 + (30 - i) * 60)
    assert len(list(logs.iterdir())) == 30

    removed = gc_mod.rotate_logs(logs)

    assert len(removed) == 30 - gc_mod.LOG_KEEP_RECENT
    kept = sorted(path.name for path in logs.iterdir())
    assert len(kept) == gc_mod.LOG_KEEP_RECENT
    assert "bootstrap-2026-29.log" in kept        # the newest survives
    assert "bootstrap-2026-00.log" not in kept    # the oldest does not
    # …and it is safe on a machine that has never been launched (a fresh install).
    assert gc_mod.rotate_logs(tree / "apps" / APP / "data" / "nope") == []


# ── (2) an empty plan on a full disk still tells the operator where it went ──

def test_an_empty_plan_on_a_full_disk_still_says_where_the_space_went(
        tree, monkeypatch, capsys):
    """「沒有可回收的項目」 is a true answer to a question the operator did not ask.
    They asked where their C: drive went. GC never called shutil.disk_usage() — zero
    uses in the entire repo — so it could not even tell them whether the disk was
    full, let alone that this tree is not what filled it."""
    (tree / "deps" / "runtimes" / FP1 / "big.bin").write_bytes(b"0" * (3 * 1024 * 1024))
    fake_disk(monkeypatch, total_gb=120, free_mb=800)       # C: is full

    plan = gc_mod.collect_plan(tree)

    assert plan.is_empty()                                  # nothing to reclaim: true
    assert plan.disk.known and plan.disk.nearly_full()      # …and the disk IS full
    assert 700 < plan.disk.free_mb < 900
    assert plan.store_mb() > 2                              # what this tree holds
    assert plan.biggest(3)[0].kind == "runtime"             # the biggest consumer, named
    assert not plan.store_is_the_problem()                  # 3 MB is not why C: is full

    text = plan.summary()
    assert "磁碟" in text and "可用" in text                 # how much is left…
    assert "不是被它吃掉的" in text                          # …and that it is not us
    assert f"runtime {FP1}" in text                         # …and what the big things are
    assert "沒有可回收的項目" in text                        # still true, still said
    assert "沒有可回收的項目" in plan.headline()
    text.encode("cp950")

    # …and the same through the real entry point, on --apply.
    code = gc_main(tree, monkeypatch, ["--apply"])
    assert code == gc_mod.EXIT_EMPTY_PLAN
    out = capsys.readouterr().out
    assert "磁碟" in out and "不是被它吃掉的" in out
    assert "已刪除" not in out and "實際回收合計" not in out    # because nothing was
    out.encode("cp950")


def test_when_the_store_really_is_the_hog_gc_says_so_instead_of_pointing_elsewhere(
        tree, monkeypatch):
    """The mirror image has to work too: on a small disk this tree IS the problem,
    and sending the operator off to look somewhere else would send them hunting for
    space that is sitting right here."""
    (tree / "deps" / "runtimes" / FP1 / "big.bin").write_bytes(b"0" * (8 * 1024 * 1024))
    fake_disk(monkeypatch, total_gb=0.05, free_mb=10)       # a 51 MB disk, 41 MB used

    plan = gc_mod.collect_plan(tree)

    assert plan.store_is_the_problem()
    text = plan.summary()
    assert "不是被它吃掉的" not in text
    assert "快滿了" in text
    text.encode("cp950")


def test_a_gui_can_read_where_the_space_went_without_re_deriving_it(tree, monkeypatch):
    """The fields a portal/GUI shows. A pre-rendered console string is neither
    queryable nor clickable."""
    (tree / "deps" / "runtimes" / FP1 / "big.bin").write_bytes(b"0" * (2 * 1024 * 1024))
    for i in range(gc_mod.LOG_KEEP_RECENT + 5):        # more launches than we keep
        write_log(tree / "apps" / APP / "data" / "logs", f"launcher-old-{i:02d}.log",
                  kb=64, age_seconds=90 * 86400 + i)
    fake_disk(monkeypatch, total_gb=120, free_mb=900)

    plan = gc_mod.collect_plan(tree)

    assert plan.disk.total_mb > plan.disk.free_mb > 0
    assert plan.disk.path                                   # "C:" — what they call it
    assert plan.store_mb() >= sum(c.mb for c in plan.biggest(1))
    [runtime] = [c for c in plan.consumers if c.kind == "runtime"]
    assert runtime.label.startswith("runtime ") and runtime.mb > 1
    assert runtime.reclaimable_mb == 0                      # FP1 is `current`: kept
    [logs] = [c for c in plan.consumers if c.kind == "logs"]
    assert logs.mb > 0 and logs.reclaimable_mb > 0          # …the logs, we CAN reclaim
    for consumer in plan.consumers:                         # everything shows a size
        consumer.line().encode("cp950")


# ── (5) a second start.bat does not start a second instance ──────────────────

def test_a_second_start_bat_does_not_start_a_second_instance(tmp_path):
    """There was no single-instance check at all. The app update lock is taken only
    around the state WRITE, so it is free the entire time the app runs — and
    FileLock.acquire() defaults to timeout=30: a user who double-clicked start.bat
    got a second launcher that waited half a minute and then started anyway. Two
    launchers, two Streamlits, one state.json."""
    import time as _time
    data_dir = tmp_path / "data"

    first = locks.acquire_single_instance(data_dir)         # the window they opened
    try:
        started = _time.monotonic()
        with pytest.raises(locks.AlreadyRunning) as caught:
            locks.acquire_single_instance(data_dir)         # the second double-click
        waited = _time.monotonic() - started
    finally:
        first.release()

    assert waited < 5                                       # it does NOT sit out 30s…
    message = str(caught.value)
    assert "這個 App 已經在執行中" in message               # …and it says so
    assert caught.value.owner.get("pid") == os.getpid()     # …and who is holding it
    assert "工作管理員" in message                          # …and what to do if it hung
    message.encode("cp950")

    # …and it is not a one-way door: when the first copy exits, the next launch
    # starts normally.
    again = locks.acquire_single_instance(data_dir)
    again.release()


def test_an_app_that_crashed_does_not_lock_the_machine_out_of_its_own_app(tmp_path):
    """A single-instance lock a dead process can hold forever is worse than no lock:
    the operator could never start the app again. PID + process start time, so a
    reused PID cannot inherit a dead app's lock either."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / locks.INSTANCE_LOCK_NAME).write_text(json.dumps({
        "pid": 999999999, "process_start_time": 12345,
        "operation_id": "dead", "what": "app instance lock"}), encoding="utf-8")

    lock = locks.acquire_single_instance(data_dir)          # takes over the corpse
    try:
        assert json.loads((data_dir / locks.INSTANCE_LOCK_NAME)
                          .read_text("utf-8"))["pid"] == os.getpid()
    finally:
        lock.release()


def test_the_instance_lock_is_not_the_update_lock(tree):
    """Different files, on purpose. The instance lock is held for the WHOLE session;
    the update lock is taken for one state.json write. Holding the update lock for
    a session would block every updater for the length of a shift."""
    paths = paths_of(tree)
    assert locks.instance_lock(paths.data_dir).path != locks.app_lock(paths.state_dir).path

    running = locks.acquire_single_instance(paths.data_dir)
    try:
        # …an update can still be staged while the app is open — which is exactly
        # what the updater does all day.
        assert store_of(tree).mutate(lambda s: state.set_pending(s, "v2")).pending == "v2"
    finally:
        running.release()


# ── (4) the message a person reads down the phone, from a factory floor ──────

def test_a_broken_state_file_names_the_file_and_speaks_the_operators_language(store):
    """state.py raised 「corrupt state.json: Expecting value…」 — English, and with no
    path in it. The person reading it is standing in a factory, on the phone, in
    front of a machine that has several state.json files on it."""
    store.initialize("demo", "v1")
    store.path.write_text("{ half json", encoding="utf-8")

    with pytest.raises(state.StateError) as caught:
        store.load()

    message = str(caught.value)
    assert str(store.path) in message         # WHICH file
    assert "毀損" in message                  # …in a language they read
    assert "版本" in message                  # …and what it is for / what to do
    message.encode("cp950")                   # …on a zh-TW console


def test_a_missing_state_file_says_which_one_and_what_it_means(store):
    with pytest.raises(state.StateError) as caught:
        store.load()
    message = str(caught.value)
    assert str(store.path) in message and "找不到" in message
    message.encode("cp950")


# ── hardlink dedup between version slots ─────────────────────────────────────
#
# store_builder now os.link()s byte-identical files between version slots instead of
# re-copying them (CV_Viewer: versions\ 168 MB -> 84 MB; a second version costs 0 MB).
# Three things downstream had to learn about it, and each of these tests is named for
# the failure it prevents.

BIG = b"W" * (4 * 1024 * 1024)          # a stand-in for the 84 MB DINOv2 weight file
WEIGHT = "application/weights.bin"


def add_file(vdir: Path, relpath: str, blob: bytes) -> Path:
    """Put a file into a COMPLETE version slot and re-earn its files.json/.complete."""
    path = Path(vdir) / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(blob)
    reseal(vdir)
    return path


def reseal(vdir: Path) -> None:
    integrity.remove_complete(vdir)
    integrity.write_files_json(vdir, integrity.build_files_json(vdir))
    integrity.write_complete(vdir)


def link_into(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    os.link(src, dst)


def run_argv(tree, monkeypatch, argv):
    """bootstrap.main() with the RAW argv — including the case of no --app at all,
    which is the one that matters here."""
    monkeypatch.setattr(bootstrap, "_store_root", lambda: Path(tree))
    monkeypatch.setattr(bootstrap.subprocess, "Popen",
                        lambda *a, **k: pytest.fail("launcher must not be started"))
    return bootstrap.main(argv)


def make_payload_for(tmp_path: Path, app_id: str, version: str, *,
                     folder: str | None = None, revision: str = "r1",
                     fingerprint: str = FP1, body: str = "two") -> Path:
    """export_update()'s output for ANY app — including one this machine has never
    heard of, which is the entire point of the --install tests below."""
    payload = tmp_path / "usb" / (folder or app_id)
    payload.mkdir(parents=True, exist_ok=True)
    build_version(payload / "versions" / version, version, fingerprint,
                  app=app_id, complete=False, body=body)
    (payload / "release.json").write_text(json.dumps({
        "schema": 1, "app_id": app_id, "version": version, "revision": revision,
        "runtime_fingerprint": fingerprint,
    }), encoding="utf-8")
    return payload


# ── (1) a second app can never arrive by the update path ─────────────────────

def test_installing_a_new_app_onto_an_existing_store_refuses_with_the_honest_reason(
        tree, tmp_path, monkeypatch, capsys):
    """THE BLOCKER. main() resolved the app from apps\\ BEFORE dispatching --install,
    so on a machine already running app A:

        --app app-b --install <B's payload>   ->  「找不到 app 'app-b'」

    which is a lie about the payload: B is not in apps\\ BECAUSE it has not been
    installed yet, which is the whole reason the command was run. And the advice the
    provider gives for the no---app case is 「請指定它:--app app-b」 — i.e. the command
    on the line above, which could not work. The two halves sent the operator in a
    circle.

    A payload genuinely CANNOT be a first install (no start-<app>.bat, no messages\\
    for it to print, no admin console), so the answer is a refusal — but it must be
    the TRUE refusal, naming the app, naming what is missing and naming the fix."""
    payload = make_payload_for(tmp_path, "app-b", "v9")

    code = run_argv(tree, monkeypatch, ["--app", "app-b", "--install", str(payload)])

    assert code == 2
    err = capsys.readouterr().err
    assert "找不到 app" not in err                  # the old lie
    assert "更新包" in err and "完整交付" in err     # this is an update, not a delivery
    assert "app-b" in err                           # …for THIS app
    assert "start-app-b.bat" in err                 # …and this is what it cannot bring
    assert "第一次安裝這個 App" in err               # …and this is the way in
    err.encode("cp950")                             # …on a zh-TW console

    # AND IT LEFT NOTHING BEHIND. A refused install that has already created
    # apps\app-b\data\ is a half-app, and the next --status would find it.
    assert not (tree / "apps" / "app-b").exists()
    assert paths_mod.list_app_ids(tree) == [APP]


def test_a_new_apps_payload_is_never_silently_handed_to_the_app_that_is_installed(
        tree, tmp_path, monkeypatch, capsys):
    """Bare --install on a machine running app A used to resolve to A (the only app in
    apps\\) and hand it B's payload. provider's app_id check refused it — so nothing
    was ever mis-installed — but the message blamed the payload for being 「別的 app
    的」 and sent the operator to a command that cannot work. The app comes from the
    PAYLOAD now, so the answer is about the real situation."""
    payload = make_payload_for(tmp_path, "app-b", "v9")

    code = run_argv(tree, monkeypatch, ["--install", str(payload)])

    assert code == 2
    err = capsys.readouterr().err
    assert "完整交付" in err and "app-b" in err
    assert "不是 'demo' 的" not in err       # not the old 「你拿錯更新包了」 accusation
    assert store_of(tree).load().current == "v1"        # …and A is untouched
    assert store_of(tree).load().pending is None
    err.encode("cp950")


def test_install_reads_the_app_from_the_payload_so_a_two_app_store_needs_no_app_flag(
        tree, tmp_path, monkeypatch, capsys):
    """The other half of the same bug. With two apps installed, bare --install died
    with 「apps\\ 下有 2 個 app,請用 --app 指定」 — over a payload whose release.json
    names its app in the first line. We refused to read the answer we were standing on.
    """
    make_app(tree, "app-b", {"v1": FP1}, current="v1")      # a second app, installed
    assert len(paths_mod.list_app_ids(tree)) == 2
    payload = make_payload_for(tmp_path, "app-b", "v2")

    code = run_argv(tree, monkeypatch, ["--install", str(payload)])       # no --app

    assert code == 0, capsys.readouterr().err
    assert state.StateStore(tree / "apps" / "app-b" / "state").load().pending == "v2"
    assert store_of(tree).load().pending is None            # …and app A was not touched


def test_install_of_a_payload_for_another_app_than_the_one_named_says_which(
        tree, tmp_path, monkeypatch, capsys):
    """--app demo --install <B's payload>: the operator grabbed the wrong folder. Name
    the app the folder is actually FOR, not just the mismatch."""
    payload = make_payload_for(tmp_path, "app-b", "v9")

    code = run_argv(tree, monkeypatch, ["--app", APP, "--install", str(payload)])

    assert code == 2
    err = capsys.readouterr().err
    assert "app-b" in err and "demo" in err
    err.encode("cp950")


def test_install_finds_the_payload_in_a_folder_the_operator_renamed(
        tree, tmp_path, monkeypatch):
    """The operator copies the export onto a stick and renames it 「v2更新包」, then
    points --install at the stick's ROOT. The folder name is decoration; the app_id in
    release.json is the fact."""
    make_payload_for(tmp_path, APP, "v2", folder="v2更新包")

    code = run_argv(tree, monkeypatch, ["--install", str(tmp_path / "usb")])

    assert code == 0
    assert store_of(tree).load().pending == "v2"


# ── (2) the target machine must not re-copy what it already has ──────────────

def test_an_update_hardlinks_the_unchanged_weight_file_instead_of_copying_it(
        tree, tmp_path, monkeypatch, capsys):
    """updater.py:175 passed no link_from, so download_app fell back to a plain copy:
    the factory PC copied CV_Viewer's 84 MB weight file out of the USB stick on EVERY
    release, while a byte-identical copy sat in the version slot next door. The store
    layout is sold on 「一次改版只搬十幾 MB」 and on the one machine that matters it
    was not true."""
    installed = tree / "apps" / APP / "versions" / "v1"
    add_file(installed, WEIGHT, BIG)                    # v1 holds the big file
    payload = make_payload_for(tmp_path, APP, "v2")
    (payload / "versions" / "v2" / WEIGHT).parent.mkdir(parents=True, exist_ok=True)
    (payload / "versions" / "v2" / WEIGHT).write_bytes(BIG)   # …and so does v2, identically
    integrity.write_files_json(payload / "versions" / "v2")

    assert run_argv(tree, monkeypatch, ["--install", str(payload)]) == 0

    staged = tree / "apps" / APP / "versions" / "v2" / WEIGHT
    old = installed / WEIGHT
    assert staged.is_file() and staged.read_bytes() == BIG      # …and it is CORRECT
    # ONE set of bytes, two names: the update cost a directory entry, not 4 MB.
    assert os.stat(staged).st_ino == os.stat(old).st_ino
    assert os.stat(staged).st_nlink == 2
    # …and the version still verifies, which is what the sentinel is earned on.
    assert integrity.verify_tree(tree / "apps" / APP / "versions" / "v2") == []


def test_a_wrong_hardlink_can_never_be_promoted(tree, tmp_path, monkeypatch, capsys):
    """The safety claim link_from rests on, tested rather than trusted: stage_release
    runs verify_tree() over the STAGED tree — hashing whatever the link points AT —
    before anything is renamed into place.

    The scenario that would poison an install: the prior slot's files.json is honest,
    but its FILE has silently rotted (bit rot, antivirus 'repair'). The linker trusts
    that files.json (by design: it never re-hashes a completed slot), so it links the
    ROTTEN bytes into the new version. verify_tree must catch it, because the new
    version's OWN files.json disagrees."""
    installed = tree / "apps" / APP / "versions" / "v1"
    add_file(installed, WEIGHT, BIG)                   # files.json now declares sha(BIG)
    rotted = BIG[:-1] + b"X"                           # same size, different bytes
    (installed / WEIGHT).write_bytes(rotted)           # …and nobody updated files.json

    payload = make_payload_for(tmp_path, APP, "v2")
    (payload / "versions" / "v2" / WEIGHT).parent.mkdir(parents=True, exist_ok=True)
    (payload / "versions" / "v2" / WEIGHT).write_bytes(BIG)
    integrity.write_files_json(payload / "versions" / "v2")

    code = run_argv(tree, monkeypatch, ["--install", str(payload)])

    assert code == 2                                   # refused…
    out = capsys.readouterr()
    assert "驗證失敗" in (out.out + out.err)
    # …and NOTHING was promoted: no v2, no pending, no sentinel, no staging left over.
    assert not (tree / "apps" / APP / "versions" / "v2").exists()
    assert store_of(tree).load().pending is None
    assert store_of(tree).load().current == "v1"
    assert not list((tree / "apps" / APP / "staging").glob("*"))


# ── (3) GC must not claim bytes another slot still holds ─────────────────────

def test_gc_never_claims_to_free_bytes_that_another_slot_still_holds(tree):
    """THE LIE. GC summed st_size, so deleting a version whose 84 MB weight file is
    hardlinked from the version next door freed roughly NOTHING while GC announced
    「回收完成…實際回收 84 MB」. The data is safe — the OS keeps the bytes until the
    last name goes — but 「回收完成」 over a run that freed nothing is exactly the bug
    class this file has been fixed for twice already."""
    current = tree / "apps" / APP / "versions" / "v1"
    orphan = make_version(tree, "v0")                  # older, unreferenced: GC will take it
    add_file(current, WEIGHT, BIG)
    link_into(current / WEIGHT, orphan / WEIGHT)       # what store_builder now does
    reseal(orphan)
    assert os.stat(orphan / WEIGHT).st_nlink == 2

    free_before = shutil.disk_usage(tree).free
    plan = gc_mod.run_gc(tree, apply=True, log=lambda *_a: None)
    freed_on_disk = (shutil.disk_usage(tree).free - free_before) / 1024 ** 2

    assert not orphan.exists()                         # the slot went…
    assert plan.reclaimed_mb() < 1                     # …and it gave back nothing
    assert freed_on_disk < 1                           # …which is what the disk says
    assert plan.reclaimed_mb() <= freed_on_disk + 1    # never claim more than the disk
    # …and the 4 MB is REPORTED, not silently dropped.
    assert plan.shared_skipped_mb() >= 3
    report = plan.report()
    assert "其他版本共用" in report and "沒有釋放" in report
    assert "回收完成:刪掉 1 項,實際回收 4 MB" not in plan.headline()   # the old lie
    report.encode("cp950")
    plan.headline().encode("cp950")

    # AND THE DATA IS SAFE: the surviving version still has every byte.
    assert (current / WEIGHT).read_bytes() == BIG
    assert integrity.verify_tree(current) == []


def test_the_gc_forecast_does_not_promise_space_that_cannot_arrive(tree):
    """Same lie, one step earlier. 「試算:可回收 4 MB」 is a promise, and the operator
    who reads it clears their afternoon for a disk that will not move."""
    current = tree / "apps" / APP / "versions" / "v1"
    orphan = make_version(tree, "v0")
    add_file(current, WEIGHT, BIG)
    link_into(current / WEIGHT, orphan / WEIGHT)
    reseal(orphan)

    plan = gc_mod.collect_plan(tree)

    assert (APP, "v0") in {(a, v) for a, v, _p in plan.delete_versions}
    assert plan.reclaimable_mb() < 1                   # the honest forecast
    assert plan.shared_mb() >= 3                       # …and where the 4 MB went
    summary = plan.summary()
    assert "其他版本共用" in summary and "不會釋放" in summary
    summary.encode("cp950")
    assert "其他版本共用" in plan.headline()
    plan.headline().encode("cp950")
    # the version consumer must not promise it either
    [versions] = [c for c in plan.consumers if c.kind == "versions"]
    assert versions.reclaimable_mb < 1 and versions.shared_mb >= 3
    versions.line().encode("cp950")


def test_gc_credits_the_shared_bytes_to_the_last_slot_that_holds_them(tree):
    """The other half of honesty: when EVERY name is going, the bytes really do come
    back, and GC must not under-report that either. Counting 「nlink == 1 at the moment
    of deletion」 gets this right by construction — the first slot sees 2 names and
    reports 0, the second sees 1 and reports the lot."""
    make_version(tree, "v0")
    orphan_a = tree / "apps" / APP / "versions" / "v0"
    orphan_b = make_version(tree, "v0-b")
    add_file(orphan_a, WEIGHT, BIG)
    link_into(orphan_a / WEIGHT, orphan_b / WEIGHT)
    reseal(orphan_b)
    assert store_of(tree).load().current == "v1"        # neither orphan is referenced

    free_before = shutil.disk_usage(tree).free
    plan = gc_mod.run_gc(tree, apply=True, log=lambda *_a: None)
    freed_on_disk = (shutil.disk_usage(tree).free - free_before) / 1024 ** 2

    assert not orphan_a.exists() and not orphan_b.exists()
    assert plan.reclaimed_mb() >= 3                     # the bytes DID come back…
    assert freed_on_disk >= 3                           # …and the disk agrees
    assert plan.shared_skipped_mb() < 1                 # nothing was left behind


def test_gc_does_not_credit_bytes_a_slot_that_refused_to_delete_still_holds(
        tree, monkeypatch):
    """The case a precomputed forecast can never get right, and the reason the apply
    path measures at the moment of deletion: two doomed slots share the file, the first
    REFUSES to delete (the App has it open), so the bytes never come back — and the
    second slot must not be credited with the 4 MB the forecast promised."""
    orphan_a = make_version(tree, "v0")
    orphan_b = make_version(tree, "v0-b")
    add_file(orphan_a, WEIGHT, BIG)
    link_into(orphan_a / WEIGHT, orphan_b / WEIGHT)
    reseal(orphan_b)

    plan_before = gc_mod.collect_plan(tree)
    assert plan_before.reclaimable_mb() >= 3           # the forecast: both are going

    real_rmtree = gc_mod.shutil.rmtree

    def refuse_the_first(path, *args, **kwargs):
        if Path(path).name == "v0":                    # the App still has it open
            raise PermissionError(32, "the file is in use by another process")
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(gc_mod.shutil, "rmtree", refuse_the_first)
    plan = gc_mod.run_gc(tree, apply=True, log=lambda *_a: None)

    assert orphan_a.is_dir() and not orphan_b.exists()      # one stayed, one went
    assert plan.survivors                                   # …and it is reported
    assert plan.reclaimed_mb() < 1                          # the 4 MB did NOT come back
    assert plan.shared_skipped_mb() >= 3                    # …and GC says why
    text = plan.report()
    assert "其他版本共用" in text
    text.encode("cp950")
