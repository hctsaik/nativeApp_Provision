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
    with pytest.raises(state.StateError, match="corrupt"):
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


# ── launcher exit-code contract ──────────────────────────────────────────────

class FakeLauncher:
    """`healthy` writes the marker the way launch.py does once its WINDOW has stood
    for ~12s. `revoke` deletes it again on the way out — which is exactly what the
    real launcher's _revoke_marker() does when the app it was hosting dies."""

    def __init__(self, env, *, healthy: bool, exit_code: int = 0, polls: int = 1,
                 revoke: bool = False):
        self.pid = 4242
        self.returncode = None
        self._exit_code = exit_code
        self._polls_left = polls
        self._revoke = revoke
        self.marker = Path(env["CIM_HEALTHY_MARKER"])
        if healthy:
            self.marker.parent.mkdir(parents=True, exist_ok=True)
            self.marker.write_text("http://127.0.0.1:9999", encoding="utf-8")

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
