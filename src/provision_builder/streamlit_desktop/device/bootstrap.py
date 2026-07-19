"""bootstrap — what start.bat runs; lives OUTSIDE every version directory.

Per start (spec §8.1):
    promote pending (verify first; quarantine on failure)
  → deep-verify the runtime on its first use
  → lease → spawn the CURRENT version's launcher with the CORRECT runtime
  → commit candidate→LKG when the launcher EXITS CLEANLY, still healthy
  → if a candidate dies: roll back and relaunch once.

Runs under ANY runtime found in deps/ (all modules stdlib-only); the app itself
always runs under the runtime its manifest names. Never touches a running
instance; never scans process names.


WHAT COMMITS A CANDIDATE TO last-known-good
===========================================
The healthy marker APPEARING is not the commit signal, and neither is the marker
merely EXISTING. What it SAYS is. launch.py gives the marker two bodies:

    "no-session"   a window came up, and the user never pressed Start. Streamlit's
                   server ran; the app's own script did not — not one line of it.
    <the app url>  the app was ASKED TO RUN (/control/start) and did not fail on
                   arrival.

Reading only `marker.exists()` is what made the most ordinary thing a user can do
— open the app, look at the portal, close the window without pressing Start —
produce exit 0 + a marker, and commit a version that had NEVER EXECUTED A LINE as
last-known-good. If that build was broken, the next launch died and the version we
"rolled back" to was the same broken build. The whole automatic-rollback promise
was dead on the commonest daily path.

The commit signal is the process EXITING CLEANLY with a marker that says the app
actually ran:

    marker body at exit | exit code | what happens to state
    --------------------+-----------+-------------------------------------------
    <a url>             | 0         | commit candidate -> last_known_good
    "no-session"        | 0         | NOTHING. The app never ran, so there is
                        |           | nothing to promote (it never proved itself)
                        |           | and nothing to blame (it never got the
                        |           | chance to fail). The version stays the
                        |           | candidate and is retried next launch.
                        |           | Emphatically NOT an EXIT_APP_FAILURE.
    absent              | 0         | never came up (or the launcher revoked the
                        |           | marker on its way out) -> treated as 3
    any                 | 3 / 4     | fail_candidate + roll back to the resolved
                        |           | target, marker or no marker
    never seen          | other !=0 | it never came up: treated as 3 (fail + roll back)
    SEEN                | other !=0 | NOTHING. Something outside our launcher ended a
                        |           | working window (Task Manager, a power event, a
                        |           | hard crash of the shell). Not committed either —
                        |           | it was not a clean exit — so the version stays
                        |           | ON TRIAL and gets to prove itself next launch.
    any                 | 5         | NOTHING. Not the version's fault.

A marker seen mid-session is only ever *recorded* (it starts the background
update check, which is what it is genuinely evidence for: a window that came up).


LAUNCHER EXIT-CODE CONTRACT (bootstrap <-> launcher/launch.py)
=============================================================
The launcher template owns the codes; bootstrap owns what they MEAN. Both sides
must agree, so the contract is written down here, once:

    0   OK. Clean exit (the user closed the window).
    3   APP FAILURE. The Streamlit app or its script died: import error, bad
        entrypoint, exception at start-up. VERSION-SPECIFIC.
    4   VERSION INTEGRITY FAILURE. THIS VERSION's own tree is wrong: a bad
        manifest, a files.json mismatch inside apps/<app>/versions/<ver>/, the
        entrypoint the manifest names is missing. VERSION-SPECIFIC.
    5   ENVIRONMENT FAILURE. THIS MACHINE is broken, in a way no version can fix:
        no WebView2 runtime, an .exe quarantined by antivirus, a SHARED component
        (deps/runtimes/<fp>, deps/shells/<fp>) missing or failing verification, a
        location bootstrap cannot even write its log to. **NOT version-specific.**

Only 3 and 4 mark the candidate failed and roll back. Rolling back on 5 is worse
than useless: the older version launches the same shell against the same missing
WebView2 — or names the same missing shared runtime — fails identically, and the
operator is left with an app that will not start AND a version they did not ask
to downgrade, while the real cause (install WebView2 / un-quarantine the folder /
re-copy the shared component) goes unmentioned. Do that twice and the machine has
two versions in failed_versions and a false story about what happened.

So 5 touches NO state, blames NO version, claims NO rollback, and prints what to
DO about it. That is why runtime_store raises SharedComponentError for a missing
or corrupt SHARED tree: the shell and the runtime belong to the machine, not to
the version that happened to trip over them.

Any other non-zero code did NOT come out of our launcher's decision logic — 3, 4
and 5 are the only failures it ever chooses. An unknown code means something
OUTSIDE ended the process: Task Manager, a power event, a hard crash of the shell.
What we do with it depends on the one piece of evidence we have, the marker:

  * marker NEVER seen — nothing ever came up. We cannot prove it was environmental,
    and an app that dies before it can even open a window is what a bad version
    looks like. Treat it as 3: fail the candidate and roll back.
  * marker SEEN — a window came up, stood for its 12 seconds, and was then killed
    from outside. That is NOT evidence against the build. Marking a version failed
    is destructive and STICKY: the background updater then refuses to re-stage it,
    and the operator has to run --clear-failed to undo. We only pay that price on
    evidence we actually have. So: no fail_candidate, no rollback — and no commit
    either, because it was not a clean exit and the LKG promotion has not been
    earned. The version stays the candidate and gets another chance next launch.
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
import stat
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

if __package__:
    from . import gc as gc_mod
    from . import integrity, leases, notifications, paths as paths_mod, state as state_mod, updater
    from . import update_signing
    from .identifiers import IdentifierError
    from .locks import AlreadyRunning, LockTimeout, acquire_single_instance, app_lock
    from .provider import FolderUpdateProvider, ProviderError
    from .runtime_store import (
        RuntimeStore, RuntimeStoreError, SharedComponentError, ShellStore)
else:  # loose files in <ROOT>/bootstrap/
    _HERE = Path(__file__).resolve().parent
    sys.path.insert(0, str(_HERE))
    import integrity
    import leases
    import notifications
    import paths as paths_mod
    import state as state_mod
    import update_signing
    import updater
    from identifiers import IdentifierError
    from locks import AlreadyRunning, LockTimeout, acquire_single_instance, app_lock
    from provider import FolderUpdateProvider, ProviderError
    from runtime_store import (
        RuntimeStore, RuntimeStoreError, SharedComponentError, ShellStore)

    # `gc` IS A BUILT-IN MODULE NAME, and BuiltinImporter sits ahead of PathFinder in
    # sys.meta_path — so a plain `import gc` here hands us Python's garbage collector,
    # NOT the gc.py lying right next to us, no matter what we put on sys.path. And
    # this is the branch that actually SHIPS (start.bat runs `python bootstrap\
    # bootstrap.py`, so __package__ is empty), while every test imports us as a
    # package and takes the branch above. A naive `import gc` would therefore be green
    # in CI and silently dead on every machine in the factory. Load it by path.
    import importlib.util as _importlib_util

    _gc_spec = _importlib_util.spec_from_file_location("cim_device_gc", _HERE / "gc.py")
    gc_mod = _importlib_util.module_from_spec(_gc_spec)
    # REGISTER IT BEFORE EXECUTING IT. gc.py declares @dataclass classes, and
    # dataclasses resolves a class's own module through sys.modules[cls.__module__] —
    # which is None for a module that was created from a spec but never registered, so
    # exec_module() dies with a bare AttributeError inside dataclasses.py. Every launch,
    # on every device, while the package-import branch the tests take is perfectly fine.
    sys.modules[_gc_spec.name] = gc_mod
    _gc_spec.loader.exec_module(gc_mod)

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
    """True when this version COULD be the suspect: 3, 4, and any unknown non-zero.

    "Could". For an unknown code, whether it actually IS the suspect depends on the
    marker (see is_unknown_failure and the module docstring): a window that came up
    and was then killed from outside is not evidence against the build.
    """
    return code != EXIT_OK and not is_environment_failure(code)


def is_unknown_failure(code: int) -> bool:
    """Non-zero, and NOT a code our launcher would ever choose.

    launch.py owns 3 (the app died), 4 (this version's tree is wrong) and 5 (the
    machine is broken); anything else — 1 from Task Manager, an access violation,
    a code we have never heard of — means the process was ended by something
    OUTSIDE our decision logic. It is the only class of failure whose meaning
    depends on whether a healthy window was ever seen.
    """
    return code not in (EXIT_OK, EXIT_APP_FAILURE, EXIT_VERSION_INTEGRITY,
                        EXIT_SHELL_ENVIRONMENT)


# The marker's "a window came up, but the app was never asked to run" body. This is
# the device half of a contract written down in launch.py (MARKER_NO_SESSION); the
# two files are delivered together and must never disagree about this string.
MARKER_NO_SESSION = "no-session"


def _marker_body(marker: Path) -> str | None:
    """What the healthy marker SAYS, or None when it is not there.

    Existence is not the signal — the body is. See the module docstring: a marker
    that says "no-session" is a window the user opened and closed without ever
    pressing Start, and promoting a version on that is how a build that never ran
    became the machine's idea of a safe fallback.
    """
    try:
        return marker.read_text("utf-8").strip()
    except OSError:
        return None


@dataclass(frozen=True)
class LaunchOutcome:
    """What one launcher session actually did — the exit code AND the evidence.

    start_app cannot decide an unknown non-zero code from the number alone: an
    app that never opened a window is a broken build, and an app that ran for two
    hours and was killed from Task Manager is not. Only run_version watches the
    marker, so the evidence has to travel back with the code.
    """
    code: int
    marker_seen: bool = False       # a window came up at some point
    marker_at_exit: bool = False    # …and the marker was still there when it ended
    app_ran: bool = False           # …and it said the app was actually ASKED TO RUN


_SHELL_ENVIRONMENT_HELP = (
    "應用程式外殼(Tauri / WebView2)無法啟動。這不是版本的問題,退回舊版也不會好。\n"
    "  最可能的原因:\n"
    "    1. 這台電腦沒有安裝 Microsoft Edge WebView2 Runtime"
    "(請執行 tools\\安裝WebView2.bat)。\n"
    "    2. 防毒軟體把外殼的 .exe 隔離或刪除了"
    "(請 IT 把整個安裝資料夾加進防毒排除清單)。\n"
    "  請先安裝 WebView2 Runtime,或把整個安裝資料夾加入防毒白名單,再重新啟動。\n"
    "  (版本狀態沒有任何變更,也沒有退回任何版本。)"
)


def _report_shared_component(paths: paths_mod.AppPaths, exc: SharedComponentError) -> int:
    """A SHARED tree is missing or corrupt: exit 5, blame nobody, fix nothing.

    This is the failure the exit-code contract exists for. deps/runtimes/<fp> and
    deps/shells/<fp> are used by EVERY version on the machine, so the version that
    tripped over one of them is not the suspect — and marking it failed (which is
    what mapping this to exit 4 did) costs the operator a good version, rolls back
    onto a version that fails identically, and reports "已恢復前一版本" about a
    recovery that never happened.
    """
    log.error("共用元件不可用(這台機器的問題,不是版本的問題):%s", exc)
    print(f"\n[bootstrap][ERROR] 這台機器的共用元件壞了,退版救不了。\n"
          f"  {exc}\n"
          f"{exc.advice()}\n"
          f"  記錄:{paths.data_dir / 'logs'}\n", file=sys.stderr, flush=True)
    return EXIT_SHELL_ENVIRONMENT


# ── pending promotion (spec §8.1 top half) ───────────────────────────────────

class VerifyProgress:
    """「正在驗證共用元件…(x/y)」 while ensure_verified() hashes the shared runtime.

    First boot on a factory machine deep-verifies ~500 MB of python-build-standalone
    + site-packages. With progress=None that is a black window for minutes, on the
    one occasion the user has never seen the product before — indistinguishable from
    a hang. They power-cycle the PC, and now the verification restarts from zero.

    Throttled: a 12 000-file runtime must prove it is alive, not scroll the console.
    Printing can never break the run (cp950 consoles cannot encode everything, and a
    dead stdout must not cost the user their app), so _emit swallows what it cannot say.
    """

    INTERVAL = 1.0        # seconds between lines: enough to prove life, not spam

    def __init__(self, total: int = 0, *, out=None, clock=time.monotonic):
        self.total = max(int(total), 0)
        self.done = 0
        self._out = out
        self._clock = clock
        self._last = 0.0

    def _emit(self, message: str) -> None:
        try:
            print(message, file=self._out or sys.stdout, flush=True)
        except (UnicodeEncodeError, OSError, ValueError):
            pass

    def __call__(self, _rel: str) -> None:
        self.done += 1
        if self.done == 1:
            self._emit("[bootstrap] 正在驗證共用元件(第一次啟動要逐檔檢查,請稍候,不要關掉視窗)…")
        now = self._clock()
        final = bool(self.total) and self.done >= self.total
        if not final and (now - self._last) < self.INTERVAL:
            return
        self._last = now
        counted = f"{self.done}/{self.total}" if self.total else f"{self.done}"
        self._emit(f"  正在驗證共用元件…({counted})")
        if final:
            self._emit("[bootstrap] 共用元件驗證完成,正在啟動…")


def _verify_progress(rstore: RuntimeStore, fingerprint: str) -> VerifyProgress | None:
    """A progress callback ONLY when a deep verification is actually going to run.

    is_complete() is the same gate ensure_verified() uses, so an already-verified
    runtime (every start after the first) costs one stat() and prints nothing.
    """
    try:
        if rstore.is_complete(fingerprint):
            return None
        manifest = integrity.load_files_json(rstore.path_for(fingerprint))
        total = len(manifest.get("files") or [])
    except (integrity.IntegrityError, RuntimeStoreError, OSError, ValueError):
        # No file count available (a missing/corrupt files.json is ensure_verified's
        # problem to report, not ours) — count up instead of counting down.
        return VerifyProgress(0)
    return VerifyProgress(total)


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
            fingerprint = manifest["runtime_fingerprint"]
            try:
                # An update that ships a NEW runtime deep-verifies it here — the
                # same minutes of silence as a first boot, so the same progress.
                rstore.ensure_verified(fingerprint,
                                       progress=_verify_progress(rstore, fingerprint))
            except SharedComponentError as exc:
                # The SHARED runtime is missing/corrupt — that says nothing about
                # the pending version's own tree, which just verified byte for
                # byte. Quarantining it here would blacklist a good build (and
                # the updater would then refuse to re-stage it) because antivirus
                # ate a folder every version uses. Keep it pending, blame nobody,
                # and say what to fix.
                log.error("待套用的 %s 需要的共用 runtime 不可用,這次不套用也不標記失敗:%s",
                          current.pending, exc)
                print(f"\n[bootstrap][注意] 待套用的版本 {current.pending} 需要的共用元件"
                      f"目前不可用,所以這次仍然啟動 {current.current}。\n"
                      f"  {exc}\n"
                      f"{exc.advice()}\n"
                      f"  {current.pending} 沒有被標記為失敗,共用元件修好後會自動套用。\n",
                      file=sys.stderr, flush=True)
                return current
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


def commit_if_still_candidate(store: state_mod.StateStore, version: str) -> bool:
    """candidate → last_known_good, but only if `version` is still THE candidate.

    Re-read under the lock: while the app was open (hours), another process may
    have rolled back, promoted, or already resolved this candidate. Committing
    blind would set last_known_good=current for whatever `current` had become.
    """
    def commit(state: state_mod.AppState) -> state_mod.AppState:
        if state.candidate != version or state.current != version:
            return state
        return state_mod.commit_candidate(state)

    before = store.load()
    if before.candidate != version or before.current != version:
        log.info("不提交 %s:它已經不是 candidate(current=%s candidate=%s)",
                 version, before.current, before.candidate)
        return False
    store.mutate(commit)
    log.info("乾淨結束且健康:%s 已成為 last-known-good", version)
    return True


def run_version(paths: paths_mod.AppPaths, store: state_mod.StateStore,
                rstore: RuntimeStore, version: str, launcher_args: list[str], *,
                is_candidate: bool, notify=None,
                popen=subprocess.Popen) -> LaunchOutcome:
    # Resolved at call time, not bound as a default: a default freezes the real
    # MessageBox into the signature, where no test can get at it.
    notify = notify or notifications.notify
    problems = paths_mod.verify_version(paths, version, deep=False)
    if problems:
        raise BootstrapError(f"版本 {version} 不可啟動:{'; '.join(problems)}")
    vdir = paths.version_dir(version)
    manifest = paths_mod.load_manifest(vdir)
    fingerprint = manifest["runtime_fingerprint"]

    log.info("驗證 runtime %s(首次啟動會逐檔檢查,可能需要幾分鐘)…", fingerprint)
    rstore.ensure_verified(fingerprint, progress=_verify_progress(rstore, fingerprint))

    shell_exe = None
    if manifest.get("shell_fingerprint"):
        shell_exe = ShellStore(paths.deps_dir).resolve(
            manifest["shell_fingerprint"], manifest.get("shell_name", "cim-light.exe"))

    paths.ensure_data_dirs()
    marker = paths.data_dir / "tmp" / f"healthy-{uuid.uuid4().hex}"
    lease = leases.create_lease(paths.data_dir / "leases", app_id=paths.app_id,
                                version=version, runtime_fingerprint=fingerprint)
    updater_started = False
    try:
        proc = popen(
            [str(rstore.python_exe(fingerprint)), str(vdir / "launcher" / "launch.py"),
             *launcher_args],
            cwd=str(vdir), env=_launch_env(paths, marker, shell_exe),
        )
        log.info("launcher 已啟動 pid=%s(版本 %s)", proc.pid, version)

        # The marker appearing is NOT the commit signal — see the module docstring.
        # All it proves is that a window came up, which is exactly what the update
        # check needs to know and nothing more. RECORD it; mutate no state.
        marker_seen = False
        while proc.poll() is None:
            if not marker_seen and marker.exists():
                marker_seen = True
                log.info("healthy marker 出現:%s 的視窗起來了"
                         "(尚未提交為 last-known-good:要等乾淨結束)", version)
                if not updater_started:
                    updater_started = True
                    threading.Thread(
                        target=updater.background_check, daemon=True,
                        args=(paths, store, rstore),
                        kwargs={"notify": notify, "log": log},
                    ).start()
            time.sleep(0.5)

        code = proc.returncode
        # Re-READ the marker after the exit — its BODY, not just its existence. The
        # launcher deletes it (_revoke_marker) when the app it was hosting dies, and
        # writes the app's URL into it only once the app was actually asked to run.
        # So a URL body + a clean exit is the one thing that proves this version
        # worked; "no-session" proves only that a window opened.
        body = _marker_body(marker)
        marker_now = body is not None
        app_ran = bool(body) and body != MARKER_NO_SESSION
        log.info("launcher 結束 code=%s marker=%s(app 真的被啟動過=%s;"
                 "啟動途中曾經出現視窗=%s)",
                 code, body if marker_now else "(不存在)", app_ran, marker_seen)

        seen = LaunchOutcome(code, marker_seen=marker_seen, marker_at_exit=marker_now,
                             app_ran=app_ran)
        if code == EXIT_OK and app_ran:
            if is_candidate:
                commit_if_still_candidate(store, version)
            return seen
        if code == EXIT_OK and marker_now:
            # "no-session": the window came up and the user closed it WITHOUT ever
            # pressing Start. Streamlit's server ran; the app did not. There is
            # nothing to promote (the version never proved itself) and nothing to
            # blame (it never got the chance to fail). Touch NO state — and above
            # all do NOT fall through to EXIT_APP_FAILURE below: failing a version
            # because the user did not feel like using it would roll the machine
            # back for nothing. It stays the candidate and is retried next launch.
            log.info("launcher exit 0,但使用者始終沒有按 Start(marker=%s):"
                     "%s 這次根本沒跑起來過,既不提交為 last-known-good,也不算失敗;"
                     "下次啟動再驗證一次", MARKER_NO_SESSION, version)
            return seen
        if code == EXIT_OK:
            # Exit 0 with no marker at all: it never came up, or it came up and then
            # the app died and the launcher revoked the marker. Either way nothing
            # proved healthy, and a 0 here is a lie the caller must not act on.
            log.warning("launcher exit 0 但 marker 不在(曾經出現過=%s):"
                        "視為 app 啟動失敗", marker_seen)
            return LaunchOutcome(EXIT_APP_FAILURE, marker_seen=marker_seen,
                                 marker_at_exit=marker_now)
        # Non-zero. 3 and 4 blame the version and 5 blames the machine, marker or
        # no marker — a version that came up and THEN died is exactly the failure
        # the LKG latch used to hide. An unknown code is the one case the marker
        # decides (start_app): our launcher never chose it, so something outside
        # ended the process, and a window that was working is not evidence of a
        # bad build.
        return seen
    finally:
        lease.release()
        try:
            marker.unlink(missing_ok=True)
        except OSError:
            pass


def resolve_rollback_target(paths: paths_mod.AppPaths,
                            state: state_mod.AppState) -> str | None:
    """The best version we can actually fall back to, checked against the disk.

    state.rollback_target() only knows last_known_good and previous, and both can
    be gone, half-copied, or themselves in failed_versions. When they are, the
    machine may STILL have an intact older version sitting in versions\\ — the
    honest answer is to use it, not to report "rolled back" and relaunch the same
    broken build.

    Returns None when there is genuinely nothing left; callers must then fail
    loudly rather than pretend a rollback happened.

    THERE IS NO `force` HERE, and there must not be. This function answers "where
    would the machine take itself?", and the answer may never be a version in
    failed_versions — least of all now that fail_candidate() records the build we
    just fled in `previous`, which is the second slot this scan reads. A `force`
    flag that skipped is_failed() (which is what the dead one used to do, and no
    caller ever passed) would march the machine straight back onto the version that
    stopped the line ten seconds ago. An operator who genuinely wants a failed build
    already has the explicit route — `--rollback-to <版本> --force`, where they name
    it themselves — and that path does its own is_failed check, on purpose.
    """
    def usable(version: str | None) -> bool:
        if not version or version == state.current:
            return False
        if state.is_failed(version):
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


def _report_abnormal_session(paths: paths_mod.AppPaths, version: str, code: int) -> int:
    """A working window was ended from OUTSIDE. Blame nobody; promote nobody.

    Our launcher only ever chooses 3, 4 or 5, so an unknown code after a healthy
    window means Task Manager, a power event, or a hard crash of the shell took the
    process down. Marking a version failed is destructive and sticky — the updater
    then refuses to re-stage it and the operator must run --clear-failed to undo —
    so we do not pay that price for "the user killed a window that was working".

    Nor do we commit it: the session did not end cleanly, so the LKG promotion has
    not been earned. The version stays the candidate and proves itself next launch.
    """
    log.warning("launcher exit %s(非我們的 launcher 會選的代碼,而且視窗曾經正常起來):"
                "版本 %s 未被標記為失敗、未回退、也未提交為 last-known-good", code, version)
    print(f"\n[bootstrap][注意] App 這次是「非正常結束」的(代碼 {code}),"
          f"但它先前已經正常開啟過視窗。\n"
          f"  常見原因:從工作管理員強制結束、電腦直接斷電、外殼當掉。\n"
          f"  這不算 {version} 的錯:沒有把它標記為失敗,也沒有退回任何版本。\n"
          f"  但它也還沒通過驗證(要「正常關閉視窗」才算),下次啟動會再試一次。\n"
          f"  記錄:{paths.data_dir / 'logs'}\n", flush=True)
    return code


# THE DEAD END: this version will not start, and there is nothing on the machine to
# fall back to. Now that store_builder ships state.json with candidate=current, a
# one-version delivery whose FIRST BOOT fails lands here — the single most dangerous
# moment in the product's life, on a machine with no previous version by definition.
# "It failed and there is nothing to roll back to" is honest, and it is also a line
# operator standing in front of a dead machine with no next step. There IS a next
# step, and it is the only one that gets the line running again: put a working version
# on with a USB stick.
_NO_WAY_BACK_HELP = (
    "  這台機器上沒有任何可以退回的版本(通常表示這是第一次安裝,還沒有「上一版」)。\n"
    "  請用 tools\\admin.bat → [3] 套用已複製進來的更新包,\n"
    "  從 USB / 網路芳鄰把一個「可以用的版本」裝上去。\n"
    "  (命令列:bootstrap\\bootstrap.py --install <更新資料夾>)\n"
)


def _report_broken_non_candidate(paths: paths_mod.AppPaths, store: state_mod.StateStore,
                                 version: str, code: int) -> int:
    """A version that is NOT on trial has failed (3/4). Say what to DO about it.

    Automatic rollback only ever fires for a CANDIDATE — a version that has not yet
    completed a session on this machine. This one is not a candidate, and there are
    two ways to get here, both real:

      * a version that already proved itself and has now gone bad (a file went
        missing, antivirus ate a DLL, the disk rotted); or
      * far more common, a FRESHLY DELIVERED tree. store_builder writes state.json
        with candidate=None, so the very first boot of a delivery is not on trial at
        all — and 讀我 promises the operator an automatic rollback that, on that path,
        was never going to happen.

    Either way nobody is coming to roll it back, and returning the code in silence
    left the operator staring at a window that will not open, with no next step. So
    print one: the version is broken and here is the button that fixes it.
    """
    target = resolve_rollback_target(paths, store.load())
    log.error("launcher exit %s:版本 %s 不是 candidate,不會自動退版", code, version)
    print(f"\n[bootstrap][ERROR] 版本 {version} 啟動失敗(代碼 {code})。\n"
          f"  這一版壞了。請執行 tools\\admin.bat → [2] 退回上一版。\n"
          + (f"  會退回到:{target}\n"
             f"  (命令列:bootstrap\\bootstrap.py --rollback;"
             f"要指定版本用 --rollback-to <版本>。)\n" if target else _NO_WAY_BACK_HELP)
          + f"  記錄:{paths.data_dir / 'logs'}\n", file=sys.stderr, flush=True)
    return code


def start_app(paths: paths_mod.AppPaths, launcher_args: list[str], *,
              notify=None, popen=subprocess.Popen) -> int:
    """ONE app, ONE instance — then run the current version.

    Double-clicking start.bat twice used to start a second EVERYTHING: a second
    launcher, a second Streamlit on a second port, and two processes writing one
    state.json. The store lock did not stop it, because locks.acquire() WAITS
    (timeout=30) and then proceeds anyway — the slowest possible way to do the wrong
    thing. locks.acquire_single_instance() answers immediately instead, and takes over
    the lock of an owner that died (pid + process start time, so a recycled pid cannot
    lock the app out of its own machine).

    EXIT 0 when somebody else already holds it. The user pressed Start twice; nothing
    is broken and their app IS running. A non-zero code here would be read by the rest
    of this module as a failed launch — it would blame the version, mark it failed and
    roll the machine back, all because a user double-clicked. So: say so, and leave.
    """
    try:
        instance = acquire_single_instance(paths.data_dir)
    except AlreadyRunning as exc:
        log.info("已經有一個 %s 在執行中,這次不再開一個:%s", paths.app_id, exc.owner)
        print(f"\n[bootstrap] {exc}\n", flush=True)
        return EXIT_OK
    try:
        return _start_app_locked(paths, launcher_args, notify=notify, popen=popen)
    finally:
        instance.release()


def _start_app_locked(paths: paths_mod.AppPaths, launcher_args: list[str], *,
                      notify=None, popen=subprocess.Popen) -> int:
    notify = notify or notifications.notify
    store = state_mod.StateStore(paths.state_dir)
    rstore = RuntimeStore(paths.deps_dir)

    current_state = promote_if_pending(paths, store, rstore)
    version = current_state.current
    is_candidate = current_state.candidate == version

    try:
        outcome = run_version(paths, store, rstore, version, launcher_args,
                              is_candidate=is_candidate, notify=notify, popen=popen)
    except SharedComponentError:
        # NOT this version's fault: the shared runtime/shell belongs to the
        # machine and every version points at the same one. main() turns this
        # into EXIT_SHELL_ENVIRONMENT (5) with actionable advice; nothing here
        # writes state, fails a version, or pretends to have rolled anything back.
        raise
    except (BootstrapError, RuntimeStoreError) as exc:
        if not is_candidate:
            raise  # a stable version failing is an environment problem: fail loud
        # We could not even get to the launcher and it was NOT a shared component:
        # this version's own tree did not check out (bad manifest, files.json
        # mismatch under versions/<ver>/). That is version-specific. No launcher
        # ever ran, so no window ever came up: no marker, no evidence, code 4.
        outcome = LaunchOutcome(EXIT_VERSION_INTEGRITY)
        log.error("candidate %s 無法啟動:%s", version, exc)

    code = outcome.code
    if code == EXIT_OK:
        return code
    if is_environment_failure(code):
        # SHARED failure. Rolling back would fail the same way and would also
        # cost the user a version they never asked to lose.
        return _report_environment_failure(paths, version, code)
    if is_unknown_failure(code) and outcome.marker_seen:
        # A working window, ended from outside our launcher (Task Manager, power
        # cut, a hard crash of the shell). failed_versions is a destructive, sticky
        # verdict — the updater will not re-stage a version that is in it, and only
        # --clear-failed takes it back out — so we spend it only on evidence we
        # actually have. This is not it. Nothing is failed, nothing is rolled back,
        # and nothing is committed either: the version is still on trial.
        #
        # Checked BEFORE `is_candidate`, because it is true of ANY version: killing a
        # working window from Task Manager is not evidence against the build, whether
        # or not that build happens to be on trial. Asking the wrong question first
        # told an operator who had just used End Task that 「這一版壞了」.
        return _report_abnormal_session(paths, version, code)
    if not is_candidate:
        # 3/4 (or an unknown code from a window that never came up) on a version
        # nobody is going to roll back for them — most often the first boot of a
        # fresh delivery, whose state.json has candidate=None. Do not return in
        # silence: tell them which button rolls it back.
        return _report_broken_non_candidate(paths, store, version, code)

    # The candidate died (spec §8.2). "Died" is a version-failure exit (3, 4, or an
    # unknown code from a session that never once came up) — NOT merely "a non-zero
    # exit". A version whose window came up and whose app then blew up 20 minutes
    # later, when the user pressed Start, IS a broken version and lands here; it
    # used to become last_known_good on the strength of that marker and then skip
    # this entire block, because commit_candidate() had cleared `candidate` and the
    # check below therefore always fell through.
    refreshed = store.load()
    if refreshed.candidate != version:
        return code  # someone else already resolved it
    target = resolve_rollback_target(paths, refreshed)
    if not target:
        # The first boot of a one-version delivery, failing. There is no "previous"
        # to go back to — there never was one — so this is not a rollback we can do
        # for them. Say what CAN be done instead of stopping at the bad news.
        log.error("無可用的回滾目標(LKG/previous/其他完整版本都沒有),維持失敗狀態")
        print(f"\n[bootstrap][ERROR] 版本 {version} 啟動失敗,而且沒有任何可以退回的版本。\n"
              f"{_NO_WAY_BACK_HELP}"
              f"  記錄:{paths.data_dir / 'logs'}\n", file=sys.stderr, flush=True)
        return code
    store.mutate(lambda s: state_mod.fail_candidate(s, target=target))
    notify("已恢復前一版本",
           f"新版本 {version} 啟動失敗,已自動恢復 {target}。\n"
           f"詳細記錄:{paths.data_dir / 'logs'}")
    log.warning("rollback:%s 啟動失敗,改起 %s", version, target)
    return run_version(paths, store, rstore, target, launcher_args,
                       is_candidate=False, notify=notify, popen=popen).code


# ── CLI ──────────────────────────────────────────────────────────────────────

def _store_root() -> Path:
    """<ROOT> of the deployed tree: bootstrap.py lives in <ROOT>\\bootstrap\\."""
    return Path(__file__).resolve().parents[1]


def _rotate_logs(paths: paths_mod.AppPaths) -> None:
    """RETENTION. Three producers write into data\\logs\\ and NOTHING ever deleted
    them: launcher-*.log and streamlit-*.log (launch.py) and bootstrap-*.log (us).
    On a factory machine that has been up for months that is a real part of what
    actually fills the disk — and until gc.py learned to see them, nobody was even
    counting it.

    This belongs where the logs are CREATED, which is here: every single launch goes
    through bootstrap. GC can only mop up afterwards; this is what stops the puddle.

    gc.stale_logs() keeps the newest LOG_KEEP_RECENT of each family PLUS anything
    written in the last hour, so the log of the session that is starting right now —
    and of one still running — is never a candidate.

    Housekeeping must NEVER cost the user their app: an old deployment whose gc.py
    predates rotate_logs, a permission error, a locked file — none of that is a reason
    to refuse to start. Swallow it and get on with the launch.
    """
    try:
        removed = gc_mod.rotate_logs(paths.data_dir / "logs")
    except Exception as exc:                  # noqa: BLE001 - see the docstring
        log.debug("log rotation skipped: %s", exc)
        return
    if removed:
        log.info("已清掉 %d 份舊記錄檔(每一種只留最近 %d 份,一小時內的一律保留)",
                 len(removed), gc_mod.LOG_KEEP_RECENT)


def _setup_logging(paths: paths_mod.AppPaths) -> None:
    paths.ensure_data_dirs()
    # BEFORE the handler opens today's file: rotate first and the new log is not even
    # a candidate for deletion (it would be spared as "fresh" anyway).
    _rotate_logs(paths)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[logging.FileHandler(paths.data_dir / "logs" / f"bootstrap-{stamp}.log",
                                      encoding="utf-8"),
                  logging.StreamHandler(sys.stdout)],
    )


def _report_unwritable_location(paths: paths_mod.AppPaths, exc: OSError) -> int:
    """We cannot even open our own log file. Say so in Chinese, in the terms of
    the machine it happened on.

    This used to run OUTSIDE main()'s try block, so on a locked-down production
    machine or a read-only USB stick the very first thing the product ever showed
    a line operator was an English Python traceback out of logging.FileHandler.
    It is an ENVIRONMENT failure (exit 5): no version is at fault, nothing is
    written, nothing is rolled back.
    """
    print(f"\n[bootstrap][ERROR] 這台機器不讓程式寫入自己的資料夾,連記錄檔都建立不了。\n"
          f"  想寫入:{paths.data_dir}\n"
          f"  系統回報:{exc}\n"
          f"  最可能的原因與做法:\n"
          f"    1. 這份程式放在唯讀的位置(唯讀 USB、光碟、唯讀的網路磁碟機)。"
          f"請整個資料夾複製到本機硬碟(例如 D:\\)再執行。\n"
          f"    2. 這台機器不給這個資料夾寫入權限。"
          f"請 IT 給目前登入的使用者「修改」權限,或改放到使用者自己的磁碟位置。\n"
          f"    3. 防毒軟體擋住寫入。請 IT 把整個安裝資料夾加進防毒排除清單(白名單)。\n"
          f"  這是「這台機器」的問題,不是版本的問題:"
          f"沒有任何版本被標記為失敗,也沒有退回任何版本。\n",
          file=sys.stderr, flush=True)
    return EXIT_SHELL_ENVIRONMENT


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


# What each last_operation kind DID, in the words of the person reading it. The kinds
# are state.py's (plus store_builder's "deliver", which is written on the build machine
# and travels to the device inside state.json).
_OPERATION_LABELS = {
    "initialize": "初次安裝",
    "deliver": "出廠交付",
    "set_pending": "設定待套用版本",
    "promote": "套用更新",
    "commit_candidate": "確認版本可用",
    "clear_bad_pending": "取消不合格的更新",
    "rollback": "自動退版",
    "manual_rollback": "手動退版",
}


def _local_time(stamp: str | None) -> str | None:
    """state.json's UTC stamp, as the clock on the WALL of the room the machine is in.

    The question this screen answers is 「這台機器昨晚發生了什麼事?」, and the person
    asking it compares what we print against the shift log next to them. A Z-suffixed
    UTC string is not an answer to that; 2026-07-14 06:12 is.
    """
    if not stamp:
        return None
    try:
        moment = datetime.strptime(str(stamp), "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return str(stamp)          # some other format: raw beats nothing
    return (moment.replace(tzinfo=timezone.utc).astimezone()
            .strftime("%Y-%m-%d %H:%M"))


def describe_operation(op: dict) -> str | None:
    """「自動退版(v1.1.0 → v1.0.0),2026-07-14 06:12」

    THE single most useful line for 「這台機器昨晚發生了什麼事?」, and --status never
    printed it. state.json has recorded last_operation since the first commit — kind,
    status, timestamp, and (now) the versions it moved between — and the status screen
    read every other field and skipped this one, so the only way to answer the question
    was to open state.json on the machine and read raw JSON down the phone.

    Degrades instead of failing: an operation written by an older build has no
    from/to, and an unknown kind prints as itself rather than vanishing.
    """
    if not op:
        return None
    label = _OPERATION_LABELS.get(op.get("kind")) or op.get("kind")
    if not label:
        return None
    moved_from, moved_to = op.get("from_version"), op.get("to_version")
    if moved_from and moved_to:
        label = f"{label}({moved_from} → {moved_to})"
    elif op.get("version"):
        label = f"{label}({op['version']})"
    when = _local_time(op.get("timestamp_utc"))
    return f"{label},{when}" if when else label


def _slot_health(paths: paths_mod.AppPaths, version: str | None) -> str:
    """Does the version this field NAMES actually still exist on disk?

    state.json is just a set of names. A user who deletes a version folder in
    Explorer leaves those names pointing at nothing — and a status screen that
    reads only state.json will cheerfully report 「目前版本 v1.0.0」 for a version
    whose files are gone, the exact 'the report says fine, the disk says
    otherwise' lie this whole project exists to kill. So before we print a name,
    look for its bytes."""
    if not version:
        return ""
    vdir = paths.version_dir(version)
    if not vdir.is_dir():
        return "  ← ⚠ 版本資料夾不見了(可能被手動刪除),請重新安裝或退到其他版本"
    if not (vdir / ".complete").is_file():
        return "  ← ⚠ 版本檔案不完整(缺 .complete),這個版本不能用"
    return ""


def print_status(paths: paths_mod.AppPaths) -> int:
    """One screen an operator can read down the phone."""
    state = state_mod.StateStore(paths.state_dir).load()
    print(f"\n應用      : {paths.app_id}")
    # The slot health warning WINS over the candidate note: a folder that is gone
    # is a bigger fact than a folder that has not passed first-launch yet.
    current_note = _slot_health(paths, state.current) or (
        "  ← 尚未通過首次啟動驗證" if state.candidate == state.current else "")
    print(f"目前版本  : {state.current}{current_note}")
    # NEVER the same version in two fields. fail_candidate() now moves `previous`, so
    # a rollback leaves the two describing two different things — but a machine that
    # rolled back under an OLDER build still has previous == current sitting in its
    # state.json, and 「目前版本 v1.0.0 / 上一版 v1.0.0」 read down a phone is precisely
    # the confusion this screen exists to prevent. In that state there is no known
    # older version, so say that instead of saying it twice.
    previous = state.previous if state.previous != state.current else None
    note = ("  ← 啟動失敗過,已退回目前版本"
            if previous and state.is_failed(previous) else "")
    print(f"上一版    : {previous or '(無)'}{note or _slot_health(paths, previous)}")
    lkg = state.last_known_good
    print(f"最後可用  : {lkg or '(尚未有)'}"
          + (_slot_health(paths, lkg) if lkg and lkg != state.current else ""))
    print(f"待套用    : {state.pending or '(無)'}")
    source = paths.config().get("update_source")
    print(f"更新來源  : {source or '(未設定;用 --set-update-source 指定)'}")
    if state.failed_versions:
        print("啟動失敗過:")
        for entry in state.failed_versions:
            print(f"  · {entry.get('version')}  (revision {entry.get('revision') or '未知'})")
    last = describe_operation(state.last_operation)
    if last:
        print(f"最後動作  : {last}")
    target = resolve_rollback_target(paths, state)
    if target:
        print(f"\n可退回到  : {target}")
    elif _already_on_last_known_good(state):
        # Not a dead end — the opposite. See rollback_now().
        print(f"\n可退回到  : (不需要:{state.current} 就是最後確認可用的版本)")
    else:
        print("\n可退回到  : (沒有可退回的版本)")
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


def _already_on_last_known_good(state: state_mod.AppState) -> bool:
    """We are standing ON the last version that is known to have worked.

    This is NOT a dead end, and telling the operator it is one is the harmful part:
    it is what an EARLIER rollback was supposed to achieve. The automatic path lands
    here every time it does its job — v1.1.0 dies, we fall back to v1.0.0, and
    last_known_good has said v1.0.0 all along.
    """
    return bool(state.last_known_good) and state.last_known_good == state.current


def _report_already_on_last_known_good(state: state_mod.AppState) -> int:
    """「你已經在最好的版本上了」 — and that is a 0, not an ERROR.

    What this used to print instead: [ERROR], three guessed reasons (「都不存在、不完整,
    或本身就在失敗清單裡」 — one of which is true and we know which), and, worst of all,
    「--rollback-to <版本> --force」 pointed at the list of "other" versions on the disk,
    which after an automatic rollback consists of exactly the version that just failed.
    Forcing the machine back onto that build is the one action guaranteed to break the
    line again, and we were recommending it, in red, with an exit code that told any
    script calling us that the rollback had failed.
    """
    failed = [e.get("version") for e in state.failed_versions
              if e.get("version") and e.get("version") != state.current]
    fled = failed[-1] if failed else None
    happened = describe_operation(state.last_operation)
    print(f"[bootstrap] 目前的 {state.current} 就是最後一個確認可用的版本,"
          f"不需要、也不能再退。\n"
          + (f"  ({fled} 啟動失敗,系統已經自動退回 {state.current} 了。)\n" if fled else "")
          + (f"  最後一次動作:{happened}\n" if happened else "")
          + f"  這台機器上沒有更舊、而且確認可用的版本可以退。\n"
          f"  如果 {state.current} 現在也有問題,請用 tools\\admin.bat → [3] "
          f"裝一個新的版本上來(命令列:--install <更新資料夾>)。",
          flush=True)
    return 0


def rollback_now(paths: paths_mod.AppPaths) -> int:
    """Operator-initiated rollback (the automatic one only fires on a failed
    first start; a version that starts but behaves wrongly needs this)."""
    store = state_mod.StateStore(paths.state_dir)
    state = store.load()
    target = resolve_rollback_target(paths, state)
    if target:
        return _apply_rollback(paths, store, state, target)
    if _already_on_last_known_good(state):
        # 「無路可退」 and 「你已經在最好的版本上了」 are not the same machine, and they
        # were being printed the same way. This one is working as designed.
        return _report_already_on_last_known_good(state)

    # A real dead end: nothing here has ever been proven, and there is nothing older
    # to fall back to. Do NOT return 0 — "nothing to roll back to" used to be reported
    # as a success by the automatic path, so an operator whose only good version had
    # been GC'd was told the rollback worked and then hit the same crash.
    installed = _installed_versions(paths)
    # NEVER offer --force at a version we have already watched fail: that is the one
    # move that reliably re-breaks the machine. Failed builds are listed, separately,
    # as what they are.
    failed = [v for v in installed if state.is_failed(v)]
    others = [v for v in installed if v != state.current and v not in failed]
    print(f"[bootstrap][ERROR] 沒有任何「曾經成功啟動過、而且比現在舊」的版本可以退回"
          f"(目前 {state.current})。\n"
          f"  last-known-good / 上一版 都不存在、不完整,或本身就在失敗清單裡。\n"
          f"  這台機器上已安裝:{'、'.join(installed) or '(無)'}\n"
          + (f"  啟動失敗過(不可以退回去):{'、'.join(failed)}\n" if failed else "")
          + (f"  只剩比較新、而且從沒成功啟動過的版本({'、'.join(others)}),\n"
             f"  退到那裡不叫退回——所以不會自動這樣做。\n"
             f"  真的要用它:--rollback-to <版本>。\n" if others else "")
          + f"  建議用 --install 裝一個確定可用的版本上來。",
          file=sys.stderr, flush=True)
    return 1


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

def _payload_candidates(folder: Path) -> dict:
    """{app_id: the child folder that holds ITS release.json}.

    Keyed by what each release.json SAYS, never by what its folder is called: the
    exporter writes <out>/<app_id>/, but the operator copies it to a stick and
    renames it 「v1.1.0更新包」, and after that the folder name is decoration. A
    directory nobody can read, or one with no release.json in it, is simply not a
    candidate — a USB stick has junk on it, and junk is not an error.
    """
    found: dict = {}
    try:
        children = sorted(Path(folder).iterdir())
    except OSError:
        return found
    for child in children:
        if not child.is_dir() or not (child / "release.json").is_file():
            continue
        app_id = FolderUpdateProvider.from_payload_dir(child).payload_app_id()
        if app_id and app_id not in found:
            found[app_id] = child
    return found


def _payload_root(payload_dir: Path, app_id: str) -> Path:
    """The folder that actually holds release.json — the payload dir itself, or
    the folder it sits in.

    export_update(root, app_id, ver, out) returns <out>/<app_id>/ and writes
    release.json there — but the operator copies "the whole thing" onto a stick
    and then points --install at whatever they see. Both are unambiguous, so
    accept both rather than teaching a factory the difference.

    What we must NOT do is remember only the PARENT and re-derive the payload as
    <parent>/<app_id>: the folder is only called <app_id> until somebody renames
    it to 「v1.1.0更新包」 — an entirely human thing to do — and then --install
    goes looking for a release.json at a path that has never existed. Whatever we
    find here is what gets read (see FolderUpdateProvider.from_payload_dir).
    """
    payload_dir = Path(payload_dir)
    if not payload_dir.is_dir():
        raise BootstrapError(f"找不到更新資料夾:{payload_dir}")
    if (payload_dir / "release.json").is_file():
        return payload_dir
    nested = payload_dir / app_id
    if (nested / "release.json").is_file():
        return nested
    # …and the same folder AFTER somebody renamed it. The name is decoration; the
    # app_id inside release.json is the fact. Without this, pointing --install at
    # the stick's ROOT worked only while the payload folder still had its original
    # name.
    renamed = _payload_candidates(payload_dir).get(app_id)
    if renamed is not None:
        return renamed
    raise BootstrapError(
        f"{payload_dir} 不是更新資料夾:裡面沒有 release.json。\n"
        f"  正確的更新資料夾長這樣:<資料夾>\\release.json + versions\\ + runtimes\\\n"
        f"  (資料夾叫什麼名字都可以,重點是 release.json 要在裡面。)")


# ── which app is an --install FOR? ───────────────────────────────────────────
#
# THE PAYLOAD SAYS, AND ONLY THE PAYLOAD SAYS. main() used to answer this with
# _resolve_app(root, args.app) — i.e. out of apps\, the list of apps ALREADY on the
# machine — before it had even looked at what it was being asked to install. On a
# machine running App A that made the second app impossible to deliver by update:
#
#   --app app-b --install <B's payload>   ->  「找不到 app 'app-b'」  (exit 2)
#         …which is a lie about the payload: B is not in apps\ BECAUSE we have not
#         installed it yet, which is the entire point of the command being run.
#   --install <B's payload>               ->  resolved to A, handed B's payload to
#         provider.get_latest_release("app-a"), and was refused as 「別的 app 的」
#         with advice to re-run with `--app app-b` — the command on the line above,
#         which cannot work. The two halves of the product sent the operator in a
#         circle.
#   --install <A's payload> on a 2-app store -> 「apps\ 下有 2 個 app,請用 --app
#         指定」, over a payload that names its app in the first line of its
#         release.json. We refused to read the answer we were standing on.
#
# So --install resolves the app from the payload. apps\ is then consulted for ONE
# question only, and it is a different question: is this app already installed?

def _resolve_install_app(payload_dir: Path, requested: str | None) -> tuple[str, Path]:
    """(app_id, the folder holding its release.json) — read out of the payload."""
    payload_dir = Path(payload_dir)
    if not payload_dir.is_dir():
        raise BootstrapError(f"找不到更新資料夾:{payload_dir}")

    if (payload_dir / "release.json").is_file():
        found = FolderUpdateProvider.from_payload_dir(payload_dir).payload_app_id()
        if not found:
            raise BootstrapError(
                f"{payload_dir / 'release.json'} 讀不到、或裡面沒有合法的 app_id。\n"
                f"  請重新從建置機匯出一份更新包。")
        if requested and requested != found:
            raise BootstrapError(
                f"這個資料夾是 {found!r} 的更新包,不是 {requested!r} 的。\n"
                f"  更新包:{payload_dir}\n"
                f"  要裝它:--app {found} --install <這個資料夾>(或直接不要加 --app)。")
        return found, payload_dir

    # A folder that CONTAINS payloads: the exporter's <out>\, or a USB stick's root.
    offered = _payload_candidates(payload_dir)
    if not offered:
        raise BootstrapError(
            f"{payload_dir} 不是更新資料夾:裡面沒有 release.json。\n"
            f"  正確的更新資料夾長這樣:<資料夾>\\release.json + versions\\ + runtimes\\\n"
            f"  (資料夾叫什麼名字都可以,重點是 release.json 要在裡面。)")
    if requested:
        if requested not in offered:
            raise BootstrapError(
                f"這個資料夾裡沒有 {requested!r} 的更新包。\n"
                f"  {payload_dir}\n"
                f"  裡面有的是:{'、'.join(sorted(offered)) or '(沒有任何更新包)'}")
        return requested, offered[requested]
    if len(offered) == 1:
        return next(iter(offered.items()))
    raise BootstrapError(
        f"這個資料夾裡有 {len(offered)} 個 app 的更新包,請用 --app 指定要裝哪一個:\n"
        f"  {payload_dir}\n"
        + "\n".join(f"  · --app {app_id} --install \"{payload_dir}\""
                    for app_id in sorted(offered)))


# A payload is an UPDATE, not a delivery. It carries versions\ (+ runtimes\ and
# shells\ when they changed) and nothing else — no start-<app>.bat, no
# messages\*.txt for that bat to `type`, no tools\admin-<app>.bat. Those are
# per-app, they are generated on the BUILD machine by store_builder, and no amount
# of payload will conjure them here.
#
# So installing a brand-new app from a payload could only ever produce a version
# tree on disk that the operator has no way to start: the app would be "installed"
# and invisible. Worse, store_builder's own rule for a second app is that the
# generic start.bat is REMOVED in favour of start-<app>.bat — a device-side
# --install that started renaming App A's launcher out from under the line's
# desktop shortcut is not a thing this command may do.
#
# The build machine's 「匯出完整交付」 already delivers an app INTO an existing
# store (it writes the per-app bats, rewrites the admin console's chooser, and
# leaves the apps already there alone). That is the fix, so that is what we name.
# What we may NOT do is pretend, or half-do it.
def _refuse_new_app(app_id: str, payload_root: Path, installed: list) -> str:
    return (
        f"這是「更新包」,不是「完整交付」,而 {app_id!r} 還沒有安裝在這台機器上。\n"
        f"  更新包:{payload_root}\n"
        f"  這台機器目前有的 app:{'、'.join(installed) or '(一個也沒有)'}\n"
        f"  更新包裡只有版本內容,沒有這個 App 的啟動檔(start-{app_id}.bat)、"
        f"訊息檔與管理主控台;\n"
        f"  就算把版本裝進去,也沒有任何辦法啟動它。\n"
        f"  第一次安裝這個 App,請用建置機匯出的「完整交付」資料夾"
        f"(在打包工具裡選「匯出完整交付」)。\n"
        f"  它會補上啟動檔,並且不會動到這台機器上已經有的 app。\n"
        f"  裝好一次之後,以後的改版就可以用 --install 這個更新包了。")


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
    # Read the payload from the folder we ACTUALLY found release.json in — not
    # from <its parent>/<app_id>, which only exists while nobody has renamed or
    # copied the folder.
    provider = FolderUpdateProvider.from_payload_dir(root)
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
    # A disconnected UNC root does not consistently behave like a missing local
    # path on Windows.  Depending on the redirector/cache state, stat() can raise
    # WinError 53/64/67 instead of returning ENOENT.  An offline update share is a
    # valid configuration (VPN down, factory file server unavailable); only a path
    # we positively reached and proved to be a regular/non-directory file is an
    # input error.  Probe once so the warning branch cannot trigger a second stat
    # with a different result.
    try:
        source_stat = path.stat()
    except OSError:
        source_stat = None
    source_is_dir = bool(source_stat and stat.S_ISDIR(source_stat.st_mode))
    if source_stat is not None and not source_is_dir:
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
    if not source_is_dir:
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

    root = _store_root()
    try:
        # AppPaths validates the app id, so a hand-renamed apps\<dir> ("我的 App")
        # raises here rather than as a raw IdentifierError traceback.
        payload_root = None
        if args.install:
            # --install asks the PAYLOAD which app this is. Resolving it out of
            # apps\ first is what made the second app undeliverable: see
            # _resolve_install_app. This runs BEFORE _setup_logging() on purpose —
            # that calls ensure_data_dirs(), and an app we are about to refuse must
            # not leave an apps\<id>\data\ skeleton behind as a souvenir.
            app_id, payload_root = _resolve_install_app(Path(args.install), args.app)
            installed = paths_mod.list_app_ids(root)
            if app_id not in installed:
                raise BootstrapError(_refuse_new_app(app_id, payload_root, installed))
        else:
            app_id = _resolve_app(root, args.app)
        paths = paths_mod.AppPaths(root, app_id)
    except (BootstrapError, IdentifierError) as exc:
        print(f"[bootstrap][ERROR] {exc}", file=sys.stderr, flush=True)
        return 2
    except OSError as exc:
        print(f"[bootstrap][ERROR] 讀不到 {root / 'apps'}:{exc}\n"
              f"  如果這份程式放在網路磁碟機或 USB 上,請先整個複製到本機硬碟再執行。",
              file=sys.stderr, flush=True)
        return EXIT_SHELL_ENVIRONMENT

    try:
        _setup_logging(paths)
    except OSError as exc:
        # Read-only stick, locked-down factory PC, antivirus blocking writes:
        # an environment failure, and the operator's FIRST contact with the
        # product. Never a traceback.
        return _report_unwritable_location(paths, exc)

    try:
        if args.status:
            return print_status(paths)
        if args.install:
            # payload_root is the folder we PROVED holds this app's release.json,
            # so install_payload never has to re-derive it from a folder name.
            return install_payload(paths, payload_root or Path(args.install),
                                   force=args.force)
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
            # --set-pending 是繞過 updater 的手動路徑;簽章政策在這裡同樣成立,
            # 否則「把未簽章版本手動拷進 versions\ 再 set-pending」就是一條後門。
            try:
                update_signing.check_version_signature(
                    paths.version_dir(args.set_pending), config=paths.config(),
                    trust_path=paths.app_dir / update_signing.TRUST_STORE_NAME)
            except update_signing.SignaturePolicyError as exc:
                raise BootstrapError(f"發行者簽章檢查未過:{exc}") from exc
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
                               is_candidate=False).code

        return start_app(paths, passthrough)
    except SharedComponentError as exc:
        # THE mapping this contract turns on: a SHARED runtime/shell that is
        # missing or corrupt is exit 5 (this machine), never exit 4 (this
        # version). It must be caught BEFORE RuntimeStoreError below, which is
        # its base class.
        return _report_shared_component(paths, exc)
    except (BootstrapError, state_mod.StateError, RuntimeStoreError,
            paths_mod.LayoutError, ProviderError, updater.UpdateError,
            integrity.IntegrityError, LockTimeout) as exc:
        log.error("%s", exc)
        print(f"\n[bootstrap][ERROR] {exc}\n  記錄:{paths.data_dir / 'logs'}",
              file=sys.stderr, flush=True)
        return 2
    except OSError as exc:
        # Disk full, USB yanked mid-copy, a file the app still has open. Same
        # shape as the logging failure: the machine, not the version — and the
        # operator got a raw traceback for it until now.
        log.error("檔案系統操作失敗:%s", exc)
        print(f"\n[bootstrap][ERROR] 磁碟或檔案操作失敗,這是「這台機器」的問題:\n"
              f"  {exc}\n"
              f"  請檢查:磁碟空間是否不足、USB 是否被拔掉、"
              f"防毒軟體是否鎖住了安裝資料夾。\n"
              f"  沒有任何版本被標記為失敗,也沒有退回任何版本。\n"
              f"  記錄:{paths.data_dir / 'logs'}\n", file=sys.stderr, flush=True)
        return EXIT_SHELL_ENVIRONMENT


if __name__ == "__main__":
    raise SystemExit(main())
