"""Data structures for the Streamlit desktop folder builder.

Kept free of Tkinter and of the builder itself so the GUI, the CLI and the
tests all speak the same vocabulary.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

SCHEMA_VERSION = 1
# 0 = no preference → the launcher picks a random FREE port in 8000–9000.
# A fixed 8501 default is how packaged apps collide with each other and with
# whatever Streamlit the developer left running.
DEFAULT_PREFERRED_PORT = 0
DEFAULT_STARTUP_TIMEOUT = 60

# Directories that are never part of a deliverable — split by DEPTH, because a
# bare name means two different things depending on where it sits.
#
# The rule, and the reason for it:
#
#   ANY DEPTH  — only names that cannot possibly BE the application. Every one of
#   these is either machine state (a VCS database, a virtualenv full of absolute
#   paths from the build box), a regenerable tool cache, or an install tree the
#   packaged runtime already replaces. None of them is ever read at run time by a
#   packaged Streamlit app, and none of them is a name a Python package can carry
#   (a leading dot or a hyphen is not an importable identifier; `__pycache__` is
#   reserved by CPython; nothing in a Python app opens `node_modules`). Dropping
#   one of these at depth cannot break an app — it can only make the package
#   smaller, which is the whole point: AI4BI's component carries a 200 MB nested
#   `node_modules/` that exists only to BUILD its frontend.
#
#   ROOT ONLY  — names that mean "the project's own build junk" at the project
#   root and mean something completely different one level down. `dist/` is the
#   case that cost us a delivered app: a Streamlit custom component ships its
#   COMPILED frontend in `<component>/frontend/dist/` and points
#   `components.declare_component(path=...)` straight at it. AI4BI's
#   `ai4bi/ui/components/field_well/frontend/dist/` IS the component. Deleting it
#   by name, at any depth, built a package that ran, reported success, and
#   rendered a blank box where the component should have been. Same story for
#   `build/` (a legitimate subpackage name), `vendor/` (vendored SOURCE that gets
#   imported), `wheels`/`wheelhouse` (a package's data directory), and
#   `venv`/`env` (`config/env/` is a normal thing to have). At the root they are
#   unambiguously the operator's own junk — CV_Viewer's root `wheels/` alone was
#   124 MB of .whl files the user's machine would never open. One level down we
#   have no business guessing, so we keep them: a package that is too big is a
#   complaint, a package that is missing its component is a broken delivery.
EXCLUDED_DIRS_ANY_DEPTH = (
    ".git", ".hg", ".svn",
    ".venv",
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    ".streamlit_cache",
    "node_modules",
    "site-packages",
)

EXCLUDED_DIRS_ROOT_ONLY = (
    "venv", "env",
    "wheels", "wheelhouse", "vendor",
    "dist", "build",
)

# Kept as the union so `x in EXCLUDED_DIRS` still answers "is this name ever junk"
# for the callers that only need a cheap name filter (imports.py's local-module
# walk). The DEPTH-aware decision lives in builder.ignore_reason() and nowhere
# else — that is the one that decides what travels.
EXCLUDED_DIRS = EXCLUDED_DIRS_ANY_DEPTH + EXCLUDED_DIRS_ROOT_ONLY

# Build-time artifacts, split by DEPTH for exactly the same reason the directories
# above are — and it is the same bug, one class up.
#
#   ANY DEPTH — files a running app can never be reading, wherever they sit. `*.pyc`
#   / `*.pyo` are regenerated from the source we are shipping beside them;
#   `*.egg-info` (a DIRECTORY, not a file) is install metadata; and a `.whl` is a
#   package waiting to be installed, never runtime data — nothing in a delivered app
#   opens a wheel, because its dependencies are already installed into runtime/.
#   CV_Viewer's root `wheels/` was 124 MB of them, and a nested `vendor/wheels/` is
#   the same 124 MB at a different address, which is why this one stays at depth.
#
#   ROOT ONLY — ARCHIVES. At the project root, `release.zip` / `dist.tar.gz` is
#   something a build left lying about: junk, and frequently 200 MB of it. NESTED, an
#   archive is DATA. `assets/data.zip`, `models/weights.tar.gz`,
#   `tests/fixtures/sample.7z` — the app OPENS THESE AT RUN TIME. Dropping them by
#   name at any depth is the `dist/` disaster again: the build reports success, the
#   package even runs on the BUILD machine (where the file is still sitting next to
#   the source we copied from), and it dies on the factory floor with a
#   FileNotFoundError naming a path that was there the whole time. A package that is
#   too big is a complaint; a package that lost its model bundle is a broken delivery.
#   An operator who really does keep a 200 MB dataset.zip under assets/ can say so:
#   `assets/*.zip` in .provisionignore. There is no way to say the reverse in time.
EXCLUDED_FILES_ANY_DEPTH = ("*.pyc", "*.pyo", "*.whl", "*.egg-info")
EXCLUDED_FILES_ROOT_ONLY = ("*.tar.gz", "*.zip", "*.7z")

# The union, for callers that only need the cheap "is this name ever junk" filter.
# The DEPTH-aware decision lives in builder.ignore_reason() and nowhere else.
EXCLUDED_FILES = EXCLUDED_FILES_ANY_DEPTH + EXCLUDED_FILES_ROOT_ONLY

# The project can add its own patterns, gitignore-style, without touching the GUI.
PROVISIONIGNORE = ".provisionignore"


# The slug of a name with no latin characters at all. It used to be this constant
# and nothing else, which made it a COLLISION: slugify('影像檢視器') and
# slugify('報表分析') both returned "streamlit-app", so two different programs got
# the same app_id (`app-streamlit-app`), the same store folder, the same start bat
# and the same manifest. The second build then looked like a version collision in
# the FIRST app — and the operator, following that message, bumped the version and
# replaced the production line's App A with a completely different program, under
# App A's name and entry point.
#
# The suffix is a digest of the display name, so it is deterministic (the same
# name always builds into the same app), and two different names practically never
# meet. It is not pretty — `app-streamlit-app-4f8c1e2a` is not a name anybody wants
# on a factory PC's start bat — which is exactly why store builds REFUSE it and ask
# for BuildRequest.app_id_override instead (store_builder._resolve_app_id). Here it
# is the floor: whatever else happens, two different apps never silently become one.
SLUG_FALLBACK = "streamlit-app"
_SLUG_HASH_LEN = 8

# An explicit app id: what store_builder/the GUI accept in BuildRequest.app_id_override.
# Same shape device/identifiers.py enforces on every path component, plus the `app-`
# prefix the portal keys its full-height-iframe rendering off.
APP_ID_RE = re.compile(r"^app-[a-z0-9][a-z0-9._-]{0,90}$")


def slug_is_derived(name: str) -> bool:
    """True when `name` carries no latin character we can build a slug out of, so
    slugify() had to fall back to a digest. Store builds refuse these (an opaque
    app_id ends up in folder names, in start-<app_id>.bat and in the operator's
    hands); fat builds accept them (one package, one folder, no shared namespace)."""
    return not re.search(r"[a-zA-Z0-9]", name or "")


def slugify(name: str) -> str:
    """A display name -> a filesystem- and tool_id-safe slug.

    A name with no latin characters gets `streamlit-app-<8 hex of the name>`, NOT a
    shared constant — see SLUG_FALLBACK.
    """
    name = (name or "").strip()
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", name.lower()).strip("-")
    if slug:
        return slug
    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:_SLUG_HASH_LEN]
    return f"{SLUG_FALLBACK}-{digest}"


def normalize_app_id(value: str | None) -> str | None:
    """An operator-typed app id -> the canonical form, or None when blank.

    Accepts 'image-viewer' as well as 'app-image-viewer': the `app-` prefix is a
    rendering contract, not something a person should have to remember. Anything
    that is still not a legal identifier is returned as-is, lowercased — the caller
    (store_builder._resolve_app_id) validates and reports it in the operator's terms
    rather than raising out of a dataclass constructor.
    """
    if value is None:
        return None
    text = str(value).strip().lower()
    if not text:
        return None
    return text if text.startswith("app-") else f"app-{text}"


def app_id_for(name: str) -> str:
    """Portal renders `app-*` tools as ONE full-height iframe with no chrome
    (engine.py::_derive_category), which is exactly what a packaged app wants."""
    return f"app-{slugify(name)}"


@dataclass
class BuildRequest:
    project_dir: Path
    entrypoint: Path
    display_name: str
    output_dir: Path
    shell_exe: Path
    runtime_template: Path
    version: str = "1.0.0"
    preferred_port: int = DEFAULT_PREFERRED_PORT
    startup_timeout_seconds: int = DEFAULT_STARTUP_TIMEOUT
    # None = discover it (requirements.lock.txt / requirements.txt / pyproject.toml);
    # set it to pin an exact file, e.g. a lock file for store builds.
    requirements: Path | None = None
    # Extra fnmatch patterns to exclude, on top of EXCLUDED_DIRS/EXCLUDED_FILES and
    # the project's own .provisionignore. Matched against the entry NAME.
    extra_excludes: tuple[str, ...] = ()
    # [project.optional-dependencies] groups the admin opted in, e.g. ("llm",).
    # Only meaningful when the deps come from pyproject: a lock file is the truth.
    extras: tuple[str, ...] = ()
    # The WebView2 OFFLINE installer: the Evergreen Standalone Installer
    # (MicrosoftEdgeWebView2RuntimeInstallerX64.exe, ~130 MB), which carries the
    # runtime inside the file. NOT the ~2 MB MicrosoftEdgeWebview2Setup.exe — that
    # is the Evergreen *Bootstrapper*, it contains no WebView2, and it downloads one
    # at install time, so on the air-gapped machine this field exists for it cannot
    # work. Both builders accept whatever file is given, copy it into <package>/prereq/
    # UNDER ITS OWN NAME (never renamed), and warn when it is small enough to be the
    # bootstrapper. prereq/ is the ONLY place tools\安裝WebView2.bat looks, and it
    # runs any .exe it finds there.
    # Leave it None and the build still succeeds — but it emits a warning saying
    # so, because the package we just handed the operator cannot start on an
    # air-gapped machine that lacks WebView2, and that machine is the whole point
    # of shipping a folder instead of a URL. Nothing else in the tree ever
    # CREATES prereq/: the store builder only copies one if it already exists.
    webview2_installer: Path | None = None
    # The app's identity in a store tree, when the display name cannot carry it.
    # `apps\<app_id>\`, `start-<app_id>.bat`, `tools\admin-<app_id>.bat` and the
    # manifest's app_id all come from here, and a store keeps versions of an app
    # under ONE id forever — so for a name like 「影像檢視器」 (no latin characters
    # to slugify) the operator must name the app themselves rather than get a
    # digest. None = derive it from display_name (app_id_for).
    # Blank/prefix-less input is normalized: "image-viewer" -> "app-image-viewer".
    app_id_override: str | None = None

    def __post_init__(self) -> None:
        self.app_id_override = normalize_app_id(self.app_id_override)
        self.project_dir = Path(self.project_dir).expanduser().resolve()
        self.entrypoint = Path(self.entrypoint).expanduser().resolve()
        self.output_dir = Path(self.output_dir).expanduser().resolve()
        self.shell_exe = Path(self.shell_exe).expanduser().resolve()
        self.runtime_template = Path(self.runtime_template).expanduser().resolve()
        if self.requirements is not None:
            self.requirements = Path(self.requirements).expanduser().resolve()
        if self.webview2_installer is not None:
            self.webview2_installer = Path(self.webview2_installer).expanduser().resolve()
        self.extra_excludes = tuple(self.extra_excludes)
        self.extras = tuple(self.extras)

    @property
    def explicit_requirements(self) -> Path | None:
        return self.requirements

    @property
    def app_id(self) -> str:
        """The id everything downstream keys off. An explicit one wins: it is the
        only way two apps whose names share a slug can stay two apps."""
        return self.app_id_override or app_id_for(self.display_name)

    @property
    def has_explicit_app_id(self) -> bool:
        return self.app_id_override is not None

    @property
    def app_id_is_derived_from_a_nameless_slug(self) -> bool:
        """No explicit id AND nothing latin in the display name to derive one from:
        the id is a digest. Legal, unique — and unreadable, so a store refuses it."""
        return not self.has_explicit_app_id and slug_is_derived(self.display_name)

    @property
    def package_dir(self) -> Path:
        # Fat mode: the folder is named after the app, so an explicit app id names
        # it too (minus the `app-` prefix, which is a portal rendering contract and
        # means nothing to a folder on a USB stick).
        if self.app_id_override:
            return self.output_dir / self.app_id_override[len("app-"):]
        return self.output_dir / slugify(self.display_name)


@dataclass
class BuildResult:
    ok: bool
    package_dir: Path | None = None
    size_bytes: int = 0
    duration_seconds: float = 0.0
    log_path: Path | None = None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # The operator pressed 取消. NOT a failure: nothing is broken — but the caller
    # must never render it as "完成", and it must not claim a cleanup that did not
    # happen either.
    cancelled: bool = False
    # WHAT IS STILL ON THE DISK. A cancel during the pip install kills a process
    # tree whose handles Windows keeps open for a moment; the rmtree then fails and
    # ~600 MB of staging stays in the operator's output folder. `message` already
    # says so in words — but a GUI cannot branch on a sentence, so it rendered its
    # own hardcoded 「暫存目錄已清乾淨」 over the top of it and the operator went
    # looking for the disk space that was never freed. None = really gone.
    staging_left: Path | None = None
    message: str = ""

    @property
    def size_mb(self) -> float:
        return self.size_bytes / (1024 * 1024)

    def summary(self) -> str:
        if self.cancelled:
            return "已取消 — " + (self.message or "已取消建置,暫存目錄已清乾淨")
        if self.ok:
            return (f"OK — {self.package_dir}  ({self.size_mb:.0f} MB, "
                    f"{self.duration_seconds:.0f}s)")
        return "FAILED — " + "; ".join(self.errors or [self.message])
