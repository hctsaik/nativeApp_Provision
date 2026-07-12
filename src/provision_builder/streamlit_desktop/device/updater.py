"""Background staging of updates while the app runs (spec §9).

Stage only — never touch the running version, never promote here. Everything
lands in staging first, is byte-verified, moves into place atomically, gets its
sentinel, and only then does state.pending change. Any failure leaves the
system exactly as it was (minus a cleaned staging dir).
"""

from __future__ import annotations

import json
import logging
import shutil
import uuid
from pathlib import Path

if __package__:
    from . import integrity, notifications, paths as paths_mod, state as state_mod
    from .locks import LockTimeout, held, runtime_lock, store_gc_lock
    from .provider import FolderUpdateProvider, ProviderError, ReleaseMetadata, UpdateProvider
    from .runtime_store import RUNTIME_META, RuntimeStore, ShellStore
else:
    import integrity
    import notifications
    import paths as paths_mod
    import state as state_mod
    from locks import LockTimeout, held, runtime_lock, store_gc_lock
    from provider import FolderUpdateProvider, ProviderError, ReleaseMetadata, UpdateProvider
    from runtime_store import RUNTIME_META, RuntimeStore, ShellStore

# Staging holds the SAME lock GC takes (gc.store_gc_lock). Without it, GC's
# "delete every .staging-* leftover" pass is free to rmtree the half-downloaded
# runtime this updater is standing in — the tree then fails verification and the
# operator is told the update source is corrupt. One lock, both sides.
_STAGE_LOCK_TIMEOUT = 120.0


class UpdateError(Exception):
    pass


def _provider_carries(provider: UpdateProvider, method: str, release) -> bool:
    """A provider that predates the has_* probes is assumed to carry everything —
    it will fail its own way if it does not, which is the old behaviour."""
    probe = getattr(provider, method, None)
    return True if probe is None else bool(probe(release))


def _runtime_already_here(rstore: RuntimeStore, fingerprint: str, log) -> bool:
    """Is this runtime on the machine already — even if it has not been verified yet?

    An incremental update deliberately ships WITHOUT the runtime: the whole point
    is that the target already has it. But a freshly delivered tree carries no
    .complete sentinel (export strips it, so it gets earned here rather than
    trusted from a USB stick), and a machine that has not booted the app yet has
    not earned it. Treating "no sentinel" as "no runtime" made every incremental
    install go looking for a runtime the payload never claimed to carry, and then
    fail naming a path that was never supposed to exist.

    So: if the tree is there, verify it HERE and earn the sentinel. That is the
    same work first boot would have done, just earlier.
    """
    if rstore.is_complete(fingerprint):
        return True
    if not rstore.path_for(fingerprint).is_dir():
        return False
    log.info("驗證這台機器上既有的 runtime %s …", fingerprint)
    try:
        rstore.ensure_verified(fingerprint)
    except Exception as exc:                      # 驗不過就當它不在,讓下面去要一份
        log.warning("既有 runtime %s 驗證失敗:%s", fingerprint, exc)
        return False
    return True


def _stage_runtime(rstore: RuntimeStore, provider: UpdateProvider,
                   release: ReleaseMetadata, log) -> None:
    fingerprint = release.runtime_fingerprint
    if _runtime_already_here(rstore, fingerprint, log):
        return
    if not _provider_carries(provider, "has_runtime", release):
        raise UpdateError(
            f"這個更新包不含 runtime {fingerprint},而這台機器上也沒有這一份。\n"
            "  代表新版換了 Python 相依,增量更新包不夠用。\n"
            "  請改用「完整交付」包,或在打包工具匯出更新包時勾選「包含 runtime」。")
    staging = rstore.runtimes / f".staging-{uuid.uuid4().hex}"
    try:
        with held(store_gc_lock(rstore.runtimes.parent), timeout=_STAGE_LOCK_TIMEOUT):
            log.info("下載 runtime %s …", fingerprint)
            provider.download_runtime(release, staging)
            meta_fp = None
            try:
                meta_fp = json.loads(
                    (staging / RUNTIME_META).read_text("utf-8")).get("fingerprint")
            except (OSError, ValueError):
                pass
            if meta_fp != fingerprint:
                raise UpdateError(
                    f"runtime 指紋不符:payload 記錄 {meta_fp!r},release 宣告 {fingerprint!r}")
            problems = integrity.verify_tree(staging, extra_excluded={RUNTIME_META})
            if problems:
                raise UpdateError(f"runtime 驗證失敗({len(problems)} 項):{problems[:5]}")
            with runtime_lock(rstore.runtimes, fingerprint):
                target = rstore.path_for(fingerprint)
                if rstore.is_complete(fingerprint):
                    return  # someone else finished while we downloaded
                if target.exists():
                    integrity.remove_complete(target)
                    shutil.rmtree(target, ignore_errors=True)
                staging.rename(target)
                integrity.write_complete(target)   # earned here, never trusted
                log.info("runtime %s 已就緒", fingerprint)
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _stage_shell(deps_dir: Path, provider: UpdateProvider,
                 release: ReleaseMetadata, log) -> None:
    """Same contract as the runtime: copy → verify → move → earn the sentinel."""
    fingerprint = release.shell_fingerprint
    if not fingerprint:
        return  # legacy release: the shell still rides inside the version dir
    sstore = ShellStore(deps_dir)
    if sstore.is_complete(fingerprint):
        return
    if sstore.path_for(fingerprint).is_dir():
        # Same story as the runtime: it is already here, just not yet vouched for.
        problems = integrity.verify_tree(sstore.path_for(fingerprint))
        if not problems:
            integrity.write_complete(sstore.path_for(fingerprint))
            return
        log.warning("既有 Tauri 殼 %s 驗證失敗:%s", fingerprint, problems[:3])
    if not hasattr(provider, "download_shell") \
            or not _provider_carries(provider, "has_shell", release):
        raise UpdateError(
            f"這個更新包不含 Tauri 殼 {fingerprint},而這台機器上也沒有這一份。\n"
            "  請改用「完整交付」包。")
    staging = sstore.shells / f".staging-{uuid.uuid4().hex}"
    try:
        with held(store_gc_lock(deps_dir), timeout=_STAGE_LOCK_TIMEOUT):
            log.info("下載 Tauri 殼 %s …", fingerprint)
            provider.download_shell(release, staging)
            problems = integrity.verify_tree(staging)
            if problems:
                raise UpdateError(f"Tauri 殼驗證失敗({len(problems)} 項):{problems[:5]}")
            target = sstore.path_for(fingerprint)
            if target.exists():
                integrity.remove_complete(target)
                shutil.rmtree(target, ignore_errors=True)
            target.parent.mkdir(parents=True, exist_ok=True)
            staging.rename(target)
            integrity.write_complete(target)
            log.info("Tauri 殼 %s 已就緒", fingerprint)
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _stage_version(paths: paths_mod.AppPaths, provider: UpdateProvider,
                   release: ReleaseMetadata, log) -> None:
    """Copy → verify every byte → move into place → THEN write .complete.

    The sentinel is written here, on this machine, only after verify_tree() has
    passed. It is never copied in from the payload: a .complete that travelled on
    a USB stick would assert "these bytes are intact" about a copy that had not
    happened yet.
    """
    target = paths.version_dir(release.version)
    if integrity.is_complete(target):
        return
    paths.staging_dir.mkdir(parents=True, exist_ok=True)
    staging = paths.staging_dir / uuid.uuid4().hex
    try:
        with held(store_gc_lock(paths.deps_dir), timeout=_STAGE_LOCK_TIMEOUT):
            log.info("下載版本 %s …", release.version)
            provider.download_app(release, staging)
            manifest = paths_mod.load_manifest(staging)
            for key, expected in (("app_id", release.app_id), ("version", release.version),
                                  ("runtime_fingerprint", release.runtime_fingerprint)):
                if manifest.get(key) != expected:
                    raise UpdateError(
                        f"版本 manifest {key} 不符:{manifest.get(key)!r} != {expected!r}")
            problems = integrity.verify_tree(staging)
            if problems:
                raise UpdateError(f"版本驗證失敗({len(problems)} 項):{problems[:5]}")
            paths.versions_dir.mkdir(parents=True, exist_ok=True)
            if target.exists():  # a previous failed install; it has no sentinel
                shutil.rmtree(target, ignore_errors=True)
            staging.rename(target)
            integrity.write_complete(target)
            log.info("版本 %s 已就緒", release.version)
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def stage_release(paths: paths_mod.AppPaths, rstore: RuntimeStore,
                  provider: UpdateProvider, release: ReleaseMetadata, log) -> None:
    """Everything a release needs, on disk and verified — but nothing promoted.

    Order matters: the version dir is the last thing to become visible, so a
    version can never be pending while the runtime or shell it names is missing.
    """
    _stage_runtime(rstore, provider, release, log)
    _stage_shell(paths.deps_dir, provider, release, log)
    _stage_version(paths, provider, release, log)


def check_once(paths: paths_mod.AppPaths, store: state_mod.StateStore,
               rstore: RuntimeStore, provider: UpdateProvider, *,
               notify=notifications.notify, log=logging.getLogger("updater")) -> str:
    """One check-stage-notify pass. Returns a short outcome tag (for tests/logs)."""
    current = store.load()
    release = provider.get_latest_release(paths.app_id, current.current)
    if release is None:
        return "no-release"
    if release.version in (current.current, current.pending):
        return "up-to-date"
    if current.is_failed(release.version, release.revision):
        log.info("略過 %s:先前啟動失敗且 revision 未變", release.version)
        return "skipped-failed"

    stage_release(paths, rstore, provider, release, log)

    def set_pending(state: state_mod.AppState) -> state_mod.AppState:
        if state.pending == release.version or state.current == release.version:
            return state  # someone raced us; nothing to do
        return state_mod.set_pending(state, release.version, revision=release.revision)

    store.mutate(set_pending)
    notify("更新已就緒",
           f"新版本 {release.version} 已準備完成。\n關閉並重新開啟 App 後將自動套用。")
    return "staged"


def background_check(paths: paths_mod.AppPaths, store: state_mod.StateStore,
                     rstore: RuntimeStore, *, notify=notifications.notify,
                     log=logging.getLogger("updater")) -> str:
    """What bootstrap fires after health check. Never raises: a broken update
    source must not take down a healthy app."""
    try:
        source = paths.config().get("update_source")
        if not source:
            return "no-update-source"
        provider = FolderUpdateProvider(Path(source))
        return check_once(paths, store, rstore, provider, notify=notify, log=log)
    except (UpdateError, ProviderError, state_mod.StateError, LockTimeout,
            paths_mod.LayoutError, OSError) as exc:
        # LockTimeout: GC is running (or another instance is staging). Not an
        # error the user can act on — the next start checks again.
        log.error("背景更新失敗(App 不受影響):%s", exc)
        return "error"
