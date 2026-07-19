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
  4  the version tree is broken (bad/missing manifest, path escapes package, a file
     THIS VERSION declares is not in THIS VERSION's folder)
     -> this VERSION is bad: mark failed, roll back
  5  the machine is broken (no WebView2, antivirus ate the shell, no window, or a
     SHARED component is gone: deps/shells/<fp>/, deps/runtimes/<fp>/ — see
     SharedComponentError)
     -> the shell and the runtime are SHARED; every version fails the same way. Touch
        no state, claim no rollback, tell the operator what to install.

THE HEALTHY MARKER (CIM_HEALTHY_MARKER), and what bootstrap may conclude
=======================================================================
The marker is our one bit of good news, so it must not claim more than it knows.
It has a BODY, and the body carries TWO different facts that used to be confused:

    body "no-session"  a window opened, and the app was NEVER ASKED TO RUN.
                       Proves the MACHINE can host this version. Proves NOTHING
                       about the version's own code — not one line of it ran.
    body <the app url> the app was asked to run (/control/start) and did not fail
                       on arrival. THIS is the only thing that may promote a
                       candidate to last-known-good.

Why the body and not merely the file: Streamlit does not execute the app script
until a session opens, and a session opens only when the user presses Start in the
portal. Open the app, look at the portal, close the window without pressing Start
— the most ordinary thing a user does — and the old marker (written the moment the
window came up) said "healthy", the launcher exited 0, and bootstrap committed a
version that had never executed a line as last-known-good. If that build was
broken, the next launch died and the version it "rolled back" to was the same
broken build. Automatic rollback was dead on the commonest daily path.

We write "no-session" once the window has survived its creation phase (or exited 0
inside it — see run_shell), REWRITE it to the URL when /control/start arrives, and
DELETE it if the log later proves the app was never usable.

  exit  marker body  what happened                        bootstrap does
  ----  ----------   ----------------------------------   -----------------------
   0    <url>        app ran, user closed the window      commit candidate -> LKG
   0    <url>        app rendered, threw LATER (red box,  commit; we printed a
                     user carried on) -> [WARN]           warning to console + log
   0    no-session   window opened, user never pressed    NOTHING. Not proven, not
                     Start -> the app never ran           blamed. Still the
                                                          candidate; retried next
                                                          launch.
   0    absent       nothing came up (or the marker was   treated as 3: fail + roll
                     revoked on the way out)              back
   3    absent       app never became usable (missing     version failed: roll back
                     module / syntax error / raised on
                     arrival / Streamlit never healthy);
                     the marker is REVOKED
   4    absent       this version's tree/manifest wrong   version failed: roll back
   5    either       the machine is broken                machine: touch no state

  (--no-shell is a developer flag: no window is involved and bootstrap never
   passes it. It writes the URL body on a healthy Streamlit and nothing else.)
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

# The healthy marker's first state: a window came up, and nobody ever asked the app
# to run. bootstrap READS this body — it must stay in step with the copy in
# device/bootstrap.py, which is the other half of the contract.
MARKER_NO_SESSION = "no-session"


# ── WHO IS READING THIS ──────────────────────────────────────────────────────
#
# Every string this file prints in red goes to a LINE WORKER standing in front of a
# machine on a factory floor. They have never opened the packaging tool, they cannot
# add a package to a requirements file, and they cannot rebuild a version. There are
# exactly two things they need from us:
#
#     1. is the line going to run again?          -> yes, the machine fixes itself
#     2. who do I give this to?                   -> the admin
#
# Everything else on the screen is written for the admin, who is NOT in the room, and
# is therefore written to be FORWARDED — read down a phone, photographed, pasted into
# a ticket — not acted on by the reader.
#
# We were getting this exactly backwards. missing_modules_message() ended with
# 「請回到打包工具,把上面的套件加進 requirements(或 lock 檔)後重新建置這個版本」 —
# an instruction, as the last word on the screen, addressed to somebody who is not
# there, given to somebody who cannot follow it and is now certain the line is down
# until they can. _ARRIVAL_FAILURE_HINT was the only message in the file that led with
# what the reader actually needs; now they all do, from this one constant.
_USER_ROLLBACK_LEAD = (
    "  系統會自動退回上一個可用版本,您不需要做任何事。\n"
    "  請把這段訊息交給管理員。"
)
# Below this line we stop talking to the person in the room.
_ADMIN_SECTION = "  ── 以下請交給管理員 ──"

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
    """THIS VERSION's tree or manifest is wrong -> exit 4: fail it, roll back."""


class SharedComponentError(ManifestError):
    """A SHARED component is missing: deps/shells/<fp>/ or deps/runtimes/<fp>/.

    THE MACHINE is broken, not this version -> exit 5: touch no state, blame nobody.

    Every version in the store points at the SAME shell and the SAME runtime, so the
    version that happened to trip over a missing one is not the suspect. Reporting it
    as a broken version tree (which is what a plain ManifestError does — exit 4) makes
    bootstrap mark a perfectly good release failed, roll back onto a version that is
    missing the very same shared folder and fails identically, and then announce
    「已恢復前一版本」 about a recovery that never happened. And failed_versions is
    STICKY: the updater will not re-stage a version that is in it, so the operator has
    lost that release until somebody runs --clear-failed.

    A SUBCLASS of ManifestError, so every existing `except ManifestError` still catches
    it — but any handler that must tell the two apart MUST test for this one FIRST, or
    the base class swallows it and we are back to exit 4. (main() does; that ordering
    is the whole point of the class.) The same idea, and the same name, as
    device/runtime_store.py's SharedComponentError, which is bootstrap's half of this
    contract — we cannot import it: launch.py ships INSIDE a version package and runs
    stdlib-only under whatever runtime the manifest names.
    """

    def __init__(self, what: str, path):
        super().__init__(f"{what}不在:{path}")
        self.what = what
        self.path = path


# _shell / _python can each come from EITHER the version (a fat schema-1 package, where
# they are this version's own files) or the store's shared deps/ (schema 2, where they
# belong to the machine). Same manifest key, opposite verdicts — so load_manifest tracks
# which source each one actually came from rather than guessing later.
_SHARED_NAMES = {"_shell": "共用的應用程式外殼(Tauri 視窗程式)",
                 "_python": "共用的 Python runtime"}


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

    # WHOSE FILE IS IT? Every path below is checked for existence in the same loop at
    # the end, and until now they all raised the same ManifestError — so a shell that
    # antivirus ate out of the SHARED deps/shells/<fp>/ (this MACHINE is broken; exit
    # 5; touch no state) was reported exactly like a missing entrypoint (this VERSION
    # is broken; exit 4; mark it failed and roll back onto a version that is missing
    # the identical shared folder). `shared` is what keeps them apart.
    shared: set[str] = set()

    # Store layout: the shell is SHARED (deps/shells/<fp>/), so it necessarily
    # lives outside this version directory. bootstrap resolves and integrity-checks
    # it, then hands us the path — we still refuse to run if it is not there.
    shared_shell = os.environ.get("CIM_SHELL_EXE")
    if shared_shell:
        data["_shell"] = Path(shared_shell)
        shared.add("_shell")
    elif data.get("shell_executable"):
        # A fat package carries its own shell INSIDE the version tree, so a missing
        # one really is this version's tree being wrong. Same key, different owner.
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
        shared.add("_python")        # deps/runtimes/<fp>/ — the machine's, not ours
    elif data.get("python"):
        data["_python"] = resolve_inside(pkg_root, data["python"], what="python")
    else:
        raise ManifestError(f"{MANIFEST_NAME} needs either python or runtime_fingerprint")
    for key in ("_entrypoint", "_python", "_shell", "_shim"):
        if data[key].is_file():
            continue
        if key in shared:
            # NOT this version's fault. See SharedComponentError: exit 5, no rollback.
            raise SharedComponentError(_SHARED_NAMES[key], data[key])
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


# The scope labels are the BUILD SIDE's (imports.py). Both halves must say the same
# thing to the same operator, and — far more important — must decide "does this import
# run at startup?" the same way. Two implementations of that question is exactly how
# the build gate and this preflight drifted apart: the gate started catching a package
# that this side still waved through to the factory floor.
MODULE_SCOPE = "module"          # required
CALLED_SCOPE = "module-called"   # required: a def whose body the MODULE BODY runs
_REQUIRED_SCOPES = (MODULE_SCOPE, CALLED_SCOPE)


def _catches_import_error(node: ast.Try) -> bool:
    """try/except ImportError (or bare except, or Exception) — the author wrote a
    fallback, so the import is optional. Mirrors imports.py:_catches_import_error,
    including the bare-except and dotted-name cases the old version here missed."""
    for handler in node.handlers:
        if handler.type is None:                       # bare except
            return True
        candidates = (handler.type.elts if isinstance(handler.type, ast.Tuple)
                      else [handler.type])
        for candidate in candidates:
            name = getattr(candidate, "id", None) or getattr(candidate, "attr", None)
            if name in ("ImportError", "ModuleNotFoundError", "Exception"):
                return True
    return False


def _is_main_guard(node: ast.If) -> bool:
    """`if __name__ == "__main__":` — the one module-level block whose CALLS we refuse
    to treat as "the module body runs this".

    Streamlit really does set `__name__ == "__main__"` on the entry script, so the
    block does execute. We decline to promote the functions it calls anyway, on
    purpose: doing so would turn every `main()`-style script's lazy imports into hard
    requirements, and a wrong REQUIRED refuses an app that works. Fail open, and say
    why. (imports.py:_is_main_guard — same rule, same reason.)
    """
    return any(isinstance(sub, ast.Name) and sub.id == "__name__"
               for sub in ast.walk(node.test))


def _called_from_module_scope(tree: ast.Module) -> set[str]:
    """Names of this file's own functions that the MODULE BODY invokes.

    `_setup()` at the bottom of the file, `CONFIG = boot()`, a call inside a
    module-level `if`/`with`, `@register` on a module-level def — all of them run
    while Streamlit is importing the script, so an import in their body executes on
    the first render exactly like a module-level import. Calling those "lazy" is how
    a missing dependency reaches the operator as a red box with a green build behind it.

    What this deliberately does NOT cover (one level, same file, by design):
      · a call two hops deep — module calls `main()`, `main()` calls `_setup()`:
        `_setup()`'s imports stay optional.
      · methods — `App().boot()` at module scope does not promote `boot`.
      · a decorator that CALLS the function it decorates (`@run_now def _setup()`):
        we see that `run_now` runs, not that `_setup` does.
      · `if __name__ == "__main__":` — see _is_main_guard.
    Each of those stays LAZY, i.e. not required. That is the safe direction: a wrong
    REQUIRED refuses an app that works, while a missed one still meets `find_spec`
    against the real runtime a moment later.

    Copied in shape and rule from imports.py:_called_from_module_scope — read that
    one before changing this one, and change both.
    """
    called: set[str] = set()

    def scan(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                # The body is NOT module scope — but the decorators run right now,
                # so `@register` really does execute register().
                for decorator in child.decorator_list:
                    target = (decorator.func if isinstance(decorator, ast.Call)
                              else decorator)
                    if isinstance(target, ast.Name):
                        called.add(target.id)
                continue
            if isinstance(child, ast.If) and _is_main_guard(child):
                continue
            if isinstance(child, ast.Call) and isinstance(child.func, ast.Name):
                called.add(child.func.id)
            scan(child)          # into module-level if/with/try, call args, ...

    scan(tree)
    return called


def _module_level_imports(tree: ast.Module) -> set[str]:
    """The imports that RUN when Streamlit loads the script — the only ones a missing
    package can break the FIRST RENDER with.

    REQUIRED = module level, **or the body of a `def` that the module body calls**.

    An `import anthropic` inside a function nobody calls at import time is lazy by
    construction: the app starts fine without it, and hard-failing over an optional
    LLM backend nobody enabled is how a good version gets refused. But a function the
    MODULE BODY calls is not lazy at all — it runs on the first render, exactly like a
    module-level import. Treating `def _setup(): import cv2` + `_setup()` as lazy is
    the defect this fixes: the build gate now catches such a package, and this
    preflight would still have let it reach the factory floor.

    Promotion is same-file and ONE LEVEL DEEP; the exclusions are listed in
    _called_from_module_scope and every one of them fails OPEN. A try/except
    ImportError still degrades to optional even inside a promoted function — the
    author wrote the fallback either way.
    """
    required: set[str] = set()
    runs_on_import = _called_from_module_scope(tree)

    def walk(node: ast.AST, *, scope: str, guarded: bool) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                # Promoted from MODULE scope only, so a def nested inside a
                # module-called def is lazy again — that is what "one level" means.
                if (scope == MODULE_SCOPE and not guarded
                        and child.name in runs_on_import):
                    walk(child, scope=CALLED_SCOPE, guarded=guarded)
                continue                     # otherwise lazy: cannot break first render
            if isinstance(child, ast.ClassDef):
                continue                     # a method is lazy (imports.py: CLASS_SCOPE
                                             # is optional too — keep the sides agreeing)
            if isinstance(child, ast.Try) and _catches_import_error(child):
                # Body AND handlers: `except ImportError: import simplejson as json`
                # is the fallback, not a second requirement. True inside a
                # module-called function too: it degrades gracefully either way.
                walk(child, scope=scope, guarded=True)
                continue
            if isinstance(child, ast.Import):
                if scope in _REQUIRED_SCOPES and not guarded:
                    required.update(a.name.split(".")[0] for a in child.names)
            elif isinstance(child, ast.ImportFrom):
                if (scope in _REQUIRED_SCOPES and not guarded
                        and child.level == 0 and child.module):
                    required.add(child.module.split(".")[0])
            # Module-level if/with/try(not import-guarded)/… still runs on import, so
            # anything inside keeps the scope it inherited.
            walk(child, scope=scope, guarded=guarded)

    walk(tree, scope=MODULE_SCOPE, guarded=False)
    return required


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


class LauncherIncomplete(Exception):
    """A file this launcher needs is not in the package — the VERSION is broken.

    `pages.py` holds the ONE implementation of "what does Streamlit actually
    load", shared with the build-side import gate (provision_builder's
    imports.py). It is copied next to this file when a package is built. If it is
    not here, this package was assembled by a builder that does not know about it,
    and the page half of the preflight would be silently missing — the exact
    blindness that shipped a broken multipage app. Say so out loud instead: a
    version whose launcher folder is incomplete is a broken version (exit 4).
    """


_PAGES_MARK = "cim-streamlit-pages/1"
_PAGES_MODULE = None


def shared_pages():
    """The page rules — loaded BY PATH, because this file must also run inside a
    delivered package where there is no `provision_builder` to import from.

      launcher/pages.py           the delivered copy (what runs on the device)
      ../pages.py                 the repo: src/.../streamlit_desktop/pages.py,
                                  which is the file the builders copy. Loading the
                                  canonical file here is what keeps the tests
                                  honest: they exercise the same loader the device
                                  does, against the same source.

    The MODULE_MARK check is not ceremony: `pages.py` is a common enough name that
    picking up a stranger's file and calling it "the rules" is a real way to be
    wrong. No mark, no deal.
    """
    global _PAGES_MODULE
    if _PAGES_MODULE is not None:
        return _PAGES_MODULE
    here = Path(__file__).resolve().parent
    tried = []
    for path in (here / "pages.py", here.parent / "pages.py"):
        tried.append(str(path))
        if not path.is_file():
            continue
        try:
            spec = importlib.util.spec_from_file_location("cim_streamlit_pages", path)
            if spec is None or spec.loader is None:
                continue
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        except Exception as exc:                  # a truncated / half-copied file
            log.warning("could not load the page rules from %s: %s", path, exc)
            continue
        if getattr(module, "MODULE_MARK", None) != _PAGES_MARK:
            log.warning("%s is not the page-rules module (no %s mark)", path, _PAGES_MARK)
            continue
        _PAGES_MODULE = module
        return module
    raise LauncherIncomplete(
        "launcher 資料夾不完整:找不到 pages.py(Streamlit 多頁面規則)。\n"
        "  這個版本是用舊版打包工具組出來的,請重新建置這個版本。\n"
        "  找過的位置:" + "、".join(tried))


def preflight(entrypoint: Path, app_root: Path) -> tuple[list[str], str | None]:
    """(missing third-party modules, syntax error) — reachable from the entrypoint.

    Follows first-party imports transitively, so a module the app never touches
    cannot fail the check (CV_Viewer ships a `verify/` folder that imports
    playwright; the app does not, and must not be blamed for it).

    The queue starts at the entrypoint *and* at every page Streamlit will run on
    its own (pages.seed_scripts) — a page is reachable for the user even though it
    is unreachable for an import walk. The pages folder is a first-party root too
    (pages.first_party_roots): a .py next to a page IS a page's helper, not a PyPI
    package, and reporting「請 pip install 2_report」made a working CV_Viewer
    refuse to start.
    """
    pages = shared_pages()
    roots = pages.first_party_roots(entrypoint, app_root)
    missing: list[str] = []
    seen_files: set[Path] = set()
    queue = list(pages.seed_scripts(entrypoint, app_root))
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
    """The version is missing a package it needs — said to the person in the room first.

    This message used to close with 「請回到打包工具...重新建置這個版本」: the last word
    on a factory machine's screen, addressed to somebody who is not standing there,
    telling the person who IS to do something they have no tool for and no authority to
    do. They read it as "the line is down until the admin comes". It never was — the
    machine rolls itself back to the last working version within seconds. That is the
    first thing they must be told, and it was not on the screen at all.

    The rebuild instruction is still here. It is now ONE line, under a heading that
    says who it is for. See _USER_ROLLBACK_LEAD.
    """
    lines = [
        "這個版本的 App 少了它需要的套件,所以沒辦法啟動。",
        _USER_ROLLBACK_LEAD,
        "",
        _ADMIN_SECTION,
        "  這台機器的 runtime 裡找不到這些套件:",
    ]
    for name in missing:
        hint = _DIST_HINT.get(name)
        lines.append(f"    - {name}" + (f"(要裝的套件叫 {hint})" if hint else ""))
    lines += [
        "  這不是這台電腦的問題,是這個版本打包時漏掉了相依套件。",
        "  修法:回到打包工具,把上面的套件加進 requirements(或 lock 檔),重新建置這個版本。",
        f"  App 目錄:{app_root}",
    ]
    return "\n".join(lines)


class StreamlitExited(Exception):
    """Streamlit died before it became healthy — never open an empty shell."""


class StreamlitTimedOut(StreamlitExited):
    """The Streamlit process is STILL ALIVE but the health endpoint has not
    answered in time. This is NOT the same failure as the process dying, and
    conflating the two is how a good version gets blamed for a slow machine.

    A process that is up and simply not answering /_stcore/health yet is what a
    first boot under antivirus looks like: Defender is reading every byte of a
    600 MB runtime, the disk is cold, and the server needs 65s where a warm
    machine needs 3. Nothing about the VERSION is wrong — its imports already
    resolved (preflight), its files already verified. The runtime is SHARED by
    every version in the store, so 'the runtime is slow to come up' is a fact
    about THIS MACHINE, identical for every version we could roll back onto.

    So this maps to EXIT_MACHINE_BROKEN (5), not EXIT_APP_BROKEN (3): retry,
    advise the operator to check antivirus, but never fail the version and never
    roll a working build back onto one that will be exactly as slow."""


# THE ARRIVAL WINDOW — how long after THE APP IS ASKED TO RUN an error still counts
# as "the app failed on arrival" rather than "the app broke while it was being used".
#
# ANCHORED TO THE SESSION, NOT TO THE HEALTH CHECK. This used to start counting when
# /_stcore/health first answered 200 — i.e. when the Streamlit *server* came up. But
# the server comes up at launch, and the app script does not run until the user
# presses "Start" in the portal, which can be minutes later. So the clock starts at
# _session_at (the /control/start hit), and until that exists the window CANNOT
# close: with no session there is no app to have arrived, and anything in the log is
# still an arrival failure.
#
# AND IT IS NOT A WALL CLOCK. "20 seconds have passed, therefore the app has
# rendered" is a guess, and it is wrong for precisely the apps that need the safety
# net most: the ones with a heavy first render (a model loaded at import, a big table
# read at module scope, a slow machine with antivirus reading every byte). Those are
# still legitimately starting at T+20s, so a fatal error at T+30s landed in the
# "late" half of the log, was downgraded to a warning, and the broken version was
# committed as last-known-good.
#
# So the window closes on the app going QUIET, not on the clock running out:
#
#   * the log must have been UNCHANGED for APP_ARRIVAL_QUIET_SECONDS. A log that is
#     still growing is an app that is still visibly working, and we do not get to
#     declare it "arrived" while it is still talking to us.
#   * never sooner than APP_ARRIVAL_SECONDS after the session started (the FLOOR: an
#     app that logs nothing at all is quiet from its first breath, and we still owe
#     it time to render before we start forgiving its errors).
#   * always by APP_ARRIVAL_MAX_SECONDS, whatever the log is doing (the BOUND, so
#     this always terminates).
#
# WHAT THE BOUND COSTS: an app that never stops writing — a progress line every
# second, a chatty library, a polling loop — holds the window open until the bound
# and no further. After that, its errors are warnings. So an app whose first render
# takes longer than APP_ARRIVAL_MAX_SECONDS and only THEN dies is still committed as
# last-known-good. That is the price of always terminating. Five minutes is far
# beyond any first render we have measured; the alternative (no bound) is a window
# that never closes, which turns every red box the user ever causes into a failed
# version and an unasked-for downgrade — a worse bug than the one we are fixing.
#
# HONEST ABOUT WHAT THIS CANNOT DO: a SILENT slow starter (one that loads a big model
# without logging a thing) is indistinguishable, from the log alone, from an app that
# finished instantly and is idle. For those, only the floor protects us. Streamlit
# reports no render, so there is no better signal available to a stdlib-only launcher.
APP_ARRIVAL_SECONDS = 20.0
APP_ARRIVAL_QUIET_SECONDS = 20.0
APP_ARRIVAL_MAX_SECONDS = 300.0


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
        # Three different moments, and confusing two of them is what stamped a
        # broken version as last-known-good:
        #   _healthy_at      the SERVER answered /_stcore/health. Says nothing
        #                    about the app: Streamlit serves 200 while the script
        #                    is dying. Kept for the log, never for a verdict.
        #   _session_at      the app was ASKED TO RUN (/control/start). This is
        #                    when the script actually executes, and the only
        #                    honest start for the arrival window.
        #   _arrival_offset  how many bytes of the log had been written when the
        #                    arrival window closed. None = still arriving, so the
        #                    WHOLE log is arrival and any error in it is fatal.
        self._healthy_at = None
        self._session_at = None
        self._arrival_offset = None
        # How we tell "still starting" from "started and idle": the size of the log
        # and when it last changed. A log that is still growing is an app that is
        # still working — see the arrival-window constants above.
        self._log_size = None
        self._log_changed_at = None

    # -- state ---------------------------------------------------------------

    @property
    def session_started(self) -> bool:
        """Was the app ever actually ASKED TO RUN this session?

        False means the user opened the window, looked at the portal, and closed it
        without pressing Start: Streamlit's server ran, the app's own code did not.
        Such a session proves nothing about the version and must never promote it.
        """
        return self._session_at is not None

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
            # Preserve the DISTINCTION the retries kept blurring: if the last
            # attempt timed out with the process still alive (a slow machine),
            # this is StreamlitTimedOut -> machine, not the version. Only if the
            # process actually died do we hand back a plain StreamlitExited.
            summary = (f"Streamlit did not become healthy after "
                       f"{BIND_RACE_RETRIES} attempts: {last_error}")
            if isinstance(last_error, StreamlitTimedOut):
                raise StreamlitTimedOut(summary)
            raise StreamlitExited(summary)

    def _spawn_and_wait(self, port: int) -> str:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        log_path = self.log_dir / f"streamlit-{stamp}-{port}.log"
        self._log_path = log_path
        # A fresh process writing a fresh log: a fresh arrival window. Reset every
        # one of these, or the previous run's offset would declare this run's startup
        # crash "late" and keep the marker — and the previous log's size would be
        # compared against the new log's, faking a "change" (or a false quiet) on the
        # first tick. Note this only ever runs when we are NOT already running
        # (start() short-circuits on `if self.running`), so a portal that presses
        # Start twice cannot rewind its own window.
        self._healthy_at = None
        self._session_at = None
        self._arrival_offset = None
        self._log_size = None
        self._log_changed_at = None
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
        # The process is STILL ALIVE (the poll() above never fired) — it just did
        # not answer /_stcore/health in time. That is a slow machine, not a broken
        # version: StreamlitTimedOut so main() can send the operator to check
        # antivirus instead of failing the build. We still terminate it: a shell
        # onto a half-started server is worse than an honest "the machine is busy".
        self._terminate_tree()
        raise StreamlitTimedOut(f"Streamlit was not healthy within {self.timeout:.0f}s "
                                f"(the process was still running — a slow first boot, "
                                f"not a broken version). See {log_path}")

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

    def note_session_start(self) -> None:
        """The app has just been ASKED TO RUN — start the arrival clock, and say so
        in the marker.

        Called from the control channel's /control/start (the portal pressing
        "Start"), which is the first moment Streamlit executes the app script.
        Idempotent: a portal that presses Start twice on a running app must not
        push the window forward and turn a startup crash into a "late" error.

        THE MARKER'S SECOND STATE IS WRITTEN HERE, and it is written here rather
        than in the HTTP handler so that it cannot be forgotten: "the app was asked
        to run" and "the marker may now speak for this version" are the same fact,
        and a version whose app was never asked to run must never be promoted.
        """
        if self._session_at is not None:
            return
        self._session_at = time.monotonic()
        # The gap is the whole bug, so put it in the log where the next person can
        # see it: this is how long /_stcore/health had been answering 200 while the
        # app had not executed a single line. Anchoring the window to THAT is what
        # let a version die and still be committed as last-known-good.
        waited = (self._session_at - self._healthy_at) if self._healthy_at else 0.0
        log.info("the app was asked to run %.1fs after the server became healthy: "
                 "arrival window opens (floor %.0fs, quiet %.0fs, bound %.0fs)",
                 waited, APP_ARRIVAL_SECONDS, APP_ARRIVAL_QUIET_SECONDS,
                 APP_ARRIVAL_MAX_SECONDS)
        # THE BASELINE for "is the app still writing?": how big the log was at the
        # moment the app was asked to run. Without it the first tick has nothing to
        # compare against, so an app that had ALREADY written three progress lines by
        # then would look like it had been quiet since the session started — and its
        # window would close while it was still visibly working, which is the whole
        # bug we are here to fix.
        if self._log_path is not None:
            try:
                self._log_size = self._log_path.stat().st_size
            except OSError:
                self._log_size = None    # note_arrival_window seeds it on its next tick
        url = self.url
        if url:
            _write_marker(url)

    @property
    def arriving(self) -> bool:
        """True while an error in the log would mean "the app never worked"."""
        return self._arrival_offset is None

    def note_arrival_window(self) -> None:
        """Freeze how much of the log belongs to "the app arriving".

        Called on a tick while the shell is up (run_shell). The window opens when the
        app is asked to run (note_session_start) and closes once the app has gone
        QUIET — see the arrival-window constants above. Whatever the app logs after
        that happened to an app that had already rendered for the user.

        TWO THINGS KEEP IT OPEN, and both are the point:

        UNTIL THERE IS A SESSION, IT NEVER CLOSES. The user may stare at the portal
        for ten minutes before pressing Start; Streamlit has been healthy that whole
        time and the app has not run a line.

        WHILE THE APP IS STILL WRITING, IT NEVER CLOSES (up to the bound). A growing
        log is an app that is still working. Closing on a bare 20-second wall clock
        meant a slow first render was declared "arrived" while it was still starting,
        so its dying breath was filed as a late warning and the version was committed
        as last-known-good.

        Honest about the limits: Streamlit logs NOTHING on a successful run, so we
        cannot observe a render. "It became usable" is inferred from "it was asked to
        run, then went quiet". An app that blows up on a page the user opens a minute
        in is reported as a warning, not a failed version — the direction we want to
        be wrong in: a red box the user can retry is not worth downgrading a machine
        over.
        """
        if self._arrival_offset is not None:
            return
        session_at, log_path = self._session_at, self._log_path
        if session_at is None or log_path is None:
            return                       # nobody asked the app to run yet
        now = time.monotonic()
        try:
            size = log_path.stat().st_size
        except OSError:
            return                       # try again on the next tick

        if self._log_size is None:
            self._log_size = size        # note_session_start could not stat: seed here
        elif size != self._log_size:
            self._log_size = size
            self._log_changed_at = now   # still talking: still arriving

        # _log_changed_at is None only while the log has not moved SINCE THE SESSION
        # STARTED (note_session_start took that baseline) — i.e. a silent app. It has
        # therefore been quiet for exactly as long as the session has been open.
        quiet_since = session_at if self._log_changed_at is None else self._log_changed_at

        elapsed = now - session_at
        if elapsed < APP_ARRIVAL_SECONDS:
            return                       # the floor: too early to call it arrived
        if (now - quiet_since) < APP_ARRIVAL_QUIET_SECONDS and elapsed < APP_ARRIVAL_MAX_SECONDS:
            return                       # still writing, and the bound has not hit
        self._arrival_offset = size
        log.info("arrival window closed at %d bytes of %s (%.0fs after the app was "
                 "asked to run; quiet for %.0fs)",
                 self._arrival_offset, log_path.name, elapsed, now - quiet_since)

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
            # The arrival window never closed: either the user shut the app down
            # inside it, or they never pressed Start at all. Nothing in this log
            # had time to be "an app that already worked".
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

    def failing_on_arrival(self) -> str | None:
        """The same verdict, asked ON A TICK instead of at the end of the session.

        We already poll every 0.5s while the shell is up. If the app has died on
        arrival, everything after this point is the operator staring at a red box
        until they give up and close the window — and only then would we fail the
        version and roll back. There is nothing to wait for: the verdict cannot
        change while the window is still open (an error inside it is fatal by
        definition). Close the shell, fail the candidate, let bootstrap roll back
        NOW. That is the difference between the machine fixing itself and the
        operator finding out tomorrow.

        Deliberately the SAME question app_error_in_log() answers at exit, so a
        session can never be killed for something the end-of-session verdict would
        have forgiven.
        """
        if not self.arriving:
            return None                  # the window closed: errors are warnings now
        return self.app_error_in_log()

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
                    # THE app's real starting gun. Streamlit has been serving
                    # /_stcore/health since the launcher came up, but the script
                    # does not execute until a session opens — and a session opens
                    # because the portal, right now, was told where the app is.
                    # Everything the app logs from here is "the app arriving".
                    # (After start(): a restart resets the window, and this puts it
                    # back — see StreamlitSupervisor.note_session_start.)
                    server.supervisor.note_session_start()
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


def _terminate_shell(proc) -> None:
    """Close the window WE opened. Only ever this PID's tree — never a name scan."""
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=5)
        return
    except subprocess.TimeoutExpired:
        log.warning("shell pid=%s ignored terminate; killing tree", proc.pid)
    except OSError as exc:
        log.warning("terminate failed for shell pid=%s: %s", proc.pid, exc)
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                       capture_output=True, check=False)
    else:  # pragma: no cover - packages are Windows-only
        proc.kill()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        log.error("shell pid=%s survived kill", proc.pid)


# What the operator sees when we close the window on them. They just watched it
# vanish; not saying why is how a self-healing rollback looks like a crash.
_ARRIVAL_FAILURE_HINT = (
    "\n[start][ERROR] App 一啟動就出錯,畫面上只會是一個紅色錯誤方塊,所以視窗已經關閉。\n"
    + _USER_ROLLBACK_LEAD + "\n\n"
    + _ADMIN_SECTION + "\n"
    "  這個版本會被標記為失敗,自動更新不會再把它裝回來。\n"
    "  請把下面的錯誤訊息交給開發者。"
)


def run_shell(manifest: dict, control: ControlServer, data_dir: Path,
              *, on_window_ready=None, on_tick=None) -> int:
    """Run the window to its end and return the code the SESSION deserves.

    `on_tick` is called every _SHELL_TICK_SECONDS while the window is up. It
    returns truthy to say "stop now, the app is dead" — see
    StreamlitSupervisor.failing_on_arrival. We do not make the operator sit in
    front of a red box for the rest of the afternoon so that a rollback can start
    when they finally close it.
    """
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
            if on_tick is not None and on_tick():
                # The app died on arrival. Waiting for the user to close the
                # window would leave the machine on a version we already KNOW is
                # broken — for minutes, or until tomorrow morning. Close it, and
                # let finish_session revoke the marker and fail the candidate so
                # bootstrap rolls back on the spot.
                log.error("the app failed on arrival; closing the window and "
                          "failing this version")
                print(_ARRIVAL_FAILURE_HINT, file=sys.stderr, flush=True)
                _terminate_shell(proc)
                return EXIT_APP_BROKEN


# ── main ─────────────────────────────────────────────────────────────────────

def _marker_body(path: Path) -> str | None:
    """What the marker currently says, or None if it is not there / unreadable."""
    try:
        return path.read_text("utf-8").strip()
    except OSError:
        return None


def _write_marker(body: str) -> None:
    """Record in the marker's BODY what this session has actually proved.

      MARKER_NO_SESSION  a window opened and the app was never asked to run. Says
                         the MACHINE can host this version; says nothing about the
                         version's code, because none of it ran.
      <the app url>      the app was asked to run (/control/start). Only this may
                         promote a candidate to last-known-good.

    NEVER DOWNGRADES. on_window_ready can land AFTER /control/start — a user who
    presses Start inside the three-second window-creation watch, or a shell that
    exits cleanly inside it — and a "no-session" written over a real session would
    throw away the one fact that lets a good version be committed.
    """
    marker = os.environ.get("CIM_HEALTHY_MARKER")
    if not marker:
        return
    path = Path(marker)
    try:
        if body == MARKER_NO_SESSION:
            current = _marker_body(path)
            if current not in (None, "", MARKER_NO_SESSION):
                return                    # a session is already recorded: keep it
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    except OSError as exc:
        log.warning("could not write healthy marker %s: %s", marker, exc)


def _revoke_marker() -> None:
    """Take the marker back entirely — both of its states.

    The app was asked to run and proved it never became usable. Neither body is
    true any more: not the URL (the app did not survive arrival) and not
    MARKER_NO_SESSION (that would say "the user never pressed Start", and they
    did). An absent marker with exit 3 is exactly what bootstrap reads as "this
    version is broken, roll back".

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
      app raised LATER       -> keep the URL marker, warn loudly, exit 0
      the app never ran      -> leave the marker at "no-session", exit 0. The
                                session proves nothing: bootstrap neither commits
                                the version nor blames it.
      the window never came  -> exit 5 (machine); the marker was never written
      clean close            -> keep the URL marker, exit 0
    """
    fatal = supervisor.app_error_in_log()
    if fatal:
        _revoke_marker()
        log.error("the app failed on arrival: %s", fatal)
        print("\n[start][ERROR] 這個版本的 App 一啟動就出錯,使用者根本看不到畫面。\n"
              + _USER_ROLLBACK_LEAD + "\n"
              + _ADMIN_SECTION + "\n"
              f"{fatal}\n"
              f"  完整記錄:{supervisor.log_path}", file=sys.stderr, flush=True)
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

    if not supervisor.session_started:
        # The user opened the window, looked at the portal, and closed it without
        # ever pressing Start. Streamlit's SERVER ran; the app's own code did not —
        # not one line. The marker therefore still says MARKER_NO_SESSION, and
        # bootstrap will neither commit this version nor fail it: it stays the
        # candidate and gets another chance next launch.
        #
        # This is the single most ordinary thing a user can do, and committing on it
        # made a version that had never executed the machine's last-known-good — the
        # very build a later rollback would fall back to.
        log.info("the app was never asked to run (no /control/start): the marker "
                 "stays %r, so this session neither proves nor condemns the version",
                 MARKER_NO_SESSION)

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
    except SharedComponentError as exc:
        # MUST come BEFORE ManifestError — it is a SUBCLASS of it, and the base clause
        # below returns exit 4, which is bootstrap's cue to mark this version failed and
        # roll back. There is nothing to roll back TO: deps/shells/<fp>/ and
        # deps/runtimes/<fp>/ are shared by every version in the store, so the version
        # we roll onto is missing exactly the same folder and dies exactly the same way,
        # and we have thrown away a good release to get there. This is the machine.
        log.error("shared component missing (the machine, not this version): %s", exc)
        print(f"\n[start][ERROR] 這台電腦少了「所有版本共用」的元件,App 開不起來:\n"
              f"  {exc}\n"
              f"  這不是版本的問題:系統不會退版,也不會把這個版本標記為失敗。\n"
              f"  這台電腦要先修好,請把這段訊息交給管理員或 IT。\n\n"
              + _ADMIN_SECTION + "\n" + _MACHINE_HINT +
              f"\n  log: {log_path}", file=sys.stderr, flush=True)
        return EXIT_MACHINE_BROKEN
    except ManifestError as exc:
        log.error("bad package: %s", exc)
        print(f"\n[start][ERROR] 這個版本的套件描述檔壞了,App 開不起來。\n"
              + _USER_ROLLBACK_LEAD + "\n"
              + _ADMIN_SECTION + "\n"
              f"  {exc}\n"
              f"  這個版本要重新建置。\n  log: {log_path}",
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
    except LauncherIncomplete as exc:
        # The launcher folder is missing the page rules it shares with the build
        # gate. Refusing here is the point: a silent fallback would go blind to
        # every pages/*.py, which is precisely the failure this module was fixed
        # for. An incomplete version tree is a broken version (exit 4).
        log.error("incomplete launcher: %s", exc)
        print(f"\n[start][ERROR] 這個版本的檔案不完整,App 開不起來。\n"
              + _USER_ROLLBACK_LEAD + "\n"
              + _ADMIN_SECTION + "\n"
              f"  {exc}\n  log: {log_path}", file=sys.stderr, flush=True)
        return EXIT_VERSION_BROKEN
    finally:
        sys.path[:] = saved_path
    if syntax_error:
        log.error("preflight: %s", syntax_error)
        print(f"\n[start][ERROR] 這個版本的 App 有語法錯誤,無法執行。\n"
              + _USER_ROLLBACK_LEAD + "\n"
              + _ADMIN_SECTION + "\n"
              f"  {syntax_error}\n"
              f"  修好程式後重新建置這個版本。\n  log: {log_path}",
              file=sys.stderr, flush=True)
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
        except StreamlitTimedOut as exc:
            # The server was still coming up when we ran out of patience — a slow
            # machine (antivirus scanning a 600 MB runtime on first boot, a cold
            # disk), NOT a broken version. Blaming the version here would fail a
            # good build and, if there is a previous version, roll back onto one
            # that is exactly as slow. So: exit 5 (the machine), touch no state,
            # and point the operator at the thing they can actually change.
            log.error("Streamlit did not become healthy in time (still running): %s", exc)
            print(f"\n[start][ERROR] App 起不來,但這比較像「這台電腦現在很忙」而不是版本的問題。\n"
                  f"  Streamlit 有啟動,只是沒能在時限內回應 —— 第一次開機時,防毒軟體正在\n"
                  f"  掃描剛裝好的執行環境(數百 MB),這會讓啟動變得很慢。\n"
                  f"  系統不會退版,也不會把這個版本標記為失敗。可以做的事:\n"
                  f"    1. 過一下再開一次 start.bat(掃描完就會快很多)。\n"
                  f"    2. 請 IT 把這個交付資料夾加進防毒的排除清單。\n"
                  + _ADMIN_SECTION + "\n"
                  f"  {exc}\n  log: {log_path}", file=sys.stderr, flush=True)
            return EXIT_MACHINE_BROKEN
        except StreamlitExited as exc:
            # The process actually DIED before becoming healthy — that is this
            # version's runtime failing to run at all. Fail loudly instead of
            # opening a shell onto nothing.
            log.error("Streamlit failed to start: %s", exc)
            print(f"\n[start][ERROR] 這個版本的 App 起不來(Streamlit 沒有正常啟動)。\n"
                  + _USER_ROLLBACK_LEAD + "\n"
                  + _ADMIN_SECTION + "\n"
                  f"  {exc}\n  log: {log_path}", file=sys.stderr, flush=True)
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

        # A window that is really up proves the MACHINE can host this version — a
        # missing WebView2 runtime kills the shell in a second, and that is exactly
        # the case rollback exists for. So the marker is written here, but with the
        # body MARKER_NO_SESSION: the app script has not run, and will not run until
        # the user presses Start. note_session_start() rewrites the body to the URL
        # if and when that happens, and ONLY that body promotes a version.
        #
        # The tick does two things, every half second, while the user works:
        #   1. closes the app's arrival window once the app has been asked to run and
        #      has gone quiet (note_arrival_window), and
        #   2. answers "is this app dying right now?" (failing_on_arrival) — a
        #      truthy tick tells run_shell to close the window instead of leaving
        #      the operator in front of a red box until they give up.
        def tick() -> bool:
            supervisor.note_arrival_window()
            return supervisor.failing_on_arrival() is not None

        code = run_shell(manifest, control, data_dir,
                         on_window_ready=lambda: _write_marker(MARKER_NO_SESSION),
                         on_tick=tick)
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
