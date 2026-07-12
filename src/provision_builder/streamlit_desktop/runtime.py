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

# How often we look up from pip's output to ask "has the operator given up?".
CANCEL_POLL_SECONDS = 0.2


class RuntimeError_(Exception):
    """Runtime staging failed in a way the admin can fix."""


class BuildCancelled(Exception):
    """The operator pressed 取消. Not a failure — nothing is broken, and the
    caller is expected to clean the staging directory and say so plainly."""


def copy_runtime(template: Path, dest: Path) -> Path:
    python = template / "python.exe"
    if not python.is_file():
        raise RuntimeError_(f"runtime 範本沒有 python.exe:{template}")
    # The template itself ships stdlib .pyc files; ignore_patterns skips the
    # __pycache__ directories but not the loose ones, and any .pyc left behind
    # gets declared in files.json and then dropped on export — which is exactly
    # the mismatch that made every exported runtime fail verification.
    shutil.copytree(template, dest, dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"))
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
