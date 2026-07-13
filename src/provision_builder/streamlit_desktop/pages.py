"""What Streamlit actually LOADS — the one answer, used by both sides of the fence.

A multipage app's `pages/*.py` are discovered and executed by Streamlit itself.
NOTHING imports them. So an import closure seeded with the entry script alone is
blind to the entire folder: a module-level `import zzz_nope` in `pages/2_report.py`
sails through every build-side check, gets delivered, gets committed as
last-known-good, and finally appears as a red traceback the first time the
operator clicks that page — which is the exact failure the import gate exists to
prevent.

The device-side launcher (templates/launch.py) already knew this and seeded its
preflight with the pages; the build-side gate (imports.py) did not. Two
implementations of "what does Streamlit load" is how that drifted, so there is
now exactly one, and it lives here.

BOTH SIDES IMPORT THIS FILE, AND THEY IMPORT IT DIFFERENTLY:

  build side   `from . import pages` — an ordinary module of the provision_builder
               package (imports.py).

  device side  there is no provision_builder inside a delivered package. This file
               is COPIED next to launch.py (`launcher/pages.py`) and loaded from
               there BY PATH (launch.py::_shared_pages). Hence: stdlib only, no
               relative imports, no package state — the same file must work as a
               loose script on the shipped portable Python.

WHAT IS COVERED
  * `pages/` next to the entry script — Streamlit's own convention
    (script_runner/_mpa_v1: every `*.py` directly inside it, minus dotfiles and
    __init__.py; that rule is copied from Streamlit, not guessed).
  * `st.Page("pages/2_report.py")` — the st.navigation API, when the path is a
    LITERAL sitting in the AST.
  * `.streamlit/pages.toml` — the third-party `st-pages` convention
    ([[pages]] path = "…"), read with tomllib when it is available.

WHAT IS NOT COVERED, AND WE DO NOT PRETEND OTHERWISE
  A page list built at RUNTIME: a loop over a directory, page names out of a
  database, `st.Page(some_variable)`. Those paths do not exist until the app runs,
  so NO static gate can see them. Such a page's missing dependency will surface as
  a red box, and the log scan at the end of the session (launch.py) is what has to
  catch it. This is a known, accepted hole — not an oversight.
"""

from __future__ import annotations

import ast
import os
from pathlib import Path

# Bumped only when the contract below changes. launch.py loads this file by path
# and refuses anything that does not carry the mark — a stray `pages.py` picked up
# from the wrong directory must never be mistaken for the real rules.
MODULE_MARK = "cim-streamlit-pages/1"

PAGES_DIRNAME = "pages"

# Where the builders find this file to copy it into a delivered package's
# `launcher/` folder, next to launch.py. It is not in templates/ because the build
# side imports it as a normal module; it still has to SHIP, because the device has
# no provision_builder to import it from.
SOURCE = Path(__file__).resolve()
DELIVERED_NAME = "pages.py"

# The `st.Page(...)` / `StreamlitPage(...)` constructors whose first argument is a
# path to a page script.
_PAGE_CALLS = ("Page", "StreamlitPage")


def pages_dir(entrypoint: Path | str) -> Path:
    """Streamlit's multipage folder: `pages/` NEXT TO THE ENTRY SCRIPT."""
    return Path(entrypoint).parent / PAGES_DIRNAME


def page_scripts(entrypoint: Path | str, app_root: Path | str) -> list[Path]:
    """Every page script Streamlit will run that nothing imports.

    The convention folder first, then everything a literal string declares. Order
    is stable (sorted within the folder) so a build log is diffable.
    """
    found: list[Path] = []
    folder = pages_dir(entrypoint)
    if folder.is_dir():
        found += sorted(p for p in folder.glob("*.py")
                        if p.is_file() and not p.name.startswith(".")
                        and p.name != "__init__.py")
    for path in declared_pages(entrypoint, app_root):
        if path not in found:
            found.append(path)
    return found


def declared_pages(entrypoint: Path | str, app_root: Path | str) -> list[Path]:
    """Page scripts named by a LITERAL string: st.Page(...) and st-pages' toml."""
    bases = [Path(entrypoint).parent, Path(app_root)]
    found: list[Path] = []

    def add(raw) -> None:
        if not isinstance(raw, str) or not raw.endswith(".py") or os.path.isabs(raw):
            return
        for base in bases:
            candidate = base / raw
            if candidate.is_file() and candidate not in found:
                found.append(candidate)
                return

    for value in _page_call_constants(entrypoint):
        add(value)
    for value in _pages_toml_paths(app_root):
        add(value)
    return found


def _page_call_constants(entrypoint: Path | str) -> list[str]:
    """The literal first argument of every st.Page(...) in the entry script."""
    try:
        tree = ast.parse(Path(entrypoint).read_text("utf-8", errors="replace"))
    except (OSError, SyntaxError, ValueError):
        # A syntax error is reported by the caller's own gate (preflight /
        # classify); it is not this module's job to shout about it twice.
        return []
    out: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        func = node.func
        name = (func.attr if isinstance(func, ast.Attribute)
                else func.id if isinstance(func, ast.Name) else "")
        if name not in _PAGE_CALLS:
            continue
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            out.append(first.value)
        # else: st.Page(some_variable) — a runtime page list. See the module
        # docstring: no static gate can resolve it, and we do not guess.
    return out


def _pages_toml_paths(app_root: Path | str) -> list[str]:
    toml_path = Path(app_root) / ".streamlit" / "pages.toml"
    if not toml_path.is_file():
        return []
    try:
        import tomllib                     # stdlib on the shipped cp311 runtime
    except ImportError:                    # pragma: no cover - older runtime
        return []
    try:
        data = tomllib.loads(toml_path.read_text("utf-8", errors="replace"))
    except (OSError, ValueError):
        return []
    entries = data.get("pages")
    if not isinstance(entries, list):
        return []
    return [entry.get("path") for entry in entries if isinstance(entry, dict)]


def first_party_roots(entrypoint: Path | str, app_root: Path | str) -> list[Path]:
    """Directories whose `.py` files are the APP's, not PyPI's.

    A `.py` sitting next to a page IS a page's helper, not a package to pip
    install. Without `pages/` in this list, `import shared_bits` inside
    pages/1_home.py is reported as a missing third-party module — the exact
    misdiagnosis that made a working CV_Viewer refuse to start, and the reason
    launch.py added the folder to its roots.
    """
    roots = [Path(entrypoint).parent, Path(app_root)]
    folder = pages_dir(entrypoint)
    if folder.is_dir():
        roots.append(folder)
    ordered: list[Path] = []
    seen: set[Path] = set()
    for root in roots:
        resolved = root.resolve()
        if resolved not in seen:
            seen.add(resolved)
            ordered.append(root)
    return ordered


def seed_scripts(entrypoint: Path | str, app_root: Path | str) -> list[Path]:
    """Where an import-reachability walk must START.

    The entry script AND every page: a page is unreachable for an import walk and
    perfectly reachable for the user.
    """
    return [Path(entrypoint), *page_scripts(entrypoint, app_root)]
