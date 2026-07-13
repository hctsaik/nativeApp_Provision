"""Build into the store layout (spec §4/§9.3): immutable versions + shared
runtimes + the bootstrap, all under one deployable <ROOT>.

Build-machine side — may reach PyPI. The runtime is built once per dependency
fingerprint; a rebuild whose lock has not changed costs ~17MB, not ~457MB.

Two exports come out of this module, and they are NOT the same thing:

  * export_full_tree()  — 完整交付:a runnable <ROOT> (bootstrap, start bat,
    tools, a fresh state, ONE version + its rollback target, and the deps those
    two name). A machine that has never seen this app can run it by
    double-clicking. This is what "交付" means.
    It is NOT a copy of the build machine. A build machine's tree carries every
    version ever built, a `pending` update, and a failure history; a delivery
    that carries those is not a delivery, it is a leak (and `pending` is worse
    than a leak: the target PROMOTES it on first boot).
  * export_update()     — 自動更新來源:release.json + the version (+ deps when
    the runtime changed). Consumed by device/provider.py polling, or copied to
    the machine and applied with `bootstrap.py --install <payload>`.
    It is NOT runnable by itself and never was.

Which version does 交付 mean? Ask newest_version()/list_versions(), not
state.current: this is a BUILD machine, and it never launches what it builds, so
a freshly built version sits in `pending` while `current` stays on the version
the fleet already has.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from . import imports as imports_mod
from . import requirements as requirements_mod
from . import runtime as runtime_mod
from . import builder
from .builder import _rename_with_retry, scan_project
from .device import gc as gc_mod
from .device import integrity
from .device.identifiers import validate_identifier
from .device.paths import MANIFEST_NAME, MANIFEST_SCHEMA, AppPaths, list_app_ids
from .device.runtime_store import (
    BUILDER_FORMAT_VERSION,
    RUNTIME_META,
    LockfileError,
    RuntimeStore,
    ShellStore,
    compute_fingerprint,
    normalize_lock,
)
from .device.state import AppState, StateStore, set_pending
from .models import EXCLUDED_DIRS, EXCLUDED_FILES, BuildRequest, slugify

TEMPLATES = Path(__file__).resolve().parent / "templates"
DEVICE_DIR = Path(__file__).resolve().parent / "device"
Progress = Callable[[str], None]

README_NAME = "讀我-使用說明.txt"
WEBVIEW2_BAT_NAME = "安裝WebView2.bat"
WEBVIEW2_INSTALLER = "prereq/MicrosoftEdgeWebview2Setup.exe"
WEBVIEW2_DOWNLOAD = "https://go.microsoft.com/fwlink/p/?LinkId=2124703"
# The Evergreen WebView2 runtime registers itself under this client GUID. No
# WebView2 = the Tauri window opens blank, after a 60s startup the user waits
# through. Check it BEFORE starting anything.
WEBVIEW2_CLIENT = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"

# start.bat's WebView2 refusal is an ENVIRONMENT failure, not an app failure.
# bootstrap.py::EXIT_SHELL_ENVIRONMENT is 5, and everything downstream keys off
# that number: an environment code must never be written into failed_versions
# (the version is fine; the machine is not). Exiting 1 here told every wrapper
# "this build is broken" and, on a machine where the updater had just staged a
# perfectly good version, that is how a good version gets quarantined.
EXIT_SHELL_ENVIRONMENT = 5

# gc.py's exit codes, consumed by tools\gc.bat and tools\admin-*.bat. IMPORTED,
# not re-declared: a console that maps 4 to 「鎖被佔用」 while gc.py maps 4 to
# something else is a worse lie than the one we are fixing here. The point of
# consuming them at all is that the console stops blaming the store lock for every
# failure — 「回收失敗,沒有刪掉任何東西」 was printed even when GC had just deleted
# 400 MB and merely tripped on one folder Explorer had open. Any code NOT in this
# table falls through to a message that claims nothing it cannot know.
GC_EXIT_OK = gc_mod.EXIT_OK                    # 0: the plan went away (or was empty)
GC_EXIT_PARTIAL = gc_mod.EXIT_PARTIAL          # 2: some trees went, some would not
GC_EXIT_NOTHING = gc_mod.EXIT_NOTHING_DELETED  # 3: zero bytes reclaimed
GC_EXIT_LOCKED = gc_mod.EXIT_STORE_LOCKED      # 4: an update holds the store lock


def _noop(_msg: str) -> None:
    pass


@dataclass
class StoreBuildResult:
    ok: bool
    root: Path | None = None
    app_id: str | None = None
    version: str | None = None
    fingerprint: str | None = None
    runtime_reused: bool = False
    # What the operator must be told, because it is not what they assumed:
    pending_set: bool = False          # False on a first build (it becomes current)
    is_first_app: bool = True          # False when the tree already had another app
    entry_bats: list[str] = field(default_factory=list)
    removed_start_bat: bool = False    # a second app deletes the generic start.bat
    version_mb: float = 0.0
    added_mb: float = 0.0              # what this build actually cost on disk
    duration_seconds: float = 0.0
    cancelled: bool = False            # operator pressed cancel; no debris left behind
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def summary(self) -> str:
        if self.ok:
            reuse = "runtime 重用" if self.runtime_reused else "runtime 新建"
            return (f"OK — {self.root} @ {self.version}"
                    f"(本次新增 {self.added_mb:.0f} MB,{reuse}:{self.fingerprint})")
        if self.cancelled:
            return "已取消 — 沒有留下半成品版本"
        return "FAILED — " + "; ".join(self.errors)


@dataclass
class ExportResult:
    """What was written and what it is good for.

    `out_dir` is the folder the operator copies. export_update() used to return a
    bare Path, and its callers spell things like `out / "release.json"`, so this
    keeps working as a path-ish object rather than breaking every caller.

    `entry_bats` is the S8 fix. It is not decoration: in a multi-app tree the
    generic start.bat is DELETED in favour of start-<app>.bat, and with no field
    to carry that fact the GUI's completion dialog fell back to a hardcoded
    「雙擊 start.bat」. The operator handed the folder to the line and told them to
    double-click a file that does not exist in it. What is in this list is what
    was really written into `out_dir`.
    """
    out_dir: Path
    total_mb: float = 0.0
    apps: list[str] = field(default_factory=list)
    versions: list[str] = field(default_factory=list)
    includes_runtime: bool = False
    kind: str = "full"                 # "full" = 完整交付 / "update" = 自動更新來源
    entry_bats: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def __truediv__(self, other) -> Path:
        return self.out_dir / other

    def __fspath__(self) -> str:
        return str(self.out_dir)

    def entry_hint(self) -> str:
        """What to tell the person who receives the folder. One app: one file."""
        if not self.entry_bats:
            return "(這份匯出沒有可雙擊的啟動檔)"
        if len(self.entry_bats) == 1:
            return f"雙擊 {self.entry_bats[0]}"
        return "每個應用各有自己的啟動檔:" + "、".join(self.entry_bats)

    def summary(self) -> str:
        what = ("完整交付(可直接雙擊 start bat 執行)" if self.kind == "full"
                else "自動更新來源(給已經有這棵樹的機器)")
        deps = ",含共用 runtime 與 Tauri 殼" if self.includes_runtime else ",不含 runtime"
        lines = [f"{what}:{self.out_dir}({self.total_mb:.0f} MB{deps})",
                 f"  應用:{'、'.join(self.apps) or '(無)'}",
                 f"  版本:{'、'.join(self.versions) or '(無)'}"]
        if self.kind == "full":
            lines.append(f"  User 端入口:{self.entry_hint()}")
        lines += [f"  [注意] {w}" for w in self.warnings]
        return "\n".join(lines)


class StoreBuildError(Exception):
    pass


class _Cancelled(Exception):
    """Internal: the operator pressed cancel at a stage boundary."""


def _check_cancel(should_cancel: Callable[[], bool] | None) -> None:
    if should_cancel is not None and should_cancel():
        raise _Cancelled()


# ── runtime ──────────────────────────────────────────────────────────────────

def _python_version_of(python: Path) -> str:
    proc = subprocess.run([str(python), "-c", "import platform;print(platform.python_version())"],
                          capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise StoreBuildError(f"無法查詢 runtime 範本的 Python 版本:{proc.stderr.strip()}")
    return proc.stdout.strip()


def _freeze(python: Path) -> list[str]:
    proc = subprocess.run([str(python), "-m", "pip", "freeze", "--all"],
                          capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise StoreBuildError(f"pip freeze 失敗:{proc.stderr.strip()[:400]}")
    return proc.stdout.splitlines()


def _reconcile_lock_with_freeze(pins: list[str], freeze_lines: list[str]) -> list[str]:
    """Spec §7.1: the lock must be reconcilable with what pip actually installed."""
    installed = {}
    for line in freeze_lines:
        if "==" in line and not line.startswith("-"):
            name, _, ver = line.partition("==")
            installed[name.strip().lower().replace("_", "-")] = ver.strip()
    problems = []
    for pin in pins:
        name, _, ver = pin.partition("==")
        base = name.split("[", 1)[0]
        actual = installed.get(base)
        if actual is None:
            problems.append(f"{base} 宣告了卻沒被安裝")
        elif actual != ver:
            problems.append(f"{base} 宣告 {ver} 實裝 {actual}")
    return problems


def _strip_pip(runtime_dir: Path) -> None:
    """The store runtime is immutable; shipping pip is an invitation to mutate
    it in the field. setuptools stays (pkg_resources is imported at runtime)."""
    site = runtime_dir / "Lib" / "site-packages"
    for entry in list(site.glob("pip")) + list(site.glob("pip-*.dist-info")):
        shutil.rmtree(entry, ignore_errors=True)
    for exe in (runtime_dir / "Scripts").glob("pip*.exe"):
        try:
            exe.unlink()
        except OSError:
            pass


def ensure_runtime(root: Path, request: BuildRequest, pins: list[str],
                   progress: Progress = _noop) -> tuple[str, bool]:
    """Return (fingerprint, reused). Builds under .staging-* then renames.

    This function is about the RUNTIME, not about the app. The missing-import
    gate used to live at the bottom of the try-block below — i.e. AFTER the
    `return fingerprint, True` a few lines down — so the moment a runtime was
    reused (which is the entire point of the store layout, and the normal case
    for every version after the first) the gate never ran at all. It now lives
    in build_into_store(), which runs it whether the runtime was built or reused.
    """
    template_python = request.runtime_template / "python.exe"
    python_version = _python_version_of(template_python)
    abi = f"cp{python_version.split('.')[0]}{python_version.split('.')[1]}"
    fingerprint = compute_fingerprint(python_version=python_version,
                                      platform="win_amd64", abi=abi, pins=pins)
    store = RuntimeStore(root / "deps")
    if store.is_complete(fingerprint):
        progress(f"runtime {fingerprint} 已存在,跳過 457MB 安裝")
        return fingerprint, True

    store.runtimes.mkdir(parents=True, exist_ok=True)
    staging = store.runtimes / f".staging-{uuid.uuid4().hex[:8]}"
    build_log = staging / "build.log"
    try:
        progress(f"建立 runtime {fingerprint}(複製可攜 Python + pip install)…")
        python = runtime_mod.copy_runtime(request.runtime_template, staging)
        lock_file = staging / "lock.txt"
        lock_file.write_text("\n".join(pins) + "\n", encoding="utf-8")
        runtime_mod.install_requirements(python, lock_file, build_log, progress=progress)
        runtime_mod.verify_imports(python, build_log)

        problems = _reconcile_lock_with_freeze(pins, _freeze(python))
        if problems:
            raise StoreBuildError("lock 與 pip freeze 對帳失敗:" + "; ".join(problems[:5]))

        _strip_pip(staging)
        # Some wheels ship .pyc inside them regardless of --no-compile. Any that
        # survive get hashed into files.json and then dropped by the exporter →
        # integrity failure on the target machine, with no way for the operator
        # to fix it. Nothing compiled may enter a shared runtime.
        stripped = runtime_mod.strip_bytecode(staging)
        if stripped:
            progress(f"清掉 runtime 裡的 {stripped} 個 .pyc(共用 runtime 不含編譯快取)")
        for extra in (lock_file, build_log):
            extra.unlink(missing_ok=True)

        (staging / RUNTIME_META).write_text(json.dumps({
            "schema": 1, "fingerprint": fingerprint, "python_version": python_version,
            "platform": "win_amd64", "abi": abi, "pins": pins,
            "builder_format": BUILDER_FORMAT_VERSION,
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        progress("計算 runtime files.json(逐檔 sha256)…")
        integrity.write_files_json(staging, integrity.build_files_json(
            staging, extra_excluded={RUNTIME_META}))

        target = store.runtimes / fingerprint
        _rename_with_retry(staging, target)  # Defender may briefly pin the fresh tree
        integrity.write_complete(target)     # build machine counts as verified
        return fingerprint, False
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def runtime_python(root: Path, fingerprint: str) -> Path:
    """The interpreter this version will actually be launched under."""
    return RuntimeStore(Path(root) / "deps").path_for(fingerprint) / "python.exe"


def check_app_imports(root: Path, request: BuildRequest, fingerprint: str, *,
                      reused: bool = False, progress: Progress = _noop) -> list[str]:
    """The import gate, asked of the runtime that will really run the app.

    Returns the OPTIONAL-import warnings; raises StoreBuildError on a required
    one. It must run on every build, not only on the builds that install a
    runtime: the whole point of the store is that the 2nd..Nth app reuses a
    runtime someone else's lock produced, and that is exactly the build whose app
    is most likely to import something the shared runtime never installed.

    Ground truth is the interpreter, not the lock file: a distribution can be
    pinned and still not import (wrong ABI, a wheel that quietly failed), and a
    runtime built for another app can satisfy a lock this app never wrote.
    """
    python = runtime_python(root, fingerprint)
    progress("檢查 App 的每一個 import,共用 runtime 是不是真的都載得進來…")
    try:
        report = imports_mod.missing_dependencies(request.entrypoint, request.project_dir,
                                                  python)
    except imports_mod.ImportProbeError as exc:
        # We could not ASK. Not knowing is not the same as knowing the worst —
        # say it is our probe that broke, not the operator's project.
        raise StoreBuildError(
            f"沒辦法用交付包裡的 Python 檢查 App 的 import,這次不敢直接放行:\n  {exc}\n"
            f"  這份 runtime:{python.parent}") from exc

    if report:
        # A build that just paid six minutes for a runtime should be told the
        # runtime survived — and, honestly, that it may turn out to be an orphan
        # (adding the package to the lock moves the fingerprint; moving the import
        # into a function does not, and then the reinstall is free).
        kept = ("  這份共用 runtime 已經建好、留在 deps\\ 裡了,沒有白等;"
                "若最後用不到,tools\\gc.bat 可以回收它。\n") if not reused else ""
        raise StoreBuildError(
            report.failure_message() + "\n\n"
            + f"  檢查用的 runtime:{fingerprint}\n" + kept)
    return list(report.warning_lines())


# ── version + tree ───────────────────────────────────────────────────────────

def ensure_shell(root: Path, shell_exe: Path, progress: Progress = _noop) -> str:
    """Put the shell in the shared store, keyed by its content hash."""
    digest = hashlib.sha256(shell_exe.read_bytes()).hexdigest()[:12]
    fingerprint = f"shell-{digest}"
    store = ShellStore(root / "deps")
    target = store.path_for(fingerprint)
    if store.is_complete(fingerprint) and store.exe_for(fingerprint, shell_exe.name).is_file():
        progress(f"Tauri 殼 {fingerprint} 已存在,共用")
        return fingerprint

    store.shells.mkdir(parents=True, exist_ok=True)
    staging = store.shells / f".staging-{uuid.uuid4().hex[:8]}"
    try:
        staging.mkdir()
        shutil.copy2(shell_exe, staging / shell_exe.name)
        integrity.write_files_json(staging)
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
        _rename_with_retry(staging, target)
        integrity.write_complete(target)
        progress(f"Tauri 殼進 store:{fingerprint}(所有版本共用)")
        return fingerprint
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def build_version_manifest(request: BuildRequest, version: str, fingerprint: str,
                           shell_fingerprint: str) -> dict:
    return {
        "schema_version": MANIFEST_SCHEMA,
        "app_id": request.app_id,
        "display_name": request.display_name,
        "version": version,
        "entrypoint": f"application/{request.entrypoint.relative_to(request.project_dir).as_posix()}",
        "runtime_fingerprint": fingerprint,
        "engine_shim": "launcher/engine_shim.py",
        # The shell lives in deps/shells/<fp>/ — shared, not copied per version.
        "shell_fingerprint": shell_fingerprint,
        "shell_name": request.shell_exe.name,
        "host": "127.0.0.1",
        "preferred_port": request.preferred_port,
        "startup_timeout_seconds": request.startup_timeout_seconds,
        "health_path": "/_stcore/health",
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def version_revision(paths: AppPaths, version: str) -> str:
    """A content id for this exact build of this version — the same value
    export_update() puts in release.json, so the device can tell a re-cut of a
    failed version from a retry of the identical bytes."""
    files_json = (paths.version_dir(version) / integrity.FILES_NAME).read_bytes()
    return hashlib.sha256(files_json).hexdigest()[:12]


def _next_version(version: str) -> str:
    """A concrete suggestion beats telling someone to 'pick another version'."""
    match = re.match(r"^(.*?)(\d+)([^\d]*)$", version)
    if not match:
        return version + ".1"
    prefix, number, suffix = match.groups()
    return f"{prefix}{int(number) + 1}{suffix}"


def _build_version_dir(paths: AppPaths, request: BuildRequest, version: str,
                       fingerprint: str, shell_fingerprint: str,
                       progress: Progress) -> Path:
    target = paths.version_dir(version)
    if integrity.is_complete(target):
        raise StoreBuildError(
            f"版本 {version} 已經在這棵 Store 樹裡建過了,而且是完整的。\n"
            "  版本目錄一旦完成就不可變(已經發出去的版本不能被偷偷改掉)。\n"
            f"  · 要發新版 → 把版本號改成 {_next_version(version)}\n"
            "  · 要重來一次 → 換一個乾淨的輸出根目錄,或先手動刪掉\n"
            f"    {target}")
    if target.exists():  # leftover of a failed build
        shutil.rmtree(target)
    paths.versions_dir.mkdir(parents=True, exist_ok=True)
    staging = paths.versions_dir / f".staging-{uuid.uuid4().hex[:8]}"
    try:
        progress(f"組裝版本 {version} …")
        # The SAME rule as the fat builder — not a re-implementation of it. This
        # line used to call shutil.ignore_patterns(EXCLUDED_*) directly, which
        # ignored .provisionignore and the GUI's 額外排除 field entirely: the same
        # project excluded demo.mp4 in fat mode and shipped it in store mode, and
        # the store slot is the thing that travels on every single update.
        shutil.copytree(request.project_dir, staging / "application",
                        ignore=builder.copytree_ignore(
                            builder.ignore_patterns_for(request), request.project_dir))
        (staging / "launcher").mkdir()
        for name in ("launch.py", "engine_shim.py"):
            shutil.copy2(TEMPLATES / name, staging / "launcher" / name)
        # No shell/ here: it is shared via deps/shells/<fp>/.
        manifest = build_version_manifest(request, version, fingerprint, shell_fingerprint)
        (staging / MANIFEST_NAME).write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        integrity.write_files_json(staging)
        _rename_with_retry(staging, target)
        integrity.write_complete(target)
        return target
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


# ── .bat templates ───────────────────────────────────────────────────────────
#
# All of them are written UTF-8 with `chcp 65001` on line 3 — a display name with
# Chinese characters used to raise UnicodeEncodeError while writing an ascii .bat,
# and UnicodeEncodeError is not an OSError, so it escaped every guard in sight.
# All of them use `pushd` rather than `cd /d`: `cd /d` silently fails on a UNC
# path and leaves you in C:\Windows, where every later diagnostic is a lie.

# The WebView2 probe, shared by start.bat and tools\安裝WebView2.bat so the two
# can never disagree about whether this machine has it.
#
# Three registry locations, because WebView2 can be installed three ways:
#   * HKLM\SOFTWARE\WOW6432Node\...  per-machine, seen by a 32-bit reg view
#   * HKLM\SOFTWARE\...              per-machine, 64-bit view (Server / ARM images)
#   * HKCU\SOFTWARE\...              per-USER install, which is exactly what our own
#                                    「不需要系統管理員」 installer produces
# Missing any of them = we tell a user with a working WebView2 to go install one.
#
# And `reg query ... /v pv` SUCCEEDING is not the same as WebView2 being present:
# uninstalling Evergreen leaves the client key behind with `pv = 0.0.0.0`. The old
# `reg query >nul && set WV2=1` read that empty husk as a healthy install, waved the
# start through, and handed the user the blank window this check exists to prevent.
# So we read the value and reject 0.0.0.0.
_WEBVIEW2_CHECK = r"""set "WV2="
for %%K in (
  "HKLM\SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{client}"
  "HKLM\SOFTWARE\Microsoft\EdgeUpdate\Clients\{client}"
  "HKCU\SOFTWARE\Microsoft\EdgeUpdate\Clients\{client}"
) do (
  for /f "tokens=3" %%V in ('reg query %%K /v pv 2^>nul ^| findstr /i /c:"pv"') do (
    if not "%%V"=="0.0.0.0" set "WV2=%%V"
  )
)
"""


def _webview2_check() -> str:
    return _WEBVIEW2_CHECK.format(client=WEBVIEW2_CLIENT)


_START_BAT = r"""@echo off
setlocal
chcp 65001 >nul 2>&1
title {display_name} - 啟動中,請不要關閉這個視窗
pushd "%~dp0" || (
  echo [start][ERROR] 無法進入程式資料夾。若是從網路磁碟機執行,請先複製到本機磁碟再試一次。
  pause
  exit /b 1
)
rem 視窗是用 Microsoft Edge WebView2 Runtime 畫出來的。缺了它,Streamlit 會起來、
rem 視窗卻是一片空白:先檢查再啟動,不要讓人等 60 秒才看到空白視窗。
rem pv=0.0.0.0 是「解除安裝後留下的空殼」,不算裝好。
{webview2_check}if not defined WV2 (
  echo.
  echo [start][ERROR] 這台電腦沒有 Microsoft Edge WebView2 Runtime,視窗會開不起來。
  echo                請先雙擊 tools\{webview2_bat},裝好之後再執行這個檔案。
  echo                它可以用一般使用者權限安裝,不需要系統管理員。
  echo                代碼 {exit_env} = 這台機器缺東西,不是這個版本壞掉。
  popd
  pause
  exit /b {exit_env}
)
rem Bootstrap chicken-and-egg (spec 4.1): ANY runtime can run bootstrap.py
rem (stdlib-only); bootstrap then launches the app under its DECLARED runtime.
rem Runtimes are shared and immutable - no .pyc may ever be written into them.
set "PYTHONDONTWRITEBYTECODE=1"
set "PYTHONUTF8=1"
set "PY="
for /d %%R in ("deps\runtimes\*") do if exist "%%~R\python.exe" set "PY=%%~R\python.exe"
if not defined PY (
  echo [start][ERROR] 這份交付不完整:deps\runtimes\ 底下沒有 python.exe。
  echo                請向提供者重新索取完整的資料夾。
  popd
  pause
  exit /b 1
)
echo 正在啟動 {display_name}…(第一次啟動需要檢查共用元件,可能要幾分鐘)
"%PY%" "bootstrap\bootstrap.py" --app {app_id} %*
set "RC=%errorlevel%"
popd
rem 下面這個區塊裡不能出現「半形括號」。cmd 在 ( ) 區塊裡看到任何一個沒跳脫的半形
rem 括號,整個區塊就剖不出來:畫面上只有 "was unexpected at this time",批次檔當場
rem 中止、exit 255。於是「啟動失敗、記錄在哪裡」一個字都不會印,pause 也不會執行,
rem 視窗直接關掉,使用者只看到黑窗一閃。見 test_no_bat_echoes_a_paren_inside_a_block。
if not "%RC%"=="0" (
  echo.
  echo [start] 啟動失敗,代碼 %RC%。詳細記錄在 apps\{app_id}\data\logs\ 裡。
  pause
)
exit /b %RC%
"""


# Which python.exe do the tools run under? It matters, and it used to be decided
# by accident: `for /d %%R in ("deps\runtimes\*") do ... set "PY=..."` has no
# break, so PY ended up holding whichever runtime the loop happened to visit
# LAST. In a store with two runtimes that is a coin flip, and the losing side of
# the flip is the whole point of gc: gc.py refuses to delete the runtime its own
# interpreter is executing from (rmtree of a live python = a half-dead store), so
# running gc under the orphan runtime makes gc report `self_hosted` and reclaim
# nothing. The operator runs 「回收磁碟空間」, is told there is nothing to
# reclaim, and the 450 MB they came for stays on the disk forever. That is S9.
#
# So: pick the runtime a CURRENT version actually references (state.json ->
# current -> that version's app-package.json -> runtime_fingerprint). It is by
# definition in gc's keep-set, so it is never a deletion candidate and can never
# be the thing that gets skipped. Only if no such runtime can be resolved do we
# fall back — and then to the FIRST one found, deterministically, not the last.
#
# Delayed expansion is confined to this block and handed back through
# `endlocal & set "PY=..."`: the rest of the console reads paths from `set /p`,
# and a path containing `!` would be eaten alive by delayed expansion.
#
# NOTHING in a .bat may contain U+2014 (—). cmd.exe mis-parses it under
# `chcp 65001`: it splits the line it sits on and the tail is then executed as a
# command ("'...' is not recognized"), and it corrupts a LATER line too. Proven
# by holding the file size fixed and swapping — for a CJK character of identical
# byte length: two em-dashes, two mangled lines; same bytes, no em-dash, clean.
# See test_no_generated_bat_contains_an_em_dash, which enforces it on every bat
# this module writes.
_PICK_PYTHON = r"""set "PY="
setlocal enabledelayedexpansion
rem 1) 先找「目前這一版真的在用」的 runtime。GC 不會刪掉自己正在執行的那份 runtime,
rem    所以絕不能拿一份「沒人引用的孤兒 runtime」來跑 GC:那樣它就永遠刪不掉了。
for /d %%A in ("apps\*") do (
  if not defined PY (
    set "CUR="
    for /f "tokens=2 delims=:," %%C in ('findstr /i /c:"current" "%%~A\state\state.json" 2^>nul') do (
      if not defined CUR set "CUR=%%C"
    )
    if defined CUR (
      set CUR=!CUR: =!
      set CUR=!CUR:~1,-1!
      if exist "%%~A\versions\!CUR!\app-package.json" (
        set "FP="
        for /f "tokens=2 delims=:," %%F in ('findstr /i /c:"runtime_fingerprint" "%%~A\versions\!CUR!\app-package.json" 2^>nul') do (
          if not defined FP set "FP=%%F"
        )
        if defined FP (
          set FP=!FP: =!
          set FP=!FP:~1,-1!
          if exist "deps\runtimes\!FP!\python.exe" set "PY=deps\runtimes\!FP!\python.exe"
        )
      )
    )
  )
)
rem 2) 真的問不出來(state.json 壞了之類):退而求其次,取「第一個」找得到的 runtime。
rem    取第一個而不是最後一個,結果才是可預期、可重現的。
if not defined PY (
  for /d %%R in ("deps\runtimes\*") do (
    if not defined PY if exist "%%~R\python.exe" set "PY=%%~R\python.exe"
  )
)
endlocal & set "PY=%PY%"
if not defined PY (
  echo.
  echo [{tag}][ERROR] 找不到任何可用的 python.exe:deps\runtimes\ 底下是空的,
  echo                這份交付不完整,沒有東西可以執行。
  echo                請向提供者重新索取完整的資料夾。
  popd
  pause
  exit /b 1
)
"""


def _pick_python(tag: str) -> str:
    """The interpreter-picking block, tagged with the console it prints from."""
    return _PICK_PYTHON.format(tag=tag)


_GC_BAT = r"""@echo off
rem 回收沒有任何版本槽引用的版本與 runtime。先試算,確認後才真的刪。
setlocal
chcp 65001 >nul 2>&1
title 回收磁碟空間
pushd "%~dp0.." || (
  echo [gc][ERROR] 無法進入程式資料夾。若是從網路磁碟機執行,請先複製到本機磁碟。
  pause
  exit /b 1
)
set "PYTHONDONTWRITEBYTECODE=1"
set "PYTHONUTF8=1"
{pick_python}
echo === 先試算,不會刪除任何東西 ===
echo.
"%PY%" "bootstrap\gc.py"
rem 呼叫完馬上把結果收進 RC。errorlevel 在 () 區塊裡是進區塊前就展開的,讀到的會是舊值。
set "RC=%errorlevel%"
if "%RC%"=="{locked}" goto locked
if not "%RC%"=="0" goto planfailed
echo.
set "YES="
set /p YES=以上列出的項目要真的刪除嗎? 輸入 y 後按 Enter,其他任何鍵則取消:
if /i not "%YES%"=="y" (
  echo 已取消,沒有刪除任何東西。
  popd
  pause
  exit /b 0
)
echo.
echo === 開始回收 ===
"%PY%" "bootstrap\gc.py" --apply
set "RC=%errorlevel%"
rem 一個代碼一個結局。以前這裡只有「成功」跟「回收失敗,沒有刪掉任何東西 + 大概是
rem store 鎖被佔用」兩條路:GC 明明刪掉了 400 MB、只是有一個資料夾被檔案總管開著
rem 刪不掉,畫面照樣說「沒有刪掉任何東西」,還把原因賴給一個根本沒被佔用的鎖。
if "%RC%"=="0" goto done
if "%RC%"=="{partial}" goto partial
if "%RC%"=="{nothing}" goto nothing
if "%RC%"=="{locked}" goto locked
goto failed

:done
echo.
echo [gc] 回收完成。上面列出的項目都已經刪掉了。
popd
pause
exit /b 0

:partial
echo.
echo [gc][注意] 有一部分刪掉了,但有些項目刪不掉,那些空間「沒有」回收。
echo            上面 "刪不掉" 那幾行就是還留著的東西。
echo            最常見的原因:App 還開著,或檔案總管/防毒正在讀那個資料夾。
echo            請把 App 完全關掉(所有視窗),再重跑一次這個檔案。
popd
pause
exit /b %RC%

:nothing
echo.
echo [gc][ERROR] 回收失敗(代碼 %RC%),一個項目都沒有刪掉。原因在上面那幾行。
echo             GC 寧可整個中止,也不會在看不懂這棵樹的時候亂刪東西。
popd
pause
exit /b %RC%

:locked
echo.
echo [gc][ERROR] 現在有更新正在下載或安裝(store 鎖被佔用),這次沒有刪掉任何東西。
echo             等它做完,再重跑一次這個檔案。
popd
pause
exit /b %RC%

rem 沒有這一段的時候,失敗的 GC 和成功的 GC 在畫面上長得一模一樣:視窗關掉,
rem 磁碟一點也沒少,而操作的人以為自己已經回收過了。
:planfailed
echo.
echo [gc][ERROR] 回收失敗(試算階段,代碼 %RC%),沒有刪除任何東西。原因在上面那幾行。
popd
pause
exit /b %RC%

:failed
echo.
echo [gc][ERROR] 回收沒有跑完(代碼 %RC%)。上面那幾行是 GC 自己說的原因。
echo             這個代碼不在預期之內,所以這裡不猜「刪了沒有」:請照上面的訊息處理。
popd
pause
exit /b %RC%
"""


# One console per app. The old one hardcoded apps[0] but was labelled with THIS
# build's display name: in a two-app store, "退回上一版" rolled back the wrong app.
_ADMIN_BAT = r"""@echo off
rem 管理主控台:狀態 / 退版 / 套用更新包 / 設定更新來源 / 回收 / 清除失敗記錄。
setlocal
chcp 65001 >nul 2>&1
title {display_name} - 管理主控台
pushd "%~dp0.." || (
  echo [admin][ERROR] 無法進入程式資料夾。若是從網路磁碟機執行,請先複製到本機磁碟。
  pause
  exit /b 1
)
set "PYTHONDONTWRITEBYTECODE=1"
set "PYTHONUTF8=1"
{pick_python}
:menu
cls
echo ============================================
echo   {display_name} - 管理主控台
echo   應用代號:{app_id}
echo ============================================
echo.
echo   [1] 檢視狀態
echo   [2] 退回上一版
echo   [3] 套用已複製進來的更新包
echo   [4] 設定更新來源
echo   [5] 回收磁碟空間
echo   [6] 清除失敗記錄
echo   [7] 取消還沒套用的更新
echo   [0] 離開
echo.
set "CHOICE="
set /p CHOICE=請輸入代號後按 Enter:
if "%CHOICE%"=="1" goto status
if "%CHOICE%"=="2" goto rollback
if "%CHOICE%"=="3" goto install
if "%CHOICE%"=="4" goto source
if "%CHOICE%"=="5" goto reclaim
if "%CHOICE%"=="6" goto clearfailed
if "%CHOICE%"=="7" goto clearpending
if "%CHOICE%"=="0" goto done
goto menu

:status
echo.
"%PY%" "bootstrap\bootstrap.py" --app {app_id} --status
pause
goto menu

:rollback
echo.
echo 直接按 Enter = 退回上一個能用的版本;也可以輸入指定的版本號。
set "VER="
set /p VER=版本號(可留空):
if defined VER (
  "%PY%" "bootstrap\bootstrap.py" --app {app_id} --rollback-to "%VER%"
) else (
  "%PY%" "bootstrap\bootstrap.py" --app {app_id} --rollback
)
pause
goto menu

:install
echo.
echo 請先把更新包資料夾(裡面有 release.json)複製到這台電腦,再輸入它的路徑。
set "PAYLOAD="
set /p PAYLOAD=更新包資料夾路徑:
if not defined PAYLOAD goto menu
"%PY%" "bootstrap\bootstrap.py" --app {app_id} --install "%PAYLOAD%"
pause
goto menu

:source
echo.
echo 更新來源是一個資料夾(USB 或網路磁碟),新版本會放在那裡,程式會自己去拿。
set "SRC="
set /p SRC=更新來源資料夾路徑:
if not defined SRC goto menu
"%PY%" "bootstrap\bootstrap.py" --app {app_id} --set-update-source "%SRC%"
pause
goto menu

:reclaim
echo.
echo === 先試算,不會刪除任何東西 ===
echo.
"%PY%" "bootstrap\gc.py"
rem 呼叫完馬上把結果收進 RC。errorlevel 在 () 區塊裡是進區塊前就展開的,讀到的會是舊值。
set "RC=%errorlevel%"
rem 這些 echo 裡不能有半形括號:cmd 在 ( ) 區塊裡碰到沒跳脫的半形括號會整個剖壞,
rem 訊息一個字都不會印出來。全形括號與逗號沒有這個問題。
if "%RC%"=="{gc_locked}" (
  echo.
  echo [admin][ERROR] 現在正在下載或安裝更新,store 鎖被佔用,這次沒有刪掉任何東西。
  echo                等它做完,再回來試一次。
  pause
  goto menu
)
if not "%RC%"=="0" (
  echo.
  echo [admin][ERROR] 回收失敗:試算階段就出錯了,代碼 %RC%,沒有刪掉任何東西。
  echo                原因在上面那幾行。
  pause
  goto menu
)
echo.
set "YES="
set /p YES=以上列出的項目要真的刪除嗎? 輸入 y 後按 Enter,其他任何鍵則取消:
if /i not "%YES%"=="y" (
  echo 已取消,沒有刪除任何東西。
  pause
  goto menu
)
echo.
"%PY%" "bootstrap\gc.py" --apply
set "RC=%errorlevel%"
rem 一個代碼一個結局:刪掉一部分、一個都沒刪、鎖被佔用,是三件不一樣的事,
rem 以前全部被說成「回收失敗,可能一個也沒刪掉,大概是 store 鎖被佔用」。
if "%RC%"=="0" (
  echo.
  echo [admin] 回收完成。上面列出的項目都已經刪掉了。
) else if "%RC%"=="{gc_partial}" (
  echo.
  echo [admin][注意] 有一部分刪掉了,但有些項目刪不掉,那些空間沒有回收。
  echo                最常見的原因:App 還開著,或檔案總管與防毒正在讀那個資料夾。
  echo                請把 App 完全關掉,再回來重跑一次。
) else if "%RC%"=="{gc_nothing}" (
  echo.
  echo [admin][ERROR] 回收失敗,代碼 %RC%,一個項目都沒有刪掉。原因在上面那幾行。
) else if "%RC%"=="{gc_locked}" (
  echo.
  echo [admin][ERROR] 現在正在下載或安裝更新,store 鎖被佔用,這次沒有刪掉任何東西。
  echo                等它做完,再回來試一次。
) else (
  echo.
  echo [admin][ERROR] 回收沒有跑完,代碼 %RC%。上面那幾行是 GC 自己說的原因,請照它處理。
)
pause
goto menu

:clearfailed
echo.
echo 某個版本啟動失敗過就不會再被自動套用。修好之後,在這裡清掉它的失敗記錄。
set "VER="
set /p VER=要清除哪一個版本的失敗記錄:
if not defined VER goto menu
"%PY%" "bootstrap\bootstrap.py" --app {app_id} --clear-failed "%VER%"
pause
goto menu

:clearpending
echo.
echo 已經裝好、但還沒套用的更新,可以在這裡取消(版本會留在磁碟上,之後還能再套用)。
"%PY%" "bootstrap\bootstrap.py" --app {app_id} --clear-pending
pause
goto menu

:done
popd
exit /b 0
"""


# One app: tools\admin.bat is the name the 讀我 and the docs point at, so it must
# exist — it just forwards to that app's own console.
_ADMIN_ONE_BAT = r"""@echo off
rem 這棵樹只有一個應用,直接開它的管理主控台。
call "%~dp0admin-{app_id}.bat" %*
"""


_ADMIN_CHOOSER_BAT = r"""@echo off
rem 這棵樹有多個應用:先選要管理哪一個,再進它自己的主控台。
rem (以前這裡寫死第一個 app,卻掛著另一個 app 的名字,退版會退錯 app。)
setlocal
chcp 65001 >nul 2>&1
title 管理主控台 - 選擇應用
pushd "%~dp0" || (
  echo [admin][ERROR] 無法進入 tools 資料夾。若是從網路磁碟機執行,請先複製到本機磁碟。
  pause
  exit /b 1
)
:menu
cls
echo ============================================
echo   管理主控台 - 這個資料夾裡有多個應用
echo ============================================
echo.
{entries}
echo   [0] 離開
echo.
set "CHOICE="
set /p CHOICE=請輸入代號後按 Enter:
{dispatch}
if "%CHOICE%"=="0" (
  popd
  exit /b 0
)
goto menu
"""


_WEBVIEW2_BAT = r"""@echo off
rem WebView2 Runtime:視窗的顯示元件。沒有它,應用會啟動但視窗一片空白。
setlocal
chcp 65001 >nul 2>&1
title 安裝 Microsoft Edge WebView2 Runtime
pushd "%~dp0.." || (
  echo [webview2][ERROR] 無法進入程式資料夾。若是從網路磁碟機執行,請先複製到本機磁碟。
  pause
  exit /b 1
)
rem 跟 start.bat 用同一段偵測:兩邊對「這台機器到底有沒有 WebView2」不能有兩種答案。
rem pv=0.0.0.0 = 解除安裝後留下的空殼,不算裝好(照樣要裝)。
{webview2_check}if defined WV2 (
  echo 這台電腦已經有 WebView2 Runtime,版本 %WV2%,不需要再安裝。
  popd
  pause
  exit /b 0
)
if exist "{installer}" goto install
echo 這份交付沒有附帶 WebView2 安裝檔。
echo.
echo 請用瀏覽器下載「Evergreen Bootstrapper」,執行它就會安裝:
echo   {download}
echo.
echo 它可以用一般使用者權限安裝,不需要系統管理員。
popd
pause
exit /b 1

rem 這裡不用 () 區塊:區塊裡的 %errorlevel% 在進區塊前就被展開,永遠讀到舊值。
:install
echo 正在安裝 Microsoft Edge WebView2 Runtime,可能需要幾分鐘…
"{installer}" /silent /install
set "RC=%errorlevel%"
if "%RC%"=="0" goto ok
echo.
echo [webview2][ERROR] 安裝失敗(代碼 %RC%)。
echo                   請改用瀏覽器下載安裝:{download}
popd
pause
exit /b %RC%

:ok
echo.
echo 安裝完成。現在可以回到上一層,雙擊 start 開頭的 .bat 啟動應用。
popd
pause
exit /b 0
"""


def _bat_safe(text: str) -> str:
    """cmd.exe treats & | < > ^ ( ) % ! as syntax. A display name carrying any of
    them turns `echo`/`title` into a parse error, so they never reach a .bat."""
    cleaned = "".join(" " if ch in "&|<>^()%!\"" else ch for ch in str(text))
    return " ".join(cleaned.split()) or "App"


def _display_name_of(root: Path, app_id: str, default: str | None = None) -> str:
    """The name to print on THAT app's own bat — read from that app's own manifest,
    not from whatever build happens to be running right now."""
    paths = AppPaths(Path(root), app_id)
    candidates: list[Path] = []
    try:
        state = StateStore(paths.state_dir).load()
        for slot in (state.current, state.pending, state.last_known_good, state.previous):
            if slot:
                candidates.append(paths.versions_dir / slot)
    except Exception:
        pass
    if paths.versions_dir.is_dir():
        candidates.extend(sorted(p for p in paths.versions_dir.iterdir() if p.is_dir()))
    for vdir in candidates:
        try:
            manifest = json.loads((vdir / MANIFEST_NAME).read_text("utf-8"))
        except (OSError, ValueError):
            continue
        name = manifest.get("display_name")
        if name:
            return str(name)
    return default or app_id


def _preferred_port_of(root: Path, apps: list[str]) -> int:
    for app_id in apps:
        paths = AppPaths(Path(root), app_id)
        if not paths.versions_dir.is_dir():
            continue
        for vdir in sorted(paths.versions_dir.iterdir()):
            try:
                manifest = json.loads((vdir / MANIFEST_NAME).read_text("utf-8"))
            except (OSError, ValueError):
                continue
            return int(manifest.get("preferred_port", 0) or 0)
    return 0


def _write_store_readme(root: Path, apps: list[str], bats: list[str], *,
                        preferred_port: int = 0) -> None:
    """The delivered root is otherwise apps\\ deps\\ bootstrap\\ and some .bat files —
    not one word telling the user what to double-click or that a Start button is
    waiting for them. Everything here must be TRUE on the machine that reads it."""
    entry = bats[0] if len(bats) == 1 else "、".join(bats) if bats else "start.bat"
    if preferred_port:
        port_lines = [
            f"* 這個應用預設使用 {preferred_port} 埠。",
            f"  若 {preferred_port} 埠被其他程式占用,請先關掉那個程式再啟動。",
        ]
    else:
        # The default preferred_port is 0 = "pick a free port in 8000-9000". The
        # old README literally read 「若 0 埠被其他程式占用」.
        port_lines = ["* 啟動程式每次會自動挑一個沒被占用的埠,不需手動處理。"]

    lines = [
        "使用方式",
        "========",
        "",
        f"1. 雙擊 {entry}。",
        "   (第一次啟動會先檢查共用元件的完整性,可能要幾分鐘,黑色視窗不要關。)",
        "2. 應用視窗出現後,在上方的「工作流程」下拉選單選好要跑的項目,",
        "   再按旁邊那個寫著 Start 的按鈕(按鈕上是英文 Start,不是中文)。",
        "3. 應用就會顯示在視窗裡。",
        "",
        "開始之前:WebView2",
        "-----------------",
        "這個視窗是用 Microsoft Edge WebView2 Runtime 顯示的,這台電腦必須要有它。",
        "大多數的 Windows 10/11 已經內建;如果沒有,啟動時會直接告訴你,不會開出空白視窗。",
        f"缺的時候:雙擊 tools\\{WEBVIEW2_BAT_NAME}。",
        "  · 交付包若附了安裝檔,它會直接幫你裝好。",
        f"  · 沒附的話,它會印出下載網址:{WEBVIEW2_DOWNLOAD}",
        "  · WebView2 可以用一般使用者權限安裝,不需要系統管理員。",
        "",
        "除了 WebView2 以外,這台電腦不需要安裝 Python、Streamlit、Node 或 Rust ——",
        "全部都在這個資料夾裡。整個資料夾可以複製到別的位置或別台電腦,不需重新安裝。",
        "",
        "第一次執行的安全提示",
        "--------------------",
        "* 第一次執行可能出現「Windows 已保護您的電腦」(SmartScreen),",
        "  請點「其他資訊」→「仍要執行」。",
        "* 若公司的防毒軟體會把檔案隔離,請請 IT 把這個資料夾整個加進排除清單。",
        "",
        "連接埠",
        "------",
        *port_lines,
        "",
        "更新與回復",
        "----------",
        "* 新版本會在你使用時於背景準備好,並跳出「關閉並重新開啟後套用」的通知。",
        "  下次啟動就會自動換成新版。",
        "* 萬一新版啟動失敗,系統會自動退回上一個能用的版本,並告訴你。",
        "* 管理員可雙擊 tools\\admin.bat:檢視狀態、退回上一版、套用已複製進來的更新包、",
        "  設定更新來源、回收磁碟空間、清除失敗記錄。",
        "",
        "疑難排解",
        "--------",
        "* 錯誤訊息與記錄在 apps\\<應用>\\data\\logs\\ 底下。",
        "* 應用視窗一開就關閉,或視窗一片空白:多半是缺 WebView2,見上面那一節。",
        "* 不要直接執行 deps\\shells\\ 底下的 .exe —— 那是元件,不是應用程式入口。",
        "",
        f"這個資料夾包含的應用:{'、'.join(apps) if apps else '(無)'}",
        "",
    ]
    (Path(root) / README_NAME).write_text("\n".join(lines), encoding="utf-8")


def _write_tools(root: Path, apps: list[str] | None = None, *,
                 names_from: Path | None = None) -> None:
    """tools/gc.bat + tools/安裝WebView2.bat + one admin console PER APP.

    Written UTF-8 (a Chinese display name used to raise UnicodeEncodeError here).
    `names_from` lets an export generate the consoles for a subset of apps while
    still reading each app's real display name from the source store.

    The app list is the UNION of what the caller asked for and what is actually
    installed under `root`. tools\\ describes a MACHINE, not a build: an admin
    console is removed only when its app is not in the tree that console lives
    in. Passing this function one build's app while the store holds two would
    otherwise delete the still-installed app's console — the machine keeps
    running an app it can no longer administer. (An export into a fresh folder
    is the same rule seen from the other side: only the exported apps are in
    that tree, so only their consoles are written.)
    """
    root = Path(root)
    source = Path(names_from) if names_from else root
    apps = sorted(set(apps or []) | set(list_app_ids(root)))
    tools = root / "tools"
    tools.mkdir(parents=True, exist_ok=True)

    (tools / "gc.bat").write_text(
        _GC_BAT.format(pick_python=_pick_python("gc"), partial=GC_EXIT_PARTIAL,
                       nothing=GC_EXIT_NOTHING, locked=GC_EXIT_LOCKED),
        encoding="utf-8")
    (tools / WEBVIEW2_BAT_NAME).write_text(
        _WEBVIEW2_BAT.format(webview2_check=_webview2_check(),
                             installer=WEBVIEW2_INSTALLER.replace("/", "\\"),
                             download=WEBVIEW2_DOWNLOAD),
        encoding="utf-8")

    for stale in tools.glob("admin-*.bat"):
        if stale.name[len("admin-"):-len(".bat")] not in apps:
            stale.unlink()      # an app that is not here must not keep a console

    names = {app_id: _bat_safe(_display_name_of(source, app_id)) for app_id in apps}
    for app_id in apps:
        (tools / f"admin-{app_id}.bat").write_text(
            _ADMIN_BAT.format(app_id=app_id, display_name=names[app_id],
                              pick_python=_pick_python("admin"),
                              gc_partial=GC_EXIT_PARTIAL, gc_nothing=GC_EXIT_NOTHING,
                              gc_locked=GC_EXIT_LOCKED), encoding="utf-8")

    if len(apps) == 1:
        (tools / "admin.bat").write_text(
            _ADMIN_ONE_BAT.format(app_id=apps[0]), encoding="utf-8")
    elif apps:
        entries = "\n".join(f"echo   [{i}] {names[a]}  ({a})"
                            for i, a in enumerate(apps, 1))
        dispatch = "\n".join(f'if "%CHOICE%"=="{i}" call "%~dp0admin-{a}.bat"'
                             for i, a in enumerate(apps, 1))
        (tools / "admin.bat").write_text(
            _ADMIN_CHOOSER_BAT.format(entries=entries, dispatch=dispatch), encoding="utf-8")


def _install_bootstrap(root: Path) -> None:
    target = Path(root) / "bootstrap"
    target.mkdir(parents=True, exist_ok=True)
    for source in DEVICE_DIR.glob("*.py"):
        shutil.copy2(source, target / source.name)


def _start_bat_text(root: Path, app_id: str, display_name: str) -> str:
    return _START_BAT.format(app_id=app_id, display_name=_bat_safe(display_name),
                             webview2_check=_webview2_check(),
                             webview2_bat=WEBVIEW2_BAT_NAME,
                             exit_env=EXIT_SHELL_ENVIRONMENT)


def _write_entry_bats(root: Path, display_name: str = "App") -> tuple[list[str], bool]:
    """Returns (the bats a user can double-click, whether start.bat was removed).

    With one app the entry is just start.bat — a root with two files that do the
    same thing is a coin flip for a factory operator. A second app makes start.bat
    ambiguous, so it goes; the caller MUST tell the operator, because machines out
    there have already been taught to double-click it.
    """
    root = Path(root)
    apps = list_app_ids(root)
    single = root / "start.bat"
    if len(apps) <= 1:
        for stale in root.glob("start-*.bat"):
            stale.unlink()
        if apps:
            single.write_text(_start_bat_text(root, apps[0], display_name), encoding="utf-8")
            return ["start.bat"], False
        return [], False

    removed = single.exists()
    if removed:
        single.unlink()          # ambiguous now — force the explicit per-app entry
    bats = []
    for app_id in apps:
        name = f"start-{app_id}.bat"
        (root / name).write_text(
            _start_bat_text(root, app_id, _display_name_of(root, app_id)), encoding="utf-8")
        bats.append(name)
    return bats, removed


# ── entry points ─────────────────────────────────────────────────────────────

def _directory_size(path: Path) -> int:
    return sum(f.stat().st_size for f in Path(path).rglob("*") if f.is_file())


def _webview2_installer_of(request: BuildRequest) -> Path | None:
    """BuildRequest.webview2_installer, if this build's models.py has it.

    getattr, not attribute access: the field is being added by another module and
    a store build must not start failing on an AttributeError in the meantime.
    """
    value = getattr(request, "webview2_installer", None)
    return Path(value).expanduser() if value else None


def build_into_store(request: BuildRequest, root: Path, *, version: str,
                     update_source: Path | str | None = None,
                     progress: Progress = _noop,
                     should_cancel: Callable[[], bool] | None = None) -> StoreBuildResult:
    root = Path(root)
    started = time.monotonic()
    warnings: list[str] = []
    paths: AppPaths | None = None
    try:
        validate_identifier(version, "version")
        found = requirements_mod.resolve(request.project_dir, request.explicit_requirements)
        if found.generated:
            raise StoreBuildError(
                "Store 佈局需要完全釘死的 lock 檔,不能用 pyproject 的寬鬆相依。\n"
                "  在專案的環境裡執行:pip freeze > requirements.lock.txt")
        pins = normalize_lock(found.path.read_text("utf-8", errors="replace"))
        if not any(p.startswith("streamlit==") for p in pins):
            raise StoreBuildError("lock 檔未釘死 streamlit==<版本>")
        if not request.entrypoint.is_file():
            raise StoreBuildError(f"入口檔不存在:{request.entrypoint}")
        if not request.shell_exe.is_file():
            raise StoreBuildError(f"找不到預建 Tauri 殼:{request.shell_exe}")
        # Checked HERE, not at copy time: the copy happens after the runtime
        # install, and a typo'd path is not worth six minutes of pip.
        webview2 = _webview2_installer_of(request)
        if webview2 is not None and not webview2.is_file():
            raise StoreBuildError(f"找不到 WebView2 安裝檔:{webview2}")

        # Big files / auto-exclusions: the fat path has always reported these, the
        # store path declared a `warnings` field and then never filled it, so in
        # Store mode the operator was told nothing. Scan BEFORE copying anything.
        try:
            warnings.extend(scan_project(request).warnings)
        except Exception as exc:            # a scan must never kill a build
            warnings.append(f"專案掃描失敗,這次沒有大檔警告:{exc}")

        _check_cancel(should_cancel)
        fingerprint, reused = ensure_runtime(root, request, pins, progress)

        # THE GATE. Unconditional — built or reused. On the reuse path
        # ensure_runtime() above wrote nothing at all, so this still runs before
        # anything exists on disk; on the build path it runs against the runtime
        # that was just installed. Either way it is BEFORE _build_version_dir(),
        # so a failure here cannot leave a version directory behind, let alone a
        # version directory wearing a .complete (which would be immutable, and
        # shippable, and broken on the first render).
        _check_cancel(should_cancel)
        warnings.extend(check_app_imports(root, request, fingerprint,
                                          reused=reused, progress=progress))

        _check_cancel(should_cancel)
        shell_fingerprint = ensure_shell(root, request.shell_exe, progress)

        _check_cancel(should_cancel)
        paths = AppPaths(root, request.app_id)
        _build_version_dir(paths, request, version, fingerprint, shell_fingerprint, progress)

        # The revision travels with the version from here to failed_versions: a
        # rollback that records revision=None blocks EVERY future build of that
        # version number, including the fixed one.
        revision = version_revision(paths, version)

        _check_cancel(should_cancel)
        store = StateStore(paths.state_dir)
        pending_set = False
        if not store.exists():
            store.initialize(request.app_id, version)
            progress(f"初始化 state:current={version}")
        else:
            current = store.load()
            if version not in (current.current, current.pending):
                store.mutate(lambda s: set_pending(s, version, revision=revision))
                pending_set = True
                progress(f"已設定 pending={version}(這棵樹下次啟動時自動套用)")

        _install_bootstrap(root)
        if webview2 is not None:
            # <ROOT>/prereq/MicrosoftEdgeWebview2Setup.exe — the exact path
            # tools\安裝WebView2.bat looks for. Nothing in this codebase ever
            # CREATED prereq\; the exporter only copied it if it happened to
            # exist, so on an offline factory machine with no WebView2 the
            # delivery was unusable and said so only after the operator had
            # walked there. Renamed to the canonical name on the way in: the bat
            # cannot guess what the operator called the file they picked.
            target = root / WEBVIEW2_INSTALLER
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(webview2, target)
            progress(f"已附上 WebView2 安裝檔:{target.name}(離線機器不必上網也裝得起來)")
        apps = list_app_ids(root)
        bats, removed_start_bat = _write_entry_bats(root, request.display_name)
        try:
            _write_store_readme(root, apps, bats, preferred_port=request.preferred_port)
            _write_tools(root, apps)
        except Exception as exc:
            # NOT `except OSError`: writing an ascii .bat with a Chinese display
            # name raises UnicodeEncodeError, which is a ValueError — it used to
            # sail past the OSError guard and fail a build whose version directory
            # was already complete and therefore immutable (unrecoverable).
            warnings.append(
                f"tools\\ 與說明檔沒有全部寫成功({exc})。應用本身可以啟動;"
                "修好後重跑一次建置就會補上。")

        if update_source:
            (paths.app_dir / "config.json").write_text(
                json.dumps({"update_source": str(update_source)}, ensure_ascii=False, indent=2),
                encoding="utf-8")
            progress(f"已設定更新來源:{update_source}")

        version_bytes = _directory_size(paths.version_dir(version))
        added = version_bytes if reused else version_bytes + _directory_size(
            RuntimeStore(root / "deps").path_for(fingerprint))
        progress(f"完成:{root}(本次新增 {added / 1024 ** 2:.0f} MB)")

        return StoreBuildResult(
            ok=True, root=root, app_id=request.app_id, version=version,
            fingerprint=fingerprint, runtime_reused=reused,
            pending_set=pending_set, is_first_app=len(apps) <= 1,
            entry_bats=bats, removed_start_bat=removed_start_bat,
            version_mb=version_bytes / 1024 ** 2, added_mb=added / 1024 ** 2,
            duration_seconds=time.monotonic() - started, warnings=warnings)
    except _Cancelled:
        # Never leave a half-written version dir carrying a .complete: the tree
        # would look installable and the next build of that number would refuse.
        if paths is not None:
            target = paths.versions_dir / version
            if target.exists() and not integrity.is_complete(target):
                shutil.rmtree(target, ignore_errors=True)
        progress("已取消,沒有留下半成品版本。")
        return StoreBuildResult(ok=False, cancelled=True, root=root,
                                app_id=request.app_id, version=version,
                                errors=["建置已取消(沒有留下半成品版本)"],
                                warnings=warnings,
                                duration_seconds=time.monotonic() - started)
    except (StoreBuildError, LockfileError, requirements_mod.RequirementsError,
            runtime_mod.RuntimeError_, OSError) as exc:
        return StoreBuildResult(ok=False, errors=[str(exc)], warnings=warnings)


# ── exports ──────────────────────────────────────────────────────────────────

def _copy_with_progress(src: Path, dst: Path, *, ignore, say: Progress,
                        label: str) -> None:
    """copytree, but it says something while it works.

    A shared runtime is ~457 MB and a fat one is more; copying it is a minute or
    more of a progress bar that cannot move and a console that says nothing. An
    operator watching an export that has printed one line and then gone quiet for
    ninety seconds does not conclude "it is copying", they conclude "it has hung"
    — and they kill it, half-copied, which is the one state everything downstream
    is built to distrust.
    """
    total = _directory_size(src) or 1
    seen = {"bytes": 0, "next": 0.10}

    def copy(source, target):
        shutil.copy2(source, target)
        try:
            seen["bytes"] += Path(target).stat().st_size
        except OSError:                       # a size we cannot read is not fatal
            pass
        fraction = seen["bytes"] / total
        if fraction >= seen["next"]:
            seen["next"] = fraction + 0.10
            say(f"{label} {min(fraction, 1.0) * 100:.0f}%"
                f"({seen['bytes'] / 1024 ** 2:.0f}/{total / 1024 ** 2:.0f} MB)")

    shutil.copytree(src, dst, ignore=ignore, dirs_exist_ok=True, copy_function=copy)


def _ignore_staging(_dir: str, names: list[str]) -> set[str]:
    return {n for n in names if n.startswith(".staging-")}


def _ignore_staging_and_sentinel(_dir: str, names: list[str]) -> set[str]:
    return {n for n in names if n.startswith(".staging-") or n == integrity.SENTINEL}


_ROLE_ORDER = ("current", "pending", "previous", "last_known_good")
# Short enough for a dropdown: this is what the operator picks a version FROM.
_ROLE_LABELS = {
    "current": "目前版本",
    "pending": "最新建置、尚未套用",   # on a build machine this is the newest build
    "previous": "上一版,可退回",
    "last_known_good": "最後確認可用",
}


@dataclass
class VersionInfo:
    """One installable version of one app, as the tree really has it."""
    version: str
    revision: str | None = None        # content id: sha256(files.json)[:12]
    is_complete: bool = False          # no .complete = half-built, NOT deliverable
    role: str = ""                     # current / pending / previous / last_known_good
    roles: tuple[str, ...] = ()        # a version can wear more than one hat
    size_mb: float = 0.0
    built_at: str = ""                 # from the manifest; "" when unreadable
    runtime_fingerprint: str = ""

    @property
    def role_label(self) -> str:
        return _ROLE_LABELS.get(self.role, "")

    def label(self) -> str:
        """One line for a version picker."""
        parts = [self.version]
        if self.role_label:
            parts.append(f"({self.role_label})")
        if not self.is_complete:
            parts.append("(不完整,不能交付)")
        if self.built_at:
            parts.append(self.built_at)
        return " ".join(parts)


def _natural_key(name: str) -> tuple:
    """v1.10.0 is newer than v1.9.0, and a plain string sort disagrees."""
    return tuple((1, int(part), "") if part.isdigit() else (0, 0, part)
                 for part in re.split(r"(\d+)", name) if part)


def list_versions(root: Path, app_id: str) -> list[VersionInfo]:
    """Every version of `app_id` on this tree, newest first, with its state role.

    This exists because of S2. The GUI opened a store and exported `state.current`,
    which is the right answer on a MACHINE and the wrong one on a BUILD machine: a
    freshly built version is set to `pending` (the build machine never launches it,
    so it never promotes it), so `current` is the version the fleet already has.
    「發最新的那一版」 exported the old one, and nobody found out until the factory
    reported that the fix had changed nothing.

    Nothing here raises on a broken tree: a version whose manifest will not parse
    is still listed (is_complete tells the caller whether it can be shipped).
    """
    paths = AppPaths(Path(root), app_id)
    roles_of: dict[str, list[str]] = {}
    try:
        state = StateStore(paths.state_dir).load()
    except Exception:                      # noqa: BLE001 - a listing, not a gate
        state = None
    if state is not None:
        for role in _ROLE_ORDER:
            version = getattr(state, role, None)
            if version:
                roles_of.setdefault(version, []).append(role)

    found: list[VersionInfo] = []
    if paths.versions_dir.is_dir():
        for child in sorted(paths.versions_dir.iterdir()):
            if not child.is_dir() or child.name.startswith("."):
                continue
            try:
                manifest = json.loads((child / MANIFEST_NAME).read_text("utf-8"))
            except (OSError, ValueError):
                manifest = {}
            try:
                revision = hashlib.sha256(
                    (child / integrity.FILES_NAME).read_bytes()).hexdigest()[:12]
            except OSError:
                revision = None
            roles = tuple(r for r in _ROLE_ORDER if r in roles_of.get(child.name, []))
            found.append(VersionInfo(
                version=child.name, revision=revision,
                is_complete=integrity.is_complete(child),
                role=roles[0] if roles else "", roles=roles,
                size_mb=_directory_size(child) / 1024 ** 2,
                built_at=str(manifest.get("built_at") or ""),
                runtime_fingerprint=str(manifest.get("runtime_fingerprint") or "")))

    # built_at first (that is what "newest" means), version number as the
    # tie-break — two builds in the same second are ordinary, not exotic.
    found.sort(key=lambda info: (info.built_at, _natural_key(info.version)), reverse=True)
    return found


def newest_version(root: Path, app_id: str) -> str | None:
    """The newest version that is actually deliverable (.complete), or None.

    On a build machine this is normally `pending`, NOT `current`.
    """
    for info in list_versions(root, app_id):
        if info.is_complete:
            return info.version
    return None


def _rollback_target_for(paths: AppPaths, state: AppState, deliver: str) -> str | None:
    """The version to ship ALONGSIDE `deliver` so the target can roll back.

    Delivering one version means the target's first bad update has nowhere to go:
    bootstrap rolls back to `previous`, and `previous` has to be on the disk.
    """
    candidates: list[str | None] = []
    if deliver != state.current:
        candidates.append(state.current)     # we are shipping a NEWER version
    candidates += [state.previous, state.last_known_good]
    for candidate in candidates:
        if not candidate or candidate == deliver:
            continue
        if state.is_failed(candidate):
            continue                          # it failed here; do not arm it there
        try:
            if integrity.is_complete(paths.version_dir(candidate)):
                return candidate
        except Exception:                     # noqa: BLE001 - a bad name is just a miss
            continue
    return None


def _write_target_state(dst: AppPaths, app_id: str, current: str,
                        previous: str | None) -> None:
    """A FRESH state.json for the machine that receives this delivery.

    The exporter used to copy the build machine's state.json verbatim, which
    handed the factory:
      * `pending` — a version the target was never meant to run. It is not inert:
        bootstrap PROMOTES pending on the next start, so the delivery silently
        installed a different version from the one the operator delivered, on
        first boot, in the factory.
      * `candidate` — a half-proven version belonging to a machine it never met.
      * `failed_versions` — a failure history from THIS build machine, which
        blocks the target from ever auto-applying those versions.
      * `last_known_good` — a claim that some version once started successfully.
        Nothing on the build machine ever started anything.
    None of that is true of the target. So we write what IS true: it is running
    `current`, it can fall back to `previous`, and it has no history yet.
    """
    state = AppState(
        app_id=app_id, current=current, previous=previous,
        pending=None, pending_revision=None, candidate=None, candidate_revision=None,
        last_known_good=None, failed_versions=[], generation=0,
        last_operation={"id": uuid.uuid4().hex, "kind": "deliver",
                        "status": "completed",
                        "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                       time.gmtime())})
    # write_locked(): atomic replace + read-back, and it leaves no .lock behind
    # (a fresh export folder has nobody to contend with).
    StateStore(dst.state_dir).write_locked(state)


def _version_manifests(paths: AppPaths) -> dict[str, dict]:
    found: dict[str, dict] = {}
    if not paths.versions_dir.is_dir():
        return found
    for child in sorted(paths.versions_dir.iterdir()):
        if not child.is_dir() or child.name.startswith("."):
            continue
        try:
            found[child.name] = json.loads((child / MANIFEST_NAME).read_text("utf-8"))
        except (OSError, ValueError):
            continue
    return found


def _deliverable_versions(root: Path, app_id: str) -> str:
    """For an error message: what the operator COULD have asked for."""
    listed = [f"{info.version}({info.role_label or '無角色'})"
              for info in list_versions(root, app_id) if info.is_complete]
    return "、".join(listed) or "(一個完整的版本都沒有)"


def export_full_tree(root: Path, out_dir: Path, *, app_id: str | None = None,
                     version: str | None = None,
                     progress: Progress | None = None) -> ExportResult:
    """完整交付 — exactly what the target needs to RUN and to ROLL BACK. No more.

    `version` picks what to deliver (default: that app's `current`). On a build
    machine the newest build is usually `pending`, not `current` — see
    newest_version(), which is what a caller should default this to.

    What travels, per app:
      * the delivered version, and the one version the target can roll back TO
        (state.previous, or `current` when we are delivering something newer).
      * only the runtimes and shells THOSE versions name.
      * a state.json written from scratch: current + previous, nothing else.

    What does NOT travel, and used to:
      * every other version ever built here. A store with ten versions shipped ten
        (and, via the union of their manifests, several 500 MB runtimes with them).
      * state.json verbatim — see _write_target_state(). `pending` alone was a
        blocker: the target promotes it on first boot, so the operator delivers
        v1.0.0 and the factory boots v1.2.0.
      * apps/<app>/data/ — this build machine's logs, leases and healthy markers.
      * .staging-* debris, and deps/*/.complete (the target re-earns that by
        verifying; a version's .complete DOES travel, or nothing would be runnable
        at first boot).
    """
    root = Path(root)
    out = Path(out_dir)
    say = progress or _noop

    all_apps = list_app_ids(root)
    if app_id is not None:
        if app_id not in all_apps:
            raise StoreBuildError(
                f"這棵 Store 樹裡沒有 app {app_id!r};現有:{all_apps or '(沒有任何 app)'}")
        apps = [app_id]
    else:
        apps = list(all_apps)
    if not apps:
        raise StoreBuildError(f"這棵 Store 樹裡沒有任何 app,沒有東西可以交付:{root}")
    if version is not None and len(apps) > 1:
        raise StoreBuildError(
            "指定版本時必須同時指定是哪一個 app(這棵樹上有多個 app,"
            f"版本號不會自己說明它屬於誰):{'、'.join(apps)}")
    if not (root / "bootstrap" / "bootstrap.py").is_file():
        raise StoreBuildError(
            f"這棵樹缺 bootstrap\\bootstrap.py,不是一棵完整的 Store 樹:{root}\n"
            "  請重新建置一次(建置會補上 bootstrap\\)。")
    if out.resolve() == root.resolve():
        raise StoreBuildError("匯出目的地不能就是 Store 根目錄本身。")

    out.mkdir(parents=True, exist_ok=True)

    say("複製 bootstrap\\(裝置端程式,stdlib-only)…")
    shutil.copytree(root / "bootstrap", out / "bootstrap",
                    ignore=_ignore_staging, dirs_exist_ok=True)

    versions: list[str] = []
    warnings: list[str] = []
    runtime_fps: set[str] = set()
    shell_fps: set[str] = set()
    for a in apps:
        src = AppPaths(root, a)
        dst = AppPaths(out, a)
        if not src.state_dir.is_dir():
            raise StoreBuildError(f"{a} 沒有 state\\,這棵樹不完整,無法交付。")
        try:
            state = StateStore(src.state_dir).load()
        except Exception as exc:              # noqa: BLE001 - state is the whole map
            raise StoreBuildError(
                f"{a} 的 state.json 讀不出來,無法決定要交付哪一版:{exc}") from exc

        deliver = version or state.current
        try:
            vdir = src.version_dir(deliver)
        except Exception as exc:              # noqa: BLE001 - an invalid version name
            raise StoreBuildError(f"版本名稱不合法:{deliver!r}({exc})") from exc
        if not integrity.is_complete(vdir):
            raise StoreBuildError(
                f"版本 {deliver} 不完整或不存在,交付出去啟動不了:{vdir}\n"
                f"  這個 app 可以交付的版本:{_deliverable_versions(root, a)}")
        if state.is_failed(deliver):
            warnings.append(f"{a}:{deliver} 在這棵樹的失敗記錄裡(曾經啟動失敗過)。"
                            "確定要把它交付出去嗎?")

        rollback = _rollback_target_for(src, state, deliver)
        ship = [deliver] + ([rollback] if rollback else [])
        newest = newest_version(root, a)
        if newest and newest != deliver:
            # THE S2 trap, said out loud: on a build machine the freshly built
            # version is `pending`, so `current` is the one the fleet already runs.
            warnings.append(
                f"{a}:這次交付的是 {deliver},但這棵樹上最新的完整版本是 {newest}。"
                "如果你要發的是「最新的那一版」,請改指定它。")
        if not rollback:
            warnings.append(f"{a}:這份交付只有 {deliver} 一個版本,目標機沒有可以退回的版本。"
                            "萬一這一版在現場起不來,只能重新交付。")

        say(f"複製 {a} 的版本 {'、'.join(ship)} 與狀態…")
        for name in ship:
            # .complete stays: the delivered version must be runnable at first boot.
            shutil.copytree(src.version_dir(name), dst.version_dir(name),
                            ignore=_ignore_staging, dirs_exist_ok=True)
            versions.append(f"{a}/{name}" if len(apps) > 1 else name)

        _write_target_state(dst, a, deliver, rollback)

        config = src.app_dir / "config.json"
        if config.is_file():
            shutil.copy2(config, dst.app_dir / "config.json")
        # apps/<app>/data/ is NOT copied, and apps/<app>/staging/ is not either.

        manifests = _version_manifests(src)
        for name in ship:
            manifest = manifests.get(name, {})
            # No readable manifest = we cannot know which runtime this version needs,
            # and a delivery whose deps\ is missing the interpreter its own version
            # names is a folder that cannot start. Refuse here, not there.
            if not manifest.get("runtime_fingerprint"):
                raise StoreBuildError(
                    f"{a}/{name} 的 {MANIFEST_NAME} 讀不出 runtime_fingerprint,"
                    "沒辦法知道它要用哪一份 runtime,這樣交付出去啟動不了。\n"
                    f"  請重新建置這個版本:{src.version_dir(name)}")
            runtime_fps.add(manifest["runtime_fingerprint"])
            if manifest.get("shell_fingerprint"):
                shell_fps.add(manifest["shell_fingerprint"])

    rstore = RuntimeStore(root / "deps")
    sstore = ShellStore(root / "deps")
    for fingerprint in sorted(runtime_fps):
        source = rstore.path_for(fingerprint)
        if not source.is_dir():
            raise StoreBuildError(
                f"這棵樹缺共用 runtime {fingerprint},交付出去會啟動不了:{source}")
        say(f"複製共用 runtime {fingerprint}(數百 MB,只有第一次要傳)…")
        _copy_with_progress(source, out / "deps" / "runtimes" / fingerprint,
                            ignore=_ignore_staging_and_sentinel, say=say,
                            label=f"  runtime {fingerprint}")
    for fingerprint in sorted(shell_fps):
        source = sstore.path_for(fingerprint)
        if not source.is_dir():
            raise StoreBuildError(
                f"這棵樹缺共用 Tauri 殼 {fingerprint},交付出去開不出視窗:{source}")
        say(f"複製共用 Tauri 殼 {fingerprint}…")
        shutil.copytree(source, out / "deps" / "shells" / fingerprint,
                        ignore=_ignore_staging_and_sentinel, dirs_exist_ok=True)

    # The WebView2 bootstrapper, if this store bundles one.
    prereq = root / "prereq"
    if prereq.is_dir():
        say("複製 prereq\\(WebView2 安裝檔)…")
        shutil.copytree(prereq, out / "prereq", ignore=_ignore_staging, dirs_exist_ok=True)
    if not (out / WEBVIEW2_INSTALLER).is_file():
        # The one dependency this delivery cannot satisfy by itself. Without
        # WebView2 the Tauri window opens blank, and a factory machine typically
        # has no way to reach go.microsoft.com to fix it. Say it HERE, while the
        # operator is still standing next to the build machine that could add it.
        warnings.append(
            "這份交付沒有附 WebView2 安裝檔(prereq\\MicrosoftEdgeWebview2Setup.exe)。"
            "目標機如果沒有 Microsoft Edge WebView2 Runtime,而且不能上網,"
            "這份交付就裝不起來、視窗會是空白的。"
            "請在建置時指定 WebView2 安裝檔,或先確認目標機已經有 WebView2:"
            + WEBVIEW2_DOWNLOAD)

    say("寫入 start bat、tools\\ 與讀我-使用說明.txt…")
    wanted_bats = {"start.bat"} | {f"start-{a}.bat" for a in apps}
    entry_bats: list[str] = []
    for bat in sorted(root.glob("start*.bat")):
        if bat.name in wanted_bats:
            shutil.copy2(bat, out / bat.name)
            entry_bats.append(bat.name)
    for stale in sorted(out.glob("start*.bat")):
        # Exporting again into a folder that already held a different delivery:
        # a leftover start bat points at an app (or a state) that is not here.
        if stale.name not in entry_bats:
            stale.unlink()
    if not entry_bats:
        # An old tree (or a tree whose bats were deleted): regenerate rather than
        # deliver a folder with nothing to double-click.
        entry_bats, _ = _write_entry_bats(out, _display_name_of(root, apps[0]))

    # Regenerated, not copied: tools\ must describe EXACTLY the apps in this
    # export (a chooser offering an app that is not here is worse than useless).
    _write_tools(out, apps, names_from=root)

    readme = root / README_NAME
    if readme.is_file() and len(apps) == len(all_apps):
        shutil.copy2(readme, out / README_NAME)
    else:
        _write_store_readme(out, apps, entry_bats,
                            preferred_port=_preferred_port_of(root, apps))

    total = _directory_size(out)
    say(f"完成:{out}({total / 1024 ** 2:.0f} MB)")
    for warning in warnings:
        say(f"[注意] {warning}")
    return ExportResult(out_dir=out, total_mb=total / 1024 ** 2, apps=apps,
                        versions=versions, includes_runtime=bool(runtime_fps),
                        kind="full", entry_bats=sorted(entry_bats), warnings=warnings)


def _previous_manifest(paths: AppPaths, version: str) -> dict | None:
    """The version this app shipped BEFORE `version` — i.e. what a machine that
    already has this tree is most likely running."""
    others = [(m.get("built_at", ""), name, m)
              for name, m in _version_manifests(paths).items() if name != version]
    if not others:
        return None
    others.sort()
    return others[-1][2]


def _runtime_changes(previous: dict, manifest: dict) -> list[str]:
    """What a machine on `previous` would be missing if it only got the version."""
    changed = []
    if previous.get("runtime_fingerprint") != manifest.get("runtime_fingerprint"):
        changed.append(f"Python 相依指紋:{previous.get('runtime_fingerprint')}"
                       f" -> {manifest.get('runtime_fingerprint')}")
    if previous.get("shell_fingerprint") != manifest.get("shell_fingerprint"):
        changed.append(f"Tauri 殼:{previous.get('shell_fingerprint')}"
                       f" -> {manifest.get('shell_fingerprint')}")
    return changed


def update_needs_runtime(root: Path, app_id: str, version: str) -> bool:
    """Would this update package have to carry the ~457 MB shared runtime?

    The polite question, asked BEFORE the export. export_update() also refuses an
    incremental package whose runtime moved (that raise stays: it is the safety
    net, and it fires no matter who calls), but an exception is a terrible way to
    learn something the GUI could simply have ticked a box about — the operator
    picks a destination folder, waits, and is then told the thing they chose was
    never possible.

    True  — send the runtime. Either the fingerprints really did move, or there
            is nothing in this store to compare against and "include it" is the
            answer that always works.
    False — the previous version of this same app already carries a byte-identical
            runtime AND shell, so the target machine has them: ~17 MB will do.

    Never raises. A tree we cannot read answers True, because the cost of being
    wrong that way is bandwidth, and the cost of being wrong the other way is a
    machine that installs an update, fails to start, and rolls back — forever.
    """
    try:
        paths = AppPaths(Path(root), app_id)
        manifest = json.loads((paths.version_dir(version) / MANIFEST_NAME).read_text("utf-8"))
        previous = _previous_manifest(paths, version)
    except Exception:                      # noqa: BLE001 - a question, not a gate
        return True
    if previous is None:
        return True
    return bool(_runtime_changes(previous, manifest))


def export_update(root: Path, app_id: str, version: str, out_dir: Path,
                  *, include_runtime: bool | None = None,
                  progress: Progress | None = None) -> ExportResult:
    """自動更新來源 (spec §9.1 folder-provider layout) — NOT a deliverable folder.

    Consumed by device/provider.py polling an update source, or copied to the
    machine and applied with `bootstrap.py --install <payload>`. Every sentinel is
    stripped: the target machine must verify before anything becomes visible.
    For a machine that has never seen this app, use export_full_tree().
    """
    root = Path(root)
    say = progress or _noop
    paths = AppPaths(root, app_id)
    vdir = paths.version_dir(version)
    if not integrity.is_complete(vdir):
        raise StoreBuildError(f"版本 {version} 不完整,不可匯出")
    manifest = json.loads((vdir / MANIFEST_NAME).read_text("utf-8"))
    fingerprint = manifest["runtime_fingerprint"]
    shell_fp = manifest.get("shell_fingerprint")

    # An incremental package that silently drops a changed runtime installs a
    # version whose interpreter does not exist on the target: it stages, promotes,
    # fails to start, and rolls back — every time, forever.
    if include_runtime is False:
        previous = _previous_manifest(paths, version)
        if previous is not None:
            changed = _runtime_changes(previous, manifest)
            if changed:
                raise StoreBuildError(
                    "這一版換了 Python 相依(或殼),增量包不夠,必須勾選「包含 runtime」。\n"
                    + f"  (跟上一版 {previous.get('version')} 相比)\n"
                    + "".join(f"  · {line}\n" for line in changed)
                    + "  只送版本的話,目標機器會裝好、啟動失敗、然後自動退回舊版。\n"
                    + "  (要事先問而不是撞到這個錯誤,用 update_needs_runtime()。)")

    want_deps = include_runtime is None or include_runtime
    out_app = Path(out_dir) / app_id
    out_app.mkdir(parents=True, exist_ok=True)
    # ONLY the sentinel is dropped (so the target machine earns it by verifying).
    # Filtering anything else — we used to drop __pycache__/*.pyc — deletes files
    # that files.json still declares, and every export then failed integrity on
    # the target with "重新複製" advice that could never work.
    say(f"複製版本 {version} …")
    _copy_with_progress(vdir, out_app / "versions" / version,
                        ignore=_ignore_staging_and_sentinel, say=say,
                        label=f"  版本 {version}")
    if want_deps:
        say(f"複製共用 runtime {fingerprint}(數百 MB,請等它跑完)…")
        runtime_dir = RuntimeStore(paths.deps_dir).path_for(fingerprint)
        _copy_with_progress(runtime_dir, out_app / "runtimes" / fingerprint,
                            ignore=_ignore_staging_and_sentinel, say=say,
                            label=f"  runtime {fingerprint}")
        # The shell is shared too: a target machine that has never seen this shell
        # would otherwise install the version and then fail to open a window.
        if shell_fp:
            say(f"複製共用 Tauri 殼 {shell_fp} …")
            shell_dir = ShellStore(paths.deps_dir).path_for(shell_fp)
            _copy_with_progress(shell_dir, out_app / "shells" / shell_fp,
                                ignore=_ignore_staging_and_sentinel, say=say,
                                label=f"  殼 {shell_fp}")

    files_manifest = (vdir / integrity.FILES_NAME).read_bytes()
    revision = hashlib.sha256(files_manifest).hexdigest()[:12]
    (out_app / "release.json").write_text(json.dumps({
        "schema": 1, "app_id": app_id, "version": version,
        "revision": revision, "runtime_fingerprint": fingerprint,
        "shell_fingerprint": shell_fp,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    total = _directory_size(out_app)
    say(f"完成:{out_app}({total / 1024 ** 2:.0f} MB,自動更新來源)")
    return ExportResult(out_dir=out_app, total_mb=total / 1024 ** 2, apps=[app_id],
                        versions=[version], includes_runtime=bool(want_deps),
                        kind="update")
