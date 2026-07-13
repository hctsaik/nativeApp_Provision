"""Staging the portable Python runtime and installing the app's dependencies.

The build machine may reach PyPI; the delivered folder must never need to. So
everything the app imports is installed *here*, into the copied runtime, and the
result is verified by actually importing Streamlit with the staged interpreter.

We copy a relocatable python-build-standalone tree (fetched by nativeApp's
scripts/win/fetch-standalone-python.ps1). We never package the admin's own venv:
it carries absolute paths and would break the moment the folder is moved.
"""

from __future__ import annotations

import os
import queue
import shutil
import subprocess
import threading
from collections.abc import Callable
from pathlib import Path

ShouldCancel = Callable[[], bool]
Progress = Callable[[str], None]

# How often we look up from pip's output to ask "has the operator given up?".
CANCEL_POLL_SECONDS = 0.2

# The runtime copy is 500 MB of small files with Defender inspecting every one of
# them. shutil.copytree has no cancellation hook and no progress hook, so 取消
# pressed here did nothing at all until the whole tree had been copied — tens of
# seconds of a greyed-out button under 「正在取消…」. We copy it ourselves: the
# cancel flag is read between files (and between chunks of a big one), and the
# operator gets a percentage instead of a frozen window.
MB = 1024 ** 2
COPY_CHUNK = 4 * MB                 # bytes copied between two cancel checks
PROGRESS_STEP = 50 * MB             # how often to say where we are

# The template ships stdlib .pyc files. The __pycache__ directories are skipped
# wholesale; the loose .pyc/.pyo beside their sources are not, and any left behind
# gets declared in files.json and then dropped on export — which is exactly the
# mismatch that made every exported runtime fail verification. (strip_bytecode()
# runs afterwards as the belt to this pair of braces.)
COPY_SKIP_DIRS = ("__pycache__",)
COPY_SKIP_SUFFIXES = (".pyc", ".pyo")


class RuntimeError_(Exception):
    """Runtime staging failed in a way the admin can fix."""


class BuildCancelled(Exception):
    """The operator pressed 取消. Not a failure — nothing is broken, and the
    caller is expected to clean the staging directory and say so plainly."""


def _plan_copy(template: Path) -> tuple[list, int]:
    """Every file that will travel, and how many bytes that is — a stat-only walk,
    so the percentage we show is measured rather than guessed."""
    entries: list = []
    total = 0
    for dirpath, dirnames, filenames in os.walk(template):
        dirnames[:] = [d for d in dirnames if d not in COPY_SKIP_DIRS]
        here = Path(dirpath)
        for name in filenames:
            if name.endswith(COPY_SKIP_SUFFIXES):
                continue
            source = here / name
            try:
                size = source.stat().st_size
            except OSError:
                continue
            entries.append((source, source.relative_to(template), size))
            total += size
    return entries, total


def _copy_file(source: Path, target: Path, should_cancel: ShouldCancel | None) -> None:
    """copy2, but interruptible. A single 200 MB .pyd inside a 取消 is still a
    ten-second wall if we hand it to shutil and look away."""
    with open(source, "rb") as reader, open(target, "wb") as writer:
        while True:
            if should_cancel is not None and should_cancel():
                raise BuildCancelled("複製 runtime 已被使用者中止")
            chunk = reader.read(COPY_CHUNK)
            if not chunk:
                break
            writer.write(chunk)
    shutil.copystat(source, target)


def copy_runtime(template: Path, dest: Path, *,
                 should_cancel: ShouldCancel | None = None,
                 progress: Progress | None = None) -> Path:
    """Stage the portable interpreter, cancellably.

    This is the longest single step of a build and it used to be the only one with
    no feedback and no way out: `shutil.copytree` cannot be cancelled, so 取消
    pressed during the runtime copy left the GUI sitting on 「正在取消…」 until the
    last of 500 MB had been written — and only then noticed the flag.
    """
    template, dest = Path(template), Path(dest)
    python = template / "python.exe"
    if not python.is_file():
        raise RuntimeError_(f"runtime 範本沒有 python.exe:{template}")

    entries, total = _plan_copy(template)
    dest.mkdir(parents=True, exist_ok=True)
    copied = 0
    next_tick = PROGRESS_STEP
    # Below a megabyte the copy is instant and a progress line is just noise (and
    # 「共 0 MB」 at that). The step that needs narrating is the 500 MB one.
    talk = progress is not None and total >= MB
    if talk:
        progress(f"    runtime 共 {total / MB:.0f} MB,開始複製…")

    for source, relative, size in entries:
        if should_cancel is not None and should_cancel():
            raise BuildCancelled("複製 runtime 已被使用者中止")
        target = dest / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        _copy_file(source, target, should_cancel)
        copied += size
        if talk and copied >= next_tick:
            progress(f"    複製 runtime… {copied * 100 / total:.0f}% "
                     f"({copied / MB:.0f}/{total / MB:.0f} MB)")
            next_tick = copied + PROGRESS_STEP

    # Empty directories travel too: copytree created them, and a runtime that is
    # missing one it expects (Lib\site-packages on a bare template) is a runtime
    # that fails on the target machine, not here.
    for dirpath, dirnames, _files in os.walk(template):
        dirnames[:] = [d for d in dirnames if d not in COPY_SKIP_DIRS]
        (dest / Path(dirpath).relative_to(template)).mkdir(parents=True, exist_ok=True)

    strip_bytecode(dest)
    staged = dest / "python.exe"
    if not staged.is_file():
        raise RuntimeError_(f"複製 runtime 後找不到 python.exe:{staged}")
    return staged


def strip_bytecode(runtime_dir: Path) -> int:
    """No .pyc anywhere in a shared runtime: it runs with PYTHONDONTWRITEBYTECODE,
    so they are dead weight, and their presence breaks integrity on export."""
    removed = 0
    for path in Path(runtime_dir).rglob("*.py[co]"):
        try:
            path.unlink()
            removed += 1
        except OSError:
            pass
    for cache in Path(runtime_dir).rglob("__pycache__"):
        shutil.rmtree(cache, ignore_errors=True)
    return removed


def install_requirements(python: Path, requirements: Path, log_file: Path,
                         *, offline_wheels: Path | None = None,
                         compile_bytecode: bool = False, progress=None,
                         should_cancel: ShouldCancel | None = None) -> None:
    """Install into the STAGED runtime, not the build machine's Python.

    `--no-compile` by default: the app always runs with PYTHONDONTWRITEBYTECODE
    (the shared runtime is immutable), so .pyc files are dead weight — 4,000 of
    them in CV_Viewer's runtime, 7,221 in AI4BI's. They also broke every export:
    files.json declared them while the exporter's ignore-pattern dropped them,
    so the target machine's integrity check failed on a package that was fine.
    """
    cmd = [str(python), "-m", "pip", "install", "--no-warn-script-location",
           "--progress-bar", "off", "-r", str(requirements)]
    if not compile_bytecode:
        cmd.append("--no-compile")
    if offline_wheels is not None:
        cmd += ["--no-index", f"--find-links={offline_wheels}"]
    _run(cmd, log_file, what="pip install", progress=progress, should_cancel=should_cancel)


def verify_imports(python: Path, log_file: Path) -> None:
    """Proof, not assumption: the staged interpreter really can import Streamlit."""
    _run([str(python), "-c", "import streamlit; print(streamlit.__version__)"],
         log_file, what="import streamlit")


def kill_tree(proc) -> None:
    """Kill the child AND everything it spawned.

    `proc.terminate()` on Windows kills pip and leaves its download/build
    subprocesses running: they keep writing into the staging directory we are
    about to delete, which is how a "cancelled" build ends up holding file
    handles on a folder that no longer exists. taskkill /T is the only thing
    that takes the whole tree down (same call as gui_backend.BuildProcess.cancel).
    """
    if proc.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                       check=False)
    else:                                   # pragma: no cover - 產品目標是 Windows
        proc.terminate()
    try:
        proc.wait(timeout=15)
    except Exception:                       # noqa: BLE001 - already dying; never block 取消
        pass


def _run(cmd: list[str], log_file: Path, *, what: str, progress=None,
         should_cancel: ShouldCancel | None = None) -> None:
    """Stream the child's output: a 5–10 minute pip install with no output at all
    is indistinguishable from a hang, and that is what the operator sees.

    The stream is pumped by a thread so that 取消 does not have to wait for pip's
    next line of output — a `pip install torch` can be silent for minutes while
    it downloads, and a cancel button that only responds between lines is the
    same fake button we already had.
    """
    log_file.parent.mkdir(parents=True, exist_ok=True)
    # UTF-8 mode: pip decodes requirements files with locale.getpreferredencoding()
    # unless they carry a BOM — on a zh-TW box that is cp950, so a requirements.txt
    # with Chinese comments (very common here) dies with UnicodeDecodeError before
    # a single package is downloaded. UTF-8 mode makes that call return utf-8.
    env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    with log_file.open("a", encoding="utf-8") as handle:
        handle.write(f"\n$ {' '.join(cmd)}\n")
        handle.flush()
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                env=env, text=True, encoding="utf-8", errors="replace",
                                bufsize=1)

        lines: queue.Queue = queue.Queue()

        def pump() -> None:
            try:
                for line in proc.stdout:
                    lines.put(line)
            finally:
                lines.put(None)             # sentinel: the child closed its stdout

        reader = threading.Thread(target=pump, daemon=True)
        reader.start()

        while True:
            if should_cancel is not None and should_cancel():
                kill_tree(proc)
                handle.write(f"\n[取消] {what} 已被使用者中止\n")
                raise BuildCancelled(f"{what} 已被使用者中止")
            try:
                line = lines.get(timeout=CANCEL_POLL_SECONDS)
            except queue.Empty:
                continue
            if line is None:
                break
            handle.write(line)
            handle.flush()
            if progress is not None and line.strip():
                progress("    " + line.rstrip()[:110])
        code = proc.wait()
    if code != 0:
        tail = _tail(log_file)
        raise RuntimeError_(f"{what} 失敗(exit {code})。log:{log_file}\n{tail}")


def _tail(path: Path, lines: int = 15) -> str:
    try:
        content = path.read_text("utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    return "\n".join(content[-lines:])
