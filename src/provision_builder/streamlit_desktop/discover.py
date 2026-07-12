"""Work out the answers the operator should never have to type.

The prebuilt shell, the portable runtime and the output folder are the same on
every build from a given machine — asking for them is noise that invites typos.
The entrypoint and the app name follow from the project itself.

Kept out of the GUI so the same detection can be unit-tested and reused by a CLI.
Every finder returns None rather than guessing wildly: a wrong silent default is
worse than an honest "not found" with instructions.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]

# Where a prebuilt cim-light.exe realistically lives. First hit wins.
SHELL_CANDIDATES = (
    Path("apps/host-tauri/prebuilt/cim-light.exe"),
    Path("apps/host-tauri/src-tauri/target/release/cim-light.exe"),
    Path("cim-light.exe"),
)
# Sibling repos to look in for the shell (native_Provision ships no shell of its own).
SIBLING_REPOS = ("nativeApp", "native_app", "cim-light")

RUNTIME_CANDIDATES = (
    Path(".runtime-cache/python311"),
    Path("runtime/python311"),
    Path("../nativeApp/runtime/python311"),
)

ENTRY_CANDIDATES = ("app.py", "main.py", "streamlit_app.py", "Home.py")

# Directories an entry point never lives in. Searching them produces confident,
# wrong answers (a spike script or a test fixture that also imports streamlit).
SKIPPED_DIRS = (
    ".git", ".venv", "venv", "env", "__pycache__", ".pytest_cache", ".mypy_cache",
    "node_modules", "dist", "build", "site-packages",
    "tests", "test", "spike", "spikes", "docs", "examples", "scripts",
)


@dataclass(frozen=True)
class Detected:
    value: Path | None
    source: str          # how we found it — shown to the operator, never hidden
    hint: str = ""       # what to do when value is None

    @property
    def found(self) -> bool:
        return self.value is not None


def find_shell(repo_root: Path | None = None) -> Detected:
    """The prebuilt Tauri shell. Never built here — WDAC blocks compiling one."""
    root = (repo_root or REPO_ROOT).resolve()

    override = os.environ.get("CIM_TAURI_EXE")
    if override and Path(override).is_file():
        return Detected(Path(override), "環境變數 CIM_TAURI_EXE")

    for sibling in SIBLING_REPOS:
        base = root.parent / sibling
        for candidate in SHELL_CANDIDATES:
            path = base / candidate
            if path.is_file():
                return Detected(path, f"自動偵測:{sibling}")

    for candidate in SHELL_CANDIDATES:
        path = root / candidate
        if path.is_file():
            return Detected(path, "自動偵測:本 repo")

    return Detected(None, "找不到", hint=(
        "找不到預建的 cim-light.exe。請把它放到 nativeApp\\apps\\host-tauri\\prebuilt\\,"
        "或設定環境變數 CIM_TAURI_EXE。（本機 WDAC 擋 Rust 重編，只能沿用既有的殼。）"
    ))


def find_runtime(repo_root: Path | None = None) -> Detected:
    """A relocatable CPython with pip + venv (python-build-standalone)."""
    root = (repo_root or REPO_ROOT).resolve()

    override = os.environ.get("CIM_PORTABLE_PYTHON")
    if override and (Path(override) / "python.exe").is_file():
        return Detected(Path(override), "環境變數 CIM_PORTABLE_PYTHON")

    for candidate in RUNTIME_CANDIDATES:
        path = (root / candidate).resolve()
        if (path / "python.exe").is_file():
            return Detected(path, "自動偵測")

    return Detected(None, "找不到", hint=(
        "還沒有可攜 Python runtime。按「下載可攜 Python」自動取得（需連網，只需做一次）。"
    ))


def default_output(repo_root: Path | None = None) -> Path:
    return (repo_root or REPO_ROOT) / "dist" / "streamlit-apps"


def _searchable_py(project: Path, max_depth: int = 3):
    """Project .py files worth considering as an entry point. Skips build junk
    and the places an entry point never hides (tests, spikes, docs)."""
    skip = set(SKIPPED_DIRS)
    for path in sorted(project.rglob("*.py")):
        rel = path.relative_to(project)
        if len(rel.parts) > max_depth + 1:
            continue
        if any(part in skip or part.startswith(".") for part in rel.parts[:-1]):
            continue
        yield path


# The GUI's two tabs, named exactly as the operator reads them. A rescue hint that
# says "the other tab" and makes them go looking for it is half a rescue.
CIM_TAB = "CIM 平台模組（需 plugin.yaml）"
STREAMLIT_TAB = "Streamlit 專案 → 桌面 App"

PLUGIN_MANIFEST = "plugin.yaml"
# ANnoTation keeps its 18 plugin.yaml files at modules/module_XXX/plugin.yaml, so
# a top-level-only look finds nothing and the operator is told, with total
# confidence, to pick one of 18 CIM module scripts as a Streamlit entry point.
_PLUGIN_GLOBS = (PLUGIN_MANIFEST, f"*/{PLUGIN_MANIFEST}", f"*/*/{PLUGIN_MANIFEST}",
                 f"*/*/*/{PLUGIN_MANIFEST}")


def find_plugin_manifests(project: Path, limit: int = 50) -> list[Path]:
    """The plugin.yaml files under `project`: the signature of a CIM module
    collection. Bounded (globs, a few levels, a cap) because this runs on every
    folder the operator picks, including a 40 GB one they picked by mistake."""
    project = Path(project)
    if not project.is_dir():
        return []
    found: list[Path] = []
    for pattern in _PLUGIN_GLOBS:
        try:
            for hit in sorted(project.glob(pattern)):
                if any(part in SKIPPED_DIRS or part.startswith(".")
                       for part in hit.relative_to(project).parts[:-1]):
                    continue
                found.append(hit)
                if len(found) >= limit:
                    return found
        except OSError:
            break
    return found


def wrong_tab_hint(project: Path) -> str:
    """"這裡沒有 Streamlit 入口" is true and useless when the operator has pointed
    a CIM module collection at the Streamlit tab. Say what the folder IS, and name
    the tab that wants it — the CIM tab has done this for Streamlit projects since
    day one, and the courtesy was never returned."""
    hits = find_plugin_manifests(project)
    if not hits:
        return ""
    example = hits[0].relative_to(Path(project)).as_posix()
    count = f"{len(hits)} 個" if len(hits) < 50 else "很多"
    return (f"\n這個資料夾底下有 {count} plugin.yaml(例:{example}),"
            f"看起來是 CIM 平台模組集合,不是 Streamlit 專案。\n"
            f"請改用「{CIM_TAB}」分頁,把「平台專案」指到這裡。")


def _is_streamlit_app(path: Path) -> bool:
    """Imports streamlit AND looks like a page, not a helper module: a Streamlit
    entry script calls st.<something> at module level (title/write/set_page_config…)."""
    try:
        text = path.read_text("utf-8", errors="replace")
    except OSError:
        return False
    if "import streamlit" not in text:
        return False
    return any(marker in text for marker in
               ("st.set_page_config", "st.title(", "st.write(", "st.header(",
                "st.markdown(", "st.sidebar", "st.columns("))


def find_entrypoint(project: Path) -> Detected:
    """The Streamlit entry script. Conventional names first (nearest to the root
    wins), then any file that both imports streamlit and renders something.
    Two equally plausible candidates = we ask. Guessing wrong is a build that
    fails minutes later, or worse, one that ships the wrong page."""
    project = Path(project)
    if not project.is_dir():
        return Detected(None, "專案資料夾不存在")

    candidates = list(_searchable_py(project))

    by_name = [p for p in candidates if p.name in ENTRY_CANDIDATES]
    by_name.sort(key=lambda p: (len(p.relative_to(project).parts), p.name))
    if by_name:
        # A conventional name at the shallowest level; ties broken by name order.
        best = by_name[0]
        depth = len(best.relative_to(project).parts)
        rivals = [p for p in by_name
                  if len(p.relative_to(project).parts) == depth and p.name == best.name]
        if len(rivals) > 1:
            names = "、".join(p.relative_to(project).as_posix() for p in rivals[:4])
            return Detected(None, "有多個候選",
                            hint=f"找到多個同名入口({names}…),請自行指定。"
                                 + wrong_tab_hint(project))
        return Detected(best, f"自動偵測:{best.relative_to(project).as_posix()}")

    apps = [p for p in candidates if _is_streamlit_app(p)]
    if len(apps) == 1:
        rel = apps[0].relative_to(project).as_posix()
        return Detected(apps[0], f"自動偵測:{rel}(唯一會算繪畫面的 Streamlit 檔案)")
    if len(apps) > 1:
        # ANnoTation lands HERE, not in the "no streamlit at all" branch below: its
        # 18 CIM modules each import streamlit, so we confidently offered the
        # operator four of them to choose an entry point from — on the wrong tab,
        # for a project that has no single app to build. Both dead ends need the
        # way out, not just the empty one.
        names = "、".join(p.relative_to(project).as_posix() for p in apps[:4])
        return Detected(None, "有多個候選",
                        hint=f"找到多個可能的入口({names}…),請用「瀏覽…」自行指定。"
                             + wrong_tab_hint(project))
    return Detected(None, "找不到",
                    hint="這個資料夾裡沒有 import streamlit 的 .py,請確認選對專案。"
                         + wrong_tab_hint(project))


def suggest_name(project: Path) -> str:
    """CV_Viewer must stay "CV Viewer", not become "Cv Viewer" — .title() mangles
    every acronym, and this string ends up in the manifest, the README, the
    window title and the tool dropdown the operator reads on the factory floor."""
    words = Path(project).name.replace("-", " ").replace("_", " ").split()
    return " ".join(w if any(c.isupper() for c in w) else w.capitalize() for w in words)


def looks_like_streamlit(project: Path) -> bool:
    """A weak signal, on purpose: enough to say "you are on the wrong tab" even
    when we cannot pin down which file is the entry point."""
    project = Path(project)
    if not project.is_dir():
        return False
    if (project / ".streamlit").is_dir():
        return True
    for name in ("requirements.txt", "requirements.lock.txt", "pyproject.toml"):
        path = project / name
        if path.is_file() and "streamlit" in path.read_text("utf-8", errors="replace").lower():
            return True
    for path in _searchable_py(project):
        try:
            if "import streamlit" in path.read_text("utf-8", errors="replace"):
                return True
        except OSError:
            continue
    return False
