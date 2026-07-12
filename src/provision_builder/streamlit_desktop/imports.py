"""Catch a missing dependency on the BUILD machine, not on the factory floor —
and only when it is really missing.

Streamlit's `/_stcore/health` answers 200 even when the app script dies on
`import missing_module`, so a release with a forgotten dependency sails through
every health check, gets committed as last-known-good, and only reveals itself as
a red traceback in front of the operator. The cheapest place to catch that is
here: parse the app's imports, subtract the standard library, the project's own
modules and everything that is declared, and complain about what is left.

The trap is the other direction. The first version of this walked the whole AST
(`ast.walk`), so a lazy `import anthropic` **inside a function body** counted as a
hard requirement — and AI4BI, whose `_call_anthropic()` does exactly that on
purpose to keep mock-mode free of the dependency, became unbuildable after a
six-minute pip install. An import that only runs when a function is called cannot
crash the app on startup, so it can never be a reason to refuse a build.

The rule, therefore, is about SCOPE:

    module level (incl. module-level `if` / `with` / `try` blocks)
        REQUIRED — it runs on the first render; if it is missing, the app dies.

    inside a def / async def / class body
    inside a try/except ImportError guard (its body AND its fallbacks)
        OPTIONAL — reported as a warning, never fails a build.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from .models import EXCLUDED_DIRS

# import name -> the distribution(s) that provide it, where they differ. A tuple,
# because `cv2` is equally satisfied by opencv-python, -headless or -contrib, and
# demanding the exact one we happen to know would be a false alarm of its own.
_KNOWN_ALIASES: dict[str, tuple[str, ...]] = {
    "cv2": ("opencv-python", "opencv-python-headless", "opencv-contrib-python",
            "opencv-contrib-python-headless"),
    "PIL": ("pillow",),
    "sklearn": ("scikit-learn",),
    "skimage": ("scikit-image",),
    "yaml": ("pyyaml",),
    "dateutil": ("python-dateutil",),
    "bs4": ("beautifulsoup4",),
    "dotenv": ("python-dotenv",),
    "fitz": ("pymupdf",),
    "pymupdf": ("pymupdf",),
    "OpenSSL": ("pyopenssl",),
    "google.protobuf": ("protobuf",),
    "google.cloud": ("google-cloud-core",),
    "docx": ("python-docx",),
    "pptx": ("python-pptx",),
    "serial": ("pyserial",),
    "usb": ("pyusb",),
    "win32com": ("pywin32",),
    "win32api": ("pywin32",),
    "win32con": ("pywin32",),
    "pythoncom": ("pywin32",),
    "jwt": ("pyjwt",),
    "attr": ("attrs",),
    "zoneinfo": ("backports-zoneinfo",),
    "pkg_resources": ("setuptools",),
    "markdown_it": ("markdown-it-py",),
    "st_aggrid": ("streamlit-aggrid",),
    "streamlit_option_menu": ("streamlit-option-menu",),
}

MODULE_SCOPE = "module"          # required
FUNCTION_SCOPE = "function"      # optional: only runs when someone calls it
CLASS_SCOPE = "class"            # optional (per spec: never a startup crash we own)
GUARDED_SCOPE = "guarded"        # optional: try/except ImportError = degrade gracefully

_OPTIONAL_SCOPES = (FUNCTION_SCOPE, CLASS_SCOPE, GUARDED_SCOPE)

_SCOPE_LABEL = {
    MODULE_SCOPE: "模組層級 import",
    FUNCTION_SCOPE: "函式內延遲 import",
    CLASS_SCOPE: "class 內 import",
    GUARDED_SCOPE: "try/except ImportError 保護的 import",
}


class ImportGateError(Exception):
    """The app imports something nothing provides, and it would die on startup."""


class ImportProbeError(Exception):
    """We could not ASK the staged interpreter what it can import.

    This used to be swallowed: `importable_in()` returned an empty set when the
    probe failed to run, and an empty set means "nothing is importable", which
    means "every module is missing" — a tooling failure of ours, dressed up as
    the project's fault. Not knowing is not the same as knowing the worst.
    """


@dataclass(frozen=True)
class ImportSite:
    """One import statement, and whether the app dies without it."""
    module: str            # dotted name as written, e.g. "google.protobuf"
    path: Path
    line: int
    scope: str

    @property
    def top(self) -> str:
        return self.module.split(".")[0]

    @property
    def required(self) -> bool:
        return self.scope == MODULE_SCOPE

    def where(self, root: Path | None = None) -> str:
        shown = self.path
        if root is not None:
            try:
                shown = self.path.relative_to(root)
            except ValueError:
                pass
        return f"{shown.as_posix()}:{self.line}({_SCOPE_LABEL[self.scope]})"


@dataclass
class MissingReport:
    """What the app imports but nothing provides.

    `required` is a hard failure — the module is imported at module level, so the
    very first render raises ModuleNotFoundError. `optional` is a warning: a lazy
    or guarded import that a running app can live without.
    """
    required: list[str] = field(default_factory=list)
    optional: list[str] = field(default_factory=list)
    sites: dict[str, list[str]] = field(default_factory=dict)

    # Older callers do `if missing:` / `for name in missing:` and mean the hard
    # failures. Keep them honest instead of accidentally truthy.
    def __bool__(self) -> bool:
        return bool(self.required)

    def __iter__(self):
        return iter(self.required)

    def __len__(self) -> int:
        return len(self.required)

    def where(self, name: str) -> str:
        return "、".join(self.sites.get(name, [])) or "(找不到位置)"

    def failure_message(self) -> str:
        """What the operator reads when the build stops. Not an accusation: the
        module, where it is imported from, and the two ways out."""
        lines = ["這些模組在 App 啟動時就會被 import,但相依宣告裡沒有:"]
        for name in self.required:
            hint = suggest_distribution(name)
            extra = f"(套件名可能是 {hint})" if hint and hint != name else ""
            lines.append(f"  · {name}{extra}")
            lines.append(f"      import 位置:{self.where(name)}")
        lines += [
            "",
            "兩條路,擇一即可:",
            "  1. 加進 requirements:把它寫進 requirements.txt / requirements.lock.txt,"
            "或 pyproject 的 [project].dependencies,再重新建置。",
            "  2. 這是選用相依,請忽略:如果 App 沒有它也能跑,把該 import 移到函式內"
            "(用到才 import),或用 try/except ImportError 包起來——這樣它就只會是警告。",
        ]
        return "\n".join(lines)

    def warning_lines(self) -> list[str]:
        """Optional imports: worth saying out loud, never worth failing on."""
        out = []
        for name in self.optional:
            out.append(f"選用相依「{name}」沒有宣告,但只在 {self.where(name)} 用到,"
                       "不會擋住啟動;若那條路徑要能用,請把它加進 requirements。")
        return out


# ── parsing ──────────────────────────────────────────────────────────────────

def _catches_import_error(node: ast.Try) -> bool:
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


# Reading + parsing the same 87 files twice (once to follow the imports, once to
# classify them) cost 2 seconds on AI4BI, on every click of 「檢查專案」. Keyed by
# mtime+size, so an edited file is never served from a stale cache.
_SITES_CACHE: dict[tuple[str, int, int], list[ImportSite]] = {}


def import_sites(path: Path) -> list[ImportSite]:
    """Every import in one file, classified by the scope it sits in."""
    path = Path(path)
    try:
        stat = path.stat()
    except OSError:
        return []
    key = (str(path), stat.st_mtime_ns, stat.st_size)
    cached = _SITES_CACHE.get(key)
    if cached is not None:
        return cached
    if len(_SITES_CACHE) > 5000:                  # a GUI lives for days
        _SITES_CACHE.clear()
    sites = _parse_import_sites(path)
    _SITES_CACHE[key] = sites
    return sites


def _parse_import_sites(path: Path) -> list[ImportSite]:
    try:
        tree = ast.parse(path.read_text("utf-8", errors="replace"), filename=str(path))
    except (SyntaxError, ValueError, OSError):
        return []

    found: list[ImportSite] = []

    def visit(node: ast.AST, scope: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.Import):
                for alias in child.names:
                    found.append(ImportSite(alias.name, path, child.lineno, scope))
            elif isinstance(child, ast.ImportFrom):
                if child.level == 0 and child.module:      # relative = first-party
                    found.append(ImportSite(child.module, path, child.lineno, scope))
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                # Only runs when someone calls it: it cannot break the first render.
                # A method inside a class is still a function — say so, or the
                # operator goes looking for a class-body import that is not there.
                visit(child, GUARDED_SCOPE if scope == GUARDED_SCOPE else FUNCTION_SCOPE)
            elif isinstance(child, ast.ClassDef):
                visit(child, scope if scope in _OPTIONAL_SCOPES else CLASS_SCOPE)
            elif isinstance(child, ast.Try) and _catches_import_error(child):
                # Body AND handlers: `except ImportError: import simplejson as json`
                # is the fallback, not a second requirement.
                visit(child, GUARDED_SCOPE)
            else:
                # Module-level if/with/try(not import-guarded)/... still runs on
                # import, so anything inside keeps the scope it inherited.
                visit(child, scope)

    visit(tree, MODULE_SCOPE)
    return found


@lru_cache(maxsize=8192)
def _module_file_cached(project_dir: str, entry_dir: str, dotted: str) -> Path | None:
    parts = dotted.split(".")
    for base in (Path(entry_dir), Path(project_dir), Path(project_dir) / "src"):
        candidate = base.joinpath(*parts)
        for path in (candidate.with_suffix(".py"), candidate / "__init__.py"):
            if path.is_file():
                return path
    return None


def _module_file(project_dir: Path, entry_dir: Path, dotted: str) -> Path | None:
    """Where a project-local module name actually lives, if it does.

    Memoized: this is asked once per import statement per pass, six stat() calls
    a time, and the answer cannot change during one check.
    """
    return _module_file_cached(str(project_dir), str(entry_dir), dotted)


def runtime_sources(project_dir: Path, entrypoint: Path) -> list[Path]:
    """Every project file the app can actually reach, by following its imports.

    Reachability, not folder names: CV_Viewer keeps a playwright import in
    `verify/`, experiments in `spike/`, tests in `conftest.py`; AI4BI has a
    playwright helper inside its own package. No blacklist survives the next
    project's naming — but "what does the entry script import, and what do those
    import" is exactly the question, and it has an exact answer.
    """
    project_dir, entrypoint = Path(project_dir), Path(entrypoint)
    entry_dir = entrypoint.parent
    seen: set[Path] = set()
    queue = [entrypoint]
    while queue:
        path = queue.pop()
        if path in seen or not path.is_file():
            continue
        seen.add(path)
        for site in import_sites(path):
            local = _module_file(project_dir, entry_dir, site.module)
            if local is not None and local not in seen:
                queue.append(local)
    return sorted(seen)


def classify(project_dir: Path, entrypoint: Path) -> tuple[dict[str, list[ImportSite]],
                                                           dict[str, list[ImportSite]]]:
    """(required, optional): third-party top-level import name -> where it is imported.

    A name is REQUIRED as soon as one reachable file imports it at module level;
    the same name imported lazily somewhere else does not soften that.
    """
    # Fresh view of the filesystem for every check: _module_file also caches
    # "this is NOT a project module", and a GUI that lives for days must not keep
    # calling a file the operator has just added a third-party dependency.
    _module_file_cached.cache_clear()
    project_dir, entrypoint = Path(project_dir), Path(entrypoint)
    entry_dir = entrypoint.parent
    local = local_module_names(project_dir)
    stdlib = set(sys.stdlib_module_names) | {"__future__"}

    required: dict[str, list[ImportSite]] = {}
    optional: dict[str, list[ImportSite]] = {}
    for path in runtime_sources(project_dir, entrypoint):
        for site in import_sites(path):
            top = site.top
            if top in stdlib or top in local:
                continue
            if _module_file(project_dir, entry_dir, site.module) is not None:
                continue                     # the project provides it itself
            bucket = required if site.required else optional
            bucket.setdefault(top, []).append(site)
    for name in required:                    # required wins over optional
        optional.pop(name, None)
    return required, optional


def top_level_imports(project_dir: Path, entrypoint: Path) -> set[str]:
    """The third-party modules the app needs to survive its first render."""
    required, _optional = classify(project_dir, entrypoint)
    return set(required)


def optional_imports(project_dir: Path, entrypoint: Path) -> set[str]:
    """Lazy / guarded imports: nice to have, never a reason to fail a build."""
    _required, optional = classify(project_dir, entrypoint)
    return set(optional)


def local_module_names(project_dir: Path) -> set[str]:
    """Modules the project provides itself — never a missing dependency.

    os.walk with pruning, not rglob: rglob descends into `.git` and `node_modules`
    and only filters afterwards, which cost 3.4 seconds on AI4BI — paid on every
    click of 「檢查專案」, to look at files we were always going to throw away.
    """
    project_dir = Path(project_dir)
    local: set[str] = set()
    for dirpath, dirnames, filenames in os.walk(project_dir):
        dirnames[:] = [name for name in dirnames
                       if name not in EXCLUDED_DIRS and not name.startswith(".")]
        here = Path(dirpath)
        for name in dirnames:
            if (here / name / "__init__.py").is_file():
                local.add(name)
        for name in filenames:
            if name.endswith(".py"):
                local.add(name[:-3])
    return local


# ── what the declarations provide ────────────────────────────────────────────

def _normalize(name: str) -> str:
    return name.strip().lower().replace("_", "-")


def declared_distributions(requirements_text: str) -> set[str]:
    """The distribution names a requirements/pyproject dependency list declares."""
    from . import requirements as requirements_mod

    names = set()
    for raw in requirements_text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        name = requirements_mod.distribution_name(line)
        if name:
            names.add(_normalize(name))
    return names


def candidate_distributions(dotted: str) -> tuple[str, ...]:
    """Which distribution names would satisfy this import name."""
    top = dotted.split(".")[0]
    for key in (dotted, top):
        if key in _KNOWN_ALIASES:
            return tuple(_normalize(n) for n in _KNOWN_ALIASES[key])
    return (_normalize(top),)


def suggest_distribution(dotted: str) -> str:
    candidates = candidate_distributions(dotted)
    return candidates[0] if candidates else ""


def _sites_of(name: str, buckets: dict[str, list[ImportSite]], root: Path) -> list[str]:
    return [site.where(root) for site in buckets.get(name, [])]


def missing_from_lock(entrypoint: Path, project_dir: Path,
                      requirements_text: str) -> MissingReport:
    """The same gate, answered from the DECLARATIONS alone — no interpreter, no
    pip, no I/O beyond reading the project's own .py files.

    This is what 「檢查專案」 runs: the operator learns in about a second that
    `duckdb` is not declared, instead of after a six-minute pip install.
    """
    project_dir, entrypoint = Path(project_dir), Path(entrypoint)
    required, optional = classify(project_dir, entrypoint)
    declared = declared_distributions(requirements_text)

    def unsatisfied(names) -> list[str]:
        return sorted(name for name in names
                      if not (set(candidate_distributions(name)) & declared))

    report = MissingReport(required=unsatisfied(required), optional=unsatisfied(optional))
    for name in report.required:
        report.sites[name] = _sites_of(name, required, project_dir)
    for name in report.optional:
        report.sites[name] = _sites_of(name, optional, project_dir)
    return report


# ── the post-install proof ───────────────────────────────────────────────────

def importable_in(python: Path, names: set[str]) -> set[str]:
    """Ask the STAGED runtime which of these it can actually import."""
    if not names:
        return set()
    script = (
        "import importlib.util, sys, json\n"
        "ok = []\n"
        "for n in json.loads(sys.argv[1]):\n"
        "    try:\n"
        "        if importlib.util.find_spec(n) is not None:\n"
        "            ok.append(n)\n"
        "    except Exception:\n"
        "        pass\n"
        "print(json.dumps(ok))\n"
    )
    try:
        proc = subprocess.run([str(python), "-c", script, json.dumps(sorted(names))],
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", check=False)
    except OSError as exc:
        raise ImportProbeError(
            f"無法執行交付包裡的 Python 來檢查 import:{python}({exc})") from exc
    if proc.returncode != 0:
        raise ImportProbeError(
            f"用交付包裡的 Python 檢查 import 時失敗(exit {proc.returncode}):{python}\n"
            f"{(proc.stderr or '').strip()[:500]}")
    try:
        return set(json.loads(proc.stdout.strip() or "[]"))
    except ValueError as exc:
        raise ImportProbeError(
            f"檢查 import 的子程序回了看不懂的東西:{proc.stdout.strip()[:200]!r}") from exc


def missing_dependencies(entrypoint: Path, project_dir: Path,
                         python: Path) -> MissingReport:
    """Imports the app makes that the PACKAGED runtime cannot satisfy.

    The proof after the install, where `missing_from_lock()` is the prediction
    before it: a distribution can be declared and still not import (wrong ABI, a
    wheel that quietly failed), and only the staged interpreter knows.
    """
    entrypoint, project_dir = Path(entrypoint), Path(project_dir)
    # Tolerate the two call orders: this used to be (project_dir, entrypoint, ...)
    # and a silent argument swap here would report the whole project as missing.
    if entrypoint.is_dir() and project_dir.is_file():
        entrypoint, project_dir = project_dir, entrypoint

    required, optional = classify(project_dir, entrypoint)
    wanted = set(required) | set(optional)
    if not wanted:
        return MissingReport()

    available = importable_in(python, wanted)          # raises if it cannot tell
    report = MissingReport(
        required=sorted(set(required) - available),
        optional=sorted(set(optional) - available),
    )
    for name in report.required:
        report.sites[name] = _sites_of(name, required, project_dir)
    for name in report.optional:
        report.sites[name] = _sites_of(name, optional, project_dir)
    return report
