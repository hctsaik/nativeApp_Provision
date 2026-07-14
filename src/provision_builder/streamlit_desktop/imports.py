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

The rule, therefore, is about SCOPE — but the scope that matters is "does this run
on the first render", not "is it lexically indented":

    module level (incl. module-level `if` / `with` / `try` blocks)
        REQUIRED — it runs on the first render; if it is missing, the app dies.

    inside a def that THE MODULE BODY CALLS — `_setup()` at the bottom of the file,
    a `main()` invoked at top level, a `@register` decorator
        REQUIRED. This is the hole the first version of the rule left: "imports
        inside a def are lazy" is only true of a def nobody calls at import time.
        If the module body calls it, its body runs while Streamlit is importing the
        script, and a missing package there is a red traceback on the first render —
        indistinguishable from a module-level import, except that the gate could not
        see it. Same file, one level deep (see `_called_from_module_scope` for what
        that deliberately does not cover).

    inside any other def / async def / class body
    inside a try/except ImportError guard (its body AND its fallbacks)
        OPTIONAL — reported as a warning, never fails a build.

The second trap is what we compare against. "Not in the requirements file" is not
the same as "will not be installed": `pyproject [project].dependencies` and a
hand-written requirements.txt name DIRECT dependencies only. AI4BI declares
pandas and never numpy, its `rfm.py` imports numpy at module level, and numpy
arrives anyway — pandas drags it in. Refusing that build was a guess dressed up
as a fact, and it made a perfectly good project unbuildable.

So the gate also has a SOURCE:

    a fully-pinned lock (every line `name==version` — what `pip freeze` emits,
    and what Store mode demands) IS the transitive closure. A name that is not in
    it will not be installed → hard failure, before the six-minute pip install.

    anything looser only lists what the author typed. A name that is not in it
    MAY still arrive transitively → warning, and the build goes on. The proof is
    `missing_dependencies()` against the staged interpreter after the install:
    that one knows, and it is still there to fail on.

The third trap is WHERE WE LOOK. Reachability is "what does the entry script
import, and what do those import" — and Streamlit's `pages/*.py` are imported by
NOBODY: Streamlit loads them itself, by convention. Seeding the closure with the
entrypoint alone made this gate blind to every multipage app's pages, so an
`import zzz_nope` in `pages/2_report.py` produced blocking=[] warnings=[] and the
build shipped. `pages.py` — shared with the device-side launcher, which had the
rule right — is the ONE answer to "what does Streamlit actually load", and both
sides now seed from it.

The fourth trap is our own ALIAS TABLE. To decide whether `import grpc` is
declared, you have to know that grpc comes from the `grpcio` distribution — and
the first version answered that from a hand-written dict, falling through to "the
import name must be the package name" whenever the dict was silent. That is wrong
for grpc/grpcio, psycopg2/psycopg2-binary, Levenshtein/python-Levenshtein — real
packages, correctly declared, correctly installed, and reported as MISSING. A
false MISSING here does not cost six minutes; it makes the project unbuildable.

So we ask the interpreter first (`importlib.metadata.packages_distributions()`,
stdlib, no network, reads the installed packages' own manifests), fall back to the
table, and when we are down to the identity guess we DOWNGRADE TO A WARNING rather
than block — see `satisfied_by`. The post-install `find_spec` probe is ground truth
and it is still there. A gate that guesses must fail OPEN.
"""

from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from . import pages as pages_mod
from .models import EXCLUDED_DIRS

# import name -> the distribution(s) that provide it, where they differ. A tuple,
# because `cv2` is equally satisfied by opencv-python, -headless or -contrib, and
# demanding the exact one we happen to know would be a false alarm of its own.
#
# This table is the FALLBACK, not the first answer: `_metadata_distributions()`
# asks the interpreter, which cannot be out of date the way a hand-written list
# always is. Everything here is a name the build machine may not have installed —
# and a name missing from BOTH is a guess, which by `resolve_distributions()` can
# only ever warn, never block.
_KNOWN_ALIASES: dict[str, tuple[str, ...]] = {
    "cv2": ("opencv-python", "opencv-python-headless", "opencv-contrib-python",
            "opencv-contrib-python-headless"),
    "PIL": ("pillow",),
    # Each of these was a false MISSING on a project that declared the package and
    # had it installed: the import name is simply not the distribution name, and the
    # old code fell through to "they must be equal".
    "Levenshtein": ("levenshtein", "python-levenshtein"),
    "psycopg2": ("psycopg2", "psycopg2-binary", "psycopg2cffi"),
    "psycopg": ("psycopg", "psycopg-binary"),
    "grpc": ("grpcio",),
    "grpc_status": ("grpcio-status",),
    "grpc_tools": ("grpcio-tools",),
    "MySQLdb": ("mysqlclient",),
    "pymysql": ("pymysql",),
    "redis": ("redis",),
    "magic": ("python-magic", "python-magic-bin"),
    "Crypto": ("pycryptodome", "pycryptodomex"),
    "Cryptodome": ("pycryptodomex",),
    "lxml": ("lxml",),
    "regex": ("regex",),
    "ruamel": ("ruamel-yaml",),
    "zmq": ("pyzmq",),
    "cairo": ("pycairo",),
    "gi": ("pygobject",),
    "wx": ("wxpython",),
    "Xlib": ("python-xlib",),
    "slugify": ("python-slugify",),
    "multipart": ("python-multipart",),
    "jose": ("python-jose",),
    "snappy": ("python-snappy",),
    "memcache": ("python-memcached",),
    "tkcalendar": ("tkcalendar",),
    "sqlalchemy": ("sqlalchemy",),
    "google.oauth2": ("google-auth",),
    "googleapiclient": ("google-api-python-client",),
    "OpenGL": ("pyopengl",),
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
CALLED_SCOPE = "module-called"   # required: a def whose body the MODULE BODY runs
FUNCTION_SCOPE = "function"      # optional: only runs when someone calls it
CLASS_SCOPE = "class"            # optional (per spec: never a startup crash we own)
GUARDED_SCOPE = "guarded"        # optional: try/except ImportError = degrade gracefully

_REQUIRED_SCOPES = (MODULE_SCOPE, CALLED_SCOPE)
_OPTIONAL_SCOPES = (FUNCTION_SCOPE, CLASS_SCOPE, GUARDED_SCOPE)

_SCOPE_LABEL = {
    MODULE_SCOPE: "模組層級 import",
    CALLED_SCOPE: "函式內 import,但這個函式在模組層被呼叫(啟動時就會執行)",
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
    # `from ai4bi.ui import workspace, viewer` -> ("workspace", "viewer"). Needed
    # because those may be MODULES, not attributes: without them we followed
    # ai4bi/ui/__init__.py and never opened workspace.py, so whatever it imports at
    # module level was invisible to a gate whose whole job is to look.
    names: tuple[str, ...] = ()

    @property
    def top(self) -> str:
        return self.module.split(".")[0]

    @property
    def required(self) -> bool:
        return self.scope in _REQUIRED_SCOPES

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

    `required` is a module-level import that nothing in the source we compared
    against provides. Whether that is a HARD FAILURE depends on `complete`:

        complete=True  — we compared against the whole truth (a fully-pinned lock,
                         or the staged interpreter itself). Not there = not
                         installed = the first render dies. Blocking.

        complete=False — we compared against a list of DIRECT dependencies. Not
                         there = we do not know; it may well arrive transitively
                         (numpy via pandas). A warning, never a block.

    `optional` is always a warning: a lazy or guarded import a running app lives
    without.
    """
    required: list[str] = field(default_factory=list)
    optional: list[str] = field(default_factory=list)
    sites: dict[str, list[str]] = field(default_factory=dict)
    # True = the source is the transitive closure, so absence is proof of absence.
    # Defaults to True because the post-install probe (`missing_dependencies`) is
    # exactly that, and it is the caller that must opt into uncertainty.
    complete: bool = True
    # The distribution names the source declared — used to point at the package
    # that most likely drags a missing module in ("numpy 可能由 pandas 帶進來").
    declared: frozenset[str] = frozenset()
    # Required names we could not map to a distribution with any confidence, where
    # something declared looks like it might well provide them anyway
    # (`import psycopg2` + `psycopg2-binary==2.9.9`). We had a guess, and a guess
    # may not refuse a build — these warn and the post-install probe decides.
    unsure: list[str] = field(default_factory=list)
    # Where the declarations came from, and what the project can opt INTO. Both are
    # only here to make the advice true: telling someone to 「加進 requirements」
    # when their project has no requirements.txt and the package sits in a
    # pyproject optional group is advice that cannot be followed.
    source_label: str = ""
    optional_groups: dict[str, tuple[str, ...]] = field(default_factory=dict)

    @property
    def blocking(self) -> list[str]:
        """Modules that WILL be missing. The only reason to refuse a build."""
        if not self.complete:
            return []
        return [name for name in self.required if name not in self.unsure]

    @property
    def undeclared(self) -> list[str]:
        """Modules not named in a non-closure source: maybe transitive, maybe not.
        Worth saying; never worth blocking on."""
        return [] if self.complete else list(self.required)

    @property
    def unresolved(self) -> list[str]:
        """Modules absent from a CLOSURE that we still refuse to block on, because
        all we had was a guess at their distribution name."""
        return [name for name in self.unsure if name in self.required] if self.complete else []

    def group_for(self, name: str) -> str:
        """The pyproject optional-dependency group that declares this import's
        package ("llm" for anthropic), or "" — there is a GUI field for that group,
        and it is the only advice that actually works for this project's shape."""
        candidates = set(candidate_distributions(name))
        for group, dists in self.optional_groups.items():
            if candidates & {_normalize(d) for d in dists}:
                return group
        return ""

    def _how_to_declare(self, name: str) -> str:
        """The ONE sentence that tells this operator, with this project, what to add
        and where. Not a menu of everything a Python project could theoretically do."""
        group = self.group_for(name)

        # THE INSTALL SOURCE DECIDES. A fully-pinned lock IS the source: pip is run
        # against it and nothing else, so the 「選用相依群組」 field has no effect at
        # all — validate already warns 「你勾的選用群組這次不會生效」.
        #
        # Recommending that field anyway produced a tool that contradicts itself
        # inside a single check: 「請去勾 llm」 next to 「你勾的 llm 不會生效」. The
        # operator does what we asked, nothing happens, and we told them so in the
        # same breath. Whatever the package is declared in, when a lock is the
        # source the only thing that changes the outcome is a pin in that lock.
        if self.complete:
            where = self.source_label or "lock 檔"
            hint = f"(它在 pyproject.toml 的「{group}」群組裡)" if group else ""
            return (f"相依來源是{where},pip 只會照它安裝——"
                    f"「選用相依群組」欄位在這個模式下不會生效。{hint}\n"
                    f"    請把 {name} 釘死的版本加進那個 lock 檔(例:{name}==1.2.3),再重新建置。")

        if group:
            return (f"這個套件已經宣告在 pyproject.toml 的 "
                    f"[project.optional-dependencies] 的「{group}」群組裡,"
                    f"預設不會安裝。要帶它,請在「進階設定 → 選用相依群組」填 {group},"
                    "再重新建置。")
        if "pyproject" in self.source_label:
            return ("請把它加進 pyproject.toml 的 [project].dependencies"
                    "(若它只是某條路徑才需要,也可以放進 [project.optional-dependencies] "
                    "的某個群組,再於「進階設定 → 選用相依群組」勾選),再重新建置。")
        return ("請把它加進 requirements.txt / requirements.lock.txt,再重新建置。")

    # Older callers do `if missing:` / `for name in missing:` and mean the hard
    # failures. Keep them honest instead of accidentally truthy.
    def __bool__(self) -> bool:
        return bool(self.blocking)

    def __iter__(self):
        return iter(self.blocking)

    def __len__(self) -> int:
        return len(self.blocking)

    def where(self, name: str) -> str:
        return "、".join(self.sites.get(name, [])) or "(找不到位置)"

    def failure_message(self) -> str:
        """What the operator reads when the build stops. Not an accusation: the
        module, where it is imported from, and the ways out that are actually ways
        out.

        There used to be a third one: 「把 import 移到函式內」. It made the build
        pass, so it read like a fix — and it fixes nothing. Moving the import into a
        function only moves it where this gate cannot see it; the package is still
        not installed, the code path still runs, and the app still dies, now on the
        factory floor with a green build behind it. We were teaching the operator to
        disable the check instead of fixing the thing it found. It is gone.
        """
        lines = ["這些模組在 App 啟動時就會被 import,但相依宣告裡沒有:"]
        for name in self.blocking:
            hint = suggest_distribution(name)
            extra = f"(套件名可能是 {hint})" if hint and _normalize(hint) != _normalize(name) else ""
            lines.append(f"  · {name}{extra}")
            lines.append(f"      import 位置:{self.where(name)}")
        if self.source_label:
            lines.append("")
            lines.append(f"目前的相依來源:{self.source_label}")
        lines += ["", "兩條路,擇一即可:"]
        first = self.blocking[0] if self.blocking else ""
        lines.append(f"  1. 加進相依宣告:{self._how_to_declare(first)}")
        lines.append(
            "  2. 確認它真的是選用的:如果 App 少了它也能跑,請用 try/except ImportError "
            "把 import 包起來,讓程式碼自己說明「沒有它會怎麼降級」——這樣它就只會是警告。")
        return "\n".join(lines)

    def warning_lines(self) -> list[str]:
        """Everything worth saying out loud and nothing worth failing on: the
        lazy/guarded imports, the module-level ones that are probably someone else's
        transitive dependency, and the ones whose package name we could only guess."""
        out = []
        for name in self.undeclared:
            group = self.group_for(name)
            if group:
                # Not a "maybe it arrives transitively" at all: we know EXACTLY why
                # it is missing — the project declares it in an optional group that
                # nobody opted into. Guessing about carriers here would bury the one
                # action that works.
                out.append(
                    f"「{name}」不會被安裝(import 位置:{self.where(name)})。"
                    f"{self._how_to_declare(name)}")
                continue
            carrier = _likely_carrier(name, self.declared)
            via = f"{name} 可能由 {carrier} 帶進來" if carrier else f"{name} 可能由其它套件帶進來"
            out.append(
                f"「{name}」沒有列在相依宣告裡(import 位置:{self.where(name)})。"
                f"requirements.txt / pyproject 只宣告直接相依,{via};"
                "安裝完成後會再驗一次,真的缺才會擋下來。")
        for name in self.unresolved:
            near = "、".join(sorted(d for d in self.declared if _looks_related(name, d)))
            out.append(
                f"「{name}」的套件名我們認不出來(import 位置:{self.where(name)}),"
                f"但相依宣告裡的「{near}」看起來就是提供它的套件。"
                "import 名稱和套件名稱不一樣是很常見的事(grpc 來自 grpcio、"
                "psycopg2 來自 psycopg2-binary),我們不會因為自己猜不出來就擋下建置;"
                "安裝完成後會用交付包裡的 Python 實際 import 一次,真的缺才會擋。")
        for name in self.optional:
            out.append(f"選用相依「{name}」沒有宣告,但只在 {self.where(name)} 用到,"
                       f"不會擋住啟動;若那條路徑要能用,{self._how_to_declare(name)}")
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


def _is_main_guard(node: ast.If) -> bool:
    """`if __name__ == "__main__":` — the one module-level block we do NOT treat as
    "the module body runs this".

    Strictly speaking Streamlit DOES set `__name__ == "__main__"` on the entry
    script, so this block really does run there. We skip it anyway, on purpose:
    promoting its calls would make every `main()`-style script's lazy imports hard
    requirements, and a wrong REQUIRED refuses a build that works. Skipping keeps
    those imports optional — a warning — and the post-install probe still sees them.
    Fail open, and say why.
    """
    return any(isinstance(sub, ast.Name) and sub.id == "__name__"
               for sub in ast.walk(node.test))


def _called_from_module_scope(tree: ast.Module) -> set[str]:
    """Names of this file's own functions that the MODULE BODY invokes.

    `_setup()` at the bottom of the file, `main()` called at top level, `@register`
    on a module-level def — all of them run while Streamlit is importing the script,
    so an import in their body executes on the first render exactly like a
    module-level import. Calling those "lazy" is how a missing dependency reaches
    the factory floor with a green build behind it.

    What this deliberately does NOT cover (one level, same file, by design):
      · a call two hops deep — module calls `main()`, `main()` calls `_setup()`:
        `_setup()`'s imports stay optional.
      · methods — `App().boot()` at module scope does not promote `boot`.
      · a decorator that CALLS the function it decorates (`@run_now def _setup()`):
        we see that `run_now` runs, not that `_setup` does.
      · anything imported from another module and called here.
    Each of those stays OPTIONAL, i.e. a warning — the safe direction. The
    post-install `find_spec` probe is still behind all of them.
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


def _parse_import_sites(path: Path) -> list[ImportSite]:
    try:
        tree = ast.parse(path.read_text("utf-8", errors="replace"), filename=str(path))
    except (SyntaxError, ValueError, OSError):
        return []

    found: list[ImportSite] = []
    runs_on_import = _called_from_module_scope(tree)

    def visit(node: ast.AST, scope: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.Import):
                for alias in child.names:
                    found.append(ImportSite(alias.name, path, child.lineno, scope))
            elif isinstance(child, ast.ImportFrom):
                if child.level == 0 and child.module:      # relative = first-party
                    found.append(ImportSite(child.module, path, child.lineno, scope,
                                            tuple(alias.name for alias in child.names)))
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if scope == MODULE_SCOPE and child.name in runs_on_import:
                    # The module body calls it, so its body runs on the first render
                    # just like a module-level import. Not lazy — REQUIRED.
                    # Nested defs / try-ImportError blocks inside it fall back to the
                    # optional scopes below, which is what "one level deep" means.
                    visit(child, CALLED_SCOPE)
                else:
                    # Only runs when someone calls it: it cannot break the first
                    # render. A method inside a class is still a function — say so,
                    # or the operator goes looking for a class-body import that is
                    # not there.
                    visit(child, GUARDED_SCOPE if scope == GUARDED_SCOPE else FUNCTION_SCOPE)
            elif isinstance(child, ast.ClassDef):
                visit(child, scope if scope in _OPTIONAL_SCOPES else CLASS_SCOPE)
            elif isinstance(child, ast.Try) and _catches_import_error(child):
                # Body AND handlers: `except ImportError: import simplejson as json`
                # is the fallback, not a second requirement. True inside a
                # module-called function too: it degrades gracefully either way.
                visit(child, GUARDED_SCOPE)
            else:
                # Module-level if/with/try(not import-guarded)/... still runs on
                # import, so anything inside keeps the scope it inherited.
                visit(child, scope)

    visit(tree, MODULE_SCOPE)
    return found


@lru_cache(maxsize=8192)
def _module_file_cached(roots: tuple[str, ...], dotted: str) -> Path | None:
    parts = dotted.split(".")
    for base in roots:
        candidate = Path(base).joinpath(*parts)
        for path in (candidate.with_suffix(".py"), candidate / "__init__.py"):
            if path.is_file():
                return path
    return None


def _module_file(roots: tuple[str, ...], dotted: str) -> Path | None:
    """Where a project-local module name actually lives, if it does.

    Memoized: this is asked once per import statement per pass, six stat() calls
    a time, and the answer cannot change during one check.
    """
    return _module_file_cached(roots, dotted)


def _module_roots(project_dir: Path, entrypoint: Path) -> tuple[str, ...]:
    """Every directory a first-party module name may resolve in.

    The entry script's own folder (that is what `streamlit run` puts on sys.path),
    the project root, its `src/` — and the `pages/` folder, because a `.py` next
    to a page is a page's helper, not a package to pip install. Without it,
    `import shared_bits` inside pages/1_home.py is reported as a missing PyPI
    distribution; launch.py hit exactly that and the fix now lives in pages.py,
    once, for both sides.
    """
    roots = [Path(entrypoint).parent, Path(project_dir), Path(project_dir) / "src"]
    roots += pages_mod.first_party_roots(entrypoint, project_dir)
    ordered: list[str] = []
    for root in roots:
        text = str(root)
        if text not in ordered:
            ordered.append(text)
    return tuple(ordered)


def runtime_sources(project_dir: Path, entrypoint: Path) -> list[Path]:
    """Every project file the app can actually reach, by following its imports.

    Reachability, not folder names: CV_Viewer keeps a playwright import in
    `verify/`, experiments in `spike/`, tests in `conftest.py`; AI4BI has a
    playwright helper inside its own package. No blacklist survives the next
    project's naming — but "what does the entry script import, and what do those
    import" is exactly the question, and it has an exact answer.

    …as long as you start in the right place. The walk is seeded with the
    entrypoint AND with every page Streamlit runs on its own (pages.seed_scripts):
    nothing imports `pages/2_report.py`, so an import walk that starts at the entry
    script alone never opens the file — and the operator meets its missing
    dependency as a red box, after the build said 「檢查通過」.
    """
    project_dir, entrypoint = Path(project_dir), Path(entrypoint)
    roots = _module_roots(project_dir, entrypoint)
    seen: set[Path] = set()
    queue = list(pages_mod.seed_scripts(entrypoint, project_dir))
    while queue:
        path = queue.pop()
        if path in seen or not path.is_file():
            continue
        seen.add(path)
        for site in import_sites(path):
            # `from ai4bi.ui import workspace` reaches ai4bi/ui/workspace.py, not
            # just ai4bi/ui/__init__.py — AI4BI's app.py does exactly this, and the
            # module-level imports of every file behind such a line were being
            # skipped. Following the package alone is how a gate goes quiet.
            dotted = [site.module] + [f"{site.module}.{name}" for name in site.names]
            for name in dotted:
                local = _module_file(roots, name)
                if local is not None and local not in seen:
                    queue.append(local)
    return sorted(seen)


def classify(project_dir: Path, entrypoint: Path) -> tuple[dict[str, list[ImportSite]],
                                                           dict[str, list[ImportSite]]]:
    """(required, optional): third-party top-level import name -> where it is imported.

    A name is REQUIRED as soon as one reachable file imports it at module level;
    the same name imported lazily somewhere else does not soften that. "Reachable"
    includes the app's pages — see runtime_sources.
    """
    # Fresh view of the filesystem for every check: _module_file also caches
    # "this is NOT a project module", and a GUI that lives for days must not keep
    # calling a file the operator has just added a third-party dependency.
    _module_file_cached.cache_clear()
    project_dir, entrypoint = Path(project_dir), Path(entrypoint)
    roots = _module_roots(project_dir, entrypoint)
    local = local_module_names(project_dir)
    stdlib = set(sys.stdlib_module_names) | {"__future__"}

    required: dict[str, list[ImportSite]] = {}
    optional: dict[str, list[ImportSite]] = {}
    for path in runtime_sources(project_dir, entrypoint):
        for site in import_sites(path):
            top = site.top
            if top in stdlib or top in local:
                continue
            if _module_file(roots, site.module) is not None:
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

# Who famously drags whom in. Only used to make a warning concrete — "numpy 可能由
# pandas 帶進來" is a sentence the operator can check in five seconds, where "可能
# 由其它套件帶進來" leaves them nothing to look at. Never used to decide anything.
_TRANSITIVE_CARRIERS: dict[str, tuple[str, ...]] = {
    "numpy": ("pandas", "scipy", "matplotlib", "pyarrow", "scikit-learn",
              "opencv-python", "streamlit"),
    "pandas": ("streamlit",),
    "pyarrow": ("streamlit", "pandas"),
    "altair": ("streamlit",),
    "pil": ("streamlit", "matplotlib"),
    "packaging": ("streamlit", "matplotlib"),
    "jinja2": ("streamlit", "flask"),
    "click": ("streamlit", "flask"),
    "tornado": ("streamlit",),
    "requests": ("streamlit",),
    "urllib3": ("requests",),
    "certifi": ("requests",),
    "charset-normalizer": ("requests",),
    "idna": ("requests",),
    "dateutil": ("pandas", "matplotlib"),
    "pytz": ("pandas",),
    "tzdata": ("pandas",),
    "attr": ("jsonschema",),
    "jsonschema": ("altair", "streamlit"),
    "yaml": ("uvicorn",),
    "typing-extensions": ("pydantic", "streamlit"),
    "pydantic": ("fastapi",),
    "et-xmlfile": ("openpyxl",),
    "openpyxl": ("pandas",),
    "matplotlib": ("seaborn",),
    "scipy": ("scikit-learn", "seaborn"),
    "joblib": ("scikit-learn",),
    "protobuf": ("streamlit",),
    "pyparsing": ("matplotlib",),
    "six": ("python-dateutil",),
}


def _likely_carrier(dotted: str, declared: frozenset[str]) -> str:
    """A declared package that plausibly pulls `dotted` in, or "" if we cannot
    name one. Honest by construction: only ever names something the project
    ACTUALLY declared."""
    if not declared:
        return ""
    key = _normalize(dotted.split(".")[0])
    for carrier in _TRANSITIVE_CARRIERS.get(key, ()):
        if _normalize(carrier) in declared:
            return carrier
    return ""


# `name==1.2.3`, extras and an environment marker tolerated. Anything else — a
# range, a bare name, a URL, `-e .` — means the file was written by a human and
# lists only what they thought of, not what pip will end up installing.
_PIN_LINE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]*(?:\[[A-Za-z0-9,._\s-]+\])?\s*==\s*[^\s;]+"
    r"(?:\s*;.*)?$")
# pip-compile writes `numpy==2.4.6 \` + `    --hash=sha256:…` continuation lines.
# That is a closure too — reading it as "not a lock" would quietly disarm the gate
# for every project that locks the careful way.
_HASH_FRAG = re.compile(r"\s+--hash=\S+")


def is_pinned_closure(requirements_text: str) -> bool:
    """True when the requirements text is a fully-pinned lock — every dependency
    line is `name==version`, i.e. what `pip freeze` / `pip-compile` produces.

    That is the only shape whose ABSENCES mean anything. A `pyproject`
    `[project].dependencies` list and a hand-written requirements.txt name direct
    dependencies only: numpy is missing from AI4BI's pyproject and installs
    anyway, because pandas needs it. Calling that "will be missing" blocks a build
    that works.
    """
    from . import requirements as requirements_mod       # circular at module level

    saw_pin = False
    for raw in requirements_text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if line.startswith("--hash"):                    # a hash continuation line
            continue
        line = _HASH_FRAG.sub("", line.rstrip("\\").strip()).strip()
        if not line:
            continue
        if _PIN_LINE.match(line):
            saw_pin = True
            continue
        # pip's own plumbing is stripped before install, so a `pip @ file:///…`
        # line from `pip freeze --all` must not demote a real lock to a guess.
        if requirements_mod.distribution_name(line) in requirements_mod.PLUMBING:
            continue
        return False                       # a range, a bare name, a URL, `-e .`
    return saw_pin


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


@lru_cache(maxsize=1)
def _metadata_distributions() -> dict[str, tuple[str, ...]]:
    """import name -> the distributions that ACTUALLY provide it, asked of the
    interpreter instead of guessed.

    `importlib.metadata.packages_distributions()` reads the installed
    distributions' own file lists, so it knows what no hand-written table can keep
    up with: that `grpc` comes from grpcio, `cv2` from whichever opencv build is
    installed, `psycopg2` from psycopg2-binary. It is stdlib (3.10+) and needs no
    network — the build machine has it by definition, because it is running us.

    Its limit is honest and worth stating: it only sees what is installed HERE.
    A package the build machine does not have is simply absent from the answer,
    which is why `resolve_distributions()` treats "not in here" as "ask the table",
    and "not in the table either" as "we are guessing" — never as "it is missing".
    """
    try:
        from importlib.metadata import packages_distributions
        raw = packages_distributions()
    except Exception:                      # a broken/partial site-packages, mostly
        return {}
    return {name: tuple(_normalize(d) for d in dists) for name, dists in raw.items()}


@lru_cache(maxsize=1)
def _metadata_provides() -> dict[str, frozenset[str]]:
    """The same truth, read backwards: distribution -> the import names it provides.

    This is what answers 「the project declares opencv-contrib-python; does that
    give it `cv2`?」 without any table at all — as long as the build machine has
    the package. Forward and backward together are why the table only has to cover
    what is NOT installed here.
    """
    provides: dict[str, set[str]] = {}
    for name, dists in _metadata_distributions().items():
        for dist in dists:
            provides.setdefault(dist, set()).add(name)
    return {dist: frozenset(names) for dist, names in provides.items()}


@dataclass(frozen=True)
class DistributionMatch:
    """Which distributions could provide an import name, and how sure we are.

    `source` is the whole point. "metadata" and "table" are knowledge; "guess" is
    the old code's silent assumption that an import name IS a distribution name —
    true for numpy, false for `grpc` (grpcio), `psycopg2` (psycopg2-binary),
    `Levenshtein` (python-Levenshtein). A guess may warn. A guess may never block.
    """
    candidates: tuple[str, ...]
    source: str                    # "metadata" | "table" | "guess"

    @property
    def certain(self) -> bool:
        return self.source != "guess"


def resolve_distributions(dotted: str) -> DistributionMatch:
    """Which distribution names would satisfy this import name, and how we know.

    Order — most authoritative first:
      1. the interpreter (`packages_distributions()`): it reads the installed
         packages' own manifests and cannot be stale.
      2. `_KNOWN_ALIASES`: for packages the BUILD machine does not have installed,
         which is most of them — the build machine is not the app's venv.
      3. the identity guess (import name == distribution name). Right most of the
         time, and wrong exactly where it costs a build. Marked as a guess.

    1 and 2 are UNIONed rather than short-circuited on purpose: the build machine
    happening to have opencv-python installed must not turn a project that declares
    opencv-contrib-python into a false MISSING. More candidates can only make this
    gate more forgiving, and forgiving is the safe direction.
    """
    top = dotted.split(".")[0]
    found: list[str] = []
    for name in dict.fromkeys((dotted, top)):          # dotted first, dedup
        found += _metadata_distributions().get(name, ())
    from_metadata = bool(found)

    for key in (dotted, top):
        if key in _KNOWN_ALIASES:
            found += (_normalize(n) for n in _KNOWN_ALIASES[key])
            break

    if found:
        return DistributionMatch(tuple(dict.fromkeys(found)),
                                 "metadata" if from_metadata else "table")
    return DistributionMatch((_normalize(top),), "guess")


def candidate_distributions(dotted: str) -> tuple[str, ...]:
    """Which distribution names would satisfy this import name."""
    return resolve_distributions(dotted).candidates


def suggest_distribution(dotted: str) -> str:
    candidates = candidate_distributions(dotted)
    return candidates[0] if candidates else ""


def _looks_related(import_name: str, distribution: str) -> bool:
    """Could this declared distribution plausibly be the one providing this import?

    Only ever asked when we are already GUESSING (neither the interpreter nor the
    table knew), and only ever used to soften a block into a warning. The shapes it
    catches are the ones that actually bite: `psycopg2` / psycopg2-binary,
    `grpc` / grpcio, `Levenshtein` / python-Levenshtein, `serial` / pyserial — the
    distribution name is the import name with something bolted on.
    """
    imp, dist = _normalize(import_name), _normalize(distribution)
    if imp == dist:
        return True
    if len(imp) < 3 or len(dist) < 3:      # two-letter names match far too much
        return False
    return imp in dist or dist in imp


def satisfied_by(dotted: str, declared: frozenset[str] | set[str]) -> tuple[bool, bool]:
    """(satisfied, certain) — is this import name provided by something declared?

    `certain` is what decides whether a NO may stop a build. A gate that guesses
    must fail OPEN: a false positive here does not cost six minutes, it makes a
    perfectly good project impossible to build at all, with an error message
    telling the operator to add a package they already have.
    """
    match = resolve_distributions(dotted)
    if set(match.candidates) & set(declared):
        return True, True

    # The interpreter, read backwards: does a DECLARED distribution actually ship
    # this import name? Needs no table, and it is ground truth wherever the build
    # machine has the package.
    top = dotted.split(".")[0]
    provides = _metadata_provides()
    for dist in declared:
        if top in provides.get(dist, ()):
            return True, True

    if match.certain:
        return False, True                 # we KNOW what provides it; nothing does

    # All we ever had was "the import name must be the package name". If any
    # declared package even looks like it could be the real provider, we do not
    # know enough to refuse the build — say so and let the post-install find_spec
    # probe, which is ground truth, be the one that fails.
    related = any(_looks_related(top, dist) for dist in declared)
    return False, not related


def _sites_of(name: str, buckets: dict[str, list[ImportSite]], root: Path) -> list[str]:
    return [site.where(root) for site in buckets.get(name, [])]


def missing_from_lock(entrypoint: Path, project_dir: Path,
                      requirements_text: str, *, source_label: str = "",
                      optional_groups: dict[str, tuple[str, ...]] | None = None
                      ) -> MissingReport:
    """The same gate, answered from the DECLARATIONS alone — no interpreter, no
    pip, no I/O beyond reading the project's own .py files.

    This is what 「檢查專案」 runs: the operator learns in about a second that
    `duckdb` is not declared, instead of after a six-minute pip install.

    How much that second is worth depends on what they declared. Against a
    fully-pinned lock the answer is certain and the build stops here
    (`report.blocking`). Against a direct-dependency list it is a suspicion, and
    a suspicion gets a warning (`report.undeclared`) — see `is_pinned_closure`.
    """
    project_dir, entrypoint = Path(project_dir), Path(entrypoint)
    required, optional = classify(project_dir, entrypoint)
    declared = declared_distributions(requirements_text)

    unsure: list[str] = []

    def unsatisfied(names) -> list[str]:
        out = []
        for name in sorted(names):
            ok, certain = satisfied_by(name, declared)
            if ok:
                continue
            out.append(name)
            if not certain:
                # We guessed at the package name and something declared looks like
                # it. Report it, never block on it.
                unsure.append(name)
        return out

    report = MissingReport(required=unsatisfied(required), optional=unsatisfied(optional),
                           complete=is_pinned_closure(requirements_text),
                           declared=frozenset(declared),
                           source_label=source_label,
                           optional_groups=dict(optional_groups or {}))
    report.unsure = unsure
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
        # -B / PYTHONDONTWRITEBYTECODE: this probe runs the SHARED, immutable
        # runtime's own python.exe. Without them, importing json/importlib writes
        # stdlib __pycache__ INTO that runtime — after its files.json was already
        # computed. The runtime then no longer matches its own manifest, and every
        # machine we deliver it to rejects it as corrupt ("undeclared file:
        # Lib/encodings/__pycache__/cp950.cpython-311.pyc"). A read-only question
        # must not leave fingerprints on the thing it is asking about.
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1")
        proc = subprocess.run([str(python), "-B", "-c", script, json.dumps(sorted(names))],
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", check=False, env=env)
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
                         python: Path, *, source_label: str = "",
                         optional_groups: dict[str, tuple[str, ...]] | None = None
                         ) -> MissingReport:
    """Imports the app makes that the PACKAGED runtime cannot satisfy.

    The proof after the install, where `missing_from_lock()` is the prediction
    before it: a distribution can be declared and still not import (wrong ABI, a
    wheel that quietly failed), and only the staged interpreter knows.

    Nothing here is ever `unsure`: this asks `find_spec` inside the runtime we are
    about to ship, so no distribution-name guessing is involved. What it says is
    missing IS missing. `source_label`/`optional_groups` only shape the ADVICE —
    「在『進階設定 → 選用相依群組』填 llm」 beats 「請加進 requirements」 for a
    project whose package is sitting in a pyproject extra.
    """
    entrypoint, project_dir = Path(entrypoint), Path(project_dir)
    # Tolerate the two call orders: this used to be (project_dir, entrypoint, ...)
    # and a silent argument swap here would report the whole project as missing.
    if entrypoint.is_dir() and project_dir.is_file():
        entrypoint, project_dir = project_dir, entrypoint

    required, optional = classify(project_dir, entrypoint)
    wanted = set(required) | set(optional)
    if not wanted:
        return MissingReport(source_label=source_label,
                             optional_groups=dict(optional_groups or {}))

    available = importable_in(python, wanted)          # raises if it cannot tell
    report = MissingReport(
        required=sorted(set(required) - available),
        optional=sorted(set(optional) - available),
        # The staged interpreter IS the closure: whatever pip was going to drag in
        # is already on disk. Absence here is proof, and it blocks (complete=True).
        complete=True,
        source_label=source_label,
        optional_groups=dict(optional_groups or {}),
    )
    for name in report.required:
        report.sites[name] = _sites_of(name, required, project_dir)
    for name in report.optional:
        report.sites[name] = _sites_of(name, optional, project_dir)
    return report
