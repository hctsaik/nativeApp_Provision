"""Portable launcher — the only process that owns the Streamlit process tree.

Ships inside every delivered package and runs on the package's own portable
Python, so it must stay stdlib-only.

Responsibilities (see docs/SIMPLE_STREAMLIT_TAURI_FOLDER_BUILDER_PHASE0_AND_DESIGN.md §4):
  1. read + validate app-package.json (all paths relative, must stay inside the package)
  2. pick a Streamlit port (preferred first, then OS-assigned; bind races retried)
  3. spawn Streamlit on 127.0.0.1 and wait for its health endpoint
  4. serve a token-protected loopback control channel for engine_shim.py
  5. spawn the prebuilt Tauri shell, wait for it, then tear down what WE spawned

The shell cannot open an arbitrary URL, so the URL reaches it through the engine
contract: the shell spawns engine_shim.py, which asks us (over the control
channel) where Streamlit currently is.

EXIT CODES — bootstrap.py reads these to decide whether the *version* is bad or
the *machine* is. Getting this wrong is expensive: a shared-shell failure blamed
on the version marks a perfectly good release dead and "rolls back" to a version
that fails identically.

  0  ok
  3  the app itself is broken (missing module, syntax error, script raised
     before it ever rendered)
     -> this VERSION is bad: mark failed, roll back
  4  the version tree is broken (bad/missing manifest, path escapes package)
     -> this VERSION is bad: mark failed, roll back
  5  the machine is broken (no WebView2, antivirus ate the shell, no window)
     -> the shell is SHARED; every version fails the same way. Touch no state,
        claim no rollback, tell the operator what to install.

THE HEALTHY MARKER (CIM_HEALTHY_MARKER), and what bootstrap may conclude
=======================================================================
The marker is our one bit of good news, so it must mean exactly one thing:

    the marker exists  <=>  this version opened a window
                            AND the app did not fail on arrival

We write it once the window has survived its creation phase (or exited 0 inside
it — see run_shell), and we DELETE it again if the log later proves the app was
never usable. bootstrap commits candidate -> last-known-good on a CLEAN EXIT with
the marker still present; it must not commit the instant the marker appears,
because the app script does not even run until the user presses Start.

  exit  marker   what happened                          bootstrap does
  ----  -------  -------------------------------------  --------------------------
   0    present  window opened, user closed it, app OK   commit candidate -> LKG
   0    present  app rendered, threw LATER (red box,     commit; we printed a
                 user carried on) -> [WARN], not a       warning to console + log
                 failed version
   3    absent   app never became usable (missing        version failed: roll back
                 module / syntax error / raised on
                 arrival / Streamlit never healthy);
                 the marker is REVOKED if we had
                 already written it
   4    absent   this version's tree/manifest is wrong   version failed: roll back
   5    absent   the window never opened at all          machine: touch no state
   5    present  window was up, shell died non-zero      machine (SHARED shell):
                 later                                   touch no state, no commit

  (--no-shell is a developer flag: no window is involved and bootstrap never
   passes it. It writes the marker on a healthy Streamlit and nothing else.)
"""

from __future__ import annotations

import argparse
import ast
import importlib.util
import json
import logging
import os
import random
import secrets
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

EXIT_OK = 0
EXIT_APP_BROKEN = 3
EXIT_VERSION_BROKEN = 4
EXIT_MACHINE_BROKEN = 5

PKG_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_NAME = "app-package.json"

# The shell times out every engine call after 30s (bridge.rs), and a portal
# "start" is answered only once Streamlit is healthy — so a restart must fit
# well inside that budget.
RESTART_BUDGET_SECONDS = 25.0
BIND_RACE_RETRIES = 5

log = logging.getLogger("launcher")


# ── manifest ─────────────────────────────────────────────────────────────────

class ManifestError(Exception):
    pass


def resolve_inside(root: Path, relative: str, *, what: str) -> Path:
    """Resolve a manifest path and prove it did not escape the package root."""
    if os.path.isabs(relative):
        raise ManifestError(f"{what} must be relative, got absolute: {relative}")
    resolved = (root / relative).resolve()
    root_resolved = root.resolve()
    if resolved != root_resolved and root_resolved not in resolved.parents:
        raise ManifestError(f"{what} escapes the package root: {relative}")
    return resolved


def load_manifest(pkg_root: Path) -> dict:
    path = pkg_root / MANIFEST_NAME
    if not path.is_file():
        raise ManifestError(f"missing {MANIFEST_NAME}: {path}")
    try:
        data = json.loads(path.read_text("utf-8"))
    except ValueError as exc:
        raise ManifestError(f"{MANIFEST_NAME} is not valid JSON: {exc}") from exc

    for key in ("app_id", "display_name", "entrypoint", "engine_shim"):
        if not data.get(key):
            raise ManifestError(f"{MANIFEST_NAME} missing required key: {key}")

    data["_entrypoint"] = resolve_inside(pkg_root, data["entrypoint"], what="entrypoint")
    data["_shim"] = resolve_inside(pkg_root, data["engine_shim"], what="engine_shim")

    # Store layout: the shell is SHARED (deps/shells/<fp>/), so it necessarily
    # lives outside this version directory. bootstrap resolves and integrity-checks
    # it, then hands us the path — we still refuse to run if it is not there.
    shared_shell = os.environ.get("CIM_SHELL_EXE")
    if shared_shell:
        data["_shell"] = Path(shared_shell)
    elif data.get("shell_executable"):
        data["_shell"] = resolve_inside(pkg_root, data["shell_executable"],
                                        what="shell_executable")
    else:
        raise ManifestError(
            f"{MANIFEST_NAME} has no shell_executable and CIM_SHELL_EXE is not set "
            "(a store-layout package must be started through bootstrap.py)")
    # The project root, not the entry script's folder. Streamlit projects are
    # run from their root (`streamlit run ai4bi/ui/app.py`), and that is what
    # puts the root on sys.path — without it, a package-layout app dies on
    # `import ai4bi` before rendering a thing.
    data["_app_root"] = resolve_inside(
        pkg_root, data["entrypoint"].split("/", 1)[0], what="application root")
    # Schema 1 (fat package): the interpreter ships inside the package.
    # Schema 2 (store layout): bootstrap already runs us under the version's
    # shared runtime, so the interpreter is simply sys.executable.
    if data.get("runtime_fingerprint"):
        data["_python"] = Path(sys.executable)
    elif data.get("python"):
        data["_python"] = resolve_inside(pkg_root, data["python"], what="python")
    else:
        raise ManifestError(f"{MANIFEST_NAME} needs either python or runtime_fingerprint")
    for key in ("_entrypoint", "_python", "_shell", "_shim"):
        if not data[key].is_file():
            raise ManifestError(f"file declared in {MANIFEST_NAME} does not exist: {data[key]}")
    return data


# ── ports ────────────────────────────────────────────────────────────────────

def is_port_free(port: int, host: str = "127.0.0.1") -> bool:
    """True if we can bind it right now. No SO_REUSEADDR: on Windows that would
    let us 'succeed' on a port someone else already holds."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


PORT_RANGE = (8000, 9000)
_PORT_TRIES = 20


def pick_port(preferred: int = 0, host: str = "127.0.0.1") -> int:
    """A free port, tested before we hand it out.

    Default is a RANDOM port in 8000–9000, not 8501: a fixed default collides
    with every other Streamlit on the machine (and with the stray one that was
    squatting on 8501 during development). `preferred` is honoured only when it
    is explicitly set AND actually free. Last resort: let the OS assign one.
    """
    if preferred and is_port_free(preferred, host):
        return preferred

    low, high = PORT_RANGE
    for _ in range(_PORT_TRIES):
        candidate = random.randint(low, high)
        if is_port_free(candidate, host):
            return candidate

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))          # the range is full — take anything free
        return sock.getsockname()[1]


def wait_port_released(port: int, host: str = "127.0.0.1", timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if is_port_free(port, host):
            return True
        time.sleep(0.1)
    return is_port_free(port, host)


# ── Streamlit supervision ────────────────────────────────────────────────────

def streamlit_command(python: Path, entrypoint: Path, port: int, host: str) -> list[str]:
    return [
        str(python), "-m", "streamlit", "run", str(entrypoint),
        f"--server.address={host}",
        f"--server.port={port}",
        "--server.headless=true",
        "--browser.gatherUsageStats=false",
    ]


def http_ok(url: str, timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return 200 <= resp.status < 300
    except (urllib.error.URLError, OSError, ValueError):
        return False


# ── preflight: does the app's import closure actually resolve? ───────────────
#
# This runs IN-PROCESS, because launch.py is already executed by the exact
# interpreter Streamlit will use (start.bat -> runtime\python.exe, or bootstrap
# -> the shared runtime's python.exe). So `find_spec` here answers the same
# question the app will ask a second later — for free, before anything spawns.
#
# Why we need it at all: `/_stcore/health` is answered by the Streamlit *server*.
# An app that dies on `import cv2` still gets a cheerful 200. And GET / only
# returns the static index.html — Streamlit does not run the script until a
# browser opens a websocket session, which does not happen until the user
# presses Start in the portal. So there is NO cheap HTTP probe that proves the
# app works, and the previous code's `first_render_error()` (GET / then read the
# log) was checking a page that never executed a line of the app.
#
# A missing module is the failure this catches, and it is the one that actually
# happens: it is what "the admin forgot to add opencv to requirements" looks
# like. What it does NOT catch is a script that imports fine and then raises —
# for that, see `app_error_in_log()`, which is checked when the shell exits.
#
# The closure is seeded with the entrypoint AND with the app's pages (below):
# Streamlit runs pages/*.py itself, without anyone importing them.

_STDLIB = set(getattr(sys, "stdlib_module_names", ()))
# import name -> the module the app actually needs to find. Only names that
# differ from their distribution name matter here; find_spec works on the
# import name, so this table exists purely to make the error message useful.
_DIST_HINT = {
    "cv2": "opencv-python", "PIL": "pillow", "yaml": "PyYAML",
    "sklearn": "scikit-learn", "skimage": "scikit-image", "fitz": "PyMuPDF",
    "dateutil": "python-dateutil", "bs4": "beautifulsoup4", "dotenv": "python-dotenv",
    "serial": "pyserial", "OpenSSL": "pyOpenSSL", "win32com": "pywin32",
}


def _module_level_imports(tree: ast.Module) -> set[str]:
    """Only imports that run when the module is imported.

    An `import anthropic` inside a function body is lazy by construction: the
    app starts fine without it. Treating those as required is how a build gets
    hard-failed over an optional LLM backend nobody enabled. Same for anything
    already wrapped in try/except ImportError — the author wrote the fallback.
    """
    required: set[str] = set()

    def walk(nodes, *, guarded: bool) -> None:
        for node in nodes:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue                                  # lazy: not needed to start
            if isinstance(node, ast.Try):
                handles_import = any(
                    _handler_catches_import(h) for h in node.handlers)
                walk(node.body, guarded=guarded or handles_import)
                for handler in node.handlers:
                    walk(handler.body, guarded=True)
                walk(node.orelse, guarded=guarded or handles_import)
                walk(node.finalbody, guarded=guarded)
                continue
            if isinstance(node, ast.Import):
                if not guarded:
                    required.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                if not guarded and node.level == 0 and node.module:
                    required.add(node.module.split(".")[0])
            for field in ("body", "orelse", "finalbody"):
                child = getattr(node, field, None)
                if isinstance(child, list):
                    walk(child, guarded=guarded)

    walk(tree.body, guarded=False)
    return required


def _handler_catches_import(handler: ast.ExceptHandler) -> bool:
    names = []
    if isinstance(handler.type, ast.Name):
        names = [handler.type.id]
    elif isinstance(handler.type, ast.Tuple):
        names = [e.id for e in handler.type.elts if isinstance(e, ast.Name)]
    return any(n in ("ImportError", "ModuleNotFoundError", "Exception") for n in names)


def _import_roots(entrypoint: Path, app_root: Path) -> list[Path]:
    """Every directory the app can import from — which is NOT just `application/`.

    `streamlit run x/y/app.py` puts **the script's own directory** on sys.path, and
    we additionally put the project root there (cwd + PYTHONPATH). CV_Viewer's
    entrypoint is application/5_PG_Develop/app.py with 23 sibling modules next to
    it; looking only in application/ declared every one of them a missing PyPI
    package and refused to start a package that runs perfectly well.
    """
    roots = [Path(entrypoint).parent, Path(app_root)]
    seen, ordered = set(), []
    for root in roots:
        resolved = root.resolve()
        if resolved not in seen:
            seen.add(resolved)
            ordered.append(root)
    return ordered


def _local_module_path(roots: list[Path], name: str) -> Path | None:
    for root in roots:
        for candidate in (root / f"{name}.py", root / name / "__init__.py"):
            if candidate.is_file():
                return candidate
    return None


def _pages_dir(entrypoint: Path) -> Path:
    """Streamlit's multipage folder: `pages/` NEXT TO THE ENTRY SCRIPT."""
    return Path(entrypoint).parent / "pages"


def _page_scripts(entrypoint: Path, app_root: Path) -> list[Path]:
    """The app's pages — files Streamlit executes that NOTHING imports.

    A multipage app's pages are discovered and run by Streamlit itself
    (script_runner._mpa_v1: every `*.py` directly inside a `pages/` folder next
    to the entry script, minus dotfiles and __init__.py — that rule is copied
    from there, not guessed). The entrypoint never imports them, so an import
    closure seeded with the entrypoint alone is blind to the whole folder: a
    missing dependency in pages/2_report.py sails through the gate and reaches
    the user as a red box the first time they click that page.

    Also followed, because the path is a literal sitting in the AST:
      * st.Page("pages/2_report.py") — the st.navigation API.
      * .streamlit/pages.toml — the third-party `st-pages` convention
        ([[pages]] path = "..."), read with tomllib when it is available.

    NOT followed, and we do not pretend otherwise: a page list built at RUNTIME
    (a loop over a directory, names from a database, st.Page(some_variable)).
    Its paths do not exist until the app runs, so no static gate can see them;
    such a page's missing dependency will surface as a red box, and the log
    scan at shell exit is what has to catch it.
    """
    pages: list[Path] = []
    pages_dir = _pages_dir(entrypoint)
    if pages_dir.is_dir():
        pages += sorted(p for p in pages_dir.glob("*.py")
                        if p.is_file() and not p.name.startswith(".")
                        and p.name != "__init__.py")
    pages += _declared_pages(entrypoint, app_root)
    return pages


def _declared_pages(entrypoint: Path, app_root: Path) -> list[Path]:
    """Page scripts named by a literal string: st.Page(...) and st-pages' toml."""
    bases = [Path(entrypoint).parent, Path(app_root)]
    found: list[Path] = []

    def add(raw: str) -> None:
        if not isinstance(raw, str) or not raw.endswith(".py") or os.path.isabs(raw):
            return
        for base in bases:
            candidate = base / raw
            if candidate.is_file():
                found.append(candidate)
                return

    try:
        tree = ast.parse(Path(entrypoint).read_text("utf-8", errors="replace"))
    except (OSError, SyntaxError):
        tree = None                       # preflight reports the syntax error itself
    if tree is not None:
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            func = node.func
            name = (func.attr if isinstance(func, ast.Attribute)
                    else func.id if isinstance(func, ast.Name) else "")
            if name not in ("Page", "StreamlitPage"):
                continue
            first = node.args[0]
            if isinstance(first, ast.Constant):
                add(first.value)

    toml_path = Path(app_root) / ".streamlit" / "pages.toml"
    if toml_path.is_file():
        try:
            import tomllib                      # stdlib on the shipped cp311 runtime
        except ImportError:                     # pragma: no cover - older runtime
            return found
        try:
            data = tomllib.loads(toml_path.read_text("utf-8", errors="replace"))
        except (OSError, ValueError):
            return found
        for entry in data.get("pages") or []:
            if isinstance(entry, dict):
                add(entry.get("path"))
    return found


def preflight(entrypoint: Path, app_root: Path) -> tuple[list[str], str | None]:
    """(missing third-party modules, syntax error) — reachable from the entrypoint.

    Follows first-party imports transitively, so a module the app never touches
    cannot fail the check (CV_Viewer ships a `verify/` folder that imports
    playwright; the app does not, and must not be blamed for it).

    The queue starts at the entrypoint *and* at every page Streamlit will run on
    its own (see _page_scripts) — a page is reachable for the user even though it
    is unreachable for an import walk.
    """
    roots = _import_roots(entrypoint, app_root)
    pages_dir = _pages_dir(entrypoint)
    if pages_dir.is_dir():
        # A .py next to a page IS a page, not a PyPI package. Treat the folder as
        # first-party so a sibling import is followed instead of being reported
        # as "please pip install 2_report" — the misdiagnosis that made a working
        # CV_Viewer refuse to start.
        roots = roots + [pages_dir]
    missing: list[str] = []
    seen_files: set[Path] = set()
    queue = [Path(entrypoint), *_page_scripts(entrypoint, app_root)]
    while queue:
        source = queue.pop()
        source = source.resolve()
        if source in seen_files or not source.is_file():
            continue
        seen_files.add(source)
        try:
            tree = ast.parse(source.read_text("utf-8", errors="replace"), filename=str(source))
        except SyntaxError as exc:
            return missing, f"{source.name} 第 {exc.lineno} 行語法錯誤:{exc.msg}"
        except OSError:
            continue
        for name in sorted(_module_level_imports(tree)):
            if name in _STDLIB or name in ("streamlit",):
                continue
            local = _local_module_path(roots, name)
            if local is not None:
                queue.append(local)
                continue
            if name == source.stem:
                continue
            try:
                found = importlib.util.find_spec(name) is not None
            except (ImportError, ValueError):
                found = False
            if not found and name not in missing:
                missing.append(name)
    return missing, None


def missing_modules_message(missing: list[str], app_root: Path) -> str:
    lines = ["這個版本的 App 需要的套件,這台機器的 runtime 裡沒有:"]
    for name in missing:
        hint = _DIST_HINT.get(name)
        lines.append(f"  - {name}" + (f"(要裝的套件叫 {hint})" if hint else ""))
    lines += [
        "",
        "這不是這台電腦的問題,是這個版本打包時漏掉了相依套件。",
        "請回到打包工具,把上面的套件加進 requirements(或 lock 檔)後重新建置這個版本。",
        f"  App 目錄:{app_root}",
    ]
    return "\n".join(lines)


class StreamlitExited(Exception):
    """Streamlit died before it became healthy — never open an empty shell."""


# How long after Streamlit answers /_stcore/health an error still counts as "the
# app failed on arrival" rather than "the app broke while it was being used".
# The portal iframes the app the moment our /control/start answers, so the first
# script run happens a second or two later; 20s is that, with room for a slow
# machine. Generous on purpose: the cost of calling a late error early is a
# version wrongly marked dead.
APP_ARRIVAL_SECONDS = 20.0


class StreamlitSupervisor:
    """Owns exactly one Streamlit process tree: the one it spawned."""

    def __init__(self, manifest: dict, log_dir: Path, *, popen_factory=subprocess.Popen):
        self.manifest = manifest
        self.app_root = manifest.get("_app_root") or manifest["_entrypoint"].parent
        self.log_dir = log_dir
        self.host = manifest.get("host", "127.0.0.1")
        # 0 = "no preference": pick a random free port in 8000–9000. A packaged
        # app has no reason to want a specific port, and wanting 8501 is how you
        # collide with every other Streamlit on the machine.
        self.preferred_port = int(manifest.get("preferred_port", 0) or 0)
        self.health_path = manifest.get("health_path", "/_stcore/health")
        self.timeout = float(manifest.get("startup_timeout_seconds", 60))
        self._popen_factory = popen_factory
        self._lock = threading.Lock()
        self._proc = None
        self._port = None
        self._log_file = None
        self._log_path = None
        # When Streamlit became healthy (monotonic), and how many bytes of its
        # log had been written by the time the app stopped "arriving". See
        # note_arrival_window().
        self._healthy_at = None
        self._arrival_offset = None

    # -- state ---------------------------------------------------------------

    @property
    def url(self) -> str | None:
        return f"http://{self.host}:{self._port}" if self.running else None

    @property
    def port(self) -> int | None:
        return self._port if self.running else None

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def status(self) -> dict:
        return {"running": self.running, "url": self.url, "port": self.port}

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> str:
        """Spawn Streamlit and return its URL once health-checked. Idempotent:
        if it is already up, hand back the URL we already have."""
        with self._lock:
            if self.running:
                return self.url
            last_error = None
            for attempt in range(1, BIND_RACE_RETRIES + 1):
                port = pick_port(self.preferred_port, self.host)
                try:
                    return self._spawn_and_wait(port)
                except StreamlitExited as exc:
                    # A bind race looks exactly like this: the port was free when
                    # we looked, taken by the time Streamlit bound it.
                    last_error = exc
                    log.warning("start attempt %d/%d on port %d failed: %s",
                                attempt, BIND_RACE_RETRIES, port, exc)
                    self._reap()
            raise StreamlitExited(f"Streamlit did not become healthy after "
                                  f"{BIND_RACE_RETRIES} attempts: {last_error}")

    def _spawn_and_wait(self, port: int) -> str:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        log_path = self.log_dir / f"streamlit-{stamp}-{port}.log"
        self._log_path = log_path
        self._healthy_at = None
        self._arrival_offset = None       # a fresh run, a fresh arrival window
        self._log_file = log_path.open("ab")
        cmd = streamlit_command(self.manifest["_python"], self.manifest["_entrypoint"], port, self.host)
        log.info("spawning Streamlit on port %d -> %s", port, log_path.name)

        env = dict(os.environ, PYTHONUTF8="1", STREAMLIT_BROWSER_GATHER_USAGE_STATS="false")
        # `python -m streamlit` puts the CWD on sys.path, and we run from the
        # project root — but be explicit: an app started from anywhere must still
        # resolve its own package imports.
        existing = env.get("PYTHONPATH")
        env["PYTHONPATH"] = f"{self.app_root}{os.pathsep}{existing}" if existing else str(self.app_root)
        # Store layout: the runtime is SHARED and immutable — point every
        # writable surface (bytecode cache, per-user config, temp) at app data
        # so no app can mutate a runtime other apps depend on (spec §7.2).
        app_data = os.environ.get("CIM_APP_DATA")
        if app_data:
            data = Path(app_data)
            for sub in ("cache/pycache", "home", "tmp"):
                (data / sub).mkdir(parents=True, exist_ok=True)
            env.update(
                PYTHONDONTWRITEBYTECODE="1",
                PYTHONPYCACHEPREFIX=str(data / "cache" / "pycache"),
                HOME=str(data / "home"),
                USERPROFILE=str(data / "home"),
                TMP=str(data / "tmp"),
                TEMP=str(data / "tmp"),
            )
        self._proc = self._popen_factory(
            cmd,
            cwd=str(self.app_root),
            stdout=self._log_file,
            stderr=subprocess.STDOUT,
            env=env,
        )
        self._port = port

        health = f"http://{self.host}:{port}{self.health_path}"
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            if self._proc.poll() is not None:
                raise StreamlitExited(
                    f"Streamlit exited with code {self._proc.returncode} before becoming "
                    f"healthy. See {log_path}"
                )
            if http_ok(health):
                log.info("Streamlit healthy at %s", self.url)
                self._healthy_at = time.monotonic()
                return self.url
            time.sleep(0.25)
        self._terminate_tree()
        raise StreamlitExited(f"Streamlit was not healthy within {self.timeout:.0f}s. See {log_path}")

    # Errors Streamlit prints to its own log when the app script blows up. The
    # health endpoint knows nothing about any of them.
    #
    # CAREFUL — every one of these is also what a *survivable* error looks like.
    # Streamlit logs a traceback for EVERY uncaught exception a script raises
    # (error_util.py -> _log_uncaught_app_exception), draws it as a red box, and
    # carries on: the user re-runs with a sane input and works for another hour.
    # So the presence of a marker means "an exception happened", never "this
    # version is broken". WHEN it happened is what decides that — see below.
    _APP_ERRORS = ("ModuleNotFoundError", "ImportError", "Traceback (most recent call last)",
                   "SyntaxError", "IndentationError")

    @property
    def log_path(self) -> Path | None:
        return self._log_path

    def note_arrival_window(self) -> None:
        """Freeze how much of the log belongs to "the app arriving".

        Called on a tick while the shell is up (run_shell). Streamlit runs the
        script only once a session opens, and the portal iframes the app the
        moment /control/start answers — so the first render happens within a
        second or two of the health check. Once APP_ARRIVAL_SECONDS have passed,
        whatever the app logs from here on happened to an app that had already
        rendered for the user.

        Honest about the limits: Streamlit logs NOTHING on a successful run, so
        we cannot observe a render. "It became usable" is inferred from "it was
        up, and quiet, past the arrival window". That means an app that blows up
        on a page the user opens 30 seconds in is reported as a warning, not a
        failed version — which is the direction we want to be wrong in: a red box
        the user can retry is not worth silently downgrading a machine over.
        """
        if self._arrival_offset is not None:
            return
        healthy_at, log_path = self._healthy_at, self._log_path
        if healthy_at is None or log_path is None:
            return
        if time.monotonic() - healthy_at < APP_ARRIVAL_SECONDS:
            return
        try:
            self._arrival_offset = log_path.stat().st_size
        except OSError:
            return                       # try again on the next tick

    def _split_log(self) -> tuple[str, str] | None:
        """(what the app logged on arrival, what it logged once it was working)."""
        if self._log_path is None:
            return None
        try:
            raw = self._log_path.read_bytes()
        except OSError:
            return None
        offset = self._arrival_offset
        if offset is None:
            # The arrival window never closed — the user shut the app down inside
            # it. Nothing in this log had time to be "an app that already worked".
            offset = len(raw)
        return (raw[:offset].decode("utf-8", errors="replace"),
                raw[offset:].decode("utf-8", errors="replace"))

    @classmethod
    def _error_tail(cls, text: str) -> str | None:
        if not any(marker in text for marker in cls._APP_ERRORS):
            return None
        lines = [ln for ln in text.splitlines() if ln.strip()]
        return "\n".join(lines[-12:])

    def app_error_in_log(self) -> str | None:
        """The app FAILED ON ARRIVAL: the user never got a working app.

        A missing module, a syntax error, a raise at module level — the script
        dies on its first run, the user stares at a red box where the app should
        be. THAT is a broken version: exit 3, revoke the marker, roll back.
        """
        parts = self._split_log()
        return self._error_tail(parts[0]) if parts else None

    def late_app_error_in_log(self) -> str | None:
        """The app worked, and only later did something raise.

        Bad input, a file that vanished, a bug on one screen: the user saw a red
        box, re-ran, and kept working. Reporting the version as failed here is
        how an hour of successful work ends in an unwanted downgrade. Warn, keep
        the healthy marker, exit 0.
        """
        parts = self._split_log()
        return self._error_tail(parts[1]) if parts else None

    def stop(self) -> bool:
        """Terminate our tree and wait until the port is actually released."""
        with self._lock:
            if self._proc is None:
                return True
            port = self._port
            self._terminate_tree()
            self._reap()
            released = wait_port_released(port, self.host) if port else True
            log.info("Streamlit stopped (port %s released=%s)", port, released)
            return released

    def _terminate_tree(self) -> None:
        """Only ever our own PID's tree — never a name scan for python.exe."""
        proc = self._proc
        if proc is None or proc.poll() is not None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=5)
            return
        except subprocess.TimeoutExpired:
            log.warning("Streamlit pid=%s ignored terminate; killing tree", proc.pid)
        except OSError as exc:
            log.warning("terminate failed for pid=%s: %s", proc.pid, exc)
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           capture_output=True, check=False)
        else:  # pragma: no cover - packages are Windows-only
            proc.kill()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            log.error("Streamlit pid=%s survived kill", proc.pid)

    def _reap(self) -> None:
        if self._log_file is not None:
            self._log_file.close()
            self._log_file = None
        self._proc = None
        self._port = None


# ── token-protected control channel (engine_shim -> launcher) ────────────────

class ControlServer:
    """Loopback-only, random-token HTTP channel. engine_shim.py holds no
    process ownership; it asks us to start/stop and reports what we answer."""

    def __init__(self, supervisor: StreamlitSupervisor, host: str = "127.0.0.1"):
        self.supervisor = supervisor
        self.token = secrets.token_urlsafe(32)
        self._httpd = ThreadingHTTPServer((host, 0), self._handler_class())
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._serving = False

    @property
    def url(self) -> str:
        host, port = self._httpd.server_address[:2]
        return f"http://{host}:{port}"

    def start(self) -> None:
        self._thread.start()
        self._serving = True
        log.info("control channel on %s", self.url)

    def shutdown(self) -> None:
        # BaseServer.shutdown() blocks until serve_forever()'s loop signals it —
        # which never happens if we die before start(). Teardown must not hang.
        if self._serving:
            self._httpd.shutdown()
            self._serving = False
        self._httpd.server_close()

    def _handler_class(self):
        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt, *args):  # keep stdout clean
                log.debug("control %s", fmt % args)

            def _reply(self, status: int, payload: dict) -> None:
                body = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _authorized(self) -> bool:
                if secrets.compare_digest(self.headers.get("X-CIM-Token", ""), server.token):
                    return True
                self._reply(403, {"error": "forbidden"})
                return False

            def do_GET(self):  # noqa: N802
                if not self._authorized():
                    return
                if self.path == "/control/status":
                    self._reply(200, server.supervisor.status())
                else:
                    self._reply(404, {"error": "not found"})

            def do_POST(self):  # noqa: N802
                if not self._authorized():
                    return
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    self.rfile.read(length)
                if self.path == "/control/start":
                    try:
                        url = server.supervisor.start()
                    except StreamlitExited as exc:
                        self._reply(503, {"error": str(exc)})
                        return
                    self._reply(200, {"url": url, "port": server.supervisor.port})
                elif self.path == "/control/stop":
                    released = server.supervisor.stop()
                    if released:
                        self._reply(200, {"ok": True})
                    else:
                        self._reply(500, {"error": "port was not released after stop"})
                else:
                    self._reply(404, {"error": "not found"})

        return Handler


# ── shell ────────────────────────────────────────────────────────────────────

def shell_env(manifest: dict, control: ControlServer, data_dir: Path) -> dict:
    """The shell adds only PYTHONUTF8 and never clears the environment
    (sidecar.rs), so everything here reaches engine_shim.py two hops down."""
    return dict(
        os.environ,
        CIM_ENGINE_EXE=str(manifest["_shim"]),
        CIM_ENGINE_PYTHON=str(manifest["_python"]),
        CIM_APP_ID=manifest["app_id"],
        CIM_APP_NAME=manifest["display_name"],
        CIM_APP_VERSION=str(manifest.get("version", "1.0.0")),
        CIM_LAUNCHER_URL=control.url,
        CIM_LAUNCHER_TOKEN=control.token,
        CIM_LOG_DIR=str(data_dir / "logs"),
        PYTHONUTF8="1",
    )


# The window-creation watch. A shell that cannot create a window (no WebView2,
# antivirus ate the .exe) dies within ~1s AND dies non-zero — so watch for a
# fast death instead of blocking every normal start for 12 seconds before we are
# willing to believe the window is there. We poll at 0.1s, so the operator sees
# the "install WebView2" message just as fast as before.
SHELL_ALIVE_SECONDS = 3.0
_WINDOW_POLL_SECONDS = 0.1
# While the shell is up we do nothing but wait for it — tick often enough to
# close the app's arrival window on time, rarely enough to cost nothing.
_SHELL_TICK_SECONDS = 0.5

# Printed whenever the window itself will not come up. Say what to DO — an
# operator on a factory floor cannot act on "the shell exited with code 1".
_MACHINE_HINT = (
    "  這是「這台電腦」的問題,不是這個版本的問題(換版本也一樣開不起來)。\n"
    "  最常見的兩個原因:\n"
    "    1. 缺 Microsoft Edge WebView2 Runtime -> 執行交付資料夾裡的 tools\\安裝WebView2.bat\n"
    "    2. 防毒/SmartScreen 把應用視窗程式隔離了 -> 請 IT 把這個資料夾加進排除清單"
)


def run_shell(manifest: dict, control: ControlServer, data_dir: Path,
              *, on_window_ready=None, on_tick=None) -> int:
    # cwd = data dir so the prebuilt shell (which may predate CIM_LOG_DIR support)
    # resolves its log dir to data\logs anyway.
    try:
        proc = subprocess.Popen([str(manifest["_shell"])], cwd=str(data_dir),
                                env=shell_env(manifest, control, data_dir))
    except OSError as exc:
        log.error("could not start the shell: %s", exc)
        print(f"\n[start][ERROR] 無法啟動應用視窗:{exc}\n"
              f"  執行檔:{manifest['_shell']}\n" + _MACHINE_HINT,
              file=sys.stderr, flush=True)
        return EXIT_MACHINE_BROKEN
    log.info("shell started pid=%s", proc.pid)

    # Watch the window-creation phase, and READ THE RETURN CODE. The two things
    # that can happen here look identical to a `poll() is not None` check and
    # could not be further apart:
    #
    #   non-zero  the shell could not create a window (WebView2 missing, the exe
    #             quarantined). The window never existed -> the MACHINE is broken.
    #   zero      the window opened, and the user closed it. People do glance at
    #             an app and shut it — and being told "your computer is broken"
    #             for it, while bootstrap files an environment failure, is a lie
    #             about a session that worked. The window came up and exited
    #             cleanly: that IS the health signal. Marker, exit 0.
    deadline = time.monotonic() + SHELL_ALIVE_SECONDS
    while time.monotonic() < deadline:
        code = proc.poll()
        if code is None:
            time.sleep(_WINDOW_POLL_SECONDS)
            continue
        if code != 0:
            log.error("shell exited during window creation with code %s", code)
            print("\n[start][ERROR] 應用視窗一開就關閉了。\n" + _MACHINE_HINT +
                  f"\n  詳細記錄:{data_dir / 'logs'}", file=sys.stderr, flush=True)
            # The shell is SHARED by every version of every app in this store.
            # Blaming the version would mark a good release dead and "roll back"
            # to something that fails in exactly the same way.
            return EXIT_MACHINE_BROKEN
        log.info("shell exited cleanly inside the window-creation watch "
                 "(the user closed the window)")
        if on_window_ready is not None:
            on_window_ready()
        return EXIT_OK

    # It survived its creation phase: the window is really there.
    if on_window_ready is not None:
        on_window_ready()

    while True:
        try:
            return proc.wait(timeout=_SHELL_TICK_SECONDS)
        except subprocess.TimeoutExpired:
            if on_tick is not None:
                on_tick()


# ── main ─────────────────────────────────────────────────────────────────────

def _write_marker(url: str) -> None:
    """Tell bootstrap this version actually works (commit candidate → LKG)."""
    marker = os.environ.get("CIM_HEALTHY_MARKER")
    if not marker:
        return
    try:
        Path(marker).parent.mkdir(parents=True, exist_ok=True)
        Path(marker).write_text(url, encoding="utf-8")
    except OSError as exc:
        log.warning("could not write healthy marker %s: %s", marker, exc)


def _revoke_marker() -> None:
    """Take the "this version works" claim back.

    We write the marker once the window has survived its creation phase, because
    that is what proves the machine can host it. But the app script only runs
    when the user presses Start — minutes later. If it turns out it never became
    usable, the version is NOT good, and a marker left behind would promote it to
    last-known-good: the very version rollback would later fall back to.

    Revoked ONLY when the app is proven broken (app_error_in_log), never for an
    error the app survived — see finish_session.
    """
    marker = os.environ.get("CIM_HEALTHY_MARKER")
    if not marker:
        return
    try:
        Path(marker).unlink(missing_ok=True)
    except OSError as exc:
        log.warning("could not revoke healthy marker %s: %s", marker, exc)


def finish_session(supervisor: StreamlitSupervisor, shell_code: int) -> int:
    """(shell exit code x what the app's log says) -> OUR exit code.

    The one place the marker and the exit code are made to tell the same story:

      app failed on arrival  -> revoke the marker, exit 3 (version failed)
      app raised LATER       -> keep the marker, warn loudly, exit 0
      the window never came  -> exit 5 (machine); the marker was never written
      clean close            -> keep the marker, exit 0
    """
    fatal = supervisor.app_error_in_log()
    if fatal:
        _revoke_marker()
        log.error("the app failed on arrival: %s", fatal)
        print("\n[start][ERROR] 這個版本的 App 一啟動就出錯,使用者根本看不到畫面:\n"
              f"{fatal}\n  完整記錄:{supervisor.log_path}", file=sys.stderr, flush=True)
        return EXIT_APP_BROKEN

    late = supervisor.late_app_error_in_log()
    if late:
        # It rendered, it worked, and then something raised: a bad input, a file
        # that vanished, a bug on one screen. The user saw a red box and carried
        # on — an hour of successful work must not end in「這個版本壞了」and an
        # unasked-for downgrade. Tell them, keep the marker, exit 0.
        log.warning("the app raised AFTER it was already working: %s", late)
        print("\n[start][WARN] App 執行中出現錯誤訊息(App 有正常開起來,"
              "使用者當下可能看到紅色錯誤方塊):\n"
              f"{late}\n"
              "  這不算版本失敗,不會退版,也不影響下次啟動。\n"
              f"  若這個錯誤會重複出現,請把這份記錄交給開發者:{supervisor.log_path}",
              flush=True)

    if shell_code == EXIT_MACHINE_BROKEN:
        return EXIT_MACHINE_BROKEN
    # Any other non-zero code from the SHARED shell: not this version's fault.
    return EXIT_OK if shell_code == EXIT_OK else EXIT_MACHINE_BROKEN


def setup_logging(log_dir: Path) -> Path:
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / f"launcher-{datetime.now().strftime('%Y%m%d-%H%M%S')}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(path, encoding="utf-8"), logging.StreamHandler(sys.stdout)],
    )
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Launch the packaged Streamlit app in the Tauri shell")
    parser.add_argument("--no-shell", action="store_true",
                        help="start Streamlit and the control channel, but not the shell (testing)")
    args, _unknown = parser.parse_known_args(argv)

    # Store layout: data lives at the APP root, outside the immutable version
    # dir — our own log handle must not pin a directory GC may later remove.
    data_dir = Path(os.environ.get("CIM_APP_DATA") or PKG_ROOT / "data")
    log_path = setup_logging(data_dir / "logs")
    log.info("package: %s", PKG_ROOT)

    try:
        manifest = load_manifest(PKG_ROOT)
    except ManifestError as exc:
        log.error("bad package: %s", exc)
        print(f"\n[start][ERROR] 這個版本的套件描述檔壞了:{exc}\n  log: {log_path}",
              file=sys.stderr, flush=True)
        return EXIT_VERSION_BROKEN

    # Ask, before spawning anything, the question the health endpoint cannot
    # answer: can this app's imports actually resolve against THIS runtime?
    app_root = PKG_ROOT / "application"
    saved_path = list(sys.path)
    # Mirror exactly what Streamlit will see: the script's own directory first
    # (that is what `streamlit run` does), then the project root.
    for root in reversed(_import_roots(manifest["_entrypoint"], app_root)):
        sys.path.insert(0, str(root))
    try:
        missing, syntax_error = preflight(manifest["_entrypoint"], app_root)
    finally:
        sys.path[:] = saved_path
    if syntax_error:
        log.error("preflight: %s", syntax_error)
        print(f"\n[start][ERROR] 這個版本的 App 有語法錯誤,無法執行:\n  {syntax_error}\n"
              f"  請重新建置這個版本。\n  log: {log_path}", file=sys.stderr, flush=True)
        return EXIT_APP_BROKEN
    if missing:
        log.error("preflight: missing modules %s", missing)
        print("\n[start][ERROR] " + missing_modules_message(missing, app_root) +
              f"\n  log: {log_path}", file=sys.stderr, flush=True)
        return EXIT_APP_BROKEN

    supervisor = StreamlitSupervisor(manifest, data_dir / "logs")
    control = ControlServer(supervisor)
    control.start()

    try:
        try:
            url = supervisor.start()
        except StreamlitExited as exc:
            # Fail loudly instead of opening a shell onto nothing.
            log.error("Streamlit failed to start: %s", exc)
            print(f"\n[start][ERROR] {exc}\n  log: {log_path}", file=sys.stderr, flush=True)
            return EXIT_APP_BROKEN

        # flush: stdout is block-buffered once it is piped to a file or a parent
        # process, and a user tailing the console must see this line now, not
        # whenever the buffer happens to fill.
        print(f"[start] {manifest['display_name']} ready at {url}", flush=True)

        if args.no_shell:
            _write_marker(url)
            print("[start] --no-shell: leaving Streamlit up; Ctrl+C to stop.", flush=True)
            try:
                while True:
                    time.sleep(0.5)
            except KeyboardInterrupt:
                pass
            return EXIT_OK

        # The marker means "this version WORKS", so it must not be written until
        # the window is really up: a missing WebView2 runtime kills the shell in
        # a second, and that is exactly the case rollback exists for. The tick
        # closes the app's arrival window while the user is working (see
        # StreamlitSupervisor.note_arrival_window).
        code = run_shell(manifest, control, data_dir,
                         on_window_ready=lambda: _write_marker(url),
                         on_tick=supervisor.note_arrival_window)
        log.info("shell exited with code %s", code)

        # The user has now actually used the app (or tried to). Streamlit only
        # executes the script once a session opens, so THIS is the first moment
        # its log can tell us the truth.
        return finish_session(supervisor, code)
    finally:
        supervisor.stop()
        control.shutdown()
        log.info("launcher done")


if __name__ == "__main__":
    raise SystemExit(main())
