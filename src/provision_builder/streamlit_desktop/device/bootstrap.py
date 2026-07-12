"""bootstrap — what start.bat runs; lives OUTSIDE every version directory.

Per start (spec §8.1):
    promote pending (verify first; quarantine on failure)
  → deep-verify the runtime on its first use
  → lease → spawn the CURRENT version's launcher with the CORRECT runtime
  → commit candidate→LKG when the launcher signals health
  → if a candidate dies before ever being healthy: roll back and relaunch once.

Runs under ANY runtime found in deps/ (all modules stdlib-only); the app itself
always runs under the runtime its manifest names. Never touches a running
instance; never scans process names.


LAUNCHER EXIT-CODE CONTRACT (bootstrap <-> launcher/launch.py)
=============================================================
The launcher template owns the codes; bootstrap owns what they MEAN. Both sides
must agree, so the contract is written down here, once:

    0   OK. Clean exit (the user closed the window).
    3   APP FAILURE. The Streamlit app or its script died: import error, bad
        entrypoint, exception at start-up. VERSION-SPECIFIC.
    4   VERSION INTEGRITY FAILURE. This version's own files are wrong: the
        entrypoint the manifest names is missing, the engine shim will not load.
        VERSION-SPECIFIC.
    5   SHELL / ENVIRONMENT FAILURE. The Tauri shell could not start: no WebView2
        runtime on this machine, the .exe was quarantined by antivirus, the GPU
        stack refused. **NOT version-specific** — the shell and WebView2 are
        SHARED across versions.

Only 3 and 4 mark the candidate failed and roll back. Rolling back on 5 is worse
than useless: the older version launches the same shell against the same missing
WebView2, fails identically, and the operator is left with an app that will not
start AND a version they did not ask to downgrade — while the real cause (install
WebView2 / un-quarantine the exe) goes unmentioned. So 5 prints the likely cause
and changes NOTHING.

Any other non-zero code (a hard crash, an access violation, a code we have never
heard of) is treated as 3: we cannot prove it is environmental, and the app dying
without ever reaching a healthy state is what a bad version looks like.
"""

from __future__ import annotations

import json
import sys

# Before anything else: we execute UNDER a shared, immutable runtime. Letting
# this very process write stdlib .pyc files into it would make the runtime fail
# its own byte-for-byte verification (the first real E2E caught exactly that).
sys.dont_write_bytecode = True

import argparse
import hashlib
import logging
import os
import re
import subprocess
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

if __package__:
    from . import integrity, leases, notifications, paths as paths_mod, state as state_mod, updater
    from .locks import LockTimeout, app_lock
    from .provider import FolderUpdateProvider, ProviderError
    from .runtime_store import RuntimeStore, RuntimeStoreError, ShellStore
else:  # loose files in <ROOT>/bootstrap/
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import integrity
    import leases
    import notifications
    import paths as paths_mod
    import state as state_mod
    import updater
    from locks import LockTimeout, app_lock
    from provider import FolderUpdateProvider, ProviderError
    from runtime_store import RuntimeStore, RuntimeStoreError, ShellStore

log = logging.getLogger("bootstrap")


class BootstrapError(Exception):
    pass


# ── launcher exit codes (see the module docstring for the full contract) ──────

EXIT_OK = 0
EXIT_APP_FAILURE = 3            # version-specific: the app/script died
EXIT_VERSION_INTEGRITY = 4      # version-specific: this version's files are wrong
EXIT_SHELL_ENVIRONMENT = 5      # SHARED: WebView2 missing, shell quarantined…


def is_environment_failure(code: int) -> bool:
    """True when a rollback cannot possibly help (the cause is outside the version)."""
    return code == EXIT_SHELL_ENVIRONMENT


def is_version_failure(code: int) -> bool:
    """True when this version is the suspect: 3, 4, and any unknown non-zero code."""
    return code != EXIT_OK and not is_environment_failure(code)


_SHELL_ENVIRONMENT_HELP = (
    "應用程式外殼(Tauri / WebView2)無法啟動。這不是版本的問題,退回舊版也不會好。\n"
    "  最可能的原因:\n"
    "    1. 這台電腦沒有安裝 Microsoft Edge WebView2 Runtime。\n"
    "    2. 防毒軟體把外殼的 .exe 隔離或刪除了。\n"
    "  請先安裝 WebView2 Runtime,或把整個安裝資料夾加入防毒白名單,再重新啟動。\n"
    "  (版本狀態沒有任何變更,也沒有退回任何版本。)"
)


# ── pending promotion (spec §8.1 top half) ───────────────────────────────────

def promote_if_pending(paths: paths_mod.AppPaths, store: state_mod.StateStore,
                       rstore: RuntimeStore) -> state_mod.AppState:
    """One locked critical section: load → verify pending → flip or quarantine."""
    with app_lock(paths.state_dir):
        current = store.load()
        if not current.pending:
            return current

        problems = paths_mod.verify_version(paths, current.pending, deep=True)
        if not problems:
            manifest = paths_mod.load_manifest(paths.version_dir(current.pending))
            try:
                rstore.ensure_verified(manifest["runtime_fingerprint"])
            except RuntimeStoreError as exc:
                problems = [str(exc)]

        if problems:
            log.error("pending %s 驗證失敗,保持現行版本:%s",
                      current.pending, "; ".join(problems[:5]))
            return store.write_locked(state_mod.clear_bad_pending(current))

        log.info("promote:%s -> current(前版 %s 保留為 previous)",
                 current.pending, current.current)
        return store.write_locked(state_mod.promote_pending(current))


# ── launching (spec §8.1 bottom half) ────────────────────────────────────────

def _launch_env(paths: paths_mod.AppPaths, marker: Path, shell_exe: Path | None) -> dict:
    env = dict(
        os.environ,
        CIM_APP_DATA=str(paths.data_dir),
        CIM_HEALTHY_MARKER=str(marker),
        PYTHONDONTWRITEBYTECODE="1",
        PYTHONUTF8="1",
    )
    if shell_exe is not None:
        # The shell is shared (deps/shells/<fp>/), so it sits OUTSIDE the version
        # directory — bootstrap resolves and validates it, the launcher just uses it.
        env["CIM_SHELL_EXE"] = str(shell_exe)
    return env


def run_version(paths: paths_mod.AppPaths, store: state_mod.StateStore,
                rstore: RuntimeStore, version: str, launcher_args: list[str], *,
                is_candidate: bool, notify=notifications.notify,
                popen=subprocess.Popen) -> int:
    problems = paths_mod.verify_version(paths, version, deep=False)
    if problems:
        raise BootstrapError(f"版本 {version} 不可啟動:{'; '.join(problems)}")
    vdir = paths.version_dir(version)
    manifest = paths_mod.load_manifest(vdir)
    fingerprint = manifest["runtime_fingerprint"]

    log.info("驗證 runtime %s(首次啟動會逐檔檢查,可能需要幾分鐘)…", fingerprint)
    rstore.ensure_verified(fingerprint, progress=None)

    shell_exe = None
    if manifest.get("shell_fingerprint"):
        shell_exe = ShellStore(paths.deps_dir).resolve(
            manifest["shell_fingerprint"], manifest.get("shell_name", "cim-light.exe"))

    paths.ensure_data_dirs()
    marker = paths.data_dir / "tmp" / f"healthy-{uuid.uuid4().hex}"
    lease = leases.create_lease(paths.data_dir / "leases", app_id=paths.app_id,
                                version=version, runtime_fingerprint=fingerprint)
    committed = not is_candidate
    updater_started = False
    try:
        proc = popen(
            [str(rstore.python_exe(fingerprint)), str(vdir / "launcher" / "launch.py"),
             *launcher_args],
            cwd=str(vdir), env=_launch_env(paths, marker, shell_exe),
        )
        log.info("launcher 已啟動 pid=%s(版本 %s)", proc.pid, version)

        healthy = False
        while proc.poll() is None:
            if not healthy and marker.exists():
                healthy = True
                if not committed:
                    store.mutate(state_mod.commit_candidate)
                    committed = True
                    log.info("health check 通過:%s 已成為 last-known-good", version)
                if not updater_started:
                    updater_started = True
                    threading.Thread(
                        target=updater.background_check, daemon=True,
                        args=(paths, store, rstore),
                        kwargs={"notify": notify, "log": log},
                    ).start()
            time.sleep(0.5)
        healthy = healthy or marker.exists()
        code = proc.returncode
        log.info("launcher 結束 code=%s healthy=%s", code, healthy)
        if healthy:
            return code
        # Died without ever becoming healthy. A 0 here is a lie the caller must
        # not act on (nothing ever came up), so call it what it is: an app failure.
        return code if code != EXIT_OK else EXIT_APP_FAILURE
    finally:
        lease.release()
        try:
            marker.unlink(missing_ok=True)
        except OSError:
            pass


def resolve_rollback_target(paths: paths_mod.AppPaths, state: state_mod.AppState,
                            *, force: bool = False) -> str | None:
    """The best version we can actually fall back to, checked against the disk.

    state.rollback_target() only knows last_known_good and previous, and both can
    be gone, half-copied, or themselves in failed_versions. When they are, the
    machine may STILL have an intact older version sitting in versions\\ — the
    honest answer is to use it, not to report "rolled back" and relaunch the same
    broken build.

    Returns None when there is genuinely nothing left; callers must then fail
    loudly rather than pretend a rollback happened.
    """
    def usable(version: str | None) -> bool:
        if not version or version == state.current:
            return False
        if not force and state.is_failed(version):
            return False
        return not paths_mod.verify_version(paths, version, deep=False)

    for version in (state.last_known_good, state.previous):
        if usable(version):
            return version

    # Neither slot survived. We may still fall back to an intact version on disk
    # — but ONLY to an OLDER one, and never to the update that is merely staged.
    #
    # The first version of this scan took "newest intact version, name-sorted".
    # On a machine whose only good build was v1.0.0 with v1.1.0 freshly installed
    # and pending, `--rollback` therefore rolled FORWARD onto a version that had
    # never once booted, and marked the working v1.0.0 as failed on the way out.
    # Rollback that lands you somewhere less proven than where you started is not
    # a rollback; refusing and saying so is strictly better.
    staged = {state.pending, state.candidate}
    older = [v for v in _installed_versions(paths)
             if v not in staged and usable(v) and _sorts_before(v, state.current)]
    return max(older, key=_version_key) if older else None


def _version_key(version: str) -> tuple:
    """v1.10.2 sorts after v1.9.0 — string order gets that backwards."""
    parts = re.findall(r"\d+", version or "")
    return tuple(int(p) for p in parts) if parts else ()


def _sorts_before(version: str, current: str | None) -> bool:
    """Is `version` demonstrably older than `current`?

    "Demonstrably" is the point: if either name carries no numbers we cannot
    order them, and guessing is how you roll forward onto an unproven build. No
    answer means no automatic rollback — the operator still has
    `--rollback-to <版本>`, which is an explicit choice rather than our guess.
    """
    left, right = _version_key(version), _version_key(current or "")
    return bool(left) and bool(right) and left < right


def _report_environment_failure(paths: paths_mod.AppPaths, version: str, code: int) -> int:
    log.error("launcher exit %s(shell/環境失敗):版本 %s 未被標記為失敗,狀態未變更",
              code, version)
    print(f"\n[bootstrap][ERROR] {_SHELL_ENVIRONMENT_HELP}\n"
          f"  記錄:{paths.data_dir / 'logs'}\n", file=sys.stderr, flush=True)
    return code


def start_app(paths: paths_mod.AppPaths, launcher_args: list[str], *,
              notify=notifications.notify, popen=subprocess.Popen) -> int:
    store = state_mod.StateStore(paths.state_dir)
    rstore = RuntimeStore(paths.deps_dir)

    current_state = promote_if_pending(paths, store, rstore)
    version = current_state.current
    is_candidate = current_state.candidate == version

    try:
        code = run_version(paths, store, rstore, version, launcher_args,
                           is_candidate=is_candidate, notify=notify, popen=popen)
    except (BootstrapError, RuntimeStoreError) as exc:
        if not is_candidate:
            raise  # a stable version failing is an environment problem: fail loud
        # We could not even get to the launcher: this version's tree or the
        # runtime/shell it names did not check out. That is version-specific
        # (a different version names a different runtime and shell).
        code = EXIT_VERSION_INTEGRITY
        log.error("candidate %s 無法啟動:%s", version, exc)

    if code == EXIT_OK:
        return code
    if is_environment_failure(code):
        # SHARED failure. Rolling back would fail the same way and would also
        # cost the user a version they never asked to lose.
        return _report_environment_failure(paths, version, code)
    if not is_candidate:
        return code

    # The candidate died before ever becoming healthy: roll back once (spec §8.2).
    refreshed = store.load()
    if refreshed.candidate != version:
        return code  # someone else already resolved it
    target = resolve_rollback_target(paths, refreshed)
    if not target:
        log.error("無可用的回滾目標(LKG/previous/其他完整版本都沒有),維持失敗狀態")
        print(f"\n[bootstrap][ERROR] 版本 {version} 啟動失敗,而且沒有任何可以退回的版本。\n"
              f"  記錄:{paths.data_dir / 'logs'}\n", file=sys.stderr, flush=True)
        return code
    store.mutate(lambda s: state_mod.fail_candidate(s, target=target))
    notify("已恢復前一版本",
           f"新版本 {version} 啟動失敗,已自動恢復 {target}。\n"
           f"詳細記錄:{paths.data_dir / 'logs'}")
    log.warning("rollback:%s 啟動失敗,改起 %s", version, target)
    return run_version(paths, store, rstore, target, launcher_args,
                       is_candidate=False, notify=notify, popen=popen)


# ── CLI ──────────────────────────────────────────────────────────────────────

def _setup_logging(paths: paths_mod.AppPaths) -> None:
    paths.ensure_data_dirs()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[logging.FileHandler(paths.data_dir / "logs" / f"bootstrap-{stamp}.log",
                                      encoding="utf-8"),
                  logging.StreamHandler(sys.stdout)],
    )


def _resolve_app(root: Path, requested: str | None) -> str:
    apps = paths_mod.list_app_ids(root)
    if requested:
        if requested not in apps:
            raise BootstrapError(f"找不到 app {requested!r};現有:{apps}")
        return requested
    if len(apps) == 1:
        return apps[0]
    raise BootstrapError(f"apps\\ 下有 {len(apps)} 個 app,請用 --app 指定:{apps}")


def _version_revision(paths: paths_mod.AppPaths, version: str) -> str:
    """Same content id the builder and export_update use, so a re-cut of a failed
    version is recognisably different from a retry of the identical bytes."""
    files_json = (paths.version_dir(version) / integrity.FILES_NAME).read_bytes()
    return hashlib.sha256(files_json).hexdigest()[:12]


def print_status(paths: paths_mod.AppPaths) -> int:
    """One screen an operator can read down the phone."""
    state = state_mod.StateStore(paths.state_dir).load()
    print(f"\n應用      : {paths.app_id}")
    print(f"目前版本  : {state.current}" + ("  ← 尚未通過首次啟動驗證"
                                            if state.candidate == state.current else ""))
    print(f"上一版    : {state.previous or '(無)'}")
    print(f"最後可用  : {state.last_known_good or '(尚未有)'}")
    print(f"待套用    : {state.pending or '(無)'}")
    source = paths.config().get("update_source")
    print(f"更新來源  : {source or '(未設定;用 --set-update-source 指定)'}")
    if state.failed_versions:
        print("啟動失敗過:")
        for entry in state.failed_versions:
            print(f"  · {entry.get('version')}  (revision {entry.get('revision') or '未知'})")
    target = resolve_rollback_target(paths, state)
    print(f"\n可退回到  : {target or '(沒有可退回的版本)'}")
    print(f"記錄位置  : {paths.data_dir / 'logs'}\n")
    return 0


def _leaving_revision(paths: paths_mod.AppPaths, state: state_mod.AppState) -> str | None:
    """The content id of the version we are rolling away from.

    It must land in failed_versions with the version, or the background updater
    finds the same release on the share, sees no matching failure entry, and
    re-stages the very build the operator just fled. A None here is the safe
    answer, not a missing one: is_failed() treats a revision-less entry as
    blocking EVERY revision of that version, so the worst case is a manual
    --clear-failed instead of an automatic crash loop.
    """
    if state.candidate == state.current and state.candidate_revision:
        return state.candidate_revision
    try:
        return _version_revision(paths, state.current)
    except (OSError, ValueError):
        return None


def _apply_rollback(paths: paths_mod.AppPaths, store: state_mod.StateStore,
                    state: state_mod.AppState, target: str) -> int:
    revision = _leaving_revision(paths, state)
    leaving = state.current
    store.mutate(lambda s: state_mod.rollback_to(s, target, revision=revision))
    log.warning("manual rollback:%s -> %s(%s 已記入 failed_versions)",
                leaving, target, leaving)
    print(f"[bootstrap] 已從 {leaving} 退回到 {target}。請重新啟動 App。\n"
          f"  {leaving} 已被記為「啟動失敗」,自動更新不會再把它裝回來。\n"
          f"  修好之後要再試一次:--clear-failed {leaving}", flush=True)
    return 0


def rollback_now(paths: paths_mod.AppPaths) -> int:
    """Operator-initiated rollback (the automatic one only fires on a failed
    first start; a version that starts but behaves wrongly needs this)."""
    store = state_mod.StateStore(paths.state_dir)
    state = store.load()
    target = resolve_rollback_target(paths, state)
    if not target:
        # Do NOT return 0 here. "Nothing to roll back to" used to be reported as
        # a success by the automatic path, so an operator whose only good version
        # had been GC'd was told the rollback worked and then hit the same crash.
        installed = _installed_versions(paths)
        others = [v for v in installed if v != state.current]
        print(f"[bootstrap][ERROR] 沒有任何「曾經成功啟動過、而且比現在舊」的版本可以退回"
              f"(目前 {state.current})。\n"
              f"  last-known-good / 上一版 都不存在、不完整,或本身就在失敗清單裡。\n"
              f"  這台機器上已安裝:{'、'.join(installed) or '(無)'}\n"
              + (f"  只剩比較新、而且從沒成功啟動過的版本({'、'.join(others)}),\n"
                 f"  退到那裡不叫退回——所以不會自動這樣做。\n" if others else "")
              + f"  你可以:--rollback-to <版本> --force 明確指定,或用 --install 重新安裝一版。",
              file=sys.stderr, flush=True)
        return 1
    return _apply_rollback(paths, store, state, target)


def _installed_versions(paths: paths_mod.AppPaths) -> list[str]:
    if not paths.versions_dir.is_dir():
        return []
    return sorted(p.name for p in paths.versions_dir.iterdir()
                  if p.is_dir() and not p.name.startswith("."))


def rollback_to_version(paths: paths_mod.AppPaths, version: str, *,
                        force: bool = False) -> int:
    """Explicit target — for when the operator knows which build was good."""
    store = state_mod.StateStore(paths.state_dir)
    state = store.load()
    if version == state.current:
        print(f"[bootstrap] {version} 已經是目前版本,不需要退回。", flush=True)
        return 1
    problems = paths_mod.verify_version(paths, version, deep=False)
    if problems:
        print(f"[bootstrap][ERROR] 退回目標 {version} 不可用:{'; '.join(problems[:5])}\n"
              f"  現有版本:{_installed_versions(paths) or '(無)'}",
              file=sys.stderr, flush=True)
        return 2
    if state.is_failed(version) and not force:
        print(f"[bootstrap][ERROR] {version} 在失敗清單裡(它自己啟動失敗過)。\n"
              f"  確定要用它:加 --force,或先 --clear-failed {version}。",
              file=sys.stderr, flush=True)
        return 2
    return _apply_rollback(paths, store, state, version)


# ── --install: the offline delivery path ─────────────────────────────────────

def _payload_root(payload_dir: Path, app_id: str) -> Path:
    """Accept either the app payload dir itself or the folder it sits in.

    export_update(root, app_id, ver, out) returns <out>/<app_id>/ and writes
    release.json there — but the operator copies "the whole thing" onto a stick
    and then points --install at whatever they see. Both are unambiguous, so
    accept both rather than teaching a factory the difference.
    """
    payload_dir = Path(payload_dir)
    if not payload_dir.is_dir():
        raise BootstrapError(f"找不到更新資料夾:{payload_dir}")
    if (payload_dir / "release.json").is_file():
        return payload_dir
    nested = payload_dir / app_id
    if (nested / "release.json").is_file():
        return nested
    raise BootstrapError(
        f"{payload_dir} 不是更新資料夾:裡面沒有 release.json。\n"
        f"  正確的更新資料夾長這樣:<資料夾>\\release.json + versions\\ + runtimes\\")


def install_payload(paths: paths_mod.AppPaths, payload_dir: Path, *,
                    force: bool = False) -> int:
    """Install an exported update payload from a USB stick or a share.

    Copy → verify every byte against the tree's OWN files.json → move into place
    → write .complete → set pending. The sentinel is written HERE, by this
    machine, and only after verification: export_update() strips it from every
    tree it copies precisely so that it has to be earned rather than trusted from
    a stick that may have been yanked mid-copy.

    This is exactly the path the background updater takes (updater.stage_release),
    which is why it is the same code: an update delivered by hand and an update
    delivered by the updater must not be able to differ in their safety checks.
    """
    root = _payload_root(payload_dir, paths.app_id)
    provider = FolderUpdateProvider(root.parent)
    release = provider.get_latest_release(paths.app_id, "")
    if release is None:
        raise BootstrapError(f"更新資料夾缺 release.json:{root}")

    store = state_mod.StateStore(paths.state_dir)
    state = store.load()
    if release.version == state.current and integrity.is_complete(
            paths.version_dir(release.version)):
        print(f"[bootstrap] {release.version} 已經是目前版本,不需要安裝。", flush=True)
        return 0
    if state.is_failed(release.version, release.revision) and not force:
        print(f"[bootstrap][ERROR] {release.version}(revision {release.revision or '未知'})"
              f"在失敗清單裡:它在這台機器上啟動失敗過。\n"
              f"  這是同一份內容,裝回去只會再失敗一次。\n"
              f"  真的要裝:加 --force。已修好並重新打包:內容變了,revision 會跟著變,"
              f"直接安裝即可。",
              file=sys.stderr, flush=True)
        return 2

    rstore = RuntimeStore(paths.deps_dir)
    try:
        updater.stage_release(paths, rstore, provider, release, log)
    except (updater.UpdateError, ProviderError) as exc:
        # verify_tree() failed (or a tree was missing). Nothing was moved into
        # place, nothing got a sentinel, pending is untouched — say so, because
        # the operator's next question is always "did it break my install?".
        print(f"\n[bootstrap][ERROR] 安裝失敗:{exc}\n"
              f"  複製過去的檔案已經刪掉,系統沒有任何變更(仍然是 {state.current})。\n"
              f"  最可能的原因:USB / 網路芳鄰上的檔案在複製途中損毀。\n"
              f"  請重新從來源匯出一份更新資料夾再試一次。\n"
              f"  記錄:{paths.data_dir / 'logs'}\n", file=sys.stderr, flush=True)
        return 2

    if release.version == state.current:
        print(f"[bootstrap] 已重新安裝 {release.version}(就是目前版本,不需要重啟)。",
              flush=True)
        return 0

    store.mutate(lambda s: state_mod.set_pending(s, release.version,
                                                 revision=release.revision or None))
    # Which version the user lands on if the new one will not start. Ask the real
    # resolver, against the state as it WILL be after the promote — otherwise it
    # happily answers "we would fall back to the version you are installing",
    # because that tree is now on disk and passes every check.
    promoted = state_mod.dataclasses.replace(
        state, current=release.version, previous=state.current,
        candidate=release.version)
    fallback = resolve_rollback_target(paths, promoted) or state.current
    print(f"\n[bootstrap] 已安裝 {release.version}"
          f"(下次啟動會套用;啟動失敗會自動退回 {fallback})\n"
          f"  請關閉並重新開啟 App。\n", flush=True)
    return 0


# ── --set-update-source: point a deployed machine at an update share ─────────

def set_update_source(paths: paths_mod.AppPaths, source: str) -> int:
    """Write apps/<app>/config.json {"update_source": ...}.

    Until now config.json was only ever written on the BUILD machine, so a tree
    that was already deployed could never be told where its updates live — the
    background updater read no update_source, returned "no-update-source", and
    auto-update simply never happened in the field. This is the way to fix that
    without rebuilding and re-copying the whole store.
    """
    text = str(source).strip().strip('"')
    if not text:
        raise BootstrapError("更新來源不可以是空的")
    path = Path(text)
    is_unc = text.startswith("\\\\") or text.startswith("//")
    if path.exists() and not path.is_dir():
        raise BootstrapError(f"更新來源必須是資料夾,但這是檔案:{path}")

    config_path = paths.app_dir / "config.json"
    config = paths.config()          # keep every other key the admin set
    # Store what the operator typed. str(Path(r"\\server\share")) appends a
    # trailing separator to a UNC root, and a path that comes back out of
    # config.json different from the way it went in is a support call.
    config["update_source"] = text
    config_path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(config, ensure_ascii=False, indent=2) + "\n"
    tmp = config_path.with_name(f".config-{uuid.uuid4().hex}.tmp")
    tmp.write_text(payload, encoding="utf-8")
    os.replace(tmp, config_path)     # atomic: never a half-written config

    print(f"\n[bootstrap] 更新來源已設定:{path}", flush=True)
    if not path.is_dir():
        # A share that is not mounted right now is normal (VPN down, stick out),
        # so this is a warning, not a failure — but say it, or the operator will
        # wait a week for an update that was never going to arrive.
        reason = "網路位置目前連不上" if is_unc else "這個資料夾現在不存在"
        print(f"[bootstrap][注意] {reason}:{path}\n"
              f"  設定已經寫進去了,等它連得上就會生效。\n"
              f"  如果是打錯字,請重新執行 --set-update-source。", flush=True)
    else:
        expected = path / paths.app_id / "release.json"
        if not expected.is_file():
            print(f"[bootstrap][注意] 這個資料夾裡還沒有 {paths.app_id}\\release.json。\n"
                  f"  更新來源要指向「匯出更新的那個資料夾」"
                  f"(裡面是 {paths.app_id}\\release.json + versions\\ + runtimes\\)。",
                  flush=True)
    print(f"  設定檔:{config_path}\n", flush=True)
    return 0


def clear_pending(paths: paths_mod.AppPaths) -> int:
    """Cancel a staged update before it applies.

    --install stages a version and sets it pending; the next launch promotes it.
    Between those two moments an operator can change their mind (the release was
    recalled; they installed it on the wrong machine) — and until now the only way
    back was to hand-edit state.json. The version stays on disk: cancelling an
    update is not the same as declaring it bad, so it does NOT go into
    failed_versions and can be re-armed with --set-pending.
    """
    store = state_mod.StateStore(paths.state_dir)
    state = store.load()
    if not state.pending:
        print("[bootstrap] 目前沒有待套用的版本,不需要取消。", flush=True)
        return 0
    pending = state.pending

    def clear(s: state_mod.AppState) -> state_mod.AppState:
        return state_mod.dataclasses.replace(s, pending=None, pending_revision=None)

    store.mutate(clear)
    print(f"[bootstrap] 已取消待套用的 {pending};下次啟動仍會用 {state.current}。\n"
          f"  {pending} 還在磁碟上,要再套用:--set-pending {pending}\n"
          f"  確定不要了:用 tools\\gc.bat 回收它的空間。", flush=True)
    return 0


def clear_failed(paths: paths_mod.AppPaths, version: str) -> int:
    """Let a fixed version be retried without inventing a new version number."""
    store = state_mod.StateStore(paths.state_dir)

    def clear(s: state_mod.AppState) -> state_mod.AppState:
        kept = [e for e in s.failed_versions if e.get("version") != version]
        if len(kept) == len(s.failed_versions):
            raise BootstrapError(f"{version} 不在失敗清單裡")
        return state_mod.dataclasses.replace(s, failed_versions=kept)

    store.mutate(clear)
    print(f"[bootstrap] 已清除 {version} 的失敗紀錄,可以再次嘗試。", flush=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="CIM Streamlit desktop bootstrap")
    parser.add_argument("--app", help="app id(apps\\ 下只有一個時可省略)")
    parser.add_argument("--status", action="store_true",
                        help="印出目前版本/上一版/最後可用/更新來源/失敗紀錄")
    parser.add_argument("--install", metavar="更新資料夾",
                        help="安裝一份匯出的更新(USB/網路芳鄰):複製→逐檔驗證→設為下次啟動套用。"
                             "驗證不過就完全不動系統")
    parser.add_argument("--set-update-source", metavar="路徑",
                        help="設定自動更新的來源資料夾(可用網路芳鄰 \\\\server\\share);"
                             "沒設定過的機器永遠不會自動更新")
    parser.add_argument("--rollback", action="store_true",
                        help="立刻退回上一個能用的版本(不必等它啟動失敗);"
                             "被退掉的版本會記為失敗,不會被自動裝回來")
    parser.add_argument("--rollback-to", metavar="VERSION",
                        help="退回到指定版本(必須已安裝且完整);"
                             "若它在失敗清單裡,要加 --force")
    parser.add_argument("--force", action="store_true",
                        help="搭配 --rollback-to / --install:忽略失敗清單")
    parser.add_argument("--clear-failed", metavar="VERSION",
                        help="清除某個版本的失敗紀錄,讓它可以再試一次")
    parser.add_argument("--clear-pending", action="store_true",
                        help="取消已安裝但還沒套用的更新(改變主意時用;版本會留在磁碟上)")
    parser.add_argument("--slot", choices=["PREV", "NEXT"],
                        help="診斷用:直接跑 previous/pending,不改任何狀態")
    parser.add_argument("--set-pending", metavar="VERSION",
                        help="管理員:手動把已複製好的版本設為待更新")
    args, passthrough = parser.parse_known_args(argv)

    root = Path(__file__).resolve().parents[1]
    try:
        app_id = _resolve_app(root, args.app)
    except BootstrapError as exc:
        print(f"[bootstrap][ERROR] {exc}", file=sys.stderr, flush=True)
        return 2
    paths = paths_mod.AppPaths(root, app_id)
    _setup_logging(paths)

    try:
        if args.status:
            return print_status(paths)
        if args.install:
            return install_payload(paths, Path(args.install), force=args.force)
        if args.set_update_source:
            return set_update_source(paths, args.set_update_source)
        if args.rollback_to:
            return rollback_to_version(paths, args.rollback_to, force=args.force)
        if args.rollback:
            return rollback_now(paths)
        if args.clear_failed:
            return clear_failed(paths, args.clear_failed)
        if args.clear_pending:
            return clear_pending(paths)

        if args.set_pending:
            problems = paths_mod.verify_version(paths, args.set_pending, deep=True)
            if problems:
                raise BootstrapError("版本驗證失敗:" + "; ".join(problems))
            revision = _version_revision(paths, args.set_pending)
            state_mod.StateStore(paths.state_dir).mutate(
                lambda s: state_mod.set_pending(s, args.set_pending, revision=revision))
            print(f"[bootstrap] 已設定 pending={args.set_pending},下次啟動自動套用")
            return 0

        if args.slot:
            current = state_mod.StateStore(paths.state_dir).load()
            version = current.previous if args.slot == "PREV" else current.pending
            if not version:
                raise BootstrapError(f"{args.slot} 槽是空的")
            print(f"[bootstrap] 診斷模式:直接跑 {args.slot}={version}(不改狀態)")
            return run_version(paths, state_mod.StateStore(paths.state_dir),
                               RuntimeStore(paths.deps_dir), version, passthrough,
                               is_candidate=False)

        return start_app(paths, passthrough)
    except (BootstrapError, state_mod.StateError, RuntimeStoreError,
            paths_mod.LayoutError, ProviderError, updater.UpdateError,
            integrity.IntegrityError, LockTimeout) as exc:
        log.error("%s", exc)
        print(f"\n[bootstrap][ERROR] {exc}\n  記錄:{paths.data_dir / 'logs'}",
              file=sys.stderr, flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
