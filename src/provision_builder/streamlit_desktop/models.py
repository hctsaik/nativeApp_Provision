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

# Directories that are never part of a deliverable: build machine state, caches,
# virtualenvs that would be wrong on the user's machine anyway, and the build
# artifacts of the project itself. `wheels`/`wheelhouse`/`vendor` are the big
# ones: CV_Viewer's wheelhouse alone was 124 MB of .whl files that the user's
# machine would never open — the dependencies are already installed into the
# packaged runtime.
EXCLUDED_DIRS = (
    ".git", ".hg", ".svn",
    ".venv", "venv", "env",
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    "node_modules",
    ".streamlit_cache",
    "wheels", "wheelhouse", "vendor",
    "dist", "build", "site-packages",
)

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

    def __post_init__(self) -> None:
        self.project_dir = Path(self.project_dir).expanduser().resolve()
        self.entrypoint = Path(self.entrypoint).expanduser().resolve()
        self.output_dir = Path(self.output_dir).expanduser().resolve()
        self.shell_exe = Path(self.shell_exe).expanduser().resolve()
        self.runtime_template = Path(self.runtime_template).expanduser().resolve()
        if self.requirements is not None:
            self.requirements = Path(self.requirements).expanduser().resolve()
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
