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
  3  the app itself is broken (missing module, syntax error, script raised)
     -> this VERSION is bad: mark failed, roll back
  4  the version tree is broken (bad/missing manifest, path escapes package)
     -> this VERSION is bad: mark failed, roll back
  5  the machine is broken (no WebView2, antivirus ate the shell, no window)
     -> the shell is SHARED; every version fails the same way. Touch no state,
        claim no rollback, tell the operator what to install.
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


def preflight(entrypoint: Path, app_root: Path) -> tuple[list[str], str | None]:
    """(missing third-party modules, syntax error) — reachable from the entrypoint.

    Follows first-party imports transitively, so a module the app never touches
    cannot fail the check (CV_Viewer ships a `verify/` folder that imports
    playwright; the app does not, and must not be blamed for it).
    """
    roots = _import_roots(entrypoint, app_root)
    missing: list[str] = []
    seen_files: set[Path] = set()
    queue = [Path(entrypoint)]
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
                return self.url
            time.sleep(0.25)
        self._terminate_tree()
        raise StreamlitExited(f"Streamlit was not healthy within {self.timeout:.0f}s. See {log_path}")

    # Errors Streamlit prints to its own log when the app script blows up. The
    # health endpoint knows nothing about any of them.
    _APP_ERRORS = ("ModuleNotFoundError", "ImportError", "Traceback (most recent call last)",
                   "SyntaxError", "IndentationError")

    @property
    def log_path(self) -> Path | None:
        return self._log_path

    def app_error_in_log(self) -> str | None:
        """Did the app script blow up while the user had it open?

        Read once, cheaply, from the log Streamlit writes. This is only
        meaningful AFTER a session existed — Streamlit does not execute the
        script until a browser opens a websocket, which happens when the portal
        iframes the app. So we ask at shell exit, not at boot.
        """
        if self._log_path is None:
            return None
        try:
            text = self._log_path.read_text("utf-8", errors="replace")
        except OSError:
            return None
        for marker in self._APP_ERRORS:
            if marker in text:
                lines = [ln for ln in text.splitlines() if ln.strip()]
                return "\n".join(lines[-12:])
        return None

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


SHELL_ALIVE_SECONDS = 12.0   # a shell that cannot open a window dies within ~1s

# Printed whenever the window itself will not come up. Say what to DO — an
# operator on a factory floor cannot act on "the shell exited with code 1".
_MACHINE_HINT = (
    "  這是「這台電腦」的問題,不是這個版本的問題(換版本也一樣開不起來)。\n"
    "  最常見的兩個原因:\n"
    "    1. 缺 Microsoft Edge WebView2 Runtime -> 執行交付資料夾裡的 tools\\安裝WebView2.bat\n"
    "    2. 防毒/SmartScreen 把應用視窗程式隔離了 -> 請 IT 把這個資料夾加進排除清單"
)


def run_shell(manifest: dict, control: ControlServer, data_dir: Path,
              *, on_window_ready=None) -> int:
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

    if on_window_ready is not None:
        # Survive the window-creation phase before declaring the version good.
        deadline = time.monotonic() + SHELL_ALIVE_SECONDS
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                log.error("shell exited immediately with code %s", proc.returncode)
                print("\n[start][ERROR] 應用視窗一開就關閉了。\n" + _MACHINE_HINT +
                      f"\n  詳細記錄:{data_dir / 'logs'}", file=sys.stderr, flush=True)
                # The shell is SHARED by every version of every app in this
                # store. Blaming the version would mark a good release dead and
                # "roll back" to something that fails in exactly the same way.
                return EXIT_MACHINE_BROKEN
            time.sleep(0.5)
        on_window_ready()

    return proc.wait()


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

    We write the marker once the window has survived its first seconds, because
    that is what proves the machine can host it. But the app script only runs
    when the user presses Start — minutes later. If it dies then, the version is
    NOT good, and a marker left behind would promote it to last-known-good: the
    very version rollback would later fall back to.
    """
    marker = os.environ.get("CIM_HEALTHY_MARKER")
    if not marker:
        return
    try:
        Path(marker).unlink(missing_ok=True)
    except OSError as exc:
        log.warning("could not revoke healthy marker %s: %s", marker, exc)


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
        # a second, and that is exactly the case rollback exists for.
        code = run_shell(manifest, control, data_dir, on_window_ready=lambda: _write_marker(url))
        log.info("shell exited with code %s", code)

        # The user has now actually used the app (or tried to). Streamlit only
        # executes the script once a session opens, so THIS is the first moment
        # its log can tell us the truth.
        app_error = supervisor.app_error_in_log()
        if app_error:
            _revoke_marker()
            log.error("app raised during the session: %s", app_error)
            print(f"\n[start][ERROR] 這個版本的 App 在執行中出錯:\n{app_error}\n"
                  f"  完整記錄:{supervisor.log_path}", file=sys.stderr, flush=True)
            return EXIT_APP_BROKEN
        if code == EXIT_MACHINE_BROKEN:
            return EXIT_MACHINE_BROKEN
        return EXIT_OK if code == 0 else EXIT_MACHINE_BROKEN
    finally:
        supervisor.stop()
        control.shutdown()
        log.info("launcher done")


if __name__ == "__main__":
    raise SystemExit(main())
