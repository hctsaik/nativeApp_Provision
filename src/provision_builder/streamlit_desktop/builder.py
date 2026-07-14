"""Assemble the deliverable folder.

Everything is built in a staging directory beside the output and only swapped
into place once it is complete and smoke-tested, so a failed build can never
leave a half-written folder where a working one used to be (spec §7.1.10).

No Tkinter here: the GUI passes a progress callback and gets a BuildResult back.
"""

from __future__ import annotations

import json
import os
import re
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
from . import pages as pages_mod
from . import requirements as requirements_mod
from . import runtime as runtime_mod
from .models import (
    DEFAULT_STARTUP_TIMEOUT,
    EXCLUDED_DIRS_ANY_DEPTH,
    EXCLUDED_DIRS_ROOT_ONLY,
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

# MICROSOFT SHIPS WEBVIEW2 TWO WAYS, AND ONLY ONE OF THEM CAN INSTALL ON THE
# MACHINE THIS PRODUCT EXISTS FOR.
#
#   * Evergreen BOOTSTRAPPER — MicrosoftEdgeWebview2Setup.exe, ~2 MB, LinkId=2124703.
#     It does not contain WebView2. Running it DOWNLOADS WebView2 from Microsoft. On
#     an air-gapped factory PC it cannot work, and putting it in prereq\ changes
#     nothing: the folder is carrying a downloader onto a machine that cannot
#     download.
#   * Evergreen STANDALONE INSTALLER — MicrosoftEdgeWebView2RuntimeInstallerX64.exe,
#     ~130 MB, LinkId=2124701. The whole runtime, in the file, installs with no
#     network. This is the ONLY one worth shipping in prereq\, and it is what every
#     message in this repo must ask for.
#
# We used to do the opposite: name the bootstrapper as the canonical file, print its
# URL everywhere, and — worse — RENAME whatever installer the admin picked to the
# bootstrapper's name, so an operator who correctly downloaded the 130 MB standalone
# had it silently turned into the thing that cannot work. The admin's file now keeps
# its own name (the helper bat takes any .exe in prereq\).
WEBVIEW2_INSTALLER_NAME = "MicrosoftEdgeWebView2RuntimeInstallerX64.exe"
WEBVIEW2_DOWNLOAD = "https://go.microsoft.com/fwlink/?LinkId=2124701"
# The bootstrapper is ~2 MB, the standalone runtime ~130 MB. Anything under this in
# prereq\ is the downloader, not the runtime — and on the machine that needs prereq\
# at all, that is the difference between a fixable machine and a dead end.
WEBVIEW2_MIN_OFFLINE_BYTES = 10 * 1024 * 1024

# The build succeeds, the folder is complete, and on the machine it was built FOR
# — an air-gapped factory PC — it cannot open a window. start.bat detects that and
# exits 5; tools\安裝WebView2.bat offers to fix it and then finds no installer to
# run and no network to fetch one. A clean "完成" on top of that is a lie, so the
# result carries this. Set BuildRequest.webview2_installer and it goes away.
#
# It used to offer ONE remedy — 「請在建置時指定」, i.e. rebuild. In a store that
# advice is refused outright (a completed version directory is immutable), and even
# in fat mode a rebuild costs the operator six minutes for a file they could simply
# copy. The remedy that always works comes first: drop the .exe into prereq\.
WEBVIEW2_MISSING_WARNING = (
    "未附 WebView2 離線安裝檔;目標機若沒有 WebView2,App 開不起來(exit 5),"
    "而且沒有網路就裝不了。"
    "離線機器必須用「Evergreen Standalone Installer」"
    f"({WEBVIEW2_INSTALLER_NAME},約 130 MB,它本身就含整個 runtime);"
    "2 MB 的 MicrosoftEdgeWebview2Setup.exe 是「需要連網」的 bootstrapper,"
    "它執行時才去微軟網站下載,放進 prereq\\ 也裝不起來。"
    "已經建好的包不必重建:把安裝檔複製到 <交付包>\\prereq\\ 底下就行"
    "(tools\\安裝WebView2.bat 認得那裡的任何 .exe)。"
    f"下載:{WEBVIEW2_DOWNLOAD}"
)


def webview2_bootstrapper_warning(installer: Path, size_bytes: int) -> str:
    """The admin picked a file too small to be the offline runtime.

    Said at BUILD time, on the machine that can still fix it — not on the factory
    floor, where the only thing left to do is carry the PC to a network.
    """
    return (
        f"你指定的 WebView2 安裝檔只有 {size_bytes / MB:.1f} MB"
        f"({installer.name}),這是「需要連網」的 Evergreen Bootstrapper,"
        "它本身不含 WebView2,執行時才去微軟網站下載 —— 離線的目標機裝不起來。"
        "離線機器要用的是「Evergreen Standalone Installer」"
        f"({WEBVIEW2_INSTALLER_NAME},約 130 MB):{WEBVIEW2_DOWNLOAD}"
        "。檔案已經照你指定的放進 prereq\\ 了(檔名不會被改),"
        "但請換成 Standalone 版再交付出去。"
    )
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

def _at_project_root(rel: str | None) -> bool:
    """Is this entry a direct child of the project root?

    `rel is None` means the caller did not tell us where the entry sits. We then
    assume the root, i.e. the conservative, drop-it answer — every caller inside
    this module DOES pass `rel`, so the depth-aware behaviour is what actually
    runs; this only keeps a naive external caller from silently shipping a 124 MB
    root wheelhouse.
    """
    return rel is None or "/" not in rel.replace("\\", "/").lstrip("./")


def _builtin_reason(name: str, is_dir: bool, rel: str | None = None) -> str | None:
    """The built-in exclusions, applied at the depth where they actually mean what
    their name says. See EXCLUDED_DIRS_ANY_DEPTH / EXCLUDED_DIRS_ROOT_ONLY."""
    if is_dir:
        if name in EXCLUDED_DIRS_ANY_DEPTH:
            return f"{name}/"
        if name in EXCLUDED_DIRS_ROOT_ONLY and _at_project_root(rel):
            return f"{name}/"                      # the PROJECT's build junk, at its root
    for pattern in EXCLUDED_FILES:                 # `*.egg-info` is a DIRECTORY: match both
        if fnmatch(name, pattern):
            return pattern
    return None


def ignore_reason(name: str, is_dir: bool, extra: Sequence[str] = (),
                  rel: str | None = None) -> str | None:
    """Why this entry is excluded, as a label for the report — or None to keep it.

    `rel` is the entry's path relative to the project root (posix). Patterns that
    contain a slash are matched against it; patterns without one are matched
    against the bare name, at any depth. That is what gitignore does, and getting
    it wrong here is not a cosmetic bug — see _matches_ignore.

    LAST MATCHING PATTERN WINS, gitignore-style, and a `!pattern` re-includes.
    The built-in rules are only the STARTING position: they used to be checked
    first and returned immediately, so nothing the operator could write in
    .provisionignore was able to rescue a file we had decided to drop. An escape
    hatch that cannot open is not an escape hatch. `!` lines were worse than
    absent — they were loaded, kept in the pattern list, and then quietly treated
    as "not a pattern", so a .provisionignore full of `!keep/this` looked honoured
    and did nothing at all.
    """
    reason = _builtin_reason(name, is_dir, rel)
    for raw in extra:
        pattern = raw.strip()
        if not pattern or pattern.startswith("#"):
            continue
        negated = pattern.startswith("!")
        body = pattern[1:].strip() if negated else pattern
        if not body:
            continue
        if _matches_ignore(body, name, is_dir, rel):
            reason = None if negated else f"{pattern}(排除樣式)"
    return reason


def should_ignore(name: str, is_dir: bool, extra: Sequence[str] = (),
                  rel: str | None = None) -> bool:
    """The single source of truth for 'does this entry travel into the package'."""
    return ignore_reason(name, is_dir, extra, rel) is not None


# `data/*` and `data/**` name every child of data/ — which means they name data/.
_CONTAINER = re.compile(r"/\*{1,2}$")


def _matches_ignore(pattern: str, name: str, is_dir: bool, rel: str | None = None) -> bool:
    """gitignore-flavoured, fnmatch-powered: `wheels/`, `*.mp4`, `data/*`, `notes.txt`.

    A pattern containing a slash is a PATH pattern and is matched against the
    entry's path relative to the project root. It used to be collapsed to its last
    segment — so `data/*` became `*`, `fnmatch(anything, "*")` was True, and one
    exclude pattern silently excluded every file in the project. The build still
    "succeeded": it shipped a package with no application code in it.

    THE SEPARATOR IS NORMALISED IN THE PATTERN, not only in the path. This is a
    Windows product: the operator types `recordings\\*` into 額外排除, because that
    is what every path on their screen looks like. `"/" not in pattern` was then
    False, so the whole thing was treated as a BARE NAME, fnmatch'd against
    「demo.mp4」, matched nothing, and the exclusion silently did nothing at all —
    while the GUI accepted the pattern and the report said it was in force. A
    pattern that is accepted, looks applied and does nothing is worse than one that
    is rejected.

    A leading `!` is NOT handled here: negation is a decision about the whole
    pattern list (last match wins), not about one pattern, so it lives in
    ignore_reason() and this function only ever sees the pattern body.
    """
    pattern = pattern.strip().replace("\\", "/")
    if not pattern:
        return False
    dir_only = pattern.endswith("/")
    pattern = pattern.rstrip("/")
    if pattern.startswith("./"):
        pattern = pattern[2:]
    if not pattern or (dir_only and not is_dir):
        return False

    if "/" not in pattern:                    # bare name: matches at any depth
        return fnmatch(name, pattern)

    # Path pattern. Without a relative path we cannot honestly evaluate it, and
    # guessing (the old behaviour) is what deleted everything — so we keep the
    # entry and let the caller that DOES know the path decide.
    if rel is None:
        return False
    target = rel.replace("\\", "/").lstrip("./")
    if fnmatch(target, pattern):
        return True
    if is_dir:
        # `data/*` matched every child of data/ but not data/ itself, so the
        # directory was never pruned: its children were excluded one by one and an
        # EMPTY data/ shipped. The operator who wrote `data/*` and still finds
        # data/ sitting in the package concludes, reasonably, that the exclusion
        # did not work. Match the container too, and the whole subtree goes.
        container = _CONTAINER.sub("", pattern)
        if container and container != pattern and fnmatch(target, container):
            return True
    # `data/*` should also drop everything beneath data/, not just its children.
    return fnmatch(target, pattern.rstrip("*").rstrip("/") + "/*")


def ignore_patterns_for(request: BuildRequest) -> tuple[str, ...]:
    """The project's own .provisionignore, plus whatever the caller added.

    `!` lines are KEPT: they are re-includes and ignore_reason() honours them
    (last match wins). They used to be kept here and dropped there.
    """
    patterns: list[str] = []
    ignore_file = Path(request.project_dir) / PROVISIONIGNORE
    if ignore_file.is_file():
        patterns += [line.strip()
                     for line in ignore_file.read_text("utf-8", errors="replace").splitlines()
                     if line.strip() and not line.strip().startswith("#")]
    patterns += list(getattr(request, "extra_excludes", ()) or ())
    return tuple(patterns)


def copytree_ignore(extra: Sequence[str] = (), root: Path | None = None):
    """The `ignore=` callable for shutil.copytree, driven by should_ignore().

    `root` is the project root, so a path pattern (`data/*`) can be evaluated
    against where the entry actually sits rather than guessed from its name.
    """
    def ignore(directory, names):
        base = Path(directory)
        dropped = set()
        for name in names:
            entry = base / name
            rel = None
            if root is not None:
                try:
                    rel = entry.relative_to(root).as_posix()
                except ValueError:
                    rel = None
            if should_ignore(name, entry.is_dir(), extra, rel):
                dropped.add(name)
        return dropped
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

    def _rel(path: Path) -> str | None:
        try:
            return path.relative_to(root).as_posix()
        except ValueError:
            return None

    for dirpath, dirnames, filenames in os.walk(root):
        here = Path(dirpath)
        keep_dirs = []
        for name in dirnames:
            reason = ignore_reason(name, True, extra, _rel(here / name))
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
            reason = ignore_reason(name, False, extra, _rel(path))
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

    # A directory whose own big file we have ALREADY named does not need a second
    # warning. Which directory that is, is a fact we have — the file's top-level
    # folder — and it used to be guessed with `name in warning_text`: a SUBSTRING
    # search over the warning sentences. So a genuinely 400 MB `data\` folder went
    # unmentioned the moment any other warning happened to contain the letters
    # "data" (「專案裡的大檔:metadata.bin」 is enough), and it then travelled on
    # every single update. Match the directory, not the letters.
    named_dirs: set[str] = set()
    for name, size in sorted(big_files, key=lambda item: item[1], reverse=True)[:3]:
        scan.warnings.append(f"專案裡的大檔:{name}({size / MB:.0f} MB)——確定要交付嗎?")
        head, slash, _tail = name.partition("/")
        named_dirs.add(head if slash else "(根目錄)")
    for name, size in sorted(per_dir.items(), key=lambda item: item[1], reverse=True)[:2]:
        if size > big_dir_mb * MB and name not in named_dirs:
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


def _rmtree_with_retry(path: Path, attempts: int = 8,
                       progress: Progress = _noop) -> bool:
    """Delete a tree, backing off exactly like _rename_with_retry does — and then
    CHECK. Returns True only if the directory is really gone.

    `shutil.rmtree(ignore_errors=True)` is not a delete, it is a wish. We call this
    immediately after taskkill'ing a pip process tree, and Windows keeps the handles
    of a dying process open for a moment; Defender, having just watched us write
    500 MB, is holding the tree open too. rmtree then fails, ignore_errors swallows
    it, and 600 MB stays on the disk under a message that says it is gone.
    """
    path = Path(path)
    delay = 0.5
    for attempt in range(1, attempts + 1):
        try:
            shutil.rmtree(path)
        except FileNotFoundError:
            return True
        except OSError:
            pass                      # in use: back off and try again
        if not path.exists():
            return True
        if attempt == attempts:
            return False
        if attempt == 2:              # the first couple usually win; do not chatter
            progress("暫存目錄還被系統鎖住(防毒或剛結束的 pip),等它放行…")
        time.sleep(delay)
        delay = min(delay * 2, 5.0)
    return not path.exists()


def _remove_staging(staging: Path, progress: Progress = _noop) -> tuple[bool, str]:
    """Delete the staging directory and report WHAT ACTUALLY HAPPENED.

    Never claim a cleanup you did not verify. This is the whole bug: the cancel
    path said 「暫存目錄已清乾淨」 unconditionally while doing an ignore_errors
    rmtree on a tree we had just killed a pip process inside of — so the operator
    was told the 600 MB was gone while it sat there in the output folder.
    """
    if not Path(staging).exists():
        return True, ""
    if _rmtree_with_retry(staging, progress=progress):
        return True, ""
    return False, (f"暫存目錄 {staging} 有檔案被系統鎖住,暫時刪不掉"
                   "(下次建置會自動清掉)")


def clean_orphan_staging(output_dir: Path, progress: Progress = _noop) -> int:
    """A crashed or killed build leaves a `.staging-*` directory holding a whole
    copied runtime — hundreds of MB of invisible garbage in the operator's output
    folder. Sweep them before we add one more, and say how much we got back.

    This is also the promise the cancel path makes when it cannot delete its own
    staging directory (「下次建置會自動清掉」), so it has to be a real sweep: retry
    through the transient lock, and only count the bytes that actually went away.
    It used to add `size` to the total whatever rmtree(ignore_errors=True) did with
    it, i.e. it reported reclaiming space it had not reclaimed — the same lie one
    level down.
    """
    freed = 0
    for path in sorted(Path(output_dir).glob(".staging-*")):
        if not path.is_dir():
            continue
        size = directory_size(path)
        progress(f"清掉上次沒收乾淨的暫存目錄:{path.name}({size / MB:.0f} MB)")
        if _rmtree_with_retry(path, progress=progress):
            freed += size
        else:
            progress(f"注意:{path.name} 現在刪不掉(有檔案被鎖住),"
                     "它還留在輸出資料夾裡,下次建置會再試一次。")
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
            ignore=copytree_ignore(ignore_patterns_for(request), request.project_dir),
            dirs_exist_ok=True,
        )

        check_cancel()
        progress("複製可攜 Python runtime…")
        # The 500 MB step. It gets the cancel flag AND a progress hook: a check at
        # the stage boundary only tells the operator "your 取消 will be honoured
        # once this finishes", which for this stage is half a minute of a frozen
        # button and no output at all.
        python = runtime_mod.copy_runtime(request.runtime_template, staging / "runtime",
                                          should_cancel=should_cancel, progress=progress)

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
        # The shared page rules — 「what does Streamlit actually LOAD」 — travel next
        # to launch.py. The delivered machine has no provision_builder to import them
        # from, so launch.py loads this file BY PATH; ship the package without it and
        # the launcher refuses to start (LauncherIncomplete, exit 4) rather than run a
        # preflight that is silently blind to pages\. One rulebook, both sides: the
        # build gate could not see pages\ at all, so a missing import in
        # pages\2_report.py passed every check here and failed on the factory floor.
        shutil.copy2(pages_mod.SOURCE, staging / "launcher" / pages_mod.DELIVERED_NAME)
        shutil.copy2(TEMPLATES / "start.bat", staging / "start.bat")
        _write_messages(staging)          # the Chinese start.bat `type`s; see _write_bat

        has_prereq, webview2_notes = _stage_webview2(request, staging, progress)
        if not has_prereq:
            warnings.append(WEBVIEW2_MISSING_WARNING)
        warnings += webview2_notes

        manifest = build_manifest(request, request.shell_exe.name)
        (staging / "app-package.json").write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        _write_readme(staging, manifest, has_prereq=has_prereq)

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
        #
        # But do not TELL them the staging directory is gone until it is. We reach
        # here moments after taskkill'ing pip's whole process tree, and on Windows
        # its handles outlive it; ignore_errors=True then swallowed the failure and
        # we announced 「暫存目錄已清乾淨」 over 600 MB that was still sitting in the
        # operator's output folder. Retry, verify, and say which of the two happened.
        removed, note = _remove_staging(staging, progress)
        message = ("已取消建置,暫存目錄已清乾淨" if removed
                   else f"已取消建置。{note}")
        progress(message)
        # `staging_left` is the flag a GUI can branch on. The message alone was not
        # enough: the completion dialog rendered its own 「暫存目錄已清乾淨」 over
        # the honest text, so the operator was told a 600 MB directory was gone
        # while it sat in their output folder. A field cannot be overwritten by a
        # sentence somebody hardcoded.
        return BuildResult(ok=False, cancelled=True, message=message,
                           staging_left=None if removed else staging,
                           warnings=warnings if removed else warnings + [note],
                           duration_seconds=time.monotonic() - started)
    except (runtime_mod.RuntimeError_, imports_mod.ImportGateError,
            imports_mod.ImportProbeError, requirements_mod.RequirementsError,
            OSError, shutil.Error) as exc:
        # Rescue the log BEFORE deleting staging — the old code pointed the
        # operator at a path it had just removed.
        saved_log = _rescue_log(build_log, request.output_dir, final.name)
        removed, note = _remove_staging(staging, progress)   # previous output untouched
        # ...and the exception text ITSELF names that staging path (pip's error
        # carries "log:<staging>\data\logs\build.log"). Rescuing the file while
        # still printing the old address just moves the dead end one line down.
        message = str(exc)
        if saved_log is not None:
            message = message.replace(str(build_log), str(saved_log))
        if not removed:
            progress(note)
        return BuildResult(ok=False, errors=[message], log_path=saved_log,
                           staging_left=None if removed else staging,
                           warnings=warnings if removed else warnings + [note],
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
    # launch.py loads this by path on a machine that has no provision_builder. Without
    # it the delivered App does not start at all (LauncherIncomplete → exit 4), and
    # the operator reads 「launcher 資料夾不完整…請重新建置」 about a build that this
    # very function had just declared complete.
    if not (package / "launcher" / pages_mod.DELIVERED_NAME).is_file():
        problems.append(f"缺少 launcher\\{pages_mod.DELIVERED_NAME}"
                        "(launch.py 靠它判斷 Streamlit 會載入哪些頁面)")
    # start.bat is ASCII and `type`s its Chinese out of messages\. Ship it without
    # them and every error the user can actually hit prints an English tag and then
    # silence — which is how they learn nothing at the one moment they need to.
    for name in MESSAGES:
        if not (package / "messages" / name).is_file():
            problems.append(f"缺少 messages\\{name}(start.bat 靠它印中文訊息)")
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


def _rename_with_retry(src: Path, dst: Path, attempts: int = 12,
                       progress: Progress = _noop) -> None:
    """Windows hands out ERROR_ACCESS_DENIED when anything still holds a handle
    inside the directory — and Defender reliably does, right after we have just
    written a 500 MB runtime into it. The lock is transient, so back off and
    retry instead of failing a build that actually succeeded.

    Six attempts (~15s) was not enough: a real build of CV_Viewer's runtime lost
    this race on a machine with real-time protection on. Defender's scan of a
    freshly written tree that size takes as long as it takes, so we wait for it —
    and if we do give up, we say what actually happened, because "[WinError 5]
    存取被拒。" in front of two .staging paths tells the operator nothing they can act on.
    """
    delay = 0.5
    waited = 0.0
    for attempt in range(1, attempts + 1):
        try:
            os.rename(src, dst)
            if waited:
                progress(f"（防毒掃描讓這一步多等了 {waited:.0f} 秒）")
            return
        except PermissionError as exc:
            if attempt == attempts:
                raise runtime_mod.RuntimeError_(
                    f"搬移建置好的檔案時被系統擋住(等了 {waited:.0f} 秒仍未放行):{dst}\n"
                    "  幾乎都是防毒軟體(Windows Defender / 公司防毒)還在掃描剛寫好的\n"
                    "  幾百 MB 檔案,暫時鎖住了它們。東西其實已經建好了。\n"
                    "  解法(擇一):\n"
                    "    · 直接重跑一次建置(通常第二次就過了,掃描已經做完)\n"
                    "    · 請 IT 把輸出資料夾加進防毒的排除清單,之後就不會再遇到\n"
                    f"  原始錯誤:{exc}") from exc
            if attempt == 3:      # 等到這裡才吭聲:前兩次通常瞬間就過了
                progress("防毒正在掃描剛寫好的檔案,等它放行…（這很正常,不用理它）")
            time.sleep(delay)
            waited += delay
            delay = min(delay * 2, 10.0)


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


def _stage_webview2(request: BuildRequest, staging: Path,
                    progress: Progress = _noop) -> tuple[bool, list[str]]:
    """Put the WebView2 offline installer where tools\\安裝WebView2.bat looks for it.

    Returns (the package now carries an installer, things the operator must be told).

    THE FILE KEEPS THE NAME THE ADMIN GAVE US. The helper bat runs any .exe it finds
    in prereq\\, so there is nothing to gain by renaming — and everything to lose:
    the store builder used to rename the admin's file to
    「MicrosoftEdgeWebview2Setup.exe」, which is the ~2 MB Evergreen BOOTSTRAPPER, a
    downloader that cannot install anything without a network. An operator who
    correctly fetched the 130 MB standalone runtime got it silently relabelled as the
    one thing that cannot work on the air-gapped machine this feature exists for.

    And if the file they picked really IS the 2 MB bootstrapper, we say so now — on
    the build machine, where a 130 MB download is thirty seconds — instead of on the
    factory floor, where it is a dead end.
    """
    installer = getattr(request, "webview2_installer", None)
    if installer is None:
        return False, []
    source = Path(installer)
    if not source.is_file():
        # Loud, at build time, on the machine that can still fix it — never a
        # silent downgrade to "no prereq" on a package we promised one for.
        raise runtime_mod.RuntimeError_(f"找不到指定的 WebView2 離線安裝檔:{source}")
    prereq = staging / "prereq"
    prereq.mkdir(exist_ok=True)
    shutil.copy2(source, prereq / source.name)   # its own name, never renamed
    progress(f"附上 WebView2 離線安裝檔:prereq\\{source.name}")

    notes: list[str] = []
    size = source.stat().st_size
    if size < WEBVIEW2_MIN_OFFLINE_BYTES:
        notes.append(webview2_bootstrapper_warning(source, size))
        progress("注意:" + notes[-1])
    return True, notes


def _write_readme(staging: Path, manifest: dict, has_prereq: bool = False) -> None:
    port = manifest.get("preferred_port") or 0
    port_line = ("* 啟動程式每次會自動挑一個沒被占用的埠(8000–9000),不需手動處理。"
                 if not port else
                 f"* 若 {port} 埠被其他程式占用,啟動程式會自動改用其他可用埠,不需手動處理。")
    # 這一段曾經無條件叫離線的使用者「向提供者索取 prereq\ 資料夾」——而 prereq\
    # 從來沒有人產生過。現在它只講這個交付包裡真的有的東西,而且講對要哪一支安裝檔:
    # 2 MB 的 Setup.exe 是需要連網的 bootstrapper,離線機器放進 prereq\ 也裝不起來。
    webview2_line = (
        "離線安裝檔已附在這個資料夾的 prereq\\ 底下,不需要網路。"
        if has_prereq else
        "這個交付包「沒有」附離線安裝檔。若目標機不能連網,請向提供者索取\n"
        f"「Evergreen Standalone Installer」({WEBVIEW2_INSTALLER_NAME},約 130 MB,\n"
        "檔案本身就含整個 runtime),放進這個資料夾的 prereq\\ 底下,再執行\n"
        "tools\\安裝WebView2.bat(它認得 prereq\\ 裡的任何 .exe,檔名不必改)。\n"
        "注意:2 MB 的 MicrosoftEdgeWebview2Setup.exe 是「需要連網」的 bootstrapper,\n"
        "它執行時才去微軟網站下載 WebView2,離線機器放進 prereq\\ 也裝不起來。\n"
        f"下載:{WEBVIEW2_DOWNLOAD}")
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
在啟動前就告訴你(代碼 5),並要你先執行 tools\\安裝WebView2.bat(可用一般使用者
權限安裝,不需系統管理員)。
{webview2_line}

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
    worse than saying nothing.

    It ran ONE hard-coded name, prereq\\MicrosoftEdgeWebview2Setup.exe. Microsoft
    ships the offline runtime as MicrosoftEdgeWebView2RuntimeInstallerX64.exe, and
    both take `/silent /install`, so an operator who did the right thing with the
    right file got "沒有附安裝檔". Take whatever .exe is in prereq\\.

    And when the install FAILS, size is evidence. The bootstrapper is ~2 MB and the
    standalone runtime ~130 MB, so a sub-10 MB file in prereq\\ on a machine that
    just failed to install is not a mystery to be escalated — it is a downloader on
    a machine with no network, and the bat says so in one sentence instead of
    sending the operator back to the supplier for a file they already have.

    No `goto`, no `:label` — see _write_bat. The control flow here is if/else and
    a captured RC, which needs neither.
    """
    tools = staging / "tools"
    tools.mkdir(exist_ok=True)
    _write_bat(tools / "安裝WebView2.bat", r"""@echo off
rem PURE ASCII, on purpose -- see builder._write_bat. The Chinese this prints lives
rem in messages\*.txt and is `type`d, because cmd.exe mis-seeks a non-ASCII .bat.
chcp 65001 >nul 2>&1
title Install Microsoft Edge WebView2 Runtime
pushd "%~dp0.." || (echo [ERROR] cannot enter the program folder. & pause & exit /b 1)

rem There is more than one legitimate file name: the Evergreen Bootstrapper ships as
rem MicrosoftEdgeWebview2Setup.exe and the offline runtime as
rem MicrosoftEdgeWebView2RuntimeInstallerX64.exe. Both take /silent /install. This
rem used to run ONE hard-coded name, so an operator who supplied the right file was
rem told the package had no installer. Take whatever .exe is in prereq\.
set "WV2SETUP="
for %%F in ("prereq\*.exe") do if not defined WV2SETUP set "WV2SETUP=prereq\%%~nxF"

if not defined WV2SETUP (
  type "messages\webview2-none.txt" 2>nul
  popd
  pause
  exit /b 1
)

rem How big is it? The bootstrapper is ~2 MB and does not contain WebView2 at all --
rem it downloads it. The standalone runtime is ~130 MB. On the offline machine this
rem package exists for, that number is the whole diagnosis, so read it BEFORE the
rem install: %SZ% inside the if-block below is expanded when the block is parsed.
set "SZ=0"
for %%A in ("%WV2SETUP%") do set "SZ=%%~zA"
if not defined SZ set "SZ=0"

type "messages\webview2-installing.txt" 2>nul
echo   %WV2SETUP%
"%WV2SETUP%" /silent /install
rem Capture RC OUTSIDE a block: %errorlevel% inside `if (...)` is expanded when the
rem block is read, which is before the installer has run, so it prints a stale value.
set "RC=%errorlevel%"

if not "%RC%"=="0" (
  echo [ERROR] exit code %RC%
  type "messages\webview2-failed.txt" 2>nul
  if %SZ% LSS {min_bytes} type "messages\webview2-bootstrapper.txt" 2>nul
) else (
  type "messages\webview2-done.txt" 2>nul
)
popd
pause
exit /b %RC%
""".replace("{min_bytes}", str(WEBVIEW2_MIN_OFFLINE_BYTES)))


def _write_bat(path: Path, text: str) -> None:
    """Write a batch file cmd.exe can actually parse. ASCII only, CRLF, no BOM.

    The em-dash rule everyone knows ("U+2014 breaks cmd") is a special case of a
    much worse one, and chasing the special case let the general one ship.

    Under `chcp 65001` cmd tracks its position in a .bat as a BYTE offset but
    computes it by counting CHARACTERS. While it only moves forward one line at a
    time, nothing goes wrong. The moment it has to RE-READ the file — after a
    `for /f`, a pipe, an external command, a `goto` — it seeks to an offset that is
    wrong by however many multi-byte characters came before it, lands in the MIDDLE
    of a line, and executes whatever it finds there. We watched a real cmd.exe
    execute the tail of a Chinese `rem` comment in start.bat. It happens on roughly
    1 run in 20, which is precisely how it passed review and shipped: a .bat that
    works nineteen times looks like a .bat that works.

    In an ASCII-only file, byte offset == character offset, so the seek cannot miss.
    That is the entire fix, it is mechanical, and it subsumes the em-dash rule.
    Operator-facing Traditional Chinese goes in messages\\*.txt and is printed with
    `type`, which hands the bytes to the console and never parses them.
    """
    problems = bat_problems(text)
    if problems:
        raise ValueError(f"{path.name}: " + "; ".join(problems))
    path.write_bytes(text.replace("\r\n", "\n").replace("\n", "\r\n").encode("ascii"))


def bat_problems(text: str) -> list[str]:
    """Everything we know cmd.exe gets wrong, checked mechanically. Used by
    _write_bat for the files we generate and by the tests for the static template."""
    problems = []
    if not text.isascii():
        stray = sorted({ch for ch in text if not ch.isascii()})
        problems.append(
            f"不是純 ASCII:{stray[:8]};cmd.exe 在 chcp 65001 下會 seek 到行中間"
            "(約 1/20 機率)。中文請放 messages\\*.txt,用 type 印")
    for line in text.splitlines():
        stripped = line.strip()
        # An unescaped ( or ) in an echo INSIDE a block closes the block early. The
        # WebView2 gate said `echo ... (exit 5)`, the `)` ended the `if`, and the
        # error printed UNCONDITIONALLY: every machine was turned away, WebView2 or
        # not. We never need a literal paren in a .bat echo; the .txt messages can.
        if re.match(r"(?i)^echo\b", stripped) and ("(" in stripped or ")" in stripped):
            problems.append(f"echo 行裡有括號,會把外層區塊提早關掉:{stripped!r}")
    return problems


# The Chinese start.bat and 安裝WebView2.bat print. cmd `type`s these; it never
# parses them, so they can hold anything a cp950 console can render.
MESSAGES: dict[str, str] = {
    "title.txt": "應用程式啟動中 - 請不要關閉這個視窗",
    "start-nofolder.txt":
        "無法進入程式資料夾。\n"
        "若是從網路磁碟機執行,請先把整個資料夾複製到本機磁碟,再試一次。\n",
    "start-incomplete.txt":
        "這個資料夾不完整:找不到 runtime\\python.exe。\n"
        "請向提供者重新索取完整的資料夾。\n",
    "start-webview2.txt":
        "這台電腦缺 Microsoft Edge WebView2 Runtime,應用視窗開不起來。\n"
        "\n"
        "  請先執行:tools\\安裝WebView2.bat\n"
        "  (可用一般使用者權限安裝,不需要系統管理員。裝完再跑一次 start.bat。)\n"
        "\n"
        "  沒有網路的話,安裝檔必須事先放在 prereq\\ 底下;若 prereq\\ 是空的,請向提供者\n"
        f"  索取「Evergreen Standalone Installer」({WEBVIEW2_INSTALLER_NAME},\n"
        "  約 130 MB),放進 prereq\\ 底下(檔名不必改),再執行一次上面那個檔案。\n"
        "  2 MB 的 MicrosoftEdgeWebview2Setup.exe 是「需要連網」的 bootstrapper,\n"
        "  它執行時才去微軟網站下載,離線機器放進 prereq\\ 也裝不起來。\n",
    "start-failed.txt":
        "啟動失敗。詳細記錄在 data\\logs\\ 資料夾裡:\n"
        "    launcher-*.log    啟動流程\n"
        "    streamlit-*.log   應用本身的輸出\n",
    "webview2-none.txt":
        "這個交付包裡沒有附安裝檔(prereq\\ 是空的或不存在)。請用以下任一方式:\n"
        "\n"
        "  1. 這台電腦有網路 → 開啟下面的網址下載安裝檔並執行它:\n"
        f"     {WEBVIEW2_DOWNLOAD}\n"
        "  2. 這台電腦沒有網路 → 請在「另一台有網路的電腦」上開啟同一個網址,下載\n"
        f"     「Evergreen Standalone Installer」({WEBVIEW2_INSTALLER_NAME},\n"
        "     約 130 MB,檔案本身就含整個 WebView2),複製到這個資料夾的 prereq\\ 底下\n"
        "     (檔名不必改),再執行一次本檔案。\n"
        "\n"
        "請「不要」拿 2 MB 的 MicrosoftEdgeWebview2Setup.exe:那是需要連網的\n"
        "bootstrapper,它本身不含 WebView2,執行時才去微軟網站下載,\n"
        "放進 prereq\\ 也一樣裝不起來。\n"
        "\n"
        "WebView2 可以用一般使用者權限安裝,不需要系統管理員。\n",
    "webview2-installing.txt": "正在安裝 WebView2 Runtime(不需系統管理員權限)…\n",
    "webview2-done.txt": "安裝完成。請再執行一次 start.bat。\n",
    "webview2-failed.txt": "安裝失敗。請把上面的訊息回報給提供者。\n",
    # Printed ONLY when the install failed AND the file in prereq\ is under 10 MB.
    # That is not a coincidence to be escalated to the supplier: it is the ~2 MB
    # bootstrapper, on a machine that cannot reach the internet it wants to use.
    "webview2-bootstrapper.txt":
        "prereq\\ 裡的這支安裝檔小於 10 MB。\n"
        "你手上這支是「需要連網」的 Evergreen Bootstrapper(約 2 MB):它本身不含\n"
        "WebView2,執行時才去微軟網站下載,所以離線機器裝不起來,放進 prereq\\ 也沒用。\n"
        "\n"
        "離線機器要用的是「Evergreen Standalone Installer」\n"
        f"({WEBVIEW2_INSTALLER_NAME},約 130 MB,檔案本身就含整個 runtime):\n"
        f"  {WEBVIEW2_DOWNLOAD}\n"
        "在有網路的電腦下載好,複製到這個資料夾的 prereq\\ 底下(檔名不必改),\n"
        "再執行一次本檔案。\n",
}


def _write_messages(staging: Path) -> None:
    """The Chinese that start.bat and the WebView2 helper print, as DATA. cmd `type`s
    these files; it never parses them, so no seek bug can reach them."""
    messages = staging / "messages"
    messages.mkdir(exist_ok=True)
    for name, body in MESSAGES.items():
        body.encode("cp950")                # a zh-TW console must be able to render it
        (messages / name).write_bytes(body.replace("\n", "\r\n").encode("utf-8"))
