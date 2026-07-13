"""Data structures for the Streamlit desktop folder builder.

Kept free of Tkinter and of the builder itself so the GUI, the CLI and the
tests all speak the same vocabulary.
"""

from __future__ import annotations

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

# Build-time artifacts that a running app never reads. Archives are here for the
# same reason as the wheelhouse: a 200 MB dataset.zip beside the app is payload
# nobody asked to ship, and the operator never sees it leave.
EXCLUDED_FILES = (
    "*.pyc", "*.pyo", "*.whl", "*.egg-info",
    "*.tar.gz", "*.zip", "*.7z",
)

# The project can add its own patterns, gitignore-style, without touching the GUI.
PROVISIONIGNORE = ".provisionignore"


def slugify(name: str) -> str:
    """A display name -> a filesystem- and tool_id-safe slug."""
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", name.strip().lower()).strip("-")
    return slug or "streamlit-app"


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
    # The WebView2 offline installer (MicrosoftEdgeWebview2Setup.exe, or the
    # standalone x64 runtime installer). Set it and build() copies it into
    # <package>/prereq/, which is the ONLY place tools\安裝WebView2.bat looks.
    # Leave it None and the build still succeeds — but it emits a warning saying
    # so, because the package we just handed the operator cannot start on an
    # air-gapped machine that lacks WebView2, and that machine is the whole point
    # of shipping a folder instead of a URL. Nothing else in the tree ever
    # CREATES prereq/: the store builder only copies one if it already exists.
    webview2_installer: Path | None = None

    def __post_init__(self) -> None:
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
        return app_id_for(self.display_name)

    @property
    def package_dir(self) -> Path:
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
    # The operator pressed 取消. NOT a failure: nothing is broken, nothing is
    # left behind — but the caller must never render it as "完成".
    cancelled: bool = False
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
