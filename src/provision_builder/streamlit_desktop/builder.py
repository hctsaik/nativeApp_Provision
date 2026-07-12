"""Assemble the deliverable folder.

Everything is built in a staging directory beside the output and only swapped
into place once it is complete and smoke-tested, so a failed build can never
leave a half-written folder where a working one used to be (spec §7.1.10).

No Tkinter here: the GUI passes a progress callback and gets a BuildResult back.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from fnmatch import fnmatch
from pathlib import Path

from . import imports as imports_mod
from . import requirements as requirements_mod
from . import runtime as runtime_mod
from .models import (
    DEFAULT_STARTUP_TIMEOUT,
    EXCLUDED_DIRS,
    EXCLUDED_FILES,
    PROVISIONIGNORE,
    SCHEMA_VERSION,
    BuildRequest,
    BuildResult,
)
from .runtime import BuildCancelled
from .validate import validate_request

TEMPLATES = Path(__file__).resolve().parent / "templates"
# 同一個檔名,兩種佈局共用——GUI 的完成對話框會叫使用者去看它。
README_NAME = "讀我-使用說明.txt"
Progress = Callable[[str], None]
ShouldCancel = Callable[[], bool]
MB = 1024 ** 2


def _noop(_message: str) -> None:
    pass


# ── one exclusion rule, used by BOTH the estimate and the copy ───────────────
#
# These used to disagree: scan_project() only skipped EXCLUDED_FILES for *files*
# while shutil.ignore_patterns() also matched directories, so `*.egg-info` (which
# is a directory) was counted in the estimate and then not copied. The operator
# was told 700 MB and got 640 MB, and neither number could be trusted. One rule,
# one answer.

def ignore_reason(name: str, is_dir: bool, extra: Sequence[str] = ()) -> str | None:
    """Why this entry is excluded, as a label for the report — or None to keep it."""
    if is_dir and name in EXCLUDED_DIRS:
        return f"{name}/"
    for pattern in EXCLUDED_FILES:                 # `*.egg-info` is a DIRECTORY: match both
        if fnmatch(name, pattern):
            return pattern
    for pattern in extra:
        if _matches_ignore(pattern, name, is_dir):
            return f"{pattern}(.provisionignore)"
    return None


def should_ignore(name: str, is_dir: bool, extra: Sequence[str] = ()) -> bool:
    """The single source of truth for 'does this entry travel into the package'."""
    return ignore_reason(name, is_dir, extra) is not None


def _matches_ignore(pattern: str, name: str, is_dir: bool) -> bool:
    """gitignore-flavoured, fnmatch-powered: `wheels/`, `*.mp4`, `notes.txt`.

    Patterns are matched against the entry NAME, so a pattern with a path in it
    (`data/raw/`) is honoured by its last segment. Negation (`!`) is not
    supported — say so rather than pretend.
    """
    pattern = pattern.strip()
    if not pattern or pattern.startswith(("#", "!")):
        return False
    dir_only = pattern.endswith("/")
    pattern = pattern.rstrip("/")
    if pattern.startswith("./"):
        pattern = pattern[2:]
    if "/" in pattern:
        pattern = pattern.rsplit("/", 1)[-1]
    if not pattern or (dir_only and not is_dir):
        return False
    return fnmatch(name, pattern)


def ignore_patterns_for(request: BuildRequest) -> tuple[str, ...]:
    """The project's own .provisionignore, plus whatever the caller added."""
    patterns: list[str] = []
    ignore_file = Path(request.project_dir) / PROVISIONIGNORE
    if ignore_file.is_file():
        patterns += [line.strip()
                     for line in ignore_file.read_text("utf-8", errors="replace").splitlines()
                     if line.strip() and not line.strip().startswith("#")]
    patterns += list(getattr(request, "extra_excludes", ()) or ())
    return tuple(patterns)


def copytree_ignore(extra: Sequence[str] = ()):
    """The `ignore=` callable for shutil.copytree, driven by should_ignore()."""
    def ignore(directory, names):
        base = Path(directory)
        return {name for name in names
                if should_ignore(name, (base / name).is_dir(), extra)}
    return ignore


def build_manifest(request: BuildRequest, shell_name: str) -> dict:
    """Every path is relative to the package root — an absolute path from the
    build machine would break the moment the folder is copied elsewhere."""
    return {
        "schema_version": SCHEMA_VERSION,
        "app_id": request.app_id,
        "display_name": request.display_name,
        "version": request.version,
        "entrypoint": f"application/{request.entrypoint.relative_to(request.project_dir).as_posix()}",
        "python": "runtime/python.exe",
        "shell_executable": f"shell/{shell_name}",
        "engine_shim": "launcher/engine_shim.py",
        "host": "127.0.0.1",
        "preferred_port": request.preferred_port,
        "startup_timeout_seconds": request.startup_timeout_seconds or DEFAULT_STARTUP_TIMEOUT,
        "health_path": "/_stcore/health",
        "built_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


@dataclass
class ProjectScan:
    """What the project will contribute, measured BEFORE we copy 600 MB."""
    application_mb: float = 0.0
    # warnings = 需要使用者「決定」的事(大檔、大資料夾)——GUI 會為此擋一次。
    # notes    = 純資訊(已自動排除了什麼)——只印出來,不打斷任何人。
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    # What we dropped on the way, and how big it was. "為什麼我的資料夾有 700 MB"
    # deserves an answer up front, not a shrug.
    excluded_mb: float = 0.0
    excluded: dict[str, int] = field(default_factory=dict)   # label -> bytes
    excluded_summary: str = ""


# The GUI and the store builder both talk about a "scan result"; keep one object
# and two names rather than two objects that drift apart.
ScanResult = ProjectScan


def scan_project(request: BuildRequest, big_file_mb: int = 10,
                 big_dir_mb: int = 25) -> ProjectScan:
    """Walk the project with the EXACT exclusions the build uses, so the operator
    learns about the 85 MB screen recording now — not after a ten-minute build —
    and learns what we quietly left out, with its size."""
    scan = ProjectScan()
    root = Path(request.project_dir)
    extra = ignore_patterns_for(request)
    total = 0
    per_dir: dict[str, int] = {}
    big_files: list[tuple[str, int]] = []
    excluded: dict[str, int] = {}

    for dirpath, dirnames, filenames in os.walk(root):
        here = Path(dirpath)
        keep_dirs = []
        for name in dirnames:
            reason = ignore_reason(name, True, extra)
            if reason is None:
                keep_dirs.append(name)
            else:
                excluded[reason] = excluded.get(reason, 0) + directory_size(here / name)
        dirnames[:] = keep_dirs                       # prune: do not descend

        for name in filenames:
            path = here / name
            try:
                size = path.stat().st_size
            except OSError:
                continue
            reason = ignore_reason(name, False, extra)
            if reason is not None:
                excluded[reason] = excluded.get(reason, 0) + size
                continue
            total += size
            rel = path.relative_to(root)
            top = rel.parts[0] if len(rel.parts) > 1 else "(根目錄)"
            per_dir[top] = per_dir.get(top, 0) + size
            if size > big_file_mb * MB:
                big_files.append((rel.as_posix(), size))

    scan.application_mb = total / MB
    scan.excluded = {label: size for label, size in excluded.items() if size > 0}
    scan.excluded_mb = sum(scan.excluded.values()) / MB
    # 「已經自動排除、不會進交付包」是好消息,不是警告。把它塞進 warnings 會讓
    # GUI 在開工前彈一個「這個專案裡有一些大東西」確認框,列出的卻全是已經排掉的
    # 東西,還建議你去設排除樣式——建議你排除一堆已經排除掉的東西。CV_Viewer 沒有
    # 任何該煩惱的大檔,卻一定會吃到這個假警報。它現在走 scan.notes(純資訊)。
    scan.excluded_summary = _excluded_summary(scan.excluded)
    if scan.excluded_summary:
        scan.notes.append(scan.excluded_summary)

    for name, size in sorted(big_files, key=lambda item: item[1], reverse=True)[:3]:
        scan.warnings.append(f"專案裡的大檔:{name}({size / MB:.0f} MB)——確定要交付嗎?")
    for name, size in sorted(per_dir.items(), key=lambda item: item[1], reverse=True)[:2]:
        if size > big_dir_mb * MB and not any(name in w for w in scan.warnings):
            scan.warnings.append(
                f"大資料夾:{name}\\({size / MB:.0f} MB)——它會被整個複製進交付包。")
    return scan


def _excluded_summary(excluded: dict[str, int]) -> str:
    """e.g. 已自動排除:wheels/ 124 MB、*.pyc 18 MB(共 142 MB,不會進交付包)"""
    if not excluded:
        return ""
    ranked = sorted(excluded.items(), key=lambda item: item[1], reverse=True)
    shown = [f"{label} {size / MB:.0f} MB" for label, size in ranked[:6] if size >= MB]
    if not shown:                                     # all small: one honest number
        shown = [f"{label}" for label, _size in ranked[:6]]
    total = sum(excluded.values()) / MB
    return f"已自動排除:{'、'.join(shown)}(共 {total:.0f} MB,不會進交付包)"


def clean_orphan_staging(output_dir: Path, progress: Progress = _noop) -> int:
    """A crashed or killed build leaves a `.staging-*` directory holding a whole
    copied runtime — hundreds of MB of invisible garbage in the operator's output
    folder. Sweep them before we add one more, and say how much we got back."""
    freed = 0
    for path in sorted(Path(output_dir).glob(".staging-*")):
        if not path.is_dir():
            continue
        size = directory_size(path)
        progress(f"清掉上次沒收乾淨的暫存目錄:{path.name}({size / MB:.0f} MB)")
        shutil.rmtree(path, ignore_errors=True)
        freed += size
    return freed


def runtime_would_be_reused(request: BuildRequest, root: Path) -> bool:
    """Answer the question that decides between +0 MB and +700 MB, before the
    operator commits to a build."""
    from .device.runtime_store import RuntimeStore, compute_fingerprint, normalize_lock
    from . import requirements as req

    try:
        found = req.resolve(request.project_dir, request.explicit_requirements)
        pins = normalize_lock(found.path.read_text("utf-8", errors="replace"))
    except Exception:      # noqa: BLE001 — a preview must never raise at the operator
        return False
    template_python = request.runtime_template / "python.exe"
    try:
        version = subprocess.run(
            [str(template_python), "-c", "import platform;print(platform.python_version())"],
            capture_output=True, text=True, check=False).stdout.strip()
    except OSError:
        return False
    if not version:
        return False
    abi = "cp" + "".join(version.split(".")[:2])
    fingerprint = compute_fingerprint(python_version=version, platform="win_amd64",
                                      abi=abi, pins=pins)
    return RuntimeStore(Path(root) / "deps").is_complete(fingerprint)


def directory_size(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += (Path(root) / name).stat().st_size
            except OSError:
                pass
    return total


def size_breakdown(package: Path, top: int = 6) -> list[str]:
    """Where the megabytes went — the admin should never have to go measure this
    themselves to find out that one dependency is half the package."""
    lines = []
    for part in ("runtime", "application", "shell"):
        directory = package / part
        if directory.is_dir():
            lines.append(f"  {part:<12} {directory_size(directory) / 1024 ** 2:>6.0f} MB")

    site = package / "runtime" / "Lib" / "site-packages"
    if site.is_dir():
        heavy = sorted(
            ((child.name, directory_size(child)) for child in site.iterdir() if child.is_dir()),
            key=lambda item: item[1], reverse=True)[:top]
        if heavy:
            lines.append("  最大的相依:" + "、".join(
                f"{name} {size / 1024 ** 2:.0f}MB" for name, size in heavy))

    # A single stray file can dwarf the code: AI4BI's project root held an 85 MB
    # screen recording. We will not guess that it is junk — but the operator
    # should not have to go measuring to find out it shipped.
    application = package / "application"
    if application.is_dir():
        fat = [(path, path.stat().st_size) for path in application.rglob("*")
               if path.is_file() and path.stat().st_size > 10 * MB]
        for path, size in sorted(fat, key=lambda item: item[1], reverse=True)[:3]:
            # No emoji anywhere in operator-facing text: the GUI log and the
            # console are cp950 on a zh-TW box, and an un-encodable character
            # takes the whole message down with it.
            lines.append(f"  注意:專案裡的大檔:{path.relative_to(application)} "
                         f"({size / MB:.0f} MB)——確定要交付嗎?")
    return lines


def build(request: BuildRequest, progress: Progress = _noop,
          should_cancel: ShouldCancel | None = None) -> BuildResult:
    started = time.monotonic()
    errors = validate_request(request)
    if errors:
        return BuildResult(ok=False, errors=errors)

    def check_cancel() -> None:
        """Every stage boundary. 取消 that only takes effect when the build was
        going to finish anyway is not a cancel button."""
        if should_cancel is not None and should_cancel():
            raise BuildCancelled("已取消建置")

    final = request.package_dir
    request.output_dir.mkdir(parents=True, exist_ok=True)
    # Before we add one more: sweep the ones a crashed run left behind.
    clean_orphan_staging(request.output_dir, progress)
    staging = request.output_dir / f".staging-{final.name}-{uuid.uuid4().hex[:8]}"
    build_log = staging / "data" / "logs" / "build.log"
    warnings: list[str] = []

    try:
        staging.mkdir(parents=True)
        (staging / "data" / "logs").mkdir(parents=True)

        check_cancel()
        progress("複製 Streamlit 專案…")
        shutil.copytree(
            request.project_dir, staging / "application",
            ignore=copytree_ignore(ignore_patterns_for(request)),
            dirs_exist_ok=True,
        )

        check_cancel()
        progress("複製可攜 Python runtime…")
        python = runtime_mod.copy_runtime(request.runtime_template, staging / "runtime")

        found = requirements_mod.resolve(request.project_dir, request.explicit_requirements,
                                         staging=staging, extras=request.extras)
        check_cancel()
        progress(f"安裝專案相依(來源:{found.source};建置時會連網,產出物執行時不需要)…")
        # pip/setuptools/wheel lines from a `pip freeze --all` lock point at the
        # interpreter-builder's disk, and `-e .` / `pkg @ file:///…` point at this
        # machine — all of them fail the install elsewhere (see PLUMBING).
        for_pip = requirements_mod.sanitize_for_pip(found.path, staging, progress=progress)
        runtime_mod.install_requirements(python, for_pip, build_log, progress=progress,
                                         should_cancel=should_cancel)

        check_cancel()
        progress("驗證 runtime 能 import streamlit…")
        runtime_mod.verify_imports(python, build_log)

        progress("檢查 App 啟動時要用的 import 都裝得到…")
        missing = imports_mod.missing_dependencies(request.entrypoint, request.project_dir,
                                                   python)
        if missing.required:
            raise imports_mod.ImportGateError(missing.failure_message())
        warnings += missing.warning_lines()
        for line in missing.warning_lines():
            progress("注意:" + line)

        progress("複製 Tauri 殼與 launcher…")
        (staging / "shell").mkdir()
        shutil.copy2(request.shell_exe, staging / "shell" / request.shell_exe.name)
        (staging / "launcher").mkdir()
        for name in ("launch.py", "engine_shim.py"):
            shutil.copy2(TEMPLATES / name, staging / "launcher" / name)
        shutil.copy2(TEMPLATES / "start.bat", staging / "start.bat")

        manifest = build_manifest(request, request.shell_exe.name)
        (staging / "app-package.json").write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        _write_readme(staging, manifest)

        progress("Smoke test:檢查交付包的完整性…")
        smoke_errors = smoke_test(staging, manifest)
        if smoke_errors:
            raise runtime_mod.RuntimeError_("交付包自檢失敗:\n  " + "\n  ".join(smoke_errors))

        # Build-time artifacts, not payload. build.log in particular carries the
        # build machine's absolute paths into the customer's folder.
        for_pip.unlink(missing_ok=True)
        if found.generated and found.path.name != "pyproject.toml":
            found.path.unlink(missing_ok=True)
        build_log.unlink(missing_ok=True)

        check_cancel()                    # last exit: after this the folder is live
        progress("原子換位…")
        _swap_into_place(staging, final)

        size = directory_size(final)
        progress(f"完成:{final}({size / MB:.0f} MB)")
        progress("大小組成:")
        for line in size_breakdown(final):
            progress(line)
        return BuildResult(
            ok=True, package_dir=final, size_bytes=size,
            duration_seconds=time.monotonic() - started,
            warnings=warnings + scan_project(request).warnings,
            message=f"完成:{final}",
        )
    except BuildCancelled:
        # Not a failure: nothing is broken, nothing is half-written, and the
        # caller must not be able to mistake this for success.
        shutil.rmtree(staging, ignore_errors=True)
        message = "已取消建置,暫存目錄已清乾淨"
        progress(message)
        return BuildResult(ok=False, cancelled=True, message=message,
                           duration_seconds=time.monotonic() - started)
    except (runtime_mod.RuntimeError_, imports_mod.ImportGateError,
            imports_mod.ImportProbeError, requirements_mod.RequirementsError,
            OSError, shutil.Error) as exc:
        # Rescue the log BEFORE deleting staging — the old code pointed the
        # operator at a path it had just removed.
        saved_log = _rescue_log(build_log, request.output_dir, final.name)
        shutil.rmtree(staging, ignore_errors=True)   # previous output untouched
        # ...and the exception text ITSELF names that staging path (pip's error
        # carries "log:<staging>\data\logs\build.log"). Rescuing the file while
        # still printing the old address just moves the dead end one line down.
        message = str(exc)
        if saved_log is not None:
            message = message.replace(str(build_log), str(saved_log))
        return BuildResult(ok=False, errors=[message], log_path=saved_log,
                           warnings=warnings,
                           duration_seconds=time.monotonic() - started)


def smoke_test(package: Path, manifest: dict) -> list[str]:
    """Catch a broken package here, on the build machine, rather than on the
    user's desk."""
    problems: list[str] = []
    for key in ("entrypoint", "python", "shell_executable", "engine_shim"):
        relative = manifest[key]
        if os.path.isabs(relative):
            problems.append(f"manifest.{key} 是絕對路徑:{relative}")
            continue
        target = (package / relative).resolve()
        if package.resolve() not in target.parents:
            problems.append(f"manifest.{key} 逃出交付根目錄:{relative}")
        elif not target.is_file():
            problems.append(f"manifest.{key} 指向不存在的檔案:{relative}")
    if not (package / "start.bat").is_file():
        problems.append("缺少 start.bat")
    return problems


def _rescue_log(build_log: Path, output_dir: Path, name: str) -> Path | None:
    """Keep the failed build's log where the operator can still open it."""
    if not build_log.is_file():
        return None
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = Path(output_dir) / f"build-failed-{name}-{stamp}.log"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(build_log, target)
        return target
    except OSError:
        return None


def _rename_with_retry(src: Path, dst: Path, attempts: int = 6) -> None:
    """Windows hands out ERROR_ACCESS_DENIED when anything still holds a handle
    inside the directory — and Defender reliably does, right after we have just
    written a 250 MB runtime into it. The lock is transient, so back off and
    retry instead of failing a build that actually succeeded."""
    delay = 0.5
    for attempt in range(1, attempts + 1):
        try:
            os.rename(src, dst)
            return
        except PermissionError:
            if attempt == attempts:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 8.0)


def _swap_into_place(staging: Path, final: Path) -> None:
    """The old package is moved aside first (rename needs a free destination) and
    only deleted once the new one is safely in place."""
    backup = None
    if final.exists():
        backup = final.with_name(f"{final.name}.old-{datetime.now().strftime('%Y%m%d-%H%M%S')}")
        _rename_with_retry(final, backup)
    try:
        _rename_with_retry(staging, final)
    except OSError:
        if backup is not None:
            _rename_with_retry(backup, final)  # put the working one back
        raise
    if backup is not None:
        shutil.rmtree(backup, ignore_errors=True)


def _write_readme(staging: Path, manifest: dict) -> None:
    port = manifest.get("preferred_port") or 0
    port_line = ("* 啟動程式每次會自動挑一個沒被占用的埠(8000–9000),不需手動處理。"
                 if not port else
                 f"* 若 {port} 埠被其他程式占用,啟動程式會自動改用其他可用埠,不需手動處理。")
    # 檔名與 GUI 完成對話框講的那個檔名必須是同一個。它們一度不同(對話框說
    # 「讀我-使用說明.txt」,資料夾裡卻只有 README.txt),於是管理員在資料夾裡
    # 找不到對話框叫他看的東西。步驟也只寫一次:曾經一份說兩步、一份說三步,
    # 而且叫人按一個畫面上不存在的「啟動」鈕。
    (staging / README_NAME).write_text(
        f"""{manifest['display_name']}
{'=' * len(manifest['display_name'])}

使用方式
--------
1. 雙擊 start.bat。
2. 等應用視窗出現後,在上方「工作流程」下拉確認選到「{manifest['display_name']}」。
3. 按一次旁邊的「Start」按鈕,應用就會顯示在視窗裡。

這台電腦不需要安裝 Python、Streamlit、Node 或 Rust —— 全部都在這個資料夾裡。
整個資料夾可以直接複製到別的位置或別台電腦,不需重新安裝。

唯一的例外:Microsoft Edge WebView2 Runtime
------------------------------------------
應用視窗是用 WebView2 畫出來的。Windows 10/11 大多已內建;若沒有,start.bat 會
在啟動前就告訴你,並要你先執行 tools\\安裝WebView2.bat(可用一般使用者權限安裝,
不需系統管理員)。離線機器請向提供者索取 prereq\\ 資料夾。

第一次執行時的安全性提示
------------------------
* 出現「Windows 已保護您的電腦」:點「其他資訊」→「仍要執行」。
* 公司防毒可能會隔離這個資料夾裡的程式。請 IT 把整個資料夾加進排除清單。

疑難排解
--------
* 啟動失敗時,錯誤訊息與詳細記錄在 data\\logs\\ 底下:
    launcher-*.log    啟動流程
    streamlit-*.log   應用本身的輸出
{port_line}

移除
----
直接刪掉這個資料夾即可,不會在系統其他地方留下任何東西。
""",
        encoding="utf-8",
    )
    _write_webview2_helper(staging)


def _write_webview2_helper(staging: Path) -> None:
    """The fat package told the user to run tools\\安裝WebView2.bat — a file that
    only the store layout ever produced. Pointing at a file you did not ship is
    worse than saying nothing."""
    tools = staging / "tools"
    tools.mkdir(exist_ok=True)
    (tools / "安裝WebView2.bat").write_text(
        """@echo off
chcp 65001 >nul 2>&1
title 安裝 Microsoft Edge WebView2 Runtime
pushd "%~dp0.." || (echo [ERROR] 無法進入程式資料夾。& pause & exit /b 1)

if exist "prereq\\MicrosoftEdgeWebview2Setup.exe" (
  echo 正在安裝 WebView2 Runtime(不需系統管理員權限)…
  "prereq\\MicrosoftEdgeWebview2Setup.exe" /silent /install
  goto done
)

echo 這個交付包裡沒有附安裝檔。請用以下任一方式:
echo.
echo   1. 有網路的話,開啟這個網址下載「Evergreen Bootstrapper」並執行:
echo      https://go.microsoft.com/fwlink/p/?LinkId=2124703
echo   2. 沒網路的話,請向提供者索取 prereq\\MicrosoftEdgeWebview2Setup.exe,
echo      放進這個資料夾的 prereq\\ 底下,再執行一次本檔案。
echo.
echo 它可以用一般使用者權限安裝,不需要系統管理員。

:done
popd
pause
""",
        encoding="utf-8",
    )
